"""Credentials-channel entries, and whose posts count."""
import base64
import re
from pathlib import Path
from typing import Any, Callable

from ._delivery import parse_ids
from ._settings import setting

TRUSTED = 'DISCORD_MAILBOX_CREDS_TRUSTED_USER_IDS'
_BLOCK_RE = re.compile(r'```(?:[^\n]*\n)?(.*?)```', re.DOTALL)


def token_user_id(path: Path) -> int | None:
    """A token's first dot-separated segment is its bot's user id in base64."""
    try:
        head = path.read_text(encoding='utf-8').strip().split('.', 1)[0]
        raw = base64.b64decode(head + '=' * (-len(head) % 4), validate=True)
        return int(raw) if re.fullmatch(rb'[0-9]+', raw) else None
    except (OSError, ValueError):
        return None


class _CredsTrust:
    def __init__(self, listed: frozenset[int] | None, bots: frozenset[int]):
        self.listed, self.bots = listed, bots

    @staticmethod
    def parse_entry(content: str | None) -> dict[str, Any] | None:
        """{domain, login, password} of a message with exactly 3 code blocks."""
        blocks = _BLOCK_RE.findall(content or '')
        if len(blocks) != 3:
            return None
        return dict(zip(('domain', 'login', 'password'), (b.strip() for b in blocks)))

    @classmethod
    def from_settings(cls, token_dir: Path, log: Callable[[str], None]) -> '_CredsTrust':
        raw = setting(TRUSTED)
        try:
            listed = None if raw is None else parse_ids(TRUSTED, raw)
        except ValueError as e:
            log(str(e))
            raise SystemExit(1) from e
        bots = set()
        for path in sorted(token_dir.glob('*.token')):
            uid = token_user_id(path)
            if uid is None:
                log(f'creds: {path.name} names no bot user id; its posts are not trusted')
            else:
                bots.add(uid)
        return cls(listed, frozenset(bots))

    def ids(self, own_id: int | None) -> frozenset[int]:
        return self.bots | (self.listed or frozenset()) | frozenset(() if own_id is None else (own_id,))

    def answer(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Setting unset (listed None): every entry. Set: the trusted ones, and a count."""
        untrusted = sum(not e['trusted'] for e in entries)
        if self.listed is None:
            text = f'{untrusted} of these entries are from untrusted authors; set {TRUSTED} to skip them'
            return {'entries': entries, **({'warning': text} if untrusted else {})}
        text = f'skipped {untrusted} entries from untrusted authors'
        return {'entries': [e for e in entries if e['trusted']], 'skipped_untrusted': untrusted,
                **({'warning': text} if untrusted else {})}
