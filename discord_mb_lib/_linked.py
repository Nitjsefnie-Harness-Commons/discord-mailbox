"""Message links in a body, and the cap on fetching them."""
import re

LINKED_MESSAGE_FETCH_LIMIT = 5
NOT_FETCHED = f'over the {LINKED_MESSAGE_FETCH_LIMIT}-link cap'
_LINK_RE = re.compile(r'https?://(?:\w+\.)?discord(?:app)?\.com/channels/(@me|\d+)/(\d+)/(\d+)')


def linked_message_refs(content: str) -> list[tuple[str, str, str]]:
    refs: dict[str, tuple[str, str, str]] = {}
    for guild, channel, msg_id in _LINK_RE.findall(content):
        refs.setdefault(msg_id, (channel, msg_id, f'https://discord.com/channels/{guild}/{channel}/{msg_id}'))
    return list(refs.values())
