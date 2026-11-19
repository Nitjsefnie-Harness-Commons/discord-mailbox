"""A stand-in Discord gateway: `run` drives the REAL connector closures.

Not a suite itself -- run_tests.py only loads `test_*.py`.
"""
# pylint: disable=import-outside-toplevel
import asyncio
import contextlib
import datetime
import functools
import importlib
import io
import json
import os
import types
from pathlib import Path

import _util

BOT, ALICE = 940000000000000001, 940000000000000002
GUILD, BRIDGE, DIRECTORY, ATTACHMENTS, CREDENTIALS = (950000000000000001 + i for i in range(5))
T0 = 1_000_000.0                      # a monotonic-looking origin, never 0
LOCK_ROOT_ENV = "DISCORD_MB_TEST_LOCK_ROOT"
_OWN_GUILD = object()


def message(msg_id, author, content, channel=None, guild=_OWN_GUILD, mentions=(), reactions=None,
            username=None):
    """`guild` defaults to the channel's; `reactions` collects (msg_id, emoji)."""
    msg = types.SimpleNamespace(
        id=msg_id, content=content, pinned=False,
        guild=getattr(channel, "home", None) if guild is _OWN_GUILD else guild, channel=channel,
        author=types.SimpleNamespace(id=author, name=username or f"user{author}"),
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        attachments=[], mentions=[types.SimpleNamespace(id=u) for u in mentions],
        role_mentions=[], embeds=[], components=[], poll=None, reactions=[],
        message_snapshots=[])

    async def add_reaction(emoji):
        if reactions is not None:
            reactions.append((msg_id, emoji))

    async def edit(**kwargs):
        msg.channel.edits.append((msg_id, kwargs["content"]))

    async def delete():
        msg.deleted = True
    msg.add_reaction, msg.edit, msg.delete = add_reaction, edit, delete
    return msg


def have_discord():
    try:
        for name in ("discord", "discord_mb_lib.connector"):
            importlib.import_module(name)
    except ImportError:
        return False
    return True


def inbox(tmp, msg_id=None):
    """The inbox record of `msg_id`, or the set of delivered ids."""
    root = Path(tmp) / "inbox"
    if msg_id is None:
        return {int(p.stem) for p in root.glob("*.json")}
    return json.loads((root / f"{msg_id}.json").read_text(encoding="utf-8"))


def run(tmp, script, entries=None, env=None, identity="gate-test", extension=False, outbox=False, keep=(), others=()):
    """Let `script(client)` drive the connector; `entries` is the directory history, newest first;
    `outbox` also runs its outbox watcher (ticks once a second, files in `tmp/state/outbox`), `keep`
    names more watchers to run, `others` are guilds the bot is in before the main one.
    -> the client (`events`, `reactions`, `log`); skipped without discord.py.
    The settings-file cache is pinned empty, so only `env` (a blank counts as unset) feeds `setting()`."""
    if not have_discord():
        _util.skip("discord.py is not installed")
    import discord
    from discord_mb_lib import _directory, _settings, connector as c
    root, env = Path(tmp), env or {}
    state, ids = root / "state", iter(range(500000, 600000))
    in_dir, out_dir, out_box = state / "in", state / "out", state / "outbox"
    guild = types.SimpleNamespace(id=GUILD, name="guild", roles=[], text_channels=[])

    # The stand-in channels skip discord.py's __init__: it needs a live gateway state.
    class Channel:
        def __init__(self, channel_id, name, history=(), home=guild):
            self.id, self.name, self.msgs, self.home = channel_id, name, list(history), home
            self.reads, self.sent, self.edits, self.fail, self.pinned = [], [], [], False, []

        async def history(self, *, limit=100):    # discord.py's default; None is every message
            self.reads.append(limit)
            if self.fail:
                raise RuntimeError("gateway down")
            for m in self.msgs[:limit]:
                yield m

        async def pins(self, *, limit=50):
            for m in self.pinned[:limit]:
                yield m

        async def send(self, content=None, **kwargs):
            self.sent.append(content)
            self.msgs.insert(0, message(next(ids), BOT, content, self))
            return self.msgs[0]

    class Text(Channel, discord.TextChannel):
        pass

    class DM(Channel, discord.DMChannel):
        def __init__(self, channel_id, name="dm"):
            super().__init__(channel_id, name, home=None)

    history = entries if entries is not None else [
        message(1, BOT, f"**{identity}** · <@{BOT}>"), message(2, ALICE, f"**alice** · <@{ALICE}>")]
    names = (c.BRIDGE_CHANNEL_NAME, c.DIRECTORY_CHANNEL_NAME, c.ATTACHMENTS_CHANNEL_NAME, c.CREDS_CHANNEL_NAME)
    guild.text_channels = [Text(i, n, history if n == c.DIRECTORY_CHANNEL_NAME else (), guild)
                           for i, n in zip((BRIDGE, DIRECTORY, ATTACHMENTS, CREDENTIALS), names)]
    for m in history:
        m.channel = guild.text_channels[1]
    ext = root / "relay.py"
    ext.write_text("async def setup(ctx):\n    ctx.client.ext_ctx = ctx\n", encoding="utf-8")

    class Client:
        def __init__(self, *args, **kwargs):
            self.tasks, self.handlers, self.closed, self.clock = [], {}, False, [T0]
            self.user = types.SimpleNamespace(id=BOT)
            self.guilds = [*others, guild]
            self.loop = types.SimpleNamespace(create_task=self.tasks.append)
            self.bridge, self.directory, self.attachments, self.credentials = guild.text_channels
            self.guild, self.dm, self.dms, self.responses = guild, DM, {}, {}
            self.reactions, self.events, self.log = [], [], ""
            self.message = functools.partial(message, reactions=self.reactions)

        def event(self, function):    # discord.Client.event binds the attribute an extension wraps
            self.handlers[function.__name__] = function
            setattr(self, function.__name__, function)
            return function

        def is_closed(self):
            return self.closed

        def get_user(self, user_id):
            return types.SimpleNamespace(id=user_id, dm_channel=self.dms.setdefault(user_id, DM(user_id)))

        def get_guild(self, guild_id):
            return next((g for g in self.guilds if g.id == guild_id), None)

        def get_channel(self, channel_id):
            return next((ch for ch in (*guild.text_channels, *self.dms.values()) if ch.id == channel_id), None)

        async def ask(self, requests):
            """Answer meta requests through the connector's own watcher."""
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
            kept, watchers = ("meta_watcher", *keep) + (("outbox_watcher",) if outbox else ()), []
            for coro in self.tasks:
                if coro.cr_code.co_name in kept:
                    watchers.append(asyncio.ensure_future(coro))
                else:
                    coro.close()
            try:
                await script(self)
            finally:
                self.closed = True
                await asyncio.gather(*watchers)

    swaps = {"state_dir": lambda identity: state, "inbox_dir": lambda identity: root / "inbox",
             "sweep_status_plugin": lambda identity: None, "STATE_ROOT": root / "mailbox",
             "default_status_plugin": lambda flavor: root / "no-such-plugin",
             "read_extension_registry": lambda identity, flavor=None: {"path": str(ext)} if extension else {},
             "msg_cache_dir": lambda: root / "msgs", "pins_dir": lambda: root / "pins", "TOKEN_DIR": root / "tokens",
             "meta_in_dir": lambda identity: in_dir, "meta_out_dir": lambda identity: out_dir,
             "outbox_dir": lambda identity: out_box, "write_extension_registry": lambda *args: None}
    for d in (in_dir, out_dir, out_box, root / "inbox", root / "msgs", root / "locks"):
        d.mkdir(parents=True, exist_ok=True)
    saved = {n: getattr(c, n) for n in swaps}
    saved_env = {n: os.environ.get(n) for n in (*env, LOCK_ROOT_ENV)}
    saved_client, saved_clock, saved_cache = discord.Client, _directory.monotonic, _settings._cache
    out, clients = io.StringIO(), []
    try:
        _settings._cache = {}
        for n, v in swaps.items():
            setattr(c, n, v)
        os.environ.update({LOCK_ROOT_ENV: str(root / "locks"), **env})
        discord.Client = lambda *a, **k: clients.append(Client()) or clients[-1]
        _directory.monotonic = lambda: clients[-1].clock[0]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            c.connector_main(identity, claude_pid=os.getpid(), token="unused",
                             log_path=root / "connector.log", flavor="claude")
    finally:
        for n, v in saved.items():
            setattr(c, n, v)
        for n, v in saved_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        discord.Client, _directory.monotonic, _settings._cache = saved_client, saved_clock, saved_cache
    clients[0].events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
    clients[0].log = (root / "connector.log").read_text(encoding="utf-8")
    return clients[0]
