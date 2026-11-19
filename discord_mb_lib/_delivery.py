"""Who may put a message in the inbox, and how often: the connector's delivery gates."""
import asyncio
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
GATED = 'DISCORD_MAILBOX_GATED_GUILDS'
TRAIN_MEMORY = 256
MEMBER_TTL = 300
LOOKUP_TIMEOUT = 10
EXCERPT = 60
REFUSED_EMOJI = '\u274e'  # ❎

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


def parse_gated(name: str, raw: str | None) -> dict[int, frozenset[int]]:
    """`<guild id>:<user id>,...` entries separated by `;`; an empty user list allows no member."""
    if raw is None:
        return {}
    error = ValueError(f'{name}={raw!r}: expected <guild id>:<user id>,... entries separated by ;')
    gated: dict[int, frozenset[int]] = {}
    for entry in (e for e in raw.split(';') if e.strip()):
        guild, sep, users = (part.strip() for part in entry.partition(':'))
        if not sep or not re.fullmatch('[0-9]+', guild) or int(guild) in gated:
            raise error
        try:
            gated[int(guild)] = parse_ids(name, users) if users else frozenset()
        except ValueError:
            raise error from None
    if not gated:
        raise error
    return gated


class _DeliveryGates:
    def __init__(self, users: frozenset[int], guilds: frozenset[int], rate: tuple[int, int] | None,
                 log: Callable[[str], None], gated: dict[int, frozenset[int]] | None = None):
        self.users, self.guilds, self.rate, self._log, self.gated = users, guilds, rate, log, gated or {}
        self._train_authors: collections.OrderedDict[TrainKey, int | None] = collections.OrderedDict()
        self._delivered: dict[int, collections.deque[float]] = {}
        self._warned: dict[int, float] = {}
        self._swept = self._members_swept = -math.inf
        self._members: dict[tuple[int, int], tuple[bool, float]] = {}
        self._turns: dict[int, list[Any]] = {}

    @classmethod
    def from_settings(cls, log: Callable[[str], None]) -> '_DeliveryGates':
        try:
            return cls(parse_ids(USERS, setting(USERS)), parse_ids(GUILDS, setting(GUILDS)),
                       parse_rate(RATE, setting(RATE)), log, parse_gated(GATED, setting(GATED)))
        except ValueError as e:
            log(str(e))
            raise SystemExit(1) from e

    async def judge(self, msg: Any, continuation: bool, identity_map: dict[str, int], client: Any,
                    now: float | None = None) -> bool:
        """`refuses` plus the member gate, judged before the cap; only its refusals get ❎."""
        # One author's messages are judged in arrival order, so chunk 2 sees chunk 1's train.
        turn = self._turns.setdefault(msg.author.id, [asyncio.Lock(), 0])
        turn[1] += 1
        try:
            async with turn[0]:
                return await self._judged(msg, continuation, identity_map, client, now)
        finally:
            turn[1] -= 1
            if not turn[1]:
                del self._turns[msg.author.id]

    async def _judged(self, msg: Any, continuation: bool, identity_map: dict[str, int], client: Any,
                      now: float | None) -> bool:
        now = time.monotonic() if now is None else now
        if self._admission_refuses(msg, continuation, identity_map):
            return True
        if await self._gated_member(msg, client, now):
            try:
                await msg.add_reaction(REFUSED_EMOJI)
            except Exception as e:  # pylint: disable=broad-exception-caught
                self._log(f'react failed on {msg.id}: {e}')
            return True
        return self._spends(msg, now)

    def refuses(self, msg: Any, continuation: bool, identity_map: dict[str, int],
                now: float | None = None) -> bool:
        """True when `msg` must not be delivered by every gate but the member gate, logged
        (a capped sender once per window). `continuation`: it was routed only by its chunk 2+ frame."""
        return (self._admission_refuses(msg, continuation, identity_map)
                or self._spends(msg, time.monotonic() if now is None else now))

    def _admission_refuses(self, msg: Any, continuation: bool, identity_map: dict[str, int]) -> bool:
        author = msg.author.id
        guild = getattr(getattr(msg, 'guild', None), 'id', None)
        train = train_of(msg.channel.id, msg.content)
        if (self.users or self.guilds) and author not in self.users and guild not in self.guilds:
            return self._refuse(msg, f'allow-list: neither the user nor guild {guild} is in {USERS} or {GUILDS}')
        if (continuation and not _Directory.names_held(identity_map, author)
                and (train is None or self._train_authors.get(train[0]) != author)):
            return self._refuse(msg, 'continuation: an unregistered sender that did not send chunk 1')
        return False

    def _spends(self, msg: Any, now: float) -> bool:
        """The cap's verdict; a delivered chunk 1 is remembered for its train."""
        author, train = msg.author.id, train_of(msg.channel.id, msg.content)
        if self.rate is not None and self._over_rate(msg, now, *self.rate):
            return True
        if train is not None and train[1] == 1:
            # A second author's chunk 1 for the key makes it ambiguous (None): nobody vouches for it.
            kept = self._train_authors.get(train[0], author)
            self._train_authors[train[0]] = author if kept == author else None
            self._train_authors.move_to_end(train[0])
            if len(self._train_authors) > TRAIN_MEMORY:
                self._train_authors.popitem(last=False)
        return False

    async def _gated_member(self, msg: Any, client: Any, now: float) -> bool:
        author = msg.author.id
        written_in = getattr(getattr(msg, 'guild', None), 'id', None)
        # The guild a message was written in goes first: its author is a member, so no lookup.
        for guild in sorted(self.gated, key=lambda g: g != written_in):
            if author not in self.gated[guild] and (guild == written_in or await self._is_member(client, guild, author, now)):
                excerpt = ' '.join((msg.content or '').split())[:EXCERPT]
                return self._refuse(msg, f'member gate: a member of guild {guild} not on its {GATED} list; body {excerpt!r}')
        return False

    async def _is_member(self, client: Any, guild_id: int, user_id: int, now: float) -> bool:
        if now - self._members_swept >= MEMBER_TTL:
            # Bounds memory only; the read below checks each answer's age.
            self._members = {k: v for k, v in self._members.items() if now - v[1] < MEMBER_TTL}
            self._members_swept = now
        cached = self._members.get((guild_id, user_id))
        if cached is None or now - cached[1] >= MEMBER_TTL:
            try:
                guild = client.get_guild(guild_id)
                if guild is None:
                    raise LookupError('the bot is not in that guild')
                await asyncio.wait_for(guild.fetch_member(user_id), LOOKUP_TIMEOUT)
                member = True
            except Exception as e:  # pylint: disable=broad-exception-caught
                if getattr(e, 'status', None) != 404:  # discord.NotFound: not a member
                    self._log(f'member gate: lookup of user {user_id} in guild {guild_id} failed '
                              f'({type(e).__name__}: {e}); not judged by that guild')
                    return False
                member = False
            self._members[(guild_id, user_id)] = (member, now)
        return self._members[(guild_id, user_id)][0]

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
