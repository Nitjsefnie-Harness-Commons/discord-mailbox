"""Refuse extension modules other users can rewrite."""
import os
import stat
from collections.abc import Callable
from importlib.util import cache_from_source
from pathlib import Path


def check_extension(path: str | Path, *, posix: bool = os.name != 'nt', euid: int | None = None,
                    stat_of: Callable[[Path], os.stat_result] = os.stat) -> str | None:
    if not posix:
        return None
    euid = os.geteuid() if euid is None else euid
    real = Path(path).resolve()
    warnings = []
    # Either directory lets its writer swap the module; the cache lets it swap its bytecode.
    for target in dict.fromkeys((real, real.parent, Path(path).absolute().parent,
                                 Path(cache_from_source(str(Path(path)))).parent)):
        try:
            info = stat_of(target)
        except FileNotFoundError:   # the loader reports a missing module; the import creates its cache
            continue
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o002:
            raise PermissionError(f'{target} is writable by other users')
        if info.st_uid not in (euid, 0):
            raise PermissionError(f'{target} is owned by uid {info.st_uid}, not by this user or root')
        if mode & 0o020:
            warnings.append(f'{target} is group-writable')
    return '; '.join(warnings) or None
