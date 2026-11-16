"""Who holds which agent-directory name, and when to rescan the channel."""
import re
from time import monotonic
from typing import Callable, Iterable

MISS_REFRESH_INTERVAL = 15.0  # seconds a lookup miss reuses the last scan's map


class _Directory:
    MAP_SEPARATORS = (':', '=', ' · ')  # only the map splits on the header's ' · '
    LIST_SEPARATORS = (':', '=')

    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._scanned: float | None = None

    def scan_due(self, force: bool) -> bool:
        return force or self._scanned is None or monotonic() - self._scanned >= MISS_REFRESH_INTERVAL

    def scan_completed(self) -> None:
        self._scanned = monotonic()

    def bind_names(self, entries: Iterable[tuple[str, int, int]]) -> dict[str, int]:
        """(name, mentioned id, author id), oldest first -> {name: id}."""
        held: dict[str, int] = {}
        for name, uid, author in entries:
            if uid != author:
                continue
            holder = held.setdefault(name, uid)
            if holder != uid:
                self._log(f'directory: {name!r} is held by user {holder}; '
                          f'ignoring the later entry from user {uid}')
        return held

    @staticmethod
    def parse_entry(content: str | None, separators: tuple[str, ...]) -> tuple[str, int] | None:
        content = content or ''
        uid_match = re.search(r'<@!?(\d+)>', content)
        if not uid_match:
            return None
        name = None
        bold = re.search(r'\*\*([^*\n]+)\*\*', content)
        if bold:
            name = bold.group(1).strip()
        else:
            for line in content.splitlines():
                line = line.strip().lstrip('-').strip()
                if not line:
                    continue
                for sep in separators:
                    if sep in line:
                        name = line.split(sep, 1)[0].strip()
                        break
                if name:
                    break
        return (name, int(uid_match.group(1))) if name else None
