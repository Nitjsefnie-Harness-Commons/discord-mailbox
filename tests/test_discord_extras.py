#!/usr/bin/env python3
"""discord_mb.py message ingest: embeds, Components V2, polls, forwards.

`Message.content` is only one of the ways Discord carries text, and for
bot/webhook/app posts it is usually the EMPTY one. These pin the flattening
that makes those messages legible to an agent — the regression being a
forwarded status post that arrived as `body: ""` with `forwarded: []` and
`embed_count: 0`, i.e. with nothing to indicate any content had existed.

The Components-V2 fixture below is the verbatim payload from that incident.
Objects are built with discord.py's own factories, so this exercises the real
parsing path rather than a hand-made stand-in, and needs no network.

Stdlib only apart from discord.py itself, OS-agnostic (SETUP.md edit discipline).
"""
import json
import os
import sys
import asyncio
import inspect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")

# A forwarded status.claude.com post: a type-17 container of type-10 text
# displays. Note content == "" and embeds == [] — everything is in components.
FORWARDED_V2 = {
    "content": "", "embeds": [], "flags": 16384,
    "message_snapshots": [{"message": {
        "content": "", "embeds": [], "attachments": [], "flags": 32768,
        "components": [{
            "type": 17, "id": 1, "accent_color": 3066993, "spoiler": False,
            "components": [
                {"type": 10, "id": 2,
                 "content": "# 🟢  All Systems Operational\n\nFor more "
                            "information, visit\n[**status.claude.com**]"
                            "(https://status.claude.com)"},
                {"type": 14, "id": 3, "spacing": 2, "divider": True},
                {"type": 10, "id": 4,
                 "content": "## Components\n\n🟢 **claude.ai** — operational\n\n"
                            "🟢 **Claude Code** — operational"},
                {"type": 10, "id": 6,
                 "content": "-# Last updated · auto-updated from status.claude.com"},
            ]}]}}]}


_CACHE = {}


def _mod():
    """(discord_mb, discord) or (None, None) when discord.py is absent here.

    Loaded once and cached — not for speed, but because each load re-runs the
    module's stdout UTF-8 wrap. That is idempotent now (a marker on `sys`), and
    test_repeated_import_keeps_stdout_alive below is what keeps it that way."""
    if not _CACHE:
        try:
            import discord
            _CACHE['mb'] = _util.load(MB, "discord_mb_extras")
            _CACHE['discord'] = discord
        except ImportError:
            _CACHE['mb'] = _CACHE['discord'] = None
    return _CACHE['mb'], _CACHE['discord']


class _Snapshot:
    """Stands in for discord.MessageSnapshot, whose __init__ wants a live state.

    The COMPONENTS and EMBEDS are real discord.py objects either way — only the
    container is synthetic, so the code under test sees what it sees in
    production."""
    def __init__(self, data, discord, factory):
        self.content = data.get("content") or ""
        self.attachments = []
        self.embeds = [discord.Embed.from_dict(e) for e in data.get("embeds") or []]
        self.components = [factory(c) for c in data.get("components") or []]


class _Message(_Snapshot):
    def __init__(self, data, discord, factory):
        super().__init__(data, discord, factory)
        self.poll = None
        self.message_snapshots = [
            _Snapshot(s["message"], discord, factory)
            for s in data.get("message_snapshots") or []]


def _msg(data):
    mb, discord = _mod()
    if mb is None:
        return None, None
    from discord.components import _component_factory
    return mb, _Message(data, discord, _component_factory)


def test_components_v2_inside_a_forward(_tmp):
    """The incident: content empty, payload in a snapshot's V2 components."""
    mb, msg = _msg(FORWARDED_V2)
    if mb is None:
        return
    x = mb.message_extras(msg)
    for needle in ("All Systems Operational", "claude.ai", "Claude Code",
                   "status.claude.com", "auto-updated"):
        assert needle in x["rendered"], f"{needle!r} missing from rendered text"
    assert x["forwarded"], "the forward snapshot itself was dropped"
    assert x["forwarded"][0]["components"], \
        "snapshot components dropped — this is the original bug"


def test_body_rendered_marks_a_body_that_is_not_the_message(_tmp):
    """`body_rendered` present IS the signal that `body` alone is not enough."""
    mb, msg = _msg(FORWARDED_V2)
    if mb is None:
        return
    rec = mb.attach_extras({"body": msg.content}, msg)
    assert "body_rendered" in rec
    assert "All Systems Operational" in rec["body_rendered"]


def test_plain_message_record_is_unchanged(_tmp):
    """An ordinary text message must keep EXACTLY its old record shape.

    The flattening is additive; if it started decorating every record, every
    consumer of the mailbox would have to learn new keys for nothing."""
    mb, msg = _msg({"content": "just a normal message"})
    if mb is None:
        return
    rec = mb.attach_extras({"body": msg.content}, msg)
    assert sorted(rec) == ["body"], rec


def test_classic_embed_is_captured_not_counted(_tmp):
    """Before this, an embed contributed `embed_count` and nothing else."""
    mb, msg = _msg({"content": "", "embeds": [{
        "title": "Build failed", "description": "3 tests broke",
        "url": "https://ci/1", "author": {"name": "CI"},
        "footer": {"text": "run 412"},
        "fields": [{"name": "branch", "value": "main", "inline": True}]}]})
    if mb is None:
        return
    rec = mb.attach_extras({"body": ""}, msg)
    body = rec.get("body_rendered", "")
    for needle in ("Build failed", "3 tests broke", "branch: main", "CI",
                   "run 412", "https://ci/1"):
        assert needle in body, f"{needle!r} missing from {body!r}"
    assert rec["embeds"][0]["title"] == "Build failed", "structure kept too"


def test_unknown_component_type_degrades_visibly(_tmp):
    """A component Discord ships and discord.py has not modelled yet must
    leave a trace, not vanish — silence would look like an empty message."""
    mb, _ = _msg({"content": ""})
    if mb is None:
        return

    class Unknown:
        class type:
            value = 99
    out = mb.flatten_components([Unknown()])
    assert out and "99" in out[0], out


def test_flattener_survives_a_broken_component(_tmp):
    """One malformed component costs its own line, never the whole ingest."""
    mb, _ = _msg({"content": ""})
    if mb is None:
        return

    class Exploding:
        @property
        def type(self):
            raise RuntimeError("boom")
    out = mb.flatten_components([Exploding()])
    assert out == ["[component: unreadable]"], out


def test_poll_is_captured(_tmp):
    mb, msg = _msg({"content": ""})
    if mb is None:
        return

    class Media:
        def __init__(self, text):
            self.text = text

    class Answer:
        def __init__(self, text, votes):
            self.media, self.vote_count = Media(text), votes

    class Poll:
        question = Media("ship it?")
        answers = [Answer("yes", 3), Answer("no", 1)]
        multiple = False

    msg.poll = Poll()
    x = mb.message_extras(msg)
    assert x["poll"]["question"] == "ship it?"
    assert x["poll"]["answers"][0] == {"text": "yes", "votes": 3}
    assert "ship it?" in x["rendered"] and "yes" in x["rendered"]


def test_repeated_import_keeps_stdout_alive(_tmp):
    """Loading discord_mb twice in one process must not kill stdout.

    It wraps sys.stdout in a UTF-8 TextIOWrapper at import (Windows consoles).
    Unguarded, a second import wrapped the wrapper and the discarded one closed
    the shared buffer on collection, so every later print in the PROCESS raised
    "I/O operation on closed file". Found by this suite loading the module once
    per test."""
    mb, _ = _mod()
    if mb is None:
        return
    import gc
    for i in range(3):
        _util.load(MB, f"discord_mb_reimport_{i}")
        gc.collect()
        print("", end="")                    # raises if stdout was closed
    assert not sys.stdout.closed


def test_reconnect_replays_the_complete_last_presence(_tmp):
    """A gateway reconnect must restore the plugin's unchanged presence.

    Removing the reconnect replay, or dropping its URL/status metadata, makes
    this fail even though the plugin task itself remains alive.
    """
    mb, discord = _mod()
    if mb is None:
        return

    class Client:
        def __init__(self):
            self.calls = []

        async def change_presence(self, **kwargs):
            self.calls.append(kwargs)

    client = Client()
    state = {"status_last": {
        "text": "reviewing issue 162",
        "kind": "streaming",
        "url": "https://example.invalid/live",
        "status": "idle",
        "set_at": "2026-08-10T12:00:00",
    }}
    assert asyncio.run(mb.replay_last_presence(client, state)) is True
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["activity"].name == "reviewing issue 162"
    assert call["activity"].url == "https://example.invalid/live"
    assert call["status"] is discord.Status.idle

    client.calls.clear()
    assert asyncio.run(mb.replay_last_presence(
        client, {"status_last": None})) is False
    assert client.calls == []

    connector_source = inspect.getsource(inspect.unwrap(mb._run_connector))
    assert "await replay_last_presence(client, state)" in connector_source, (
        "connector on_ready is not wired to replay the saved presence")
    assert "state['status_last'] = status_presence_record(" in connector_source, (
        "status plugin does not persist all replayable presence fields")


def test_status_plugin_transport_failure_is_retried_after_reconnect(_tmp):
    """A gateway write failure is transport failure, not a broken plugin.

    The incident behind #156 was aiohttp's ClientConnectionResetError.  Keep
    that plugin installed and restart it from on_ready; ordinary plugin bugs
    must retain the existing auto-uninstall behavior.
    """
    mb, _ = _mod()
    if mb is None:
        return

    ClientConnectionResetError = type(
        "ClientConnectionResetError", (Exception,),
        {"__module__": "aiohttp.client_exceptions"})
    async def gateway_failure(exc):
        async def call():
            raise exc
        try:
            await mb.status_plugin_gateway_call(call())
        except mb.StatusPluginGatewayTransportError as wrapped:
            return wrapped
        raise AssertionError("gateway transport error was not wrapped")

    wrapped = asyncio.run(gateway_failure(
        ClientConnectionResetError("closing transport")))
    assert isinstance(wrapped.__cause__, ClientConnectionResetError)

    async def task_outcome(run_fn):
        events = []

        async def finished():
            events.append("finished")

        async def transport_failed(_exc):
            events.append("transport")

        async def plugin_failed(_exc):
            events.append("plugin")

        outcome = await mb.run_status_plugin_task(
            run_fn, object(), finished=finished,
            transport_failed=transport_failed, plugin_failed=plugin_failed)
        return outcome, events

    async def plugin_owned_connection_error(_ctx):
        raise ConnectionResetError("plugin-owned socket")

    async def discord_gateway_connection_error(_ctx):
        async def call():
            raise ClientConnectionResetError("Discord transport")
        await mb.status_plugin_gateway_call(call())

    assert asyncio.run(task_outcome(plugin_owned_connection_error)) == (
        "crashed", ["plugin"])
    assert asyncio.run(task_outcome(discord_gateway_connection_error)) == (
        "retrying", ["transport"])

    async def exercise_recovery(restart_result, initial_state="retrying",
                                clear_results=(True,)):
        events = []
        state = {"status_state": initial_state, "status_last": {"text": "old"}}
        clear_results = iter(clear_results)

        async def restart():
            events.append("restart")
            if restart_result is False:
                state["status_state"] = "empty"
            return restart_result

        async def replay():
            events.append("replay")
            return True

        async def clear():
            events.append("clear")
            return next(clear_results)

        outcome = await mb.recover_status_plugin_after_gateway(
            state, restart=restart, replay=replay, clear=clear)
        return outcome, events, state

    outcome, events, _ = asyncio.run(exercise_recovery(True))
    assert (outcome, events) == ("restarted", ["restart"])
    outcome, events, _ = asyncio.run(exercise_recovery(None))
    assert (outcome, events) == ("pending", ["restart"])
    outcome, events, state = asyncio.run(exercise_recovery(False))
    assert (outcome, events) == ("cleared", ["restart", "clear"])
    assert state["status_last"] is None
    outcome, events, state = asyncio.run(exercise_recovery(
        False, clear_results=(False,)))
    assert (outcome, events) == ("clear-pending", ["restart", "clear"])
    assert state["status_state"] == "clearing"
    assert state["status_last"] == {"text": "old"}

    async def finish_pending_clear():
        events = []

        async def forbidden_restart():
            events.append("restart")
            raise AssertionError("clearing state must not restart again")

        async def forbidden_replay():
            events.append("replay")
            raise AssertionError("stale presence must not be replayed")

        async def clear():
            events.append("clear")
            return True

        outcome = await mb.recover_status_plugin_after_gateway(
            state, restart=forbidden_restart, replay=forbidden_replay,
            clear=clear)
        return outcome, events

    assert asyncio.run(finish_pending_clear()) == ("cleared", ["clear"])
    assert state["status_state"] == "empty"
    assert state["status_last"] is None

    async def failed_clear_restores_crashed_terminal():
        crashed = {"status_state": "retrying", "status_last": {"text": "old"}}

        async def restart():
            crashed["status_state"] = "crashed"
            return False

        async def replay():
            raise AssertionError("retry recovery must not replay stale presence")

        clear_results = iter((False, True))

        async def clear():
            return next(clear_results)

        first = await mb.recover_status_plugin_after_gateway(
            crashed, restart=restart, replay=replay, clear=clear)
        assert first == "clear-pending"
        assert crashed["status_state"] == "clearing"
        assert crashed["status_last"] == {"text": "old"}
        second = await mb.recover_status_plugin_after_gateway(
            crashed, restart=restart, replay=replay, clear=clear)
        return second, crashed

    outcome, crashed = asyncio.run(failed_clear_restores_crashed_terminal())
    assert outcome == "cleared"
    assert crashed["status_state"] == "crashed"
    assert crashed["status_last"] is None
    outcome, events, _ = asyncio.run(exercise_recovery(False, "running"))
    assert (outcome, events) == ("replayed", ["replay"])

    connector_source = inspect.getsource(inspect.unwrap(mb._run_connector))
    assert "await run_status_plugin_task(" in connector_source
    assert "state['status_state'] = 'retrying'" in connector_source
    assert "async def on_resumed():" in connector_source
    assert connector_source.count("await _recover_status_after_gateway()") == 2
    assert "status plugin restarted after gateway reconnect" in connector_source


def test_fixture_matches_the_shipped_parser(_tmp):
    """The fixture must stay loadable by whatever discord.py is installed.

    If a future discord.py stops parsing a type-17 container, this fails here
    rather than silently in production."""
    mb, msg = _msg(FORWARDED_V2)
    if mb is None:
        return
    snap = msg.message_snapshots[0]
    assert snap.components, "discord.py no longer parses the V2 container"
    assert getattr(snap.components[0].type, "value", None) == 17
    assert json.loads(json.dumps(mb.message_extras(msg))), "must stay JSON-safe"


def test_forum_create_rejects_overlong_body_before_api(_tmp):
    """`forum create` must fail client-side on >2000 chars (issue #28): the
    error names the limit and the actual length, and _meta_request is never
    reached — the old behaviour lost the whole call to a raw API 400."""
    import contextlib
    import io
    import types
    mb, _ = _mod()
    if mb is None:
        return
    args = types.SimpleNamespace(
        identity="test", forum_action="create", channel="c", name="n",
        content="x" * 2001, tags=None, timeout=60.0)

    def _boom(*a, **k):
        raise AssertionError("API contacted despite the client-side check")

    orig = mb._meta_request
    mb._meta_request = _boom
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            try:
                mb.forum_cli(args)
            except SystemExit as e:
                assert e.code == 2, f"exit {e.code}, want 2"
            else:
                raise AssertionError("over-long body was accepted")
    finally:
        mb._meta_request = orig
    assert "2000" in err.getvalue() and "2001" in err.getvalue(), \
        f"error must name the limit and the actual length: {err.getvalue()!r}"


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="discordextras_")


if __name__ == "__main__":
    sys.exit(main())
