"""Local message records: removed when deleted on Discord, or erased by author."""
import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from ._temp_provenance import _linklike, owned_temp_scope

FORGOTTEN_MAX = 4096
_ID = re.compile(r'[0-9]+')
_RECORD = re.compile(r'[0-9]+\.json')
_CACHED = re.compile(r'[0-9]+\.json(?:\.tmp)?')
_CACHES = ('messages', 'pins')


def discord_id(value) -> str:
    '''`value` as ASCII digits, the only form ever joined into a store path.'''
    text = str(value)
    if not _ID.fullmatch(text):
        raise ValueError(f'not a Discord id: {text!r}')
    return text


def _scan(directory: Path, keep: Callable[[os.DirEntry], bool]) -> list[Path]:
    try:
        with os.scandir(directory) as entries:
            return [Path(e.path) for e in entries if keep(e)]
    except OSError:
        return []


def _subdirs(directory: Path) -> list[Path]:
    '''Real directories inside `directory`: a link never leads out of the state root.'''
    return _scan(directory, lambda e: e.is_dir(follow_symlinks=False) and not _linklike(Path(e.path)))


def _records(directory: Path, pattern: re.Pattern) -> list[Path]:
    return _scan(directory, lambda e: bool(pattern.fullmatch(e.name)) and e.is_file(follow_symlinks=False))


def _inboxes(root: Path) -> list[Path]:
    return [d for identity in _subdirs(root) for d in _subdirs(identity) if d.name == 'inbox']


def _caches(root: Path) -> list[Path]:
    return [d for cache in _subdirs(root) if cache.name in _CACHES for d in _subdirs(cache)]


def _channel_dirs(root: Path, channel_id: str) -> list[Path]:
    return [d for d in _caches(root) if d.name == channel_id]


def _unlink(path: Path) -> int:
    try:
        os.unlink(path)
    except OSError:
        return 0
    return 1


def _remove_where(directories: Iterable[Path], matches: Callable[[dict], bool]) -> int:
    removed = 0
    for path in (p for d in directories for p in _records(d, _RECORD)):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and matches(record):
            removed += _unlink(path)
    return removed


def _remember(forgotten: dict, ids: Iterable[str]) -> None:
    for i in ids:
        forgotten.pop(i, None)
        forgotten[i] = None
    while len(forgotten) > FORGOTTEN_MAX:
        del forgotten[next(iter(forgotten))]


def forget_messages(root: Path, channel_id, message_ids: Iterable, forgotten: dict) -> int:
    '''Remove the messages from every inbox and the channel's caches.'''
    ids = [discord_id(m) for m in message_ids]
    _remember(forgotten, ids)
    names = [f'{i}.json' for i in ids]
    stores = [*_channel_dirs(root, discord_id(channel_id)), *_inboxes(root)]
    return sum(_unlink(d / name) for d in stores for name in names)


def forget_channel(root: Path, channel_id, forgotten: dict) -> int:
    '''Remove a channel's caches and inbox records, and those of each thread an inbox record names.'''
    cid = discord_id(channel_id)
    threads: set[str] = set()

    def from_channel(record: dict) -> bool:
        channel = record.get('channel')
        if isinstance(channel, dict) and channel.get('parent_id') == cid:
            threads.add(str(record.get('channel_id')))
            return True
        return record.get('channel_id') == cid

    removed = _remove_where(_inboxes(root), from_channel)
    ids = {cid, *filter(_ID.fullmatch, threads)}
    _remember(forgotten, ids)
    for directory in (d for d in _caches(root) if d.name in ids):
        removed += sum(_unlink(p) for p in _records(directory, _CACHED))
        with contextlib.suppress(OSError):
            os.rmdir(directory)
    return removed


def erase_author(root: Path, user_id) -> int:
    '''Remove every inbox and cache record whose author is `user_id`.'''
    uid = discord_id(user_id)
    return _remove_where([*_inboxes(root), *_caches(root)], lambda record: record.get('from_user_id') == uid)


def erase_cli(root: Path, user_id: str) -> None:
    if os.path.lexists(root) and owned_temp_scope(root) != 'shared':
        sys.exit(f'error: {root} is not a private mailbox root this user created (a link, another '
                 f"user's, open to other users or unmarked); nothing erased")
    print(json.dumps({'user_id': user_id, 'removed': erase_author(root, user_id)}))


def watch(client, root: Path, log: Callable[[str], None]) -> Callable[[Path, Any], Path | None]:
    '''Register the deletion handlers; return the step that drops a record deleted while written.'''
    forgotten: dict = {}

    def settle(path: Path, msg) -> Path | None:
        if str(msg.id) not in forgotten and str(msg.channel.id) not in forgotten:
            return path
        _unlink(path)
        return None

    def handle(name: str, forget: Callable) -> None:
        async def handler(event):
            try:
                if removed := forget(event):
                    log(f'{name}: removed {removed} local record(s)')
            except Exception as e:
                log(f'{name} failed: {type(e).__name__}: {e}')
        handler.__name__ = name
        client.event(handler)

    handle('on_raw_message_delete', lambda e: forget_messages(root, e.channel_id, [e.message_id], forgotten))
    handle('on_raw_bulk_message_delete', lambda e: forget_messages(root, e.channel_id, e.message_ids, forgotten))
    handle('on_raw_thread_delete', lambda e: forget_channel(root, e.thread_id, forgotten))
    handle('on_guild_channel_delete', lambda channel: forget_channel(root, channel.id, forgotten))
    return settle
