"""Refuse an extension whose source or bytecode other users can rewrite."""
import os
import stat
from collections.abc import Callable
from importlib.util import cache_from_source
from pathlib import Path

MAX_HOPS = 40


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
                    stat_of: Callable[[Path], os.stat_result] = os.stat) -> str | None:
    if not posix:
        return None
    euid = os.geteuid() if euid is None else euid
    real, holders = _resolve(Path(path).absolute())
    warnings, cached = [], Path(cache_from_source(str(Path(path))))
    for target in dict.fromkeys((real, real.parent, Path(path).absolute().parent, *holders, cached.parent, cached)):
        try:
            info = stat_of(target)
        except FileNotFoundError:   # the loader refuses a missing module; the import writes a missing cache
            continue
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o002:
            raise PermissionError(f'{target} is writable by other users')
        if info.st_uid not in (euid, 0):
            raise PermissionError(f'{target} is owned by uid {info.st_uid}, not by this user or root')
        if mode & 0o020:
            warnings.append(f'{target} is group-writable')
    return '; '.join(warnings) or None
