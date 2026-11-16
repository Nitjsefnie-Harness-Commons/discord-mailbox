#!/usr/bin/env python3
"""discord_mb.py `conversation --dm`: read a DM by user, not by channel id.

The DM channel id of a user is not something a caller knows, and opening the
DM must not send anything. These drive the REAL `conversation` meta op: the
connector runs on a stand-in gateway client whose `on_ready` and `meta_watcher`
are the production closures, a request file is dropped in the connector's
metadata inbox and the response file is read back.

Stdlib only apart from discord.py itself, OS-agnostic (SETUP.md edit discipline).
"""
import asyncio
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")
LOCK_ROOT_ENV = "DISCORD_MB_TEST_LOCK_ROOT"

BOT_ID = 910000000000000001
ALICE_ID = 910000000000000002
CAROL_ID = 910000000000000003      # registered only after a directory refresh
RAW_ID = 910000000000000004        # a human no directory entry names
ALICE_DM = 920000000000000001
RAW_DM = 920000000000000002
BRIDGE_ID = 930000000000000001

_SWAPPED = (
    "state_dir", "sweep_status_plugin", "default_status_plugin",
    "read_extension_registry", "msg_cache_dir", "meta_in_dir", "meta_out_dir")


def _connector():
    """(connector module, discord) or (None, None) when discord.py is absent."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        import discord
        from discord_mb_lib import connector
    except ImportError:
        return None, None
    return connector, discord


def _message(msg_id, author_id, channel_id, content):
    return types.SimpleNamespace(
        id=msg_id, content=content, pinned=False,
        author=types.SimpleNamespace(id=author_id, name=f"user{author_id}"),
        channel=types.SimpleNamespace(id=channel_id),
        created_at=datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc),
        attachments=[], mentions=[], role_mentions=[], embeds=[], components=[],
        poll=None, reactions=[], message_snapshots=[])


class _World:
    """What the stand-in gateway knows: the directory, users and channels."""

    def __init__(self, discord, connector):
        self.directory_reads = 0
        self.directory = [_message(1, BOT_ID, 5, f"**bridge-bot** · <@{BOT_ID}>"),
                          _message(2, ALICE_ID, 5, f"**alice** · <@{ALICE_ID}>")]
        self.late_directory = [_message(3, CAROL_ID, 5, f"**carol** · <@{CAROL_ID}>")]
        self.created_dms = []
        self.sent = []
        self.fetch_error = None
        world = self

        class Text(discord.TextChannel):
            def __init__(self, channel_id, name):
                self.id, self.name, self.calls = channel_id, name, []

            async def history(self, **kwargs):
                self.calls.append(kwargs)
                yield _message(10, ALICE_ID, self.id, "from the bridge")

            async def send(self, *args, **kwargs):
                world.sent.append((self.name, args))

        class DM(discord.DMChannel):
            def __init__(self, channel_id, msgs):
                self.id, self.msgs, self.calls = channel_id, msgs, []

            async def history(self, **kwargs):
                self.calls.append(kwargs)
                for m in self.msgs:
                    yield m

            async def send(self, *args, **kwargs):
                world.sent.append((self.id, args))

        class User:
            def __init__(self, user_id, dm):
                self.id, self.dm_channel = user_id, dm

            async def create_dm(self):
                world.created_dms.append(self.id)
                self.dm_channel = DM(RAW_DM, [
                    _message(30, RAW_ID, RAW_DM, "first contact"),
                    _message(31, BOT_ID, RAW_DM, "reply")])
                return self.dm_channel

        self.Text = Text
        self.bridge = Text(BRIDGE_ID, connector.BRIDGE_CHANNEL_NAME)
        self.directory_channel = Text(BRIDGE_ID + 1, connector.DIRECTORY_CHANNEL_NAME)
        self.directory_channel.history = self._directory_history
        self.alice_dm = DM(ALICE_DM, [
            _message(22, BOT_ID, ALICE_DM, "newer needle"),
            _message(21, ALICE_ID, ALICE_DM, "plain hello")])
        self.users = {ALICE_ID: User(ALICE_ID, self.alice_dm),
                      CAROL_ID: User(CAROL_ID, self.alice_dm),
                      RAW_ID: User(RAW_ID, None)}
        self.cached = {ALICE_ID, CAROL_ID}      # get_user hits; the rest fetch

    async def _directory_history(self, **kwargs):
        self.directory_reads += 1
        # The first read is the connector's own startup refresh; every later
        # one sees the entry registered since.
        entries = self.directory + (self.late_directory if self.directory_reads > 1 else [])
        for m in entries:
            yield m


def _run(c, discord, tmp, world, requests):
    """Run the real connector on a stand-in client; -> {request name: response}."""
    root = Path(tmp)
    state = root / "state"
    in_dir, out_dir = state / "in", state / "out"
    for d in (in_dir, out_dir, root / "msgs", root / "locks"):
        d.mkdir(parents=True, exist_ok=True)
    guild = types.SimpleNamespace(
        id=1, name="guild", roles=[],
        text_channels=[world.bridge, world.directory_channel])
    responses = {}

    class Client:
        def __init__(self, *args, **kwargs):
            self.tasks, self.handlers, self.closed = [], {}, False
            self.user = types.SimpleNamespace(id=BOT_ID)
            self.guilds = [guild]
            self.loop = types.SimpleNamespace(create_task=self.tasks.append)

        def event(self, function):
            self.handlers[function.__name__] = function
            return function

        def is_closed(self):
            return self.closed

        def get_user(self, user_id):
            return world.users[user_id] if user_id in world.cached else None

        def get_channel(self, channel_id):
            return world.bridge if channel_id == BRIDGE_ID else None

        async def fetch_user(self, user_id):
            if world.fetch_error:
                raise world.fetch_error
            return world.users[user_id]

        def run(self, *args, **kwargs):
            asyncio.run(self.drive())

        async def drive(self):
            await self.handlers["on_ready"]()
            meta = None
            for coro in self.tasks:
                if coro.cr_code.co_name == "meta_watcher":
                    meta = asyncio.ensure_future(coro)
                else:
                    coro.close()
            for name, payload in requests.items():
                (in_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
            for _ in range(200):        # until every request is answered or withdrawn
                if not any(in_dir.iterdir()):
                    break
                await asyncio.sleep(0.05)
            self.closed = True
            await meta
            for out in out_dir.glob("*.json"):
                responses[out.stem] = json.loads(out.read_text(encoding="utf-8"))

    from discord_mb_lib import _directory
    saved = {n: getattr(c, n) for n in _SWAPPED}
    saved_client, saved_interval = discord.Client, _directory.MISS_REFRESH_INTERVAL
    saved_env = os.environ.get(LOCK_ROOT_ENV)
    try:
        # Every lookup miss here comes after the startup scan's interval has passed.
        _directory.MISS_REFRESH_INTERVAL = 0
        c.state_dir = lambda identity: state
        c.sweep_status_plugin = lambda identity: None
        c.default_status_plugin = lambda flavor: root / "no-such-plugin"
        c.read_extension_registry = lambda identity, flavor=None: {}
        c.msg_cache_dir = lambda: root / "msgs"
        c.meta_in_dir = lambda identity: in_dir
        c.meta_out_dir = lambda identity: out_dir
        discord.Client = Client
        os.environ[LOCK_ROOT_ENV] = str(root / "locks")
        with contextlib.redirect_stderr(io.StringIO()):    # its log is in connector.log
            c.connector_main("dm-test", claude_pid=os.getpid(), token="unused",
                             log_path=root / "connector.log", flavor="claude")
    finally:
        for n, v in saved.items():
            setattr(c, n, v)
        discord.Client, _directory.MISS_REFRESH_INTERVAL = saved_client, saved_interval
        if saved_env is None:
            os.environ.pop(LOCK_ROOT_ENV, None)
        else:
            os.environ[LOCK_ROOT_ENV] = saved_env
    return responses


def _ask(tmp, requests, mutate=None):
    """-> (responses, world), or (None, None) when discord.py is absent."""
    c, discord = _connector()
    if c is None:
        return None, None
    world = _World(discord, c)
    if mutate:
        mutate(world)
    return _run(c, discord, tmp, world, requests), world


def _conv(**fields):
    return {"op": "conversation", "limit": 20, **fields}


def test_a_registered_identity_reads_its_dm_without_sending(tmp):
    responses, world = _ask(tmp, {"r": _conv(dm_user="alice")})
    if responses is None:
        return
    resp = responses["r"]
    assert resp["ok"], resp
    assert [m["content"] for m in resp["messages"]] == ["plain hello", "newer needle"], resp
    assert {m["channel_id"] for m in resp["messages"]} == {str(ALICE_DM)}, resp
    assert world.sent == [] and world.created_dms == [], (world.sent, world.created_dms)
    assert world.bridge.calls == [], "the bridge channel was read instead of the DM"
    assert world.directory_reads == 1, "a registered identity needs no refresh"


def test_a_raw_user_id_opens_a_dm_that_is_not_open_yet(tmp):
    responses, world = _ask(tmp, {"r": _conv(dm_user=str(RAW_ID))})
    if responses is None:
        return
    resp = responses["r"]
    assert resp["ok"], resp
    assert [m["content"] for m in resp["messages"]] == ["reply", "first contact"], resp
    assert world.created_dms == [RAW_ID], world.created_dms
    assert world.sent == [], world.sent
    assert world.directory_reads == 1, "a raw id needs no directory lookup"


def test_a_later_registration_is_found_by_one_directory_refresh(tmp):
    responses, world = _ask(tmp, {"r": _conv(dm_user="carol")})
    if responses is None:
        return
    assert responses["r"]["ok"], responses["r"]
    assert world.directory_reads == 2, world.directory_reads


def test_an_unknown_name_is_an_error_that_creates_nothing(tmp):
    responses, world = _ask(tmp, {"r": _conv(dm_user="ghost")})
    if responses is None:
        return
    resp = responses["r"]
    assert not resp["ok"] and "'ghost'" in resp["error"], resp
    assert world.directory_reads == 2, "exactly one refresh before giving up"
    assert world.created_dms == [] and world.sent == [], (world.created_dms, world.sent)
    assert world.bridge.calls == [] and world.alice_dm.calls == []


def test_before_after_and_grep_reach_the_dm_history(tmp):
    responses, world = _ask(tmp, {
        "a": _conv(dm_user="alice", before_id="5000"),
        "b": _conv(dm_user="alice", after_id="6000"),
        "c": _conv(dm_user="alice", grep="needle"),
    })
    if responses is None:
        return
    calls = world.alice_dm.calls
    assert calls[0]["before"].id == 5000, calls
    assert "after" not in calls[0]
    assert calls[1]["after"].id == 6000 and calls[1]["oldest_first"] is True, calls
    assert [m["content"] for m in responses["c"]["messages"]] == ["newer needle"], responses["c"]


def test_a_dm_user_together_with_a_channel_is_refused(tmp):
    responses, world = _ask(tmp, {"r": _conv(dm_user="alice", channel_id=str(BRIDGE_ID))})
    if responses is None:
        return
    assert not responses["r"]["ok"], responses["r"]
    assert world.bridge.calls == [] and world.alice_dm.calls == []


def _unopenable(world):
    world.cached.clear()
    world.fetch_error = RuntimeError("boom")


def test_an_unopenable_dm_is_an_error_carrying_the_cause(tmp):
    responses, _ = _ask(tmp, {"r": _conv(dm_user="alice")}, _unopenable)
    if responses is None:
        return
    resp = responses["r"]
    assert not resp["ok"] and "boom" in resp["error"], resp


# The same resolution backs `send --dm` and the `move` DM destination; their
# own error types and wording are what callers and `send_failed` events carry.
def test_send_dm_keeps_its_own_errors_on_the_shared_resolution(tmp):
    send = {"op": "send", "dm": True, "subject": "s", "body": "b"}
    responses, world = _ask(tmp, {"unknown": {**send, "to": "ghost"},
                                  "unopenable": {**send, "to": "alice"}}, _unopenable)
    if responses is None:
        return
    assert responses["unknown"]["error"] == "SendError: unknown identity 'ghost' for --dm", responses
    assert responses["unopenable"]["error"] == \
        "SendRetry: DM channel for 'alice' unavailable: boom", responses
    assert world.directory_reads == 2, "one refresh, for the unknown name only"
    assert world.sent == [] and world.created_dms == []


def test_move_to_a_dm_keeps_its_own_error_on_the_shared_resolution(tmp):
    responses, world = _ask(tmp, {"r": {
        "op": "move", "source": str(BRIDGE_ID), "limit": 1,
        "dest": "ghost", "dest_kind": "dm"}})
    if responses is None:
        return
    assert responses["r"]["error"] == \
        "RuntimeError: unknown identity 'ghost' for a DM destination", responses
    assert world.directory_reads == 2 and world.created_dms == [] and world.sent == []


def test_the_cli_forwards_the_dm_user_in_the_request(tmp):
    del tmp
    c, _ = _connector()
    if c is None:
        return
    from discord_mb_lib import core
    sent = []
    original = core._meta_request

    def capture(identity, payload, timeout):
        sent.append(payload)
        return {"ok": True, "messages": []}

    core._meta_request = capture
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            core.conversation_cli("me", 5, channel_id="1", dm="alice", before_id="2", after_id="3", grep="x")
    finally:
        core._meta_request = original
    assert sent == [{"op": "conversation", "limit": 5, "channel_id": "1", "dm_user": "alice",
                     "before_id": "2", "after_id": "3", "grep": "x"}], sent


def test_the_parser_wires_dm_and_refuses_it_with_channel(tmp):
    del tmp
    c, _ = _connector()
    if c is None:
        return
    from discord_mb_lib import cli
    seen = {}
    original, argv = cli.conversation_cli, sys.argv

    def capture(identity, n, **kwargs):
        seen.update(identity=identity, n=n, **kwargs)

    cli.conversation_cli = capture
    try:
        sys.argv = ["discord_mb.py", "conversation", "me", "5", "--dm", "alice"]
        cli._cli()
    finally:
        cli.conversation_cli, sys.argv = original, argv
    assert seen.get("dm") == "alice" and seen.get("channel_id") is None, seen
    both = subprocess.run(
        [sys.executable, MB, "conversation", "me", "5", "--dm", "alice", "--channel", "1"],
        capture_output=True, text=True, timeout=60, check=False)
    assert both.returncode == 2, (both.returncode, both.stderr)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="convdm_")


if __name__ == "__main__":
    sys.exit(main())
