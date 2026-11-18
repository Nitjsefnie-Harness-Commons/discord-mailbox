"""The main guild."""
from typing import Any, Sequence


def bridge_guild(guilds: Sequence[Any], bridge_name: str) -> Any | None:
    """The guild holding the bridge channel, in any listing order."""
    return next((g for g in guilds if any(c.name == bridge_name for c in g.text_channels)), None)


def setup_guild(guilds: Sequence[Any], bridge_name: str) -> Any:
    guild = bridge_guild(guilds, bridge_name) or (guilds[0] if len(guilds) == 1 else None)
    if guild is None:
        raise ValueError(f'the bot is in {len(guilds)} servers ({", ".join(repr(g.name) for g in guilds)}) and none has a '
                         f'#{bridge_name} channel; create #{bridge_name} in the intended server first')
    return guild
