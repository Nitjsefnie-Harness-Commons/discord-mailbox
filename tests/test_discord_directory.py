#!/usr/bin/env python3
"""The agent directory: what an entry names, who holds a name, and how much of
the channel each read sees.

The pure half drives `discord_mb_lib._directory` directly. The connector half
runs the REAL connector closures on a stand-in gateway client (the
`tests/test_discord_delivery_gates.py` pattern): `on_ready`, `on_message` and
the `meta_watcher` answering send, list-agents and register requests.
"""
import asyncio
import contextlib
import datetime
import io
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _directory  # noqa: E402
from discord_mb_lib._directory import _Directory as D  # noqa: E402

M, L = D.MAP_SEPARATORS, D.LIST_SEPARATORS
BOT, PROBE, MALLORY = 980000000000000001, 980000000000000002, 980000000000000003
GUILD, BRIDGE, DIRECTORY = 990000000000000001, 990000000000000002, 990000000000000003
T0 = 1_000_000.0                      # a monotonic-looking origin, never 0
LOCK_ROOT_ENV = "DISCORD_MB_TEST_LOCK_ROOT"


def test_an_entry_names_its_first_mention_and_bold_token_or_separated_word(tmp):
    rows = [  # content, map result, list-agents result
        ("**alpha** · <@11>", ("alpha", 11), ("alpha", 11)),
        ("<@!11> then **alpha** and **beta**", ("alpha", 11), ("alpha", 11)),
        ("**alpha** <@11> <@22>", ("alpha", 11), ("alpha", 11)),
        ("**alpha**", None, None),
        (None, None, None),
        ("** ** <@11>", None, None),
        ("alpha: <@11>", ("alpha", 11), ("alpha", 11)),
        ("\n  - alpha = x\n<@11>", ("alpha", 11), ("alpha", 11)),
        ("no separator here\nalpha: <@11>", ("alpha", 11), ("alpha", 11)),
        (": empty\nalpha: <@11>", ("alpha", 11), ("alpha", 11)),
        # The one difference: only the map splits on the header's ' · '.
        ("alpha · <@11>", ("alpha", 11), None),
    ]
    for content, mapped, listed in rows:
        assert D.parse_entry(content, M) == mapped, (content, D.parse_entry(content, M))
        assert D.parse_entry(content, L) == listed, (content, D.parse_entry(content, L))


def test_the_oldest_self_authored_entry_holds_a_name(tmp):
    del tmp
    logs = []
    held = D(logs.append).bind_names([  # oldest first: (name, mentioned id, author id)
        ("probe", PROBE, PROBE), ("probe", MALLORY, MALLORY), ("probe", PROBE, PROBE),
        ("helper", PROBE, MALLORY), ("other", MALLORY, PROBE), ("mallory", MALLORY, MALLORY)])
    assert held == {"probe": PROBE, "mallory": MALLORY}, held
    assert len(logs) == 1 and all(s in logs[0] for s in ("'probe'", str(PROBE), str(MALLORY))), logs
    assert D(logs.append).bind_names([("probe", MALLORY, MALLORY), ("probe", PROBE, PROBE)]) == {
        "probe": MALLORY}, "the newer entry won"


# --- the real connector -----------------------------------------------------

def _msg(msg_id, author, content, channel=None, mentions=()):
    msg = types.SimpleNamespace(
        id=msg_id, content=content, pinned=False, guild=types.SimpleNamespace(id=GUILD),
        channel=channel, author=types.SimpleNamespace(id=author, name=f"user{author}"),
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        attachments=[], mentions=[types.SimpleNamespace(id=u) for u in mentions],
        role_mentions=[], embeds=[], components=[], poll=None, reactions=[],
        message_snapshots=[], add_reaction=lambda emoji: asyncio.sleep(0))

    async def edit(**kwargs):
        msg.channel.edits.append((msg_id, kwargs["content"]))
    msg.edit = edit
    return msg


def _run(tmp, entries, script):
    """Run the real connector on a stand-in client whose directory history is
    `entries` (newest first); `script(client)` drives it. -> (client, connector log)."""
    try:
        import discord
        from discord_mb_lib import connector as c
    except ImportError:
        return None, None
    root = Path(tmp)
    state, ids = root / "state", iter(range(500000, 600000))
    in_dir, out_dir = state / "in", state / "out"

    # The stand-in channels skip discord.py's __init__: it needs a live gateway state.
    class Channel:
        def __init__(self, channel_id, name, history=()):
            self.id, self.name, self.msgs = channel_id, name, list(history)
            self.reads, self.sent, self.edits, self.fail = [], [], [], False

        async def history(self, *, limit=100):    # discord.py's default; None is every message
            self.reads.append(limit)
            if self.fail:
                raise RuntimeError("gateway down")
            for m in self.msgs[:limit]:
                yield m

        async def send(self, content=None, **kwargs):
            self.sent.append(content)
            self.msgs.insert(0, _msg(next(ids), BOT, content, self))
            return self.msgs[0]

    class Text(Channel, discord.TextChannel):
        pass

    class DM(Channel, discord.DMChannel):
        pass

    guild = types.SimpleNamespace(id=GUILD, name="guild", roles=[], text_channels=[
        Text(BRIDGE, c.BRIDGE_CHANNEL_NAME), Text(DIRECTORY, c.DIRECTORY_CHANNEL_NAME, entries)])
    for m in entries:
        m.channel = guild.text_channels[1]

    class Client:
        def __init__(self):
            self.tasks, self.handlers, self.closed, self.clock = [], {}, False, [T0]
            self.user = types.SimpleNamespace(id=BOT)
            self.guilds = [guild]
            self.loop = types.SimpleNamespace(create_task=self.tasks.append)
            self.bridge, self.directory = guild.text_channels
            self.dms, self.responses = {}, {}

        def event(self, function):
            self.handlers[function.__name__] = function
            return function

        def is_closed(self):
            return self.closed

        def get_user(self, user_id):
            return types.SimpleNamespace(id=user_id, dm_channel=self.dms.setdefault(user_id, DM(user_id, "dm")))

        async def ask(self, requests):
            for name, payload in requests.items():
                (in_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
            for _ in range(200):
                if not any(in_dir.iterdir()):
                    break
                await asyncio.sleep(0.05)
            for name in requests:
                self.responses[name] = json.loads((out_dir / f"{name}.json").read_text(encoding="utf-8"))

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
            try:
                await script(self)
            finally:
                self.closed = True
                await meta

    swaps = {"state_dir": lambda identity: state, "inbox_dir": lambda identity: root / "inbox",
             "sweep_status_plugin": lambda identity: None,
             "default_status_plugin": lambda flavor: root / "no-such-plugin",
             "read_extension_registry": lambda identity, flavor=None: {},
             "msg_cache_dir": lambda: root / "msgs",
             "meta_in_dir": lambda identity: in_dir, "meta_out_dir": lambda identity: out_dir}
    for d in (in_dir, out_dir, root / "inbox", root / "msgs", root / "locks"):
        d.mkdir(parents=True, exist_ok=True)
    saved = {n: getattr(c, n) for n in swaps}
    saved_lock, saved_client, saved_clock = os.environ.get(LOCK_ROOT_ENV), discord.Client, _directory.monotonic
    clients = []
    try:
        for n, v in swaps.items():
            setattr(c, n, v)
        os.environ[LOCK_ROOT_ENV] = str(root / "locks")
        discord.Client = lambda *a, **k: clients.append(Client()) or clients[-1]
        _directory.monotonic = lambda: clients[-1].clock[0]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            c.connector_main("dir-test", claude_pid=os.getpid(), token="unused",
                             log_path=root / "connector.log", flavor="claude")
    finally:
        for n, v in saved.items():
            setattr(c, n, v)
        if saved_lock is None:
            os.environ.pop(LOCK_ROOT_ENV, None)
        else:
            os.environ[LOCK_ROOT_ENV] = saved_lock
        discord.Client, _directory.monotonic = saved_client, saved_clock
    return clients[0], (root / "connector.log").read_text(encoding="utf-8")


def _inbox(tmp, msg_id):
    return json.loads((Path(tmp) / "inbox" / f"{msg_id}.json").read_text(encoding="utf-8"))


def _send(to, **fields):
    return {"op": "send", "to": to, "subject": "s", "body": "b", **fields}


def test_a_newer_entry_from_another_account_does_not_take_a_name(tmp):
    entries = [_msg(4, MALLORY, f"**probe** · <@{MALLORY}>"),     # newest first
               _msg(3, MALLORY, f"**helper** · <@{PROBE}>"),
               _msg(2, PROBE, f"**probe** · <@{PROBE}>"),
               _msg(1, BOT, f"**dir-test** · <@{BOT}>"),
               _msg(0, MALLORY, f"**probe** · <@{MALLORY}>")]
    entries[-1].pinned = True                                     # skipped by every read

    async def script(client):
        on_message = client.handlers["on_message"]
        await on_message(_msg(101, MALLORY, f"<@{BOT}> hi", client.bridge, mentions=(BOT,)))
        await on_message(_msg(102, PROBE, f"<@{BOT}> hi", client.bridge, mentions=(BOT,)))
        await client.ask({"mention": _send("probe"), "dm": _send("probe", dm=True),
                          "helper": _send("helper"), "list": {"op": "list-agents"}})

    client, log = _run(tmp, entries, script)
    if client is None:
        return
    assert _inbox(tmp, 101)["from"] == f"user{MALLORY}", _inbox(tmp, 101)
    assert _inbox(tmp, 102)["from"] == "probe", _inbox(tmp, 102)
    r = client.responses
    assert r["mention"]["ok"] and client.bridge.sent[0].startswith(f"<@{PROBE}> "), (r, client.bridge.sent)
    assert r["dm"]["ok"] and list(client.dms) == [PROBE] and client.dms[PROBE].sent, (r, client.dms)
    assert not r["helper"]["ok"] and "'helper'" in r["helper"]["error"], r["helper"]
    assert [(a["identity"], a["user_id"], a["author_id"]) for a in r["list"]["agents"]] == [
        ("probe", str(MALLORY), str(MALLORY)), ("helper", str(PROBE), str(MALLORY)),
        ("probe", str(PROBE), str(PROBE)), ("dir-test", str(BOT), str(BOT))], r["list"]
    conflicts = [line for line in log.splitlines() if "held by" in line]
    assert len(conflicts) == 1 and str(PROBE) in conflicts[0] and str(MALLORY) in conflicts[0], log


def test_every_directory_read_sees_entries_older_than_its_newest_messages(tmp):
    entries = [_msg(1000 + i, MALLORY, f"chatter {i}") for i in range(300)] + [
        _msg(2, PROBE, f"**probe** · <@{PROBE}>"), _msg(1, BOT, f"**dir-test** · <@{BOT}>\n\nprofile")]

    async def script(client):
        await client.ask({"send": _send("probe"), "list": {"op": "list-agents"},
                          "register": {"op": "register", "body": "new profile"}})

    client, _ = _run(tmp, entries, script)
    if client is None:
        return
    r = client.responses
    assert set(client.directory.reads) == {None}, client.directory.reads
    assert r["send"]["ok"], r["send"]
    assert [a["identity"] for a in r["list"]["agents"]] == ["probe", "dir-test"], r["list"]
    assert r["register"] == {"op": "register", "ok": True, "action": "edited", "msg_id": "1"}, r
    assert client.directory.sent == [], "self-registration or register posted a second entry"
    assert client.directory.edits == [(1, f"**dir-test** · <@{BOT}>\n\nnew profile")]


def test_a_miss_rescans_only_once_the_last_scan_is_old_but_startup_always_scans(tmp):
    entries = [_msg(1, PROBE, f"**probe** · <@{PROBE}>")]       # no entry of its own yet

    async def script(client):
        reads, client.steps = client.directory.reads, []
        assert len(reads) == 2 and len(client.directory.sent) == 1, reads  # both forced at T0
        await client.handlers["on_ready"]()                               # a reconnect scans too
        assert len(reads) == 3 and len(client.directory.sent) == 1, reads
        client.directory.msgs.insert(0, _msg(9, MALLORY, f"**carol** · <@{MALLORY}>", client.directory))
        for t, name, payload, fail in (
                (14.9, "early", _send("carol"), False), (15, "due", _send("carol"), False),
                (15, "cached", _send("carol"), False), (29.9, "dm-early", _send("ghost", dm=True), False),
                (30, "dm-due", _send("ghost", dm=True), False), (100, "failed", _send("ghost"), True),
                (100, "probe-kept", _send("probe", dm=True), True),
                (100, "after-failure", _send("ghost"), False)):
            client.clock[0], client.directory.fail = T0 + t, fail
            await client.ask({name: payload})
            client.steps.append((name, client.responses[name]["ok"], len(reads)))

    client, _ = _run(tmp, entries, script)
    if client is None:
        return
    # A failed scan completed nothing, so the next miss scans again.
    assert client.steps == [("early", False, 3), ("due", True, 4), ("cached", True, 4),
                            ("dm-early", False, 4), ("dm-due", False, 5), ("failed", False, 6),
                            ("probe-kept", True, 6), ("after-failure", False, 7)], client.steps


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="directory_")


if __name__ == "__main__":
    raise SystemExit(main())
