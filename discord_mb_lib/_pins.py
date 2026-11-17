"""Pin or unpin a message; a 403 names the permission."""
from typing import Any

import discord


class PinRefused(Exception):
    """Discord answered a pin or unpin with 403."""


async def pin_message(msg: Any, pinned: bool, reason: str) -> None:
    try:
        await (msg.pin if pinned else msg.unpin)(reason=reason)
    except discord.Forbidden as e:
        raise PinRefused(f'{e}; pinning and unpinning need the Pin Messages permission') from e


async def pin_by_id(channel: Any, msg_id: str, pinned: bool, reason: str) -> dict[str, Any]:
    try:
        msg = await channel.fetch_message(int(msg_id))
    except Exception as e:
        raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
    await pin_message(msg, pinned, reason)
    return {'msg_id': str(msg.id), 'channel_id': str(channel.id), 'pinned': pinned}
