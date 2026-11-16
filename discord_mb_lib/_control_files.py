"""Private control files; stale responses swept."""
import os
import re
import tempfile
import time
from pathlib import Path

# --timeout is unbounded; every default is 120s or less.
RESPONSE_MAX_AGE = 15 * 60
_UUID_JSON = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json'
_RESPONSE_OR_TEMP = re.compile(rf'{_UUID_JSON}(?:\.tmp)?|\.{_UUID_JSON}\.[a-z0-9_]+\.tmp')


def write_private(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        try:
            f = os.fdopen(fd, 'w', encoding='utf-8')
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def sweep_stale_responses(directory: Path) -> None:
    cutoff = time.time() - RESPONSE_MAX_AGE
    for path in sorted(directory.glob('*')):
        try:
            if _RESPONSE_OR_TEMP.fullmatch(path.name) and os.lstat(path).st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            continue
