"""Local message records: removed when deleted on Discord, or erased by author."""
import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Iterable

from ._temp_provenance import _linklike

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


def _remove_where(directories: Iterable[Path], field: str, value: str) -> int:
    removed = 0
    for path in (p for d in directories for p in _records(d, _RECORD)):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get(field) == value:
            removed += _unlink(path)
    return removed


def forget_messages(root: Path, channel_id, message_ids: Iterable) -> int:
    '''Remove the messages from every inbox and the channel's caches.'''
    names = [f'{discord_id(m)}.json' for m in message_ids]
    stores = [*_channel_dirs(root, discord_id(channel_id)), *_inboxes(root)]
    return sum(_unlink(d / name) for d in stores for name in names)


def forget_channel(root: Path, channel_id) -> int:
    '''Remove a channel's or thread's caches and every inbox record from it.'''
    cid = discord_id(channel_id)
    removed = 0
    for directory in _channel_dirs(root, cid):
        removed += sum(_unlink(p) for p in _records(directory, _CACHED))
        with contextlib.suppress(OSError):
            os.rmdir(directory)
    return removed + _remove_where(_inboxes(root), 'channel_id', cid)


def erase_author(root: Path, user_id) -> int:
    '''Remove every inbox and cache record whose author is `user_id`.'''
    return _remove_where([*_inboxes(root), *_caches(root)], 'from_user_id', discord_id(user_id))


def erase_cli(root: Path, user_id: str) -> None:
    if _linklike(root):
        sys.exit(f'error: {root} is a link; nothing erased')
    print(json.dumps({'user_id': user_id, 'removed': erase_author(root, user_id)}))


def watch(client, root: Path, log: Callable[[str], None]) -> None:
    '''Register the handlers for messages, threads and channels deleted on Discord.'''
    def handle(name: str, forget: Callable) -> None:
        async def handler(event):
            try:
                if removed := forget(event):
                    log(f'{name}: removed {removed} local record(s)')
            except Exception as e:
                log(f'{name} failed: {type(e).__name__}: {e}')
        handler.__name__ = name
        client.event(handler)

    handle('on_raw_message_delete', lambda e: forget_messages(root, e.channel_id, [e.message_id]))
    handle('on_raw_bulk_message_delete', lambda e: forget_messages(root, e.channel_id, e.message_ids))
    handle('on_raw_thread_delete', lambda e: forget_channel(root, e.thread_id))
    handle('on_guild_channel_delete', lambda channel: forget_channel(root, channel.id))
