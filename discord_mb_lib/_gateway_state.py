"""The connector's gateway state, readable from outside its process, and discord.py's
records in connector.log. A session is starting until its first READY, and a later
session's READY reads as reconnecting."""
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from . import _control_files

STATE_FILE = 'connector.state'
_STATES = {'connect': 'reconnecting', 'ready': 'connected', 'resumed': 'connected',
           'disconnect': 'disconnected'}


class _ConnectorLogHandler(logging.Handler):
    def __init__(self, log: Callable[[str], None]) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter('%(name)s %(levelname)s: %(message)s'))
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log(self.format(record))
        except Exception:
            self.handleError(record)


def run(client: Any, token: str, state_dir: Path, log: Callable[[str], None]) -> None:
    ready: list[bool] = []

    def record(state: str) -> None:
        log(f'gateway: {state}')
        try:
            _control_files.write_private(state_dir / STATE_FILE, json.dumps({'pid': os.getpid(), 'state': state}))
        except OSError as exc:
            log(f'gateway state {state!r} not recorded: {type(exc).__name__}: {exc}')

    def chain(event: str) -> None:
        bound = getattr(client, f'on_{event}', None)

        async def handler(*args: Any) -> None:
            if event == 'ready':
                ready.append(True)
            if event != 'connect' or ready:
                record(_STATES[event])
            if bound is not None:
                await bound(*args)
        handler.__name__ = f'on_{event}'          # discord.Client.event binds by name
        client.event(handler)

    for event in _STATES:
        chain(event)
    logger, handler = logging.getLogger('discord'), _ConnectorLogHandler(log)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(min(logger.getEffectiveLevel(), logging.INFO))
    try:
        record('starting')
        client.run(token, log_handler=None)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def current(state_dir: Path, identity: Any, pid_alive: Callable[[Any], bool],
            is_connector_process: Callable[[Any, Any], bool | None]) -> str | None:
    'None unless the recorded PID is a live connector for identity, judged as the PID lock judges it.'
    try:
        recorded = json.loads((state_dir / STATE_FILE).read_text(encoding='utf-8'))
        pid, state = recorded['pid'], recorded['state']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not pid_alive(pid) or is_connector_process(pid, identity) is False:
        return None
    return state
