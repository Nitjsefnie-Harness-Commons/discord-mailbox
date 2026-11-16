"""Refuse an extension whose source or bytecode other users can rewrite."""
import os
import stat
from collections.abc import Callable
from importlib.util import cache_from_source
from pathlib import Path

MAX_HOPS = 40   # Linux's limit: no chain it follows is refused


def _resolve(path: Path) -> tuple[Path, list[Path]]:
    """Resolve absolute `path` like os.path.realpath, and name the directory holding each link."""
    resolved, rest, holders = Path(path.anchor), list(reversed(path.parts[1:])), []
    while rest:
        name = rest.pop()
        if name == '..':
            resolved = resolved.parent
            continue
        candidate = resolved / name
        try:
            is_link = stat.S_ISLNK(os.lstat(candidate).st_mode)
        except FileNotFoundError:
            is_link = False
        if not is_link:
            resolved = candidate
            continue
        if len(holders) == MAX_HOPS:
            raise OSError(f'{path}: too many levels of symbolic links')
        holders.append(resolved)
        rest += reversed((resolved / os.readlink(candidate)).parts[1:])
        resolved = Path(path.anchor)
    return resolved, holders


def check_extension(path: str | Path, *, posix: bool = os.name != 'nt', euid: int | None = None,
                    stat_of: Callable[[Path | int], os.stat_result] = os.stat) -> tuple[bytes | None, str | None]:
    if not posix:
        return None, None
    euid = os.geteuid() if euid is None else euid
    real, holders = _resolve(Path(path).absolute())
    cached, cache_holders = _resolve(Path(cache_from_source(str(Path(path)))).absolute())
    try:    # `real` is resolved, so a symlink there now is a swap; O_NONBLOCK keeps a FIFO from hanging
        fd = os.open(real, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    except FileNotFoundError:
        raise FileNotFoundError(f'no such file: {path}') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileNotFoundError(f'no such file: {path}')
        warnings = []
        for target in dict.fromkeys((real, real.parent, Path(path).absolute().parent, *holders, *cache_holders, cached.parent, cached)):
            try:
                info = stat_of(fd if target == real else target)
            except FileNotFoundError:   # nothing exists there to judge
                continue
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o002:
                raise PermissionError(f'{target} is writable by other users')
            if info.st_uid not in (euid, 0):
                raise PermissionError(f'{target} is owned by uid {info.st_uid}, not by this user or root')
            if mode & 0o020:
                warnings.append(f'{target} is group-writable')
        return b''.join(iter(lambda: os.read(fd, 1 << 16), b'')), '; '.join(warnings) or None
    finally:
        os.close(fd)
