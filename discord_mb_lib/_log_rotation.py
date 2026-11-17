"""The connector log rotation transaction and the slot identities it adopts."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import errno
import os
import stat
import uuid
from pathlib import Path
from typing import Any

# The errors that mean this filesystem cannot create or name an anonymous inode.
_NO_ANONYMOUS = frozenset((errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EPERM,
                           errno.EACCES, getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP)))


class _RotationJournal:
    """Rotation methods of the connector log writer."""

    # A journal carrying this ``staging`` value stages bare payloads, owned by their claimed inode.
    _PAYLOAD_STAGING = 'payload-v1'

    def _write_rotation_temp(self: Any, data: bytes, mode: Any, name: Path,
                             directory: Path, anonymous: bool) -> Path:
        """Write one rotation payload once, claiming its inode before its name holds a byte."""
        if anonymous:
            staged = self._link_rotation_temp(data, mode, name, directory)
            if staged is not None:
                return staged
        fd = os.open(str(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_BINARY', 0), 0o600)
        opened = os.fstat(fd)
        identity = (str(opened.st_dev), str(opened.st_ino))
        try:
            if mode is not None:
                try:
                    os.fchmod(fd, mode)
                except (AttributeError, OSError):
                    os.chmod(str(name), mode)
            # A durable claim before any byte makes a torn write reclaimable by identity.
            self._claim_rotation_temp(name, identity)
            self._fsync_directory(directory)
            self._write_fd_bytes(fd, data)
            os.fsync(fd)
        except BaseException:
            self._discard_rotation_temp(fd, name, identity)
            raise
        os.close(fd)
        return name

    def _link_rotation_temp(self: Any, data: bytes, mode: Any, name: Path,
                            directory: Path) -> Path | None:
        """Write and claim an anonymous inode, then name it; None where it cannot be named.

        A crash leaves either no name or a name whose inode the journal holds.
        """
        try:
            fd = os.open(str(directory), os.O_RDWR | os.O_TMPFILE |
                         getattr(os, 'O_BINARY', 0), 0o600)
        except OSError as exc:
            if exc.errno in _NO_ANONYMOUS:
                return None
            raise
        opened = os.fstat(fd)
        identity = (str(opened.st_dev), str(opened.st_ino))
        linked = False
        try:
            if mode is not None:
                try:
                    os.fchmod(fd, mode)
                except (AttributeError, OSError):
                    pass
            self._write_fd_bytes(fd, data)
            os.fsync(fd)
            self._claim_rotation_temp(name, identity)
            try:
                self._link_anonymous_fd(fd, name)
            except OSError as exc:
                if exc.errno not in _NO_ANONYMOUS:
                    raise
                self._claim_rotation_temp(name, None)  # the portable retry claims anew
                os.close(fd)
                return None
            linked = True
            if self._slot_identity(name) != (identity, identity):
                raise RuntimeError(
                    f'connector log staging temp was rebound after its link: {name}')
            self._fsync_directory(directory)
        except BaseException:
            self._discard_rotation_temp(fd, name if linked else None, identity)
            raise
        os.close(fd)
        return name

    def _claim_rotation_temp(self: Any, name: Path,
                             identity: tuple[str, str] | None) -> None:
        """Journal the inode staged at ``name``; None revokes it."""
        if self._staging_claim_callback is not None:
            self._staging_claim_callback(name, identity)

    def _discard_rotation_temp(self: Any, fd: int, name: Path | None,
                               identity: tuple[str, str]) -> None:
        """Close a failed stage's fd, then unlink its name only while it holds that inode."""
        try:
            os.close(fd)
        finally:
            if (name is not None and
                    not self._unlink_if_identity(name, identity, identity) and
                    os.path.lexists(str(name))):
                self._staging_cleanup_failed = True

    def _adopt_rotated_identities(
            self: Any, expected: dict[Path, tuple[str, str] | None]) -> None:
        """Hold each slot to the identity this rotation published into it.

        ``expected`` maps a rotated slot to that identity.  A name that
        resolves to no rotated slot keeps the identity held before.  A slot
        holding another file then refuses here and on every later write, as
        revalidation compares against these identities.
        """
        self._check_rotation_owner()
        owner = self._owner
        owner._stable_identities = {
            path: expected.get(Path(os.path.realpath(path.parent)) / path.name,
                               owner._stable_identities.get(path))
            for path in owner._path_candidates()}
        self._published_since_snapshot = False
        owner.revalidate()

    def _mark_generation_publishing(self: Any, manifest: dict, *, include_stage: bool = False,
                                    append: bool = True) -> None:
        """Mark ``prepared`` and each slot's contract, the stage's if ``include_stage``; append them if ``append``."""
        manifest['state'] = 'prepared'
        for item in manifest['destinations']:
            if include_stage or item['method'] != 'stage':
                item.update(publish_state='publishing',
                            publish_identity=item['before_identity'],
                            publish_entry_identity=item['before_entry_identity'])
        if append:
            self._write_migration_manifest(manifest, kind='rotate')
            self._published_since_snapshot = True

    def _claim_staged_for_replace(self: Any, temporary: Path, *, expected_destination: Path,
                                  identity: Any, entry_identity: Any) -> None:
        """Register a staged name for ``os.replace`` while it holds the journaled inode."""
        current = self._slot_identity(temporary)
        if None in current or current != (self._manifest_identity(identity),
                                          self._manifest_identity(entry_identity)):
            raise RuntimeError(f'connector log staging temp for {expected_destination} '
                               f'was rebound before replace: {temporary}')
        self._VERIFIED_STAGED_REPLACEMENTS[self._staged_path_key(temporary)] = current

    def _stage_rotation_copy(self: Any, manifest: dict, data: bytes, mode: Any,
                             planned: Path) -> Path:
        """Stage the active's bytes for ``.1``, journaling the inode before it is named."""
        index = len(manifest['destinations']) - 2
        item = manifest['destinations'][index]
        self._staging_kind, self._staging_slot = 'rotate', index
        self._staging_destination = Path(item['destination'])

        def claim(staged, identity):
            recorded = list(identity) if identity is not None else None
            item['temporary_identity'] = item['temporary_entry_identity'] = recorded
            if identity is None:
                manifest['created_temporaries'].remove(str(staged))
            else:
                manifest['created_temporaries'].append(str(staged))
            self._write_migration_manifest(manifest, kind='rotate')
        self._staging_claim_callback = claim
        try:
            return self._write_migration_temp(data, mode, name=planned)
        finally:
            self._staging_claim_callback = None

    def _publish_rotation_slot(self: Any, manifest: dict, position: int, staged: Any) -> None:
        """Shift one backup by rename or unlink, or publish the stage, rechecking it first."""
        item = manifest['destinations'][position]
        destination, method = Path(item['destination']), item['method']
        # A slot the shift before it renamed away is empty; any other still holds what it held.
        moved = position > 0 and manifest['destinations'][position - 1]['method'] == 'rename'
        expected = (None, None) if moved else self._journal_pair(item, 'before')
        if method == 'stage':
            if 'publish_state' not in item:
                self._mark_destination_publishing(manifest, position, 'rotate')
            try:
                self._claim_staged_for_replace(
                    staged, expected_destination=destination,
                    identity=item['temporary_identity'],
                    entry_identity=item['temporary_entry_identity'])
                self._revalidate_destination(destination, *expected)
                self._revalidate_staged_for_replace(staged)
                os.replace(staged, destination)
            finally:
                self._forget_staged_for_replace(staged)
        elif method == 'rename':
            self._revalidate_destination(Path(item['source']), *self._journal_pair(item, 'source'))
            self._revalidate_destination(destination, *expected)
            os.replace(item['source'], destination)
        else:
            self._revalidate_destination(destination, *expected)
            if (not self._remove_slot(destination, expected) and
                    os.path.lexists(str(destination))):
                raise RuntimeError(
                    f'connector log sparse destination was rebound: {destination}')
        self._fsync_directory(self._path.parent)

    def _rotate(self: Any) -> None:
        self._use_anonymous_staging = True
        owner = self._owner
        owner.revalidate()
        try:
            mode = os.stat(self._path).st_mode & 0o7777
        except OSError:
            mode = None
        fd = os.open(str(self._path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                     getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as active:
            opened = os.fstat(fd)
            if owner.identity not in (None, (str(opened.st_dev), str(opened.st_ino))):
                raise RuntimeError(
                    f'connector log identity was replaced while owned: {self._path}')
            data = active.read()
        manifest, planned = self._rotation_plan(len(data), self._sha256_bytes(data), 4)
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()
        destinations = manifest['destinations']
        journal = self._rotation_manifest_path()
        self._write_migration_manifest(manifest, kind='rotate', create=True)
        staged, published = None, False
        try:
            if planned is not None:
                staged = self._stage_rotation_copy(manifest, data, mode, planned)
            self._mark_generation_publishing(manifest)
            published = True
            for position in range(len(destinations) - 1):
                self._publish_rotation_slot(manifest, position, staged)
            self._publish_in_place(self._path, None,
                                   *self._journal_pair(destinations[-1], 'before'))
        except BaseException:
            if not published:
                self._unstage_rotation(manifest, staged, journal)
            raise
        self._adopt_rotated_identities(self._rotation_identities(destinations))
        self._drop_rotation_journal(journal)
        self._open_active(mode)
        self._use_anonymous_staging = False

    def _unstage_rotation(self: Any, manifest: dict, staged: Any, journal: Path) -> None:
        kept = (staged is not None and
                not self._unlink_if_identity(
                    staged, *self._journal_pair(manifest['destinations'][-2], 'temporary')) and
                os.path.lexists(str(staged)))
        if not (kept or self._staging_cleanup_failed):
            try:
                journal.unlink()
            except FileNotFoundError:
                pass
            self._fsync_directory(self._path.parent)
            self._fsync_directory(journal.parent)

    def _rotation_identities(self: Any, destinations: list) -> dict:
        return {Path(item['destination']): self._owner.identity if item['method'] == 'rewrite' else
                self._manifest_identity(item.get('source_identity', item.get('temporary_identity')))
                for item in destinations}

    def _drop_rotation_journal(self: Any, journal: Path) -> None:
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(self._path.parent)
        self._fsync_directory(journal.parent)

    def _rotation_plan(self: Any, data_len: int, digest: str | None,
                       version: int) -> tuple[dict, Path | None]:
        owner = self._owner
        empty = self._sha256_bytes(b'')
        backups = [self._backup_path(index) for index in range(self._backup_count, 0, -1)]
        states = {slot: self._journal_file_state(slot, hash_content=False)
                  for slot in (*backups, self._path)}
        dropped = set()
        for slot in backups:
            try:
                info = os.lstat(slot)
            except FileNotFoundError:
                continue
            if not info.st_ino or stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f'connector log backup cannot be journaled: {slot}')
            # A rename would carry a link or special file down the window: drop its name instead.
            if (not stat.S_ISREG(info.st_mode) or
                    (str(info.st_dev), str(info.st_ino)) == owner.identity):
                dropped.add(slot)
        # Revalidate while the handle is held: a refusal must not look like a failed rotation.
        stable_paths = owner.manifest_stable_paths()
        self._transaction_nonce = uuid.uuid4().hex
        self._staging_cleanup_failed = False

        def entry(slot, method, **fields):
            state = states[slot]
            return {'destination': str(slot), 'method': method, 'present': method != 'unlink',
                    'before_identity': state['identity'],
                    'before_entry_identity': state['entry_identity'],
                    'before_present': state['present'], **fields}
        destinations = [
            entry(slot, 'rename', source=str(source),
                  source_identity=states[source]['identity'],
                  source_entry_identity=states[source]['entry_identity'])
            if states[source]['present'] and source not in dropped else entry(slot, 'unlink')
            for slot, source in zip(backups, backups[1:])]
        planned = self._owned_temp_path('rotate', len(destinations)) if backups else None
        if planned is not None:
            destinations.append(entry(
                backups[-1], 'stage', temporary=str(planned), temporary_identity=None,
                temporary_entry_identity=None, size=data_len, sha256=digest))
        destinations.append(entry(
            self._path, 'rewrite', in_place=True, cut=data_len, prefix_sha256=digest,
            tail='', tail_sha256=empty, size=0, sha256=empty))
        temporaries = [str(planned)] if planned is not None else []
        manifest = {
            'version': version,
            'identity_contract': 'entry-v1',
            'kind': 'rotate',
            'token': self._identity_digest,
            'state': 'preparing',
            'root': str(self._path.parent),
            'log_name': self._path.name,
            'backup_count': self._backup_count,
            'generation': uuid.uuid4().hex,
            'transaction_nonce': self._transaction_nonce,
            'staging': self._PAYLOAD_STAGING,
            'temporaries': temporaries,
            'planned_temporaries': list(temporaries),
            'created_temporaries': [],
            'stable_paths': stable_paths,
            'destinations': destinations,
        }
        return manifest, planned
