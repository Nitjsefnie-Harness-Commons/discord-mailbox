"""`conversation --grep`, applied by the CLI."""
import re
import sys
from typing import Any


def pattern_or_exit(grep: str | None) -> re.Pattern[str] | None:
    if not grep:
        return None
    try:
        return re.compile(grep)
    except re.error as exc:
        print(f'invalid --grep pattern {grep!r}: {exc}', file=sys.stderr, flush=True)
        sys.exit(2)


def filter_messages(msgs: list[dict[str, Any]], pattern: re.Pattern[str] | None) -> list[dict[str, Any]]:
    if pattern is None:
        return msgs
    return [m for m in msgs
            if pattern.search(m.get('content') or '') or pattern.search(m.get('content_rendered') or '')]
