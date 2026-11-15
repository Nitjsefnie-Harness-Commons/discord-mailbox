"""A meta request is claimed by the connector or withdrawn by the CLI, never both."""
import contextlib
import json
from pathlib import Path
from typing import Any, Iterator


class MetaRequestPending(Exception):
    'Claimed by the connector, not answered yet: not a failure.'


@contextlib.contextmanager
def _claim(request: Path) -> Iterator[Path | None]:
    # Out of the *.json glob the CLI withdraws by.
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
            f'the connector accepted the request and is still working on it after {timeout}s '
            f'(Discord may be rate-limiting it); this is not a failure: the result will be '
            f'written to {response}') from None
    except OSError:
        pass
    raise TimeoutError(f'connector did not respond within {timeout}s (is it running for {identity!r}?)')
