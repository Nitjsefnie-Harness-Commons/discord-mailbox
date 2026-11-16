#!/usr/bin/env python3
"""Delivery gates: who may deliver a continuation frame.

The pure half drives `discord_mb_lib._delivery` directly with synthetic
messages. The connector half runs the REAL `on_message` and `write_inbox`
closures on a stand-in gateway client (the
`tests/test_discord_conversation_dm.py` pattern) and reads what reached the
inbox, the event stream and the receipt reactions.
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
from discord_mb_lib import _delivery as D  # noqa: E402

BOT, ALICE, RAW, MALLORY = (940000000000000001, 940000000000000002, 940000000000000003,
                            940000000000000004)
GUILD = 950000000000000001
CHAN, OTHER_CHAN = 960000000000000001, 960000000000000002
DIRECTORY = {"bridge-bot": BOT, "alice": ALICE}
CHUNK1 = f"<@{BOT}> **[probe→alpha]** report\nfirst part"
CHUNK2 = "**[probe→alpha]** report (2/3)\nsecond part"


def _msg(author, content="hi", guild=GUILD, channel=CHAN, msg_id=1):
    return types.SimpleNamespace(id=msg_id, content=content, author=types.SimpleNamespace(id=author),
                                 channel=types.SimpleNamespace(id=channel),
                                 guild=None if guild is None else types.SimpleNamespace(id=guild))


def _gates():
    logs = []
    return D._DeliveryGates(logs.append), logs


# --- continuation frames ----------------------------------------------------

def _train(steps):
    """Run (author, content, continuation[, channel]) steps -> delivered flags."""
    gates, logs = _gates()
    out = []
    for author, content, continuation, *channel in steps:
        msg = _msg(author, content, channel=channel[0] if channel else CHAN)
        out.append(not gates.refuses(msg, continuation, DIRECTORY))
    return out, logs


def test_a_continuation_frame_needs_a_registered_author_or_the_chunk_one_author(tmp):
    rows = [  # steps, delivered flags
        ([(RAW, CHUNK2, True)], [False]),
        ([(ALICE, CHUNK2, True)], [True]),
        ([(RAW, CHUNK1, False), (RAW, CHUNK2, True)], [True, True]),
        ([(RAW, CHUNK1, False), (MALLORY, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace(" report", " report (1/3)"), False), (RAW, CHUNK2, True)], [True, True]),
        # Every key component separates trains: channel, sender, recipients, subject.
        ([(RAW, CHUNK1, False, OTHER_CHAN), (RAW, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace("probe→", "bridge→"), False), (RAW, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace("→alpha", "→alpha,beta"), False), (RAW, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace(" report", " other"), False), (RAW, CHUNK2, True)], [True, False]),
        # A delivered chunk 2+ frame is not a chunk 1, so it vouches for nothing.
        ([(RAW, CHUNK2, False), (RAW, CHUNK2.replace("(2/3)", "(3/3)"), True)], [True, False]),
    ]
    for steps, expected in rows:
        got, logs = _train(steps)
        assert got == expected, (steps, got)
    _, logs = _train([(RAW, CHUNK2, True)])
    assert len(logs) == 1 and "continuation" in logs[0], logs


def test_the_remembered_chunk_one_authors_are_bounded_oldest_first(tmp):
    gates, _ = _gates()

    def chunk(i, k):
        frame = f"**[probe→alpha]** part{i}"
        return _msg(RAW, frame if k == 1 else f"{frame} (2/2)")

    for i in range(D.TRAIN_MEMORY + 1):
        assert not gates.refuses(chunk(i, 1), False, DIRECTORY)
        if i == D.TRAIN_MEMORY - 1:
            assert not gates.refuses(chunk(0, 1), False, DIRECTORY)   # 0 is now newest
    assert not gates.refuses(chunk(0, 2), True, DIRECTORY)
    assert gates.refuses(chunk(1, 2), True, DIRECTORY), "the oldest train was kept"
    assert not gates.refuses(chunk(2, 2), True, DIRECTORY)


# --- the real connector -----------------------------------------------------

def _connector():
    try:
        import discord
        from discord_mb_lib import connector
    except ImportError:
        return None, None
    return connector, discord


def _run(tmp, env, script):
    """Run the real connector on a stand-in client; `script(client, kinds)` sends
    messages through on_message. -> (inbox records, message events, reactions, log)."""
    c, discord = _connector()
    if c is None:
        return None
    root = Path(tmp)
    reactions, clients = [], []

    # The stand-in channels skip discord.py's __init__: it needs a live gateway state.
    class Text(discord.TextChannel):
        def __init__(self, channel_id, name, history=()):  # pylint: disable=super-init-not-called
            self.id, self.name, self._history = channel_id, name, list(history)

        async def history(self, **kwargs):
            for m in self._history:
                yield m

        async def send(self, *args, **kwargs):
            return None

    class DM(discord.DMChannel):
        def __init__(self, channel_id):  # pylint: disable=super-init-not-called
            self.id = channel_id

    def message(msg_id, author, content, channel, guild=None, mentions=()):
        async def add_reaction(emoji):
            reactions.append((msg_id, emoji))
        return types.SimpleNamespace(
            id=msg_id, content=content, pinned=False, guild=guild, channel=channel,
            author=types.SimpleNamespace(id=author, name=f"user{author}"),
            created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            attachments=[], mentions=[types.SimpleNamespace(id=u) for u in mentions],
            role_mentions=[], embeds=[], components=[], poll=None, reactions=[],
            message_snapshots=[], add_reaction=add_reaction)

    directory = [message(1, BOT, f"**bridge-bot** · <@{BOT}>", None),
                 message(2, ALICE, f"**alice** · <@{ALICE}>", None)]
    guild = types.SimpleNamespace(id=GUILD, name="guild", roles=[], text_channels=[
        Text(CHAN, c.BRIDGE_CHANNEL_NAME), Text(CHAN + 1, c.DIRECTORY_CHANNEL_NAME, directory)])
    ext = root / "relay.py"
    ext.write_text("async def setup(ctx):\n    ctx.client.ext_ctx = ctx\n", encoding="utf-8")

    class Client:
        def __init__(self, *args, **kwargs):
            clients.append(self)
            self.tasks, self.handlers, self.closed = [], {}, False
            self.user = types.SimpleNamespace(id=BOT)
            self.guilds = [guild]
            self.loop = types.SimpleNamespace(create_task=self.tasks.append)
            self.kinds = types.SimpleNamespace(text=guild.text_channels[0], dm=DM, message=message,
                                               guild=types.SimpleNamespace(id=GUILD))

        def event(self, function):
            self.handlers[function.__name__] = function
            return function

        def is_closed(self):
            return self.closed

        def run(self, *args, **kwargs):
            asyncio.run(self.drive())

        async def drive(self):
            await self.handlers["on_ready"]()
            for coro in self.tasks:
                coro.close()
            await script(self, self.kinds)

    swaps = {"state_dir": lambda identity: root / "state", "inbox_dir": lambda identity: root / "inbox",
             "sweep_status_plugin": lambda identity: None,
             "default_status_plugin": lambda flavor: root / "no-such-plugin",
             "read_extension_registry": lambda identity, flavor=None: {"path": str(ext)},
             "msg_cache_dir": lambda: root / "msgs"}
    for d in ("state", "inbox", "msgs", "locks"):
        (root / d).mkdir(exist_ok=True)
    saved = {n: getattr(c, n) for n in swaps}
    saved_env = {n: os.environ.get(n) for n in (*env, "DISCORD_MB_TEST_LOCK_ROOT")}
    saved_client, out = discord.Client, io.StringIO()
    try:
        for n, v in swaps.items():
            setattr(c, n, v)
        os.environ.update({"DISCORD_MB_TEST_LOCK_ROOT": str(root / "locks"), **env})
        discord.Client = Client
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            c.connector_main("gate-test", claude_pid=os.getpid(), token="unused",
                             log_path=root / "connector.log", flavor="claude")
    finally:
        for n, v in saved.items():
            setattr(c, n, v)
        for n, v in saved_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        discord.Client = saved_client
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
    return ({int(p.stem) for p in (root / "inbox").glob("*.json")},
            [int(e["msg_id"]) for e in events if e.get("event") == "message"],
            [m for m, _ in reactions], (root / "connector.log").read_text(encoding="utf-8")), clients[0]


def test_a_continuation_reaches_the_inbox_from_the_chunk_one_author_only(tmp):
    async def script(client, k):
        on_message = client.handlers["on_message"]
        chunk2 = CHUNK2.replace("alpha", "gate-test")
        await on_message(k.message(201, MALLORY, chunk2, k.text, k.guild))
        await on_message(k.message(202, RAW, CHUNK1.replace("alpha", "gate-test"), k.text, k.guild,
                                   mentions=(BOT,)))
        await on_message(k.message(203, RAW, chunk2, k.text, k.guild))
        await on_message(k.message(204, MALLORY, chunk2.replace("(2/3)", "(3/3)"), k.text, k.guild))
        await on_message(k.message(205, ALICE, chunk2.replace("report", "other"), k.text, k.guild))
        await client.ext_ctx.deliver(k.message(206, MALLORY, chunk2, k.text, k.guild))

    result = _run(tmp, {}, script)
    if result is None:
        return
    (inbox, events, reactions, log), _ = result
    assert inbox == {202, 203, 205, 206}, inbox
    assert events == [202, 203, 205, 206] and reactions == events, (events, reactions)
    assert log.count("not delivered") == 2 and "continuation" in log, log


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="delivery_")


if __name__ == "__main__":
    raise SystemExit(main())
