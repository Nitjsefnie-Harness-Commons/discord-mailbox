"""Which send errors are final."""
import discord

from .core import SendError, SendRetry


def _send_failure(e: Exception, what: str, permanent: tuple[int, ...]) -> SendError | SendRetry:
    final = isinstance(e, discord.HTTPException) and e.status in permanent
    return (SendError if final else SendRetry)(f'{what}: {e}')
