"""Refuse extension modules other users can rewrite."""
import os
import stat
from collections.abc import Callable
from pathlib import Path


def check_extension(path: str | Path, *, posix: bool = os.name != 'nt', euid: int | None = None,
                    stat_of: Callable[[Path], os.stat_result] = os.stat) -> str | None:
    if not posix or not Path(path).is_file():
        return None
    euid = os.geteuid() if euid is None else euid
    real = Path(path).resolve()
    warnings = []
    # Either directory lets its writer swap the module.
    for target in dict.fromkeys((real, real.parent, Path(path).absolute().parent)):
        info = stat_of(target)
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o002:
            raise PermissionError(f'{target} is writable by other users')
        if info.st_uid not in (euid, 0):
            raise PermissionError(f'{target} is owned by uid {info.st_uid}, not by this user or root')
        if mode & 0o020:
            warnings.append(f'{target} is group-writable')
    return '; '.join(warnings) or None
