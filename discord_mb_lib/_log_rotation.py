"""The connector log rotation transaction and the slot identities it adopts."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import errno
import os
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
        """Write one rotation payload once, claiming its inode before its first byte."""
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
            self._staging_claim_callback(name, identity)
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
            self._staging_claim_callback(name, identity)
            try:
                self._link_anonymous_fd(fd, name)
            except OSError as exc:
                if exc.errno not in _NO_ANONYMOUS:
                    raise
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
        owner = self._owner
        owner._stable_identities = {
            path: expected.get(Path(os.path.realpath(path.parent)) / path.name,
                               owner._stable_identities.get(path))
            for path in owner._path_candidates()}
        self._published_since_snapshot = False
        owner.revalidate()

    def _rotate(self: Any) -> None:
        self._use_anonymous_staging = True
        self._owner.revalidate()
        try:
            mode = os.stat(self._path).st_mode & 0o7777
        except OSError:
            mode = None
        destinations = []
        for index in range(self._backup_count, 0, -1):
            source = (self._backup_path(index - 1)
                      if index > 1 else self._path)
            present = source.is_file()
            data = source.read_bytes() if present else b''
            destination = self._backup_path(index)
            # A retained backup is not protected by the active inode lock.
            # Replace its pathname atomically even when it has external hard
            # links; publishing in place would mutate another owner's file.
            in_place = False
            destinations.append((destination, data, present, in_place))
        # The active inode is the final publish and is always updated in place.
        destinations.append((self._path, b'', True, True))

        self._transaction_nonce = uuid.uuid4().hex
        self._staging_cleanup_failed = False
        temporary = []
        staged_paths = [
            self._owned_temp_path('rotate', index)
            for index, (_destination, _data, present, _in_place)
            in enumerate(destinations) if present
        ]
        staged_iter = iter(staged_paths)
        planned_paths = [next(staged_iter) if present else None
                         for _destination, _data, present, _in_place
                         in destinations]
        path_state_map = {
            destination: self._journal_file_state(destination)
            for destination, _data, _present, _in_place in destinations
        }
        # Revalidate while the handle is held: a refusal must not look like a failed rotation.
        stable_paths = self._owner.manifest_stable_paths()
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()
        manifest = {
            'version': 3,
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
            'temporaries': [str(path) for path in staged_paths],
            'planned_temporaries': [str(path) for path in staged_paths],
            'created_temporaries': [],
            'path_states': list(path_state_map.values()),
            'stable_paths': stable_paths,
            'destinations': [
                {
                    'destination': str(destination),
                    'temporary': (str(planned_paths[index])
                                  if planned_paths[index] is not None else None),
                    'present': present,
                    'size': len(data),
                    'sha256': self._sha256_bytes(data),
                    'in_place': in_place,
                    'before_identity': path_state_map[destination]['identity'],
                    'before_entry_identity': path_state_map[destination][
                        'entry_identity'],
                    'before_present': path_state_map[destination]['present'],
                    'before_size': path_state_map[destination]['size'],
                    'before_sha256': path_state_map[destination]['sha256'],
                    'temporary_identity': None,
                    'temporary_entry_identity': None,
                }
                for index, (destination, data, present, in_place)
                in enumerate(destinations)
            ],
        }
        journal = self._rotation_manifest_path()
        self._write_migration_manifest(manifest, kind='rotate', create=True)
        published_count = 0
        try:
            # Durable ownership exists before the first stage.  A hard exit
            # therefore leaves only this transaction's fixed, recoverable set.
            for index, (_destination, data, present, _in_place) in enumerate(
                    destinations):
                if not present:
                    temporary.append(None)
                    continue
                self._staging_kind = 'rotate'
                self._staging_slot = index
                self._staging_destination = destinations[index][0]

                def claim(staged, identity, index=index):
                    entry = manifest['destinations'][index]
                    entry['temporary_identity'] = list(identity)
                    entry['temporary_entry_identity'] = list(identity)
                    # A portable retry after an unnamed anonymous stage claims the same name again.
                    if str(staged) not in manifest['created_temporaries']:
                        manifest['created_temporaries'].append(str(staged))
                    self._write_migration_manifest(manifest, kind='rotate')
                self._staging_claim_callback = claim
                try:
                    staged = self._write_migration_temp(
                        data, mode, name=planned_paths[index])
                finally:
                    self._staging_claim_callback = None
                temporary.append(staged)
            self._fsync_directory(self._path.parent)
            manifest['state'] = 'prepared'
            self._write_migration_manifest(manifest, kind='rotate')
            for index, ((destination, _data, present, in_place), staged) in enumerate(
                    zip(destinations, temporary)):
                self._mark_destination_publishing(manifest, index, 'rotate')
                published_count += 1
                entry = manifest['destinations'][index]
                if present:
                    if in_place:
                        self._validate_and_remember_staged(
                            staged, expected_size=len(_data),
                            expected_digest=self._sha256_bytes(_data),
                            expected_kind='rotate', expected_slot=index,
                            expected_destination=destination,
                            expected_nonce=self._transaction_nonce,
                            expected_staging=self._PAYLOAD_STAGING)
                        try:
                            self._publish_in_place(
                                destination, staged,
                                self._manifest_identity(
                                    entry.get('before_identity')),
                                self._manifest_identity(
                                    entry.get('before_entry_identity')))
                        finally:
                            self._forget_verified_staged(staged)
                    else:
                        self._prepare_staged_for_replace(
                            staged, len(_data), self._sha256_bytes(_data),
                            expected_kind='rotate', expected_slot=index,
                            expected_destination=destination,
                            expected_nonce=self._transaction_nonce,
                            expected_staging=self._PAYLOAD_STAGING)
                        self._revalidate_destination(
                            destination,
                            self._manifest_identity(entry.get('before_identity')),
                            self._manifest_identity(
                                entry.get('before_entry_identity')))
                        self._revalidate_staged_for_replace(staged)
                        try:
                            os.replace(staged, destination)
                        finally:
                            self._forget_staged_for_replace(staged)
                else:
                    removed = self._unlink_if_identity(
                        destination,
                        self._manifest_identity(
                            entry.get('before_identity')),
                        self._manifest_identity(
                            entry.get('before_entry_identity')))
                    if (not removed and
                            os.path.lexists(str(destination))):
                        raise RuntimeError(
                            f'connector log sparse destination was rebound: '
                            f'{destination}')
                self._fsync_directory(self._path.parent)
            temporary.clear()
        except BaseException:
            cleanup_ok = not getattr(self, '_staging_cleanup_failed', False)
            if published_count == 0:
                for index, staged in enumerate(temporary):
                    if staged is not None:
                        entry = manifest['destinations'][index]
                        removed = self._unlink_if_identity(
                            staged,
                            self._manifest_identity(
                                entry.get('temporary_identity')),
                            self._manifest_identity(
                                entry.get('temporary_entry_identity')))
                        if (not removed and
                                os.path.lexists(str(staged))):
                            cleanup_ok = False
                if cleanup_ok:
                    try:
                        journal.unlink()
                    except FileNotFoundError:
                        pass
                    self._fsync_directory(self._path.parent)
                    self._fsync_directory(journal.parent)
            raise
        for index, leftover in enumerate(staged_paths):
            entry = next(
                (item for item in manifest['destinations']
                 if isinstance(item, dict) and
                 item.get('temporary') == str(leftover)),
                {})
            self._unlink_if_identity(
                leftover,
                self._manifest_identity(entry.get('temporary_identity')),
                self._manifest_identity(entry.get('temporary_entry_identity')))
        journal = self._rotation_manifest_path()
        self._adopt_rotated_identities({
            Path(entry['destination']): (
                self._owner.identity if entry['in_place'] else
                self._manifest_identity(entry['temporary_identity']))
            for entry in manifest['destinations']})
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(self._path.parent)
        self._fsync_directory(journal.parent)
        self._open_active(mode)
        self._use_anonymous_staging = False
