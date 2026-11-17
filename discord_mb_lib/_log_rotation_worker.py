"""Early connector log rotation on a worker thread while appends continue."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import base64
import hashlib
import json
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
        if (not over and self._active_bytes and not self._rotation_stop and not self._rotation_failed and
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

    def _after_fork_in_child(self: Any) -> None:
        """A forked child: a free lock, and no rotation on a worker."""
        self._thread_lock = threading.RLock()
        self._rotation_worker = self._rotation_context = self._rotation_error = None
        self._rotation_stop = True

    def _run_rotation_worker(self: Any, cut: int) -> None:
        held = self._hold_oldest_backup()
        try:
            self._rotate_on_worker(cut)
        finally:
            if held is not None:
                os.close(held)  # after the lock: the swap freed no inode

    def _hold_oldest_backup(self: Any) -> int | None:
        if os.name == 'nt' or not hasattr(os, 'posix_fadvise') or not self._backup_count:
            return None  # Windows: os.replace fails over an open file
        try:
            held = os.open(str(self._backup_path(self._backup_count)), os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                           getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except OSError:
            return None
        try:
            os.posix_fadvise(held, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        return held

    def _rotate_on_worker(self: Any, cut: int) -> None:
        try:
            context = self._prepare_rotation(cut)
        except BaseException as exc:  # a thread drops what escapes it, SystemExit included
            context = exc
        self._rotation_context = context
        self._rotation_staged.set()
        with self._thread_lock:
            if self._rotation_worker is not threading.current_thread():
                return  # the caller swapped it
            self._rotation_worker = self._rotation_context = None
            try:
                if not self._rotation_stop:
                    self._apply_staged_rotation(context)
                elif not isinstance(context, BaseException):
                    self._rollback_rotation(context)
            except BaseException as exc:
                self._rotation_error = exc  # the next write raises it

    def _await_rotation_worker(self: Any) -> None:
        """Backpressure: wait for the stage, then swap under the lock already held."""
        self._rotation_staged.wait()
        context = self._rotation_context
        self._rotation_worker = self._rotation_context = None
        self._apply_staged_rotation(context)

    def _apply_staged_rotation(self: Any, context: Any) -> None:
        """Lock held: swap; a failure drops the handle and makes rotation synchronous."""
        try:
            if isinstance(context, BaseException):
                raise context
            self._swap_rotation(context)
        except BaseException as exc:
            fh, self._fh, self._rotation_failed = self._fh, None, True
            if isinstance(exc, self._PERMANENT_ROTATION_ERRORS):  # on either thread
                self._rotation_refusal = exc
            if fh is not None:
                fh.close()
            raise

    def _prepare_rotation(self: Any, cut: int) -> dict:
        try:
            mode = os.stat(self._path).st_mode & 0o7777
        except OSError:
            mode = None
        src = os.open(str(self._path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                      getattr(os, 'O_BINARY', 0))
        context = {'src': src, 'cut': cut, 'mode': mode, 'manifest': None, 'staged': None, 'sidecar': None}
        try:
            opened = os.fstat(src)
            if self._owner.identity not in (None, (str(opened.st_dev), str(opened.st_ino))):
                raise RuntimeError(
                    f'connector log identity was replaced while owned: {self._path}')
            manifest, planned = self._rotation_plan(cut, None, 5)
            context['journal'] = self._rotation_manifest_path()
            manifest['sidecar'] = self._rotation_sidecar(context['journal']).name
            self._write_migration_manifest(manifest, kind='rotate', create=True)
            context['manifest'] = manifest
            data = self._read_rotation_prefix(src, cut)
            digest = self._sha256_bytes(data)
            manifest['destinations'][-1]['prefix_sha256'] = digest
            if planned is not None:
                manifest['destinations'][-2]['sha256'] = digest
                self._use_anonymous_staging = True
                context['staged'] = self._stage_rotation_copy(manifest, data, mode, planned)
            context['sidecar'] = self._prepare_rotation_sidecar(cut)
            if hasattr(os, 'posix_fadvise'):  # the swap's truncate then frees no page
                getattr(os, 'fdatasync', os.fsync)(src)
                os.posix_fadvise(src, 0, cut, os.POSIX_FADV_DONTNEED)
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

    @staticmethod
    def _rotation_sidecar(journal: Path) -> Path:
        return journal.with_suffix('.swap')

    def _prepare_rotation_sidecar(self: Any, cut: int) -> int:
        """Preallocate the record's file, durable with its name, for the swap to overwrite."""
        path = self._rotation_sidecar(self._rotation_manifest_path())
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:  # the largest base64 tail and the record
            self._write_fd_bytes(fd, bytes(4 * -(-(self._max_bytes - cut) // 3) + (256 << 10)))
            os.fsync(fd)
            self._fsync_directory(path.parent)
        except BaseException:
            os.close(fd)
            path.unlink()
            raise
        return fd

    def _write_rotation_record(self: Any, context: dict) -> None:
        """Lock held: write length, SHA-256 and record over the sidecar; sync its data."""
        fd, payload = context['sidecar'], json.dumps(
            context['manifest'], sort_keys=True, separators=(',', ':')).encode('utf-8')
        os.lseek(fd, 0, os.SEEK_SET)  # os.pwrite is POSIX only
        self._write_fd_bytes(fd, len(payload).to_bytes(8, 'big') + hashlib.sha256(payload).digest() + payload)
        getattr(os, 'fdatasync', os.fsync)(fd)
        context['sidecar'] = None
        os.close(fd)

    def _rollback_rotation(self: Any, context: dict) -> None:
        """Undo an unpublished stage and its journal, as a failed ``_rotate`` does."""
        try:
            if context['sidecar'] is not None:  # first: a record beside a kept journal rolls forward
                os.close(context['sidecar'])
                self._rotation_sidecar(context['journal']).unlink()
            if context['manifest'] is not None:
                self._unstage_rotation(context['manifest'], context['staged'], context['journal'])
        finally:
            os.close(context['src'])

    def _swap_rotation(self: Any, context: dict) -> None:
        """Lock held: record the tail, shift the slots, rewrite the active to the tail."""
        manifest, src, cut = context['manifest'], context['src'], context['cut']
        destinations = manifest['destinations']
        try:
            tail = self._read_rotation_bytes(src, cut, os.fstat(src).st_size)
            digest = self._sha256_bytes(tail)
            destinations[-1].update(tail=base64.b64encode(tail).decode('ascii'),
                                    tail_sha256=digest, size=len(tail), sha256=digest)
            self._mark_generation_publishing(manifest, include_stage=True, append=False)
            self._write_rotation_record(context)
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
            self._adopt_rotated_identities(self._rotation_identities(destinations))
            self._finish_rotation(context)
            self._open_active(context['mode'])
            self._use_anonymous_staging = False
        finally:
            os.close(src)

    def _finish_rotation(self: Any, context: dict) -> None:
        self._drop_rotation_journal(context['journal'])
        self._rotation_sidecar(context['journal']).unlink(missing_ok=True)
