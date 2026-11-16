"""Who may put a message in the inbox, and how often: the connector's delivery gates."""
import collections
import math
import re
import time
from typing import Any, Callable

from ._directory import _Directory
from ._settings import setting

USERS = 'DISCORD_MAILBOX_ALLOWED_USER_IDS'
GUILDS = 'DISCORD_MAILBOX_ALLOWED_GUILD_IDS'
RATE = 'DISCORD_MAILBOX_SENDER_RATE_LIMIT'
TRAIN_MEMORY = 256

# perform_send frames chunk 1 as `<mentions> **[sender→to]** subject` and chunk k
# as `**[sender→to]** subject (k/n)`: n is not on chunk 1, so a train is keyed by
# the channel, the header and the subject.
_FRAME_RE = re.compile(r'(?:<@[!&]?\d+> )*\*\*\[([^\]\n]*→[^\]\n]*)\]\*\*[ \t]*([^\n]*?)'
                       r'(?:[ \t]*\((\d+)/\d+\))?[ \t]*(?:\n|$)')

TrainKey = tuple[int, str, str]


def train_of(channel_id: int, content: str | None) -> tuple[TrainKey, int] | None:
    """(train key, chunk index) of a framed message; a frame with no (k/n) is chunk 1."""
    m = _FRAME_RE.match(content or '')
    if m is None:
        return None
    return (channel_id, m.group(1), m.group(2)), int(m.group(3) or 1)


def parse_ids(name: str, raw: str | None) -> frozenset[int]:
    if raw is None:
        return frozenset()
    ids = [item.strip() for item in raw.split(',') if item.strip()]
    if not ids or not all(re.fullmatch('[0-9]+', item) for item in ids):
        raise ValueError(f'{name}={raw!r}: expected comma-separated Discord ids')
    return frozenset(int(item) for item in ids)


def parse_rate(name: str, raw: str | None) -> tuple[int, int] | None:
    if raw is None:
        return None
    m = re.fullmatch('([0-9]+)/([0-9]+)', raw.strip())
    if m is None or int(m.group(1)) < 1 or int(m.group(2)) < 1:
        raise ValueError(f'{name}={raw!r}: expected <count>/<seconds>, both positive integers')
    return int(m.group(1)), int(m.group(2))


class _DeliveryGates:
    def __init__(self, users: frozenset[int], guilds: frozenset[int],
                 rate: tuple[int, int] | None, log: Callable[[str], None]):
        self.users, self.guilds, self.rate, self._log = users, guilds, rate, log
        self._train_authors: collections.OrderedDict[TrainKey, int] = collections.OrderedDict()
        self._delivered: dict[int, collections.deque[float]] = {}
        self._warned: dict[int, float] = {}
        self._swept = -math.inf

    @classmethod
    def from_settings(cls, log: Callable[[str], None]) -> '_DeliveryGates':
        try:
            return cls(parse_ids(USERS, setting(USERS)), parse_ids(GUILDS, setting(GUILDS)),
                       parse_rate(RATE, setting(RATE)), log)
        except ValueError as e:
            log(str(e))
            raise SystemExit(1) from e

    def refuses(self, msg: Any, continuation: bool, identity_map: dict[str, int],
                now: float | None = None) -> bool:
        """True when `msg` must not be delivered, logged (a capped sender once per
        window). `continuation`: it was routed only by its chunk 2+ frame."""
        author = msg.author.id
        guild = getattr(getattr(msg, 'guild', None), 'id', None)
        train = train_of(msg.channel.id, msg.content)
        if (self.users or self.guilds) and author not in self.users and guild not in self.guilds:
            return self._refuse(msg, f'allow-list: neither the user nor guild {guild} is in {USERS} or {GUILDS}')
        if (continuation and not _Directory.names_held(identity_map, author)
                and (train is None or self._train_authors.get(train[0]) != author)):
            return self._refuse(msg, 'continuation: an unregistered sender that did not send chunk 1')
        if self.rate is not None and self._over_rate(msg, time.monotonic() if now is None else now, *self.rate):
            return True
        if train is not None and train[1] == 1:
            self._train_authors[train[0]] = author
            self._train_authors.move_to_end(train[0])
            if len(self._train_authors) > TRAIN_MEMORY:
                self._train_authors.popitem(last=False)
        return False

    def _refuse(self, msg: Any, reason: str) -> bool:
        self._log(f'not delivered: {msg.id} from user {msg.author.id}: {reason}')
        return True

    def _over_rate(self, msg: Any, now: float, count: int, seconds: int) -> bool:
        if now - self._swept >= seconds:
            # Drop senders with nothing left in the window, so neither map grows unbounded.
            self._delivered = {a: d for a, d in self._delivered.items() if d[-1] > now - seconds}
            self._warned = {a: t for a, t in self._warned.items() if now - t < seconds}
            self._swept = now
        delivered = self._delivered.setdefault(msg.author.id, collections.deque(maxlen=count))
        while delivered and delivered[0] <= now - seconds:
            delivered.popleft()
        if len(delivered) < count:
            delivered.append(now)
            return False
        if now - self._warned.get(msg.author.id, -math.inf) >= seconds:
            self._warned[msg.author.id] = now
            self._refuse(msg, f'rate limit: over {RATE}={count}/{seconds}; '
                              f'not logged again for this sender for {seconds}s')
        return True
