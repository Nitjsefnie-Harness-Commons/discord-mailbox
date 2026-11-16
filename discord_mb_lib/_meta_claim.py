"""A meta request is claimed by the connector or withdrawn by the CLI, never both."""
import contextlib
import json
from pathlib import Path
from typing import Any, Iterator


class MetaRequestPending(Exception):
    'Claimed by the connector, not answered yet: not a failure.'


@contextlib.contextmanager
def _claim(request: Path) -> Iterator[Path | None]:
    # Out of the connector's *.json scan.
    claimed = request.with_name(request.name + '.claimed')
    try:
        request.rename(claimed)
    except FileNotFoundError:
        yield None
        return
    try:
        yield claimed
    finally:
        claimed.unlink(missing_ok=True)


def _take_response(response: Path) -> Any:
    data = json.loads(response.read_text(encoding='utf-8'))
    try:
        response.unlink()
    except OSError:
        pass
    return data


def _withdraw(request: Path, response: Path, timeout: float, identity: Any) -> Any:
    try:
        request.unlink()
    except FileNotFoundError:
        if response.exists():
            return _take_response(response)
        raise MetaRequestPending(
            f'the connector accepted the request and had not answered it after {timeout}s '
            f'(Discord may be rate-limiting it); this does not mean it failed: any result the '
            f'connector writes goes to {response}, which the connector removes once it is 15 minutes old') from None
    except OSError:
        pass
    raise TimeoutError(f'connector did not respond within {timeout}s (is it running for {identity!r}?)')
