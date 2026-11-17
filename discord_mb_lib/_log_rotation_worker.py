"""Early connector log rotation on a worker thread while appends continue."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import base64
import os
import threading
from pathlib import Path
from typing import Any


class _RotationWorker:
    """A worker stages ``[0, cut)`` for ``.1`` while appends continue; a swap under the lock
    then rewrites the active to the tail.  The caller never stages beside a worker."""

    _BACKGROUND_ROTATION_HEADROOM = 1 << 20
    _BACKGROUND_ROTATION_MIN_BYTES = 4 << 20  # below it, rotation stays synchronous

    def _rotation_before_append(self: Any, size: int) -> bool:
        """True when the caller must rotate synchronously before appending ``size`` bytes."""
        over = bool(self._active_bytes) and self._active_bytes + size > self._max_bytes
        if self._rotation_worker is not None:
            if not over:
                return False
            self._await_rotation_worker()
            over = self._active_bytes + size > self._max_bytes
        if (not over and self._active_bytes and
                self._max_bytes >= self._BACKGROUND_ROTATION_MIN_BYTES and
                self._active_bytes + size > self._max_bytes - self._BACKGROUND_ROTATION_HEADROOM):
            self._start_rotation_worker(self._active_bytes)
        return over

    def _start_rotation_worker(self: Any, cut: int) -> None:
        self._rotation_staged, self._rotation_context = threading.Event(), None
        worker = threading.Thread(target=self._run_rotation_worker, args=(cut,),
                                  name='connector-log-rotation', daemon=True)
        worker.start()
        self._rotation_worker = worker

    def _check_rotation_owner(self: Any) -> None:
        worker = self._rotation_worker
        if worker is not None and worker is not threading.current_thread():
            raise AssertionError('connector log staged or adopted beside its rotation worker')

    def _run_rotation_worker(self: Any, cut: int) -> None:
        context = None
        try:
            context = self._prepare_rotation(cut)
        finally:
            # No context after a failure: the caller rotates synchronously at the ceiling.
            self._rotation_context = context
            self._rotation_staged.set()
        with self._thread_lock:
            if self._rotation_worker is not threading.current_thread():
                return  # the caller swapped it
            self._rotation_worker = self._rotation_context = None
            if self._rotation_stop:
                self._rollback_rotation(context)
            else:
                self._swap_rotation(context)

    def _await_rotation_worker(self: Any) -> None:
        """Backpressure: wait for the stage, then swap under the lock already held."""
        self._rotation_staged.wait()
        context = self._rotation_context
        self._rotation_worker = self._rotation_context = None
        if context is not None:
            self._swap_rotation(context)

    def _prepare_rotation(self: Any, cut: int) -> dict:
        try:
            mode = os.stat(self._path).st_mode & 0o7777
        except OSError:
            mode = None
        src = os.open(str(self._path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                      getattr(os, 'O_BINARY', 0))
        context = {'src': src, 'cut': cut, 'mode': mode, 'manifest': None, 'staged': None}
        try:
            opened = os.fstat(src)
            if self._owner.identity not in (None, (str(opened.st_dev), str(opened.st_ino))):
                raise RuntimeError(
                    f'connector log identity was replaced while owned: {self._path}')
            manifest, planned = self._rotation_plan(cut, None, 5)
            self._write_migration_manifest(manifest, kind='rotate', create=True)
            context['manifest'] = manifest
            data = self._read_rotation_prefix(src, cut)
            digest = self._sha256_bytes(data)
            manifest['destinations'][-1]['prefix_sha256'] = digest
            if planned is not None:
                manifest['destinations'][-2]['sha256'] = digest
                self._use_anonymous_staging = True
                context['staged'] = self._stage_rotation_copy(manifest, data, mode, planned)
        except BaseException:
            self._rollback_rotation(context)
            raise
        return context

    def _read_rotation_prefix(self: Any, src: int, cut: int) -> bytes:
        return self._read_rotation_bytes(src, 0, cut)

    @staticmethod
    def _read_rotation_bytes(src: int, start: int, end: int) -> bytes:
        pread, chunks = getattr(os, 'pread', None), []
        while start < end:
            if pread is None:
                os.lseek(src, start, os.SEEK_SET)
                chunk = os.read(src, end - start)
            else:
                chunk = pread(src, end - start, start)
            if not chunk:
                raise OSError(f'connector log ended before byte {end} of its rotation')
            chunks.append(chunk)
            start += len(chunk)
        return b''.join(chunks)

    def _rollback_rotation(self: Any, context: dict) -> None:
        """Undo an unpublished stage and its journal, as a failed ``_rotate`` does."""
        try:
            manifest, staged = context['manifest'], context['staged']
            if manifest is None:
                return
            kept = (staged is not None and
                    not self._unlink_if_identity(
                        staged, *self._journal_pair(manifest['destinations'][-2], 'temporary')) and
                    os.path.lexists(str(staged)))
            if not (kept or self._staging_cleanup_failed):
                journal = self._rotation_manifest_path()
                try:
                    journal.unlink()
                except FileNotFoundError:
                    pass
                self._fsync_directory(self._path.parent)
                self._fsync_directory(journal.parent)
        finally:
            os.close(context['src'])

    def _swap_rotation(self: Any, context: dict) -> None:
        """Lock held: journal the tail, shift the slots, rewrite the active to the tail."""
        manifest, src, cut = context['manifest'], context['src'], context['cut']
        destinations = manifest['destinations']
        try:
            tail = self._read_rotation_bytes(src, cut, os.fstat(src).st_size)
            digest = self._sha256_bytes(tail)
            destinations[-1].update(tail=base64.b64encode(tail).decode('ascii'),
                                    tail_sha256=digest, size=len(tail), sha256=digest)
            self._mark_generation_publishing(manifest)
        except BaseException:
            self._rollback_rotation(context)
            raise
        try:
            fh, self._fh = self._fh, None
            if fh is not None:
                fh.close()
            for position in range(len(destinations) - 1):
                self._publish_rotation_slot(manifest, position, context['staged'])
            self._publish_in_place(self._path, None,
                                   *self._journal_pair(destinations[-1], 'before'), tail)
            self._adopt_rotated_identities({
                Path(item['destination']): self._owner.identity if item['method'] == 'rewrite'
                else self._manifest_identity(
                    item.get('source_identity', item.get('temporary_identity')))
                for item in destinations})
            self._finish_rotation(context)
            self._open_active(context['mode'])
            self._use_anonymous_staging = False
        finally:
            os.close(src)

    def _finish_rotation(self: Any, context: dict) -> None:
        journal = self._rotation_manifest_path()
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(self._path.parent)
        self._fsync_directory(journal.parent)
