"""Who may put a message in the inbox: the connector's delivery gates."""
import collections
import re
from typing import Any, Callable

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


class _DeliveryGates:
    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._train_authors: collections.OrderedDict[TrainKey, int] = collections.OrderedDict()

    @staticmethod
    def identity_for(identity_map: dict[str, int], user_id: int) -> str | None:
        """The directory name mapped to `user_id`; a sender that has one is registered."""
        return next((name for name, uid in identity_map.items() if uid == user_id), None)

    def refuses(self, msg: Any, continuation: bool, identity_map: dict[str, int]) -> bool:
        """True (and logged) when `msg` must not be delivered. `continuation`: it
        was routed only by its chunk 2+ frame."""
        author = msg.author.id
        train = train_of(msg.channel.id, msg.content)
        if (continuation and self.identity_for(identity_map, author) is None
                and (train is None or self._train_authors.get(train[0]) != author)):
            return self._refuse(msg, 'continuation: an unregistered sender that did not send chunk 1')
        if train is not None and train[1] == 1:
            self._train_authors[train[0]] = author
            self._train_authors.move_to_end(train[0])
            if len(self._train_authors) > TRAIN_MEMORY:
                self._train_authors.popitem(last=False)
        return False

    def _refuse(self, msg: Any, reason: str) -> bool:
        self._log(f'not delivered: {msg.id} from user {msg.author.id}: {reason}')
        return True
