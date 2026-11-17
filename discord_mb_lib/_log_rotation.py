"""The connector log rotation transaction and its crash recovery."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import os
import uuid
from pathlib import Path
from typing import Any


class _RotationJournal:
    """Rotation methods of the connector log writer."""

    def _adopt_rotated_identities(self: Any, expected: dict) -> None:
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

    def _recover_rotation(self: Any) -> None:
        """Resume an interrupted ordinary rotation from its durable journal."""
        journal = self._rotation_manifest_path()
        if not journal.exists():
            # Without a durable journal there is no ownership proof for any
            # staged pathname. Never sweep similarly named user files from
            # the log directory.
            self._cleanup_journal_temp(journal, 'rotate')
            return

        manifest, root, log_name = self._journal_manifest(journal, 'rotate')
        if manifest.get('state') == 'preparing':
            try:
                self._cleanup_preparing_temps(
                    manifest, root, 'rotate', log_name)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f'cannot recover connector log journal {journal}: {exc}') from exc
            journal.unlink()
            self._cleanup_journal_temp(journal, 'rotate')
            self._fsync_directory(root)
            self._fsync_directory(journal.parent)
            return
        try:
            entries = manifest['destinations']
            backup_count = int(manifest['backup_count'])
            if not isinstance(entries, list) or backup_count < 0:
                raise ValueError('connector log journal has invalid rotation slots')
            slot_names = {log_name}
            slot_names.update(f'{log_name}.{index}'
                              for index in range(1, backup_count + 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f'cannot recover connector log journal {journal}: {exc}') from exc

        destinations = []
        for entry in entries:
            try:
                destination = self._manifest_path(entry['destination'], root)
                if destination.name not in slot_names:
                    raise ValueError('unsafe rotation destination')
                present = bool(entry['present'])
                temporary = None
                if present:
                    temporary = self._manifest_path(entry['temporary'], root)
                    self._validate_journal_temp(temporary, 'rotate', log_name)
                    expected_size = int(entry['size'])
                    expected_digest = str(entry['sha256'])
                else:
                    expected_size = 0
                    expected_digest = self._sha256_bytes(b'')
                before_identity = self._manifest_identity(
                    entry.get('before_identity'))
                before_entry_identity = self._manifest_identity(
                    entry.get('before_entry_identity'))
                temporary_identity = self._manifest_identity(
                    entry.get('temporary_identity'))
                temporary_entry_identity = self._manifest_identity(
                    entry.get('temporary_entry_identity'))
                before_size = int(entry.get('before_size', 0))
                before_digest = str(entry.get(
                    'before_sha256', self._sha256_bytes(b'')))
                before_present = bool(entry.get('before_present',
                                                before_identity is not None))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f'cannot recover connector log journal {journal}: bad rotation') from exc
            destinations.append((destination, temporary, present, expected_size,
                                 expected_digest, bool(entry.get('in_place', False)),
                                 before_identity, before_entry_identity,
                                 temporary_identity, temporary_entry_identity,
                                 before_size, before_digest, before_present))

        self._validate_recovery_states(manifest, root, destinations)
        for index, (destination, temporary, present, expected_size, expected_digest,
                    in_place, _before_identity, _before_entry_identity,
                    temporary_identity, temporary_entry_identity, _before_size,
                    _before_digest, _before_present) in enumerate(destinations):
            if not present:
                if temporary is not None and temporary.exists():
                    self._read_staged_payload(
                        temporary, expected_kind='rotate', expected_slot=index,
                        expected_destination=destination,
                        expected_nonce=manifest.get('transaction_nonce'))
                    self._unlink_if_identity(
                        temporary, temporary_identity, temporary_entry_identity)
                destination_identity, destination_entry_identity = (
                    self._manifest_path_identity(manifest, destination))
                self._unlink_if_identity(
                    destination, destination_identity, destination_entry_identity)
                self._fsync_directory(root)
                continue

            complete = False
            if destination.is_file():
                complete = self._file_matches_state(
                    destination, expected_size, expected_digest)
            if complete:
                if temporary.exists():
                    self._read_staged_payload(
                        temporary, expected_size=expected_size,
                        expected_digest=expected_digest,
                        expected_kind='rotate', expected_slot=index,
                        expected_destination=destination,
                        expected_nonce=manifest.get('transaction_nonce'))
                    self._unlink_if_identity(
                        temporary, temporary_identity, temporary_entry_identity)
                continue
            if not temporary.is_file():
                raise RuntimeError(
                    f'connector log rotation is incomplete: '
                    f'{destination.name} has no staged replacement')
            if in_place:
                self._validate_and_remember_staged(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind='rotate',
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                try:
                    self._publish_in_place(
                        destination, temporary, _before_identity,
                        _before_entry_identity)
                finally:
                    self._forget_verified_staged(temporary)
            else:
                self._prepare_staged_for_replace(
                    temporary, expected_size, expected_digest,
                    expected_kind='rotate', expected_slot=index,
                    expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                self._revalidate_destination(
                    destination, _before_identity, _before_entry_identity)
                self._revalidate_staged_for_replace(temporary)
                try:
                    os.replace(temporary, destination)
                finally:
                    self._forget_staged_for_replace(temporary)
            self._fsync_directory(root)

        self._validate_recovery_states(manifest, root, destinations)
        for index, (destination, temporary, _present, expected_size,
                    expected_digest, _in_place, _before_identity,
                    _before_entry_identity, _temporary_identity,
                    _temporary_entry_identity, _before_size, _before_digest,
                    _before_present) in enumerate(destinations):
            if temporary is not None and temporary.exists():
                self._read_staged_payload(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind='rotate',
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                self._unlink_if_identity(
                    temporary, _temporary_identity,
                    _temporary_entry_identity)
        self._snapshot_published_names()
        journal.unlink()
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(root)
        self._fsync_directory(journal.parent)

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

                def claim(staged, index=index):
                    entry = manifest['destinations'][index]
                    staged_identity = self._owner._identity_for(staged)
                    entry['temporary_identity'] = (
                        list(staged_identity) if staged_identity is not None
                        else None)
                    staged_entry_identity = self._owner._entry_identity(staged)
                    entry['temporary_entry_identity'] = (
                        list(staged_entry_identity) if staged_entry_identity
                        is not None else None)
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
                            expected_nonce=self._transaction_nonce)
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
                            expected_nonce=self._transaction_nonce)
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
