"""One DM-user resolution for send, move and conversation, so they cannot drift."""
from typing import Any, Awaitable, Callable


async def _resolve_dm_user_id(state: dict[str, Any], name: Any,
                              refresh: Callable[[], Awaitable[None]]) -> int | None:
    uid = state['identity_map'].get(name)
    if uid is None and str(name).isdigit():
        return int(name)
    if uid is None:
        await refresh()
        uid = state['identity_map'].get(name)
    return uid


async def _open_dm_channel(client: Any, uid: int) -> Any:
    user = client.get_user(uid) or await client.fetch_user(uid)
    return user.dm_channel or await user.create_dm()
