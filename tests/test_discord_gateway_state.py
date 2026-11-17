#!/usr/bin/env python3
"""The connector's gateway state is readable from outside its process only while the
PID it names is this identity's live connector, and a timed-out request names it.
discord.py's records reach connector.log, its warnings still reach stderr."""
import asyncio
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _gateway_state, core  # noqa: E402

IDENTITY = "gw-test"


def _recorded(state_dir):
    return json.loads((Path(state_dir) / _gateway_state.STATE_FILE).read_text(encoding="utf-8"))


def test_each_gateway_event_records_its_state_before_the_handler_it_chains(tmp):
    lines, seen, runs, discord_log = [], [], [], logging.getLogger("discord")
    level = discord_log.level
    discord_log.setLevel(logging.ERROR)    # above INFO, so the run must lower it and put it back

    async def bound_on_ready():
        seen.append(_recorded(tmp)["state"])

    async def gateway():
        seen.append(_recorded(tmp)["state"])
        for event in ("connect", "disconnect", "connect", "ready", "disconnect", "connect", "resumed"):
            await getattr(client, f"on_{event}")()
            seen.append(_recorded(tmp)["state"])
        logging.getLogger("discord.gateway").info("an info record")

    client = types.SimpleNamespace(on_ready=bound_on_ready, event=lambda f: setattr(client, f.__name__, f) or f,
                                   run=lambda token, *, log_handler: runs.append((token, log_handler)) or asyncio.run(gateway()))
    try:
        _gateway_state.run(client, "token", Path(tmp), lines.append)
        logging.getLogger("discord.gateway").info("after the run")
        assert discord_log.level == logging.ERROR and not discord_log.handlers, (discord_log.level, discord_log.handlers)
    finally:
        discord_log.setLevel(level)
    assert runs == [("token", None)], runs
    assert seen == ["starting", "starting", "disconnected", "reconnecting", "connected", "connected", "disconnected", "reconnecting", "connected"], seen
    assert _recorded(tmp)["pid"] == os.getpid()
    if _util.POSIX_MODES:
        assert (Path(tmp) / _gateway_state.STATE_FILE).stat().st_mode & 0o777 == 0o600
    assert "discord.gateway INFO: an info record" in lines and not any("after the run" in line for line in lines), lines


def test_a_state_that_cannot_be_written_is_logged_and_the_gateway_still_runs(tmp):
    lines, runs, discord_log = [], [], logging.getLogger("discord")
    level = discord_log.level
    discord_log.setLevel(logging.DEBUG)    # already below INFO, so the run must not raise it
    client = types.SimpleNamespace(event=lambda f: f, run=lambda token, *, log_handler: runs.append((token, discord_log.level)))
    try:
        _gateway_state.run(client, "token", Path(tmp) / "missing", lines.append)
    finally:
        discord_log.setLevel(level)
    assert runs == [("token", logging.DEBUG)], runs
    assert any(line.startswith("gateway state 'starting' not recorded: FileNotFoundError") for line in lines), lines


def test_the_connector_logs_discord_records_and_keeps_their_warnings_on_stderr(tmp):
    err, recorded = io.StringIO(), []

    async def script(client):
        recorded.append(_recorded(Path(tmp) / "state"))
        with contextlib.redirect_stderr(err):
            logging.getLogger("discord.gateway").info("Shard ID None has successfully RESUMED session s1.")
            logging.getLogger("discord.client").warning("Attempting a reconnect in 3.00s")

    client = _gateway.run(tmp, script)
    assert recorded == [{"pid": os.getpid(), "state": "connected"}], recorded
    assert "discord.gateway INFO: Shard ID None has successfully RESUMED session s1." in client.log, client.log
    assert "discord.client WARNING: Attempting a reconnect in 3.00s" in client.log, client.log
    assert "discord.client WARNING: Attempting a reconnect in 3.00s" in err.getvalue(), err.getvalue()


def test_only_a_live_connector_is_reported_and_a_timeout_names_its_state(tmp):
    sleep, root = [sys.executable, "-c", "import time; time.sleep(60)"], Path(tmp)
    live, recycled = subprocess.Popen([*sleep, "discord_mb", IDENTITY]), subprocess.Popen(sleep)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=60)
    for name in ("inbox-metadata", "outbox-metadata"):
        (root / name).mkdir()
    saved, not_running = (core.meta_in_dir, core.meta_out_dir), "connector did not respond within 0.2s (is it running for 'gw-test'?)"
    core.meta_in_dir, core.meta_out_dir = lambda _: root / "inbox-metadata", lambda _: root / "outbox-metadata"

    def timeout_for(pid, text=None):
        (root / _gateway_state.STATE_FILE).write_text(text or json.dumps({"pid": pid, "state": "reconnecting"}), encoding="utf-8")
        try:
            return core._meta_request(IDENTITY, {"op": "list-agents"}, 0.2)
        except TimeoutError as e:
            return str(e)

    try:
        assert timeout_for(live.pid) == "connector did not respond within 0.2s (it is running for 'gw-test', gateway state 'reconnecting')"
        assert _gateway_state.current(root, IDENTITY, core.pid_alive, lambda *_: None) == "reconnecting", "unknown counts as a connector"
        assert timeout_for(dead.pid) == not_running, "a connector killed without cleanup is not running"
        assert timeout_for(recycled.pid) == not_running, "a recycled PID is not this identity's connector"
        assert timeout_for(live.pid, "{") == not_running
    finally:
        core.meta_in_dir, core.meta_out_dir = saved
        for child in (live, recycled):
            child.kill()
            child.wait(timeout=60)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="gatewaystate_")


if __name__ == "__main__":
    sys.exit(main())
