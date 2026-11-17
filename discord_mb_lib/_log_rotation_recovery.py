"""Recovery of an interrupted connector log rotation from its journal."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import os
from pathlib import Path
from typing import Any

_ABSENT = (None, None)


class _RotationRecovery:
    """Rotation recovery: every entry is decided before any is applied."""

    def _recover_rotation(self: Any) -> None:
        """Finish or discard an interrupted rotation from its durable journal."""
        journal = self._rotation_manifest_path()
        if not journal.exists():
            # Without a durable journal there is no ownership proof for any
            # staged pathname. Never sweep similarly named user files from
            # the log directory.
            self._cleanup_journal_temp(journal, 'rotate')
            return
        manifest, root, log_name = self._journal_manifest(journal, 'rotate')
        plan = None
        try:
            if manifest.get('state') == 'preparing':
                self._cleanup_preparing_temps(manifest, root, 'rotate', log_name)
            else:
                plan = self._classify_rotation(
                    self._rotation_entries(manifest, root, log_name))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f'cannot recover connector log journal {journal}: {exc}') from exc
        if plan is not None:
            self._apply_rotation(plan)
            self._adopt_rotated_identities(
                {entry['destination']: expected for entry, _, expected in plan})
        journal.unlink()
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(root)
        self._fsync_directory(journal.parent)

    def _slot_identity(self: Any, path: Path) -> tuple:
        return (self._owner._identity_for(path), self._owner._entry_identity(path))

    def _rotation_entries(self: Any, manifest: dict, root: Path,
                          log_name: str) -> list[dict[str, Any]]:
        """Read v3 slots as entries; the active's rewrite has an empty tail."""
        backup_count = int(manifest['backup_count'])
        if not isinstance(manifest['destinations'], list) or backup_count < 0:
            raise ValueError('connector log journal has invalid rotation slots')
        slot_names = {log_name, *(f'{log_name}.{index}'
                                  for index in range(1, backup_count + 1))}
        entries = []
        for index, item in enumerate(manifest['destinations']):
            def pair(field, item=item):
                return (self._manifest_identity(item.get(f'{field}_identity')),
                        self._manifest_identity(item.get(f'{field}_entry_identity')))
            destination = self._manifest_path(item['destination'], root)
            if destination.name not in slot_names:
                raise ValueError('unsafe rotation destination')
            entry: dict[str, Any] = {
                'destination': destination, 'before': pair('before'),
                'marked': bool(item.get('publish_state')),
                'temporary': None, 'staged': _ABSENT,
                'context': {'expected_kind': 'rotate', 'expected_slot': index,
                            'expected_destination': destination,
                            'expected_nonce': manifest.get('transaction_nonce')}}
            if item.get('in_place'):
                entry.update(method='rewrite', cut=int(item['before_size']),
                             prefix_sha256=str(item['before_sha256']), tail=b'',
                             size=0, sha256=self._sha256_bytes(b''))
            elif item['present']:
                entry.update(method='stage', size=int(item['size']),
                             sha256=str(item['sha256']))
            else:
                entry['method'] = 'unlink'
            if item['present']:
                temporary = self._manifest_path(item['temporary'], root)
                self._validate_journal_temp(temporary, 'rotate', log_name)
                entry.update(temporary=temporary, staged=pair('temporary'))
            entries.append(entry)
        return entries

    def _classify_rotation(self: Any, entries: list[dict[str, Any]]) -> list[tuple]:
        """Decide each entry from its slots' identities, changing nothing."""
        plan: list[tuple] = []
        decided = {}
        for entry in entries:
            destination, method = entry['destination'], entry['method']
            current = self._slot_identity(destination)
            temporary, staged = entry['temporary'], entry['staged']
            staged_temp = (None not in staged and
                           self._slot_identity(temporary) == staged)
            if method == 'stage':
                expected, gone = staged[0], not os.path.lexists(temporary)
                if (None not in staged and current == staged and
                        self._file_matches_state(
                            destination, entry['size'], entry['sha256'])):
                    outcome = 'complete'
                elif staged_temp and current in (entry['before'], _ABSENT):
                    outcome = 'publish'
                elif gone and entry['marked'] and current == _ABSENT:
                    # Published, then deleted: a retained backup's absence.
                    outcome, expected = 'absent', None
                elif gone and (not entry['marked'] or current == entry['before']):
                    raise RuntimeError(
                        f'connector log rotation is incomplete: '
                        f'{destination.name} has no staged replacement')
                else:
                    raise RuntimeError(
                        f'connector log journal destination was rebound: {destination}')
            elif method == 'unlink':
                expected = None
                if current == _ABSENT:
                    outcome = 'complete'
                elif current == entry['before']:
                    outcome = 'unlink'
                else:
                    raise RuntimeError(
                        f'connector log journal destination was rebound: {destination}')
            else:
                expected = self._owner.identity
                data, cut, tail = destination.read_bytes(), entry['cut'], entry['tail']
                holder = decided.get(destination.name + '.1')
                holds_prefix = holder is None or (
                    holder[1] in ('complete', 'publish') and
                    holder[0].get('sha256') == entry['prefix_sha256'])
                if data == tail:
                    outcome = 'complete'
                elif (data[cut:] == tail and len(data) == cut + len(tail) and
                      self._sha256_bytes(data[:cut]) == entry['prefix_sha256'] and
                      holds_prefix):
                    if not staged_temp:
                        raise RuntimeError(
                            f'connector log rotation is incomplete: '
                            f'{destination.name} has no staged replacement')
                    outcome = 'rewrite'
                elif holder is not None and holder[1] == 'absent':
                    # The only copy of the active's bytes is the active itself.
                    outcome = 'keep'
                else:
                    raise RuntimeError(
                        f'connector log journal destination content changed: {destination}')
            if outcome in ('publish', 'rewrite'):
                # Authenticate the staged bytes now, so a refusal precedes every write.
                self._read_staged_payload(
                    temporary, expected_size=entry['size'],
                    expected_digest=entry['sha256'], **entry['context'])
            decided[destination.name] = (entry, outcome)
            plan.append((entry, outcome, expected))
        return plan

    def _apply_rotation(self: Any, plan: list[tuple]) -> None:
        """Carry out each decided entry, revalidating a slot before changing it."""
        for entry, outcome, _expected in plan:
            destination, temporary = entry['destination'], entry['temporary']
            if outcome == 'publish':
                observed = self._slot_identity(destination)
                if observed not in (entry['before'], _ABSENT):
                    raise RuntimeError(
                        f'connector log journal destination was rebound: {destination}')
                try:
                    self._prepare_staged_for_replace(
                        temporary, entry['size'], entry['sha256'], **entry['context'])
                    self._revalidate_destination(destination, *observed)
                    self._revalidate_staged_for_replace(temporary)
                    os.replace(temporary, destination)
                finally:
                    self._forget_staged_for_replace(temporary)
            elif outcome == 'unlink':
                if (not self._unlink_if_identity(destination, *entry['before']) and
                        os.path.lexists(destination)):
                    raise RuntimeError(
                        f'connector log journal destination was rebound: {destination}')
            elif outcome == 'rewrite':
                self._validate_and_remember_staged(
                    temporary, expected_size=entry['size'],
                    expected_digest=entry['sha256'], **entry['context'])
                try:
                    self._publish_in_place(destination, temporary, *entry['before'])
                finally:
                    self._forget_verified_staged(temporary)
            else:
                if temporary is not None and os.path.lexists(temporary):
                    self._unlink_if_identity(temporary, *entry['staged'])
                continue
            self._fsync_directory(destination.parent)
