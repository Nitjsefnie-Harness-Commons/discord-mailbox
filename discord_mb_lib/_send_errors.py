"""Which send errors are final, and the nonce that makes a re-sent chunk the same message."""
import hashlib
from pathlib import Path

import discord

from .core import SendError, SendRetry


def _send_failure(e: Exception, what: str, permanent: tuple[int, ...]) -> SendError | SendRetry:
    final = isinstance(e, discord.HTTPException) and e.status in permanent
    return (SendError if final else SendRetry)(f'{what}: {e}')


def _send_nonce(request: Path, chunk: int) -> str:
    # 16 hex digits of the request file's id (its name up to the first dot, so an
    # outbox file and a claimed meta request agree) plus the chunk index stays under
    # Discord's 25-character limit; a hash keeps ids that share a prefix apart.
    digest = hashlib.sha256(request.name.partition('.')[0].encode()).hexdigest()
    return f'{digest[:16]}{chunk}'
