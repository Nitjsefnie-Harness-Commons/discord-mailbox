"""Private control files; stale responses swept."""
import os
import re
import tempfile
import time
from pathlib import Path

# --timeout is unbounded; every default is 120s or less.
RESPONSE_MAX_AGE = 15 * 60
_RESPONSE_NAME = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json')


def write_private(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def sweep_stale_responses(directory: Path) -> None:
    cutoff = time.time() - RESPONSE_MAX_AGE
    for path in sorted(directory.glob('*.json')):
        try:
            if _RESPONSE_NAME.fullmatch(path.name) and os.lstat(path).st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            continue
