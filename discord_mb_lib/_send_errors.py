"""Which send errors are final; a chunk's nonce."""
import hashlib
from pathlib import Path

import discord

from .core import SendError, SendRetry


def _send_failure(e: Exception, what: str, permanent: tuple[int, ...]) -> SendError | SendRetry:
    final = isinstance(e, discord.HTTPException) and e.status in permanent
    return (SendError if final else SendRetry)(f'{what}: {e}')


def _send_nonce(request: Path, chunk: int) -> str:
    # A claimed meta file adds a suffix.
    digest = hashlib.sha256(request.name.partition('.')[0].encode()).hexdigest()
    return f'{digest[:16]}{chunk}'
