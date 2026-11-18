"""Recovery of an interrupted connector log rotation from its journal."""
# The writer in storage.py supplies every attribute these methods use.
# pylint: disable=no-member,attribute-defined-outside-init
import base64
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

_ABSENT = (None, None)


class _RotationRecovery:
    """Rotation recovery: every entry is decided before any is applied."""

    def _recover_rotation(self: Any) -> None:
        """Finish or discard an interrupted rotation from its durable journal.

        Renames synced after the lock persist sooner only where metadata commits in order
        (ext4); else at most one backup is lost, never refused; the prefix is staged first.
        """
        journal = self._rotation_manifest_path()
        sidecar = self._rotation_sidecar(journal)
        if not journal.exists():
            # Without a durable journal there is no ownership proof for any
            # staged pathname. Never sweep similarly named user files from
            # the log directory.
            self._cleanup_journal_temp(journal, 'rotate')
            sidecar.unlink(missing_ok=True)
            return
        manifest, root, log_name = self._journal_manifest(journal, 'rotate')
        plan = None
        try:
            manifest = self._rotation_record(manifest, sidecar)
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
        sidecar.unlink(missing_ok=True)
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(root)
        self._fsync_directory(journal.parent)

    @staticmethod
    def _rotation_record(manifest: dict, sidecar: Path) -> dict:
        """This generation's whole sidecar record, else the journal's; ``swapped`` marks it."""
        if manifest.get('sidecar') != sidecar.name or not sidecar.exists():
            return manifest
        raw = sidecar.read_bytes()
        payload = raw[40:40 + int.from_bytes(raw[:8], 'big')]
        if hashlib.sha256(payload).digest() != raw[8:40]:
            return manifest
        record = json.loads(payload)
        if not isinstance(record, dict) or any(
                record.get(key) != manifest.get(key)
                for key in ('version', 'kind', 'generation', 'transaction_nonce', 'token')):
            return manifest
        return {**record, 'state': 'swapped'} if manifest['state'] == 'swapped' else record

    def _slot_identity(self: Any, path: Path) -> tuple:
        return (self._owner._identity_for(path), self._owner._entry_identity(path))

    def _remove_slot(self: Any, path: Path, pair: tuple) -> bool:
        """Remove ``path`` while it holds ``pair``; True once removed.

        A regular file goes through ``_unlink_if_identity``.  A symlink or
        special file is never opened: its name moves to a quarantine name,
        which is unlinked only while it is still the recorded entry, so the
        inode it names is never written.
        """
        try:
            regular = stat.S_ISREG(os.lstat(path).st_mode)
        except FileNotFoundError:
            return False
        if regular:
            return self._unlink_if_identity(path, *pair)
        if pair[1] is None or self._owner._entry_identity(path) != pair[1]:
            return False
        quarantine = path.with_name(f'.{path.name}.drop-{uuid.uuid4().hex}.tmp')
        try:
            os.rename(path, quarantine)
        except OSError:
            return False
        try:
            moved = os.lstat(quarantine)
            # A new file can reuse a freed inode number: only a still-special entry is the recorded one.
            if ((str(moved.st_dev), str(moved.st_ino)) == tuple(pair[1]) and
                    not stat.S_ISREG(moved.st_mode)):
                os.unlink(quarantine)
                return True
        except OSError:
            pass
        try:
            if not os.path.lexists(path):  # never overwrite a name recreated meanwhile
                os.rename(quarantine, path)
        except OSError:
            pass
        return False

    def _journal_pair(self: Any, item: dict, field: str) -> tuple:
        """The inode and entry identities a journal entry records under ``field``."""
        return (self._manifest_identity(item.get(f'{field}_identity')),
                self._manifest_identity(item.get(f'{field}_entry_identity')))

    def _rotation_entries(self: Any, manifest: dict, root: Path,
                          log_name: str) -> list[dict[str, Any]]:
        """Read journal slots as entries; a v3 journal's active is a rewrite with an empty tail."""
        backup_count, version = int(manifest['backup_count']), int(manifest['version'])
        v4, v5 = version >= 4, version >= 5
        if not isinstance(manifest['destinations'], list) or backup_count < 0:
            raise ValueError('connector log journal has invalid rotation slots')
        slot_names = {log_name, *(f'{log_name}.{index}'
                                  for index in range(1, backup_count + 1))}
        entries = []
        for index, item in enumerate(manifest['destinations']):
            destination = self._manifest_path(item['destination'], root)
            if destination.name not in slot_names:
                raise ValueError('unsafe rotation destination')
            method = item['method'] if v4 else (
                'rewrite' if item.get('in_place') else 'stage' if item['present'] else 'unlink')
            entry: dict[str, Any] = {
                'destination': destination, 'method': method, 'v4': v4, 'v5': v5,
                'swapped': manifest['state'] == 'swapped',
                'before': self._journal_pair(item, 'before'),
                'marked': bool(item.get('publish_state')),
                'temporary': None, 'staged': _ABSENT, 'post': _ABSENT,
                'context': {'expected_kind': 'rotate', 'expected_slot': index,
                            'expected_destination': destination,
                            'expected_nonce': manifest.get('transaction_nonce'),
                            'expected_staging': manifest.get('staging')}}
            if method == 'rewrite':
                tail = base64.b64decode(item['tail'], validate=True) if v4 else b''
                if tail and not v5:
                    raise ValueError('connector log journal has an unsupported rotation tail')
                if (len(tail) > self._max_bytes or
                        v5 and self._sha256_bytes(tail) != item['tail_sha256']):
                    raise ValueError('connector log journal has a damaged rotation tail')
                entry.update(cut=int(item['cut' if v4 else 'before_size']),
                             prefix_sha256=str(item['prefix_sha256' if v4 else 'before_sha256']),
                             tail=tail, size=0, sha256=self._sha256_bytes(b''))
            elif method == 'stage':
                entry.update(size=int(item['size']), sha256=str(item['sha256']))
            elif method == 'rename':
                source = self._manifest_path(item['source'], root)
                if source.name not in slot_names:
                    raise ValueError('unsafe rotation source')
                entry.update(source=source, post=self._journal_pair(item, 'source'))
            elif method != 'unlink':
                raise ValueError('unknown connector log rotation method')
            if method == 'stage' or (not v4 and item['present']):
                temporary = self._manifest_path(item['temporary'], root)
                self._validate_journal_temp(temporary, 'rotate', log_name)
                entry.update(temporary=temporary, staged=self._journal_pair(item, 'temporary'))
                if method == 'stage':
                    entry['post'] = entry['staged']
            entries.append(entry)
        return entries

    def _classify_rotation(self: Any, entries: list[dict[str, Any]]) -> list[tuple]:
        """Decide each entry from its slots' identities, changing nothing."""
        plan: list[tuple] = []
        decided = {}
        for position, entry in enumerate(entries):
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
            elif method == 'rename':
                expected, source = entry['post'][0], self._slot_identity(entry['source'])
                newer = entries[position + 1]['post'] if position + 1 < len(entries) else _ABSENT
                if current == entry['post']:
                    outcome = 'complete'
                elif current not in (entry['before'], _ABSENT):
                    raise RuntimeError(
                        f'connector log journal destination was rebound: {destination}')
                elif source == entry['post']:
                    outcome = 'rename'
                elif source in (_ABSENT, newer):
                    # The source was deleted, or renamed here and deleted: the backup is absent.
                    outcome, expected = ('complete' if current == _ABSENT else 'unlink'), None
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
                expected, swapped = self._owner.identity, entry['swapped']
                data, cut, tail = destination.read_bytes(), entry['cut'], entry['tail']
                holder = decided.get(destination.name + '.1')
                holds_prefix = holder is None or (
                    holder[1] in ('complete', 'publish') and
                    holder[0].get('sha256') == entry['prefix_sha256'])
                whole = (data[cut:] == tail and len(data) == cut + len(tail) and
                         self._sha256_bytes(data[:cut]) == entry['prefix_sha256'] and
                         holds_prefix)
                if data == tail:
                    outcome = 'complete'
                elif not swapped and (whole or (entry['v5'] and holds_prefix and tail.startswith(data))):
                    # Or a crash inside the tail's in-place write.
                    if temporary is not None and not staged_temp:
                        raise RuntimeError(
                            f'connector log rotation is incomplete: '
                            f'{destination.name} has no staged replacement')
                    outcome = 'rewrite'
                elif entry['v5'] and data.startswith(tail):
                    # The tail, then later appends: checked after prefix plus tail, which may begin with the tail.
                    outcome = 'complete'
                elif holder is not None and holder[1] == 'absent':
                    # The only copy of the active's bytes is the active itself.
                    outcome = 'keep'
                elif swapped and holder is not None and holds_prefix:
                    # The tail is synced: only an in-place edit or lost fsync gets here
                    outcome = 'keep'
                else:
                    raise RuntimeError(
                        f'connector log journal destination content changed: {destination}')
            if outcome in ('publish', 'rewrite') and temporary is not None:
                # Authenticate the staged bytes now, so a refusal precedes every write.
                if not entry['v4']:
                    self._read_staged_payload(
                        temporary, expected_size=entry['size'],
                        expected_digest=entry['sha256'], **entry['context'])
                elif not self._file_matches_state(temporary, entry['size'], entry['sha256']):
                    raise RuntimeError(
                        f'connector log staging temp digest mismatch: {temporary}')
            decided[destination.name] = (entry, outcome)
            plan.append((entry, outcome, expected))
        return plan

    def _apply_rotation(self: Any, plan: list[tuple]) -> None:
        """Carry out each decided entry, revalidating a slot before changing it."""
        for entry, outcome, _expected in plan:
            destination, temporary = entry['destination'], entry['temporary']
            observed = self._slot_identity(destination)
            if outcome in ('publish', 'rename') and observed not in (entry['before'], _ABSENT):
                raise RuntimeError(
                    f'connector log journal destination was rebound: {destination}')
            if outcome == 'publish':
                try:
                    if entry['v4']:
                        self._claim_staged_for_replace(
                            temporary, expected_destination=destination,
                            identity=entry['staged'][0], entry_identity=entry['staged'][1])
                    else:
                        self._prepare_staged_for_replace(
                            temporary, entry['size'], entry['sha256'], **entry['context'])
                    self._revalidate_destination(destination, *observed)
                    self._revalidate_staged_for_replace(temporary)
                    os.replace(temporary, destination)
                finally:
                    self._forget_staged_for_replace(temporary)
            elif outcome == 'rename':
                self._revalidate_destination(entry['source'], *entry['post'])
                self._revalidate_destination(destination, *observed)
                os.replace(entry['source'], destination)
            elif outcome == 'unlink':
                if (not self._remove_slot(destination, entry['before']) and
                        os.path.lexists(destination)):
                    raise RuntimeError(
                        f'connector log journal destination was rebound: {destination}')
            elif outcome == 'rewrite' and temporary is None:
                self._publish_in_place(destination, None, *entry['before'], entry['tail'])
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
