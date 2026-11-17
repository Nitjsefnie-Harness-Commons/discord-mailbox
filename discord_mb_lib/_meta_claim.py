"""A meta request is claimed by the connector or withdrawn by the CLI, never both."""
import contextlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import _control_files, _gateway_state

_INTERRUPTED = ('interrupted: the connector stopped while running this request; its outcome is '
                'unknown (it may or may not have taken effect), so check before retrying')


class MetaRequestPending(Exception):
    'Claimed by the connector, not answered yet: not a failure.'


def _answer(claimed: Path, out_dir: Path, error: str) -> None:
    response = out_dir / claimed.name.removesuffix('.claimed')
    if not response.exists():
        _control_files.write_private(response, json.dumps({'ok': False, 'error': error}, indent=2))


def _overdue(claimed: Path) -> str | None:
    'The expiry error, or None when the request is within its wait or lacks the fields.'
    try:
        req = json.loads(claimed.read_text(encoding='utf-8'))
        created, wait = req['created'], req['timeout']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in (created, wait)):
        return None
    # The CLI withdraws at its deadline, so an answer due then reaches nobody.
    age = time.time() - created
    return f'expired: the request is {age:.0f}s old and its caller waits {wait}s, so it was not run' if age >= wait else None


def recover_control_files(in_dir: Path, out_dir: Path) -> None:
    'Answer each claim a dead connector left as interrupted, then sweep stale responses.'
    for claimed in sorted(in_dir.glob('*.json.claimed')):
        try:
            _answer(claimed, out_dir, _INTERRUPTED)
            claimed.unlink(missing_ok=True)
        except OSError:
            continue
    _control_files.sweep_stale_responses(out_dir)


@contextlib.contextmanager
def _claim(request: Path, out_dir: Path) -> Iterator[Path | None]:
    # Out of the connector's *.json scan.
    claimed = request.with_name(request.name + '.claimed')
    try:
        request.rename(claimed)
    except FileNotFoundError:
        yield None
        return
    try:
        expired = _overdue(claimed)
        if expired:
            _answer(claimed, out_dir, expired)
        yield None if expired else claimed
    except BaseException:       # every abnormal exit leaves the caller waiting for an answer
        with contextlib.suppress(OSError):    # a failed write must not replace the exit
            _answer(claimed, out_dir, _INTERRUPTED)
        raise
    finally:
        claimed.unlink(missing_ok=True)


def _take_response(response: Path) -> Any:
    data = json.loads(response.read_text(encoding='utf-8'))
    try:
        response.unlink()
    except OSError:
        pass
    return data


def _withdraw(request: Path, response: Path, timeout: float, identity: Any,
              pid_alive: Callable[[Any], bool], is_connector_process: Callable[[Any, Any], bool | None]) -> Any:
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
    state_dir = response.parent.parent
    state = _gateway_state.current(state_dir, identity, pid_alive, is_connector_process)
    if state is None:
        raise TimeoutError(f'connector did not respond within {timeout}s (is it running for {identity!r}?)')
    raise TimeoutError(f'connector did not respond within {timeout}s (it is running for {identity!r}, gateway state {state!r})')
