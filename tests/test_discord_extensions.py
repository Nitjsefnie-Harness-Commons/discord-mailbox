#!/usr/bin/env python3
"""Suite for discord_mb.py per-identity extensions + the daily heartbeat.

An extension is NOT a status plugin: its registration is durable (it lives
beside the identity's token, not under the temp state root) so a binding
survives a connector restart and a reboot.
"""
# pylint: disable=attribute-defined-outside-init, no-member
# _FakeClient stands in for discord.py's client, whose event handlers
# are attached by decorator at registration time rather than declared.
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = _util.script("discord_mb.py")


def _mod(tmp):
    """discord_mb loaded fresh with its shared Discord dir in tmp."""
    m = _util.load(MB, "discord_mb_under_test")
    shared = Path(tmp) / "shared" / "discord"
    m.TOKEN_DIR = shared
    m.KIMI_TOKEN_DIR = shared
    m.CODEX_TOKEN_DIR = shared
    return m


def _write(tmp, name, body):
    p = Path(tmp) / name
    p.write_text(body)
    return str(p)


# ---------------------------------------------------------------- registry

def test_registry_round_trips(tmp):
    m = _mod(tmp)
    assert m.read_extension_registry("bob", "claude") == {}
    m.write_extension_registry("bob", {"path": "/x/y.py"}, "claude")
    assert m.read_extension_registry("bob", "claude")["path"] == "/x/y.py"


def test_registry_is_shared_across_flavors_and_per_identity(tmp):
    m = _mod(tmp)
    m.write_extension_registry("bob", {"path": "/claude.py"}, "claude")
    assert m.read_extension_registry("bob", "claude")["path"] == "/claude.py"
    assert m.read_extension_registry("bob", "kimi")["path"] == "/claude.py"
    assert m.read_extension_registry("bob", "codex")["path"] == "/claude.py"
    m.write_extension_registry("bob", {"path": "/kimi.py"}, "kimi")
    assert m.read_extension_registry("bob", "claude")["path"] == "/kimi.py"
    assert m.read_extension_registry("alice", "claude") == {}


def test_registry_is_not_under_state_root(tmp):
    """The whole point of the durable path: STATE_ROOT is a temp dir."""
    m = _mod(tmp)
    p = m.extension_registry_path("bob", "claude")
    assert str(m.STATE_ROOT) not in str(p)
    assert str(m.TOKEN_DIR) in str(p)


def test_corrupt_registry_reads_as_empty(tmp):
    m = _mod(tmp)
    p = m.extension_registry_path("bob", "claude")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert m.read_extension_registry("bob", "claude") == {}


def test_non_dict_registry_reads_as_empty(tmp):
    m = _mod(tmp)
    p = m.extension_registry_path("bob", "claude")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2, 3]")
    assert m.read_extension_registry("bob", "claude") == {}


def test_write_leaves_no_temp_file_behind(tmp):
    m = _mod(tmp)
    m.write_extension_registry("bob", {"path": "/x.py"}, "claude")
    leftovers = list(m.extension_registry_path("bob", "claude").parent.glob("*.tmp"))
    assert leftovers == []


def test_unknown_flavor_falls_back_to_claude_dir(tmp):
    m = _mod(tmp)
    assert str(m.TOKEN_DIR) in str(m.extension_registry_path("bob", "martian"))


# ------------------------------------------------------------------ loader

def test_loader_accepts_async_setup(tmp):
    src = _write(tmp, "ok_ext.py",
                 "async def setup(ctx):\n    return 1\n"
                 "async def command(ctx, argv):\n    return {'ok': True}\n")
    m = _mod(tmp)
    module, setup, command = m.load_extension(src)
    assert module is not None and setup is not None and command is not None


def test_loader_allows_a_missing_command(tmp):
    src = _write(tmp, "setup_only.py", "async def setup(ctx):\n    return 1\n")
    m = _mod(tmp)
    _module, setup, command = m.load_extension(src)
    assert setup is not None and command is None


def test_loader_rejects_missing_setup(tmp):
    src = _write(tmp, "no_setup.py", "x = 1\n")
    m = _mod(tmp)
    try:
        m.load_extension(src)
    except ValueError as e:
        assert "setup" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_loader_rejects_sync_setup(tmp):
    src = _write(tmp, "sync_setup.py", "def setup(ctx):\n    pass\n")
    m = _mod(tmp)
    try:
        m.load_extension(src)
    except ValueError as e:
        assert "async def" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_loader_rejects_sync_command(tmp):
    src = _write(tmp, "sync_cmd.py",
                 "async def setup(ctx):\n    pass\n"
                 "def command(ctx, argv):\n    pass\n")
    m = _mod(tmp)
    try:
        m.load_extension(src)
    except ValueError as e:
        assert "async def" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_loader_reports_missing_file(tmp):
    m = _mod(tmp)
    try:
        m.load_extension(str(Path(tmp) / "nope.py"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def _assert_no_leak(before):
    assert not [k for k in set(sys.modules) - before if "_mb_extension_" in k]


def test_unparseable_module_does_not_leak_into_sys_modules(tmp):
    src = _write(tmp, "broken_syntax.py", "def (:\n")
    m = _mod(tmp)
    before = set(sys.modules)
    try:
        m.load_extension(src)
    except SyntaxError:
        pass
    else:
        raise AssertionError("expected SyntaxError")
    _assert_no_leak(before)


def test_module_raising_at_import_does_not_leak_into_sys_modules(tmp):
    """Parses fine, explodes on exec -- the more common real failure."""
    src = _write(tmp, "broken_runtime.py", "raise RuntimeError('boom')\n")
    m = _mod(tmp)
    before = set(sys.modules)
    try:
        m.load_extension(src)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")
    _assert_no_leak(before)


# ----------------------------------------------------------------- context

def _ctx(m, tmp, emit=None, log=None):
    return m._ExtensionContext(client=None, identity="bob", flavor="claude",
                               log=log or (lambda s: None),
                               emit=emit or (lambda o: None))


def test_context_store_persists_through_registry(tmp):
    m = _mod(tmp)
    ctx = _ctx(m, tmp)
    ctx.store["bound"] = {"message_id": "42"}
    ctx.save()
    reg = m.read_extension_registry("bob", "claude")
    assert reg["store"]["bound"]["message_id"] == "42"


def test_context_store_reloads_on_a_fresh_context(tmp):
    """This is the restart property: a binding must survive a new connector."""
    m = _mod(tmp)
    ctx = _ctx(m, tmp)
    ctx.store["bound"] = {"message_id": "42"}
    ctx.save()
    assert _ctx(m, tmp).store["bound"]["message_id"] == "42"


def test_context_store_does_not_clobber_registration(tmp):
    m = _mod(tmp)
    m.write_extension_registry("bob", {"path": "/x/y.py"}, "claude")
    ctx = _ctx(m, tmp)
    ctx.store["k"] = "v"
    ctx.save()
    reg = m.read_extension_registry("bob", "claude")
    assert reg["path"] == "/x/y.py" and reg["store"]["k"] == "v"


def test_context_store_survives_a_corrupt_store_value(tmp):
    m = _mod(tmp)
    m.write_extension_registry("bob", {"store": "not-a-dict"}, "claude")
    assert _ctx(m, tmp).store == {}


def test_context_emit_is_forwarded_verbatim(tmp):
    m = _mod(tmp)
    seen = []
    _ctx(m, tmp, emit=seen.append).emit({"event": "x"})
    assert seen == [{"event": "x"}]


def test_context_log_is_prefixed(tmp):
    m = _mod(tmp)
    seen = []
    _ctx(m, tmp, log=seen.append).log("hello")
    assert seen and seen[0].startswith("[ext] ")


# --------------------------------------------------------------- heartbeat

def test_heartbeat_due_only_on_a_new_utc_day(tmp):
    m = _mod(tmp)
    assert m.heartbeat_due(None, "2026-08-12") is True
    assert m.heartbeat_due("2026-08-11", "2026-08-12") is True
    assert m.heartbeat_due("2026-08-12", "2026-08-12") is False


def test_heartbeat_does_not_backfill_missed_days(tmp):
    """Down for a week: one heartbeat on return, not seven."""
    m = _mod(tmp)
    assert m.heartbeat_due("2026-08-05", "2026-08-12") is True


def test_heartbeat_date_survives_restart(tmp):
    m = _mod(tmp)
    reg = m.read_extension_registry("bob", "claude")
    reg["last_heartbeat"] = "2026-08-12"
    m.write_extension_registry("bob", reg, "claude")
    last = m.read_extension_registry("bob", "claude").get("last_heartbeat")
    assert last == "2026-08-12"
    assert m.heartbeat_due(last, "2026-08-12") is False


# ------------------------------------------------ installed-package drift
# A connector imports the package once and then runs for days. When the wheel
# is replaced underneath it, nothing in the process changes -- so the running
# code and the installed code have to be compared explicitly.

def _fake_pkg(tmp, name, version, extra=""):
    """A package directory shaped like discord_mb_lib, at a chosen version."""
    root = Path(tmp) / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "core.py").write_text(f'__version__ = "{version}"\n{extra}')
    (root / "storage.py").write_text("x = 1\n")
    return root


def test_package_fingerprint_reads_the_version_off_disk(tmp):
    """Off disk, not out of memory: the point is what the NEXT import gets."""
    m = _mod(tmp)
    fp = m.package_fingerprint(_fake_pkg(tmp, "pkg", "1.2.3"))
    assert fp["version"] == "1.2.3", fp
    assert isinstance(fp["digest"], str) and len(fp["digest"]) == 64, fp


def test_package_fingerprint_depends_on_content_not_location(tmp):
    m = _mod(tmp)
    assert (m.package_fingerprint(_fake_pkg(tmp, "one", "1.2.3"))
            == m.package_fingerprint(_fake_pkg(tmp, "two", "1.2.3")))


def test_package_fingerprint_moves_when_a_module_body_changes(tmp):
    """A same-version reinstall is invisible to the version string alone."""
    m = _mod(tmp)
    before = m.package_fingerprint(_fake_pkg(tmp, "a", "1.2.3"))
    after = m.package_fingerprint(_fake_pkg(tmp, "b", "1.2.3", extra="y = 2\n"))
    assert before["version"] == after["version"] == "1.2.3", (before, after)
    assert before["digest"] != after["digest"], before


def test_package_fingerprint_ignores_files_that_are_not_modules(tmp):
    """Bytecode and scratch files churn on their own; they are not the code."""
    m = _mod(tmp)
    root = _fake_pkg(tmp, "pkg", "1.2.3")
    before = m.package_fingerprint(root)
    (root / "notes.txt").write_text("scratch")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "core.cpython-313.pyc").write_bytes(b"\x00\x01")
    assert m.package_fingerprint(root) == before


def test_package_fingerprint_of_an_unreadable_root_is_unknown(tmp):
    m = _mod(tmp)
    assert m.package_fingerprint(Path(tmp) / "not-installed") == {
        "version": None, "digest": None}


def test_running_fingerprint_reports_the_version_this_process_imported(tmp):
    m = _mod(tmp)
    assert m.running_fingerprint()["version"] == m.__version__


def test_no_event_while_the_installed_package_matches(tmp):
    m = _mod(tmp)
    fp = {"version": "1.0.0", "digest": "a"}
    assert m.package_change_event("bob", fp, dict(fp)) is None


def test_event_when_the_installed_version_moves_under_the_connector(tmp):
    m = _mod(tmp)
    ev = m.package_change_event("bob", {"version": "0.35.0", "digest": "a"},
                                {"version": "0.36.0", "digest": "b"})
    assert ev == {"event": "version_changed", "identity": "bob",
                  "running": "0.35.0", "installed": "0.36.0",
                  "restart_required": True}, ev


def test_a_same_version_reinstall_says_so(tmp):
    """Two identical version strings cannot carry it, so the payload must."""
    m = _mod(tmp)
    ev = m.package_change_event("bob", {"version": "1.0.0", "digest": "a"},
                                {"version": "1.0.0", "digest": "b"})
    assert ev["reinstalled"] is True, ev
    assert ev["running"] == ev["installed"] == "1.0.0", ev
    assert ev["restart_required"] is True, ev


def test_one_event_per_install_not_one_per_check(tmp):
    """The check runs every few minutes; the install happened once."""
    m = _mod(tmp)
    running = {"version": "0.35.0", "digest": "a"}
    installed = {"version": "0.36.0", "digest": "b"}
    assert m.package_change_event("bob", running, installed) is not None
    assert m.package_change_event("bob", running, installed, installed) is None


def test_a_further_install_is_reported_again(tmp):
    m = _mod(tmp)
    ev = m.package_change_event("bob", {"version": "0.35.0", "digest": "a"},
                                {"version": "0.37.0", "digest": "c"},
                                {"version": "0.36.0", "digest": "b"})
    assert ev is not None and ev["installed"] == "0.37.0", ev


def test_an_unreadable_install_reports_nothing(tmp):
    """Mid-install the directory is half there; silence beats a false alarm."""
    m = _mod(tmp)
    assert m.package_change_event("bob", {"version": "1.0.0", "digest": "a"},
                                  {"version": None, "digest": None}) is None


def test_a_missing_running_digest_still_catches_a_version_move(tmp):
    m = _mod(tmp)
    ev = m.package_change_event("bob", {"version": "0.35.0", "digest": None},
                                {"version": "0.36.0", "digest": "b"})
    assert ev is not None and ev["installed"] == "0.36.0", ev


def test_a_missing_running_digest_does_not_invent_a_reinstall(tmp):
    """Unknown is not evidence of change."""
    m = _mod(tmp)
    assert m.package_change_event("bob", {"version": "1.0.0", "digest": None},
                                  {"version": "1.0.0", "digest": "b"}) is None


def _connector_source():
    return (Path(_util.SCRIPTS) / "discord_mb_lib" / "connector.py").read_text(
        encoding="utf-8")


def _heartbeat_watcher_source():
    """The body of the periodic task, which is where the check belongs."""
    parts = _connector_source().split("async def heartbeat_watcher", 1)
    assert len(parts) == 2, "connector no longer runs a heartbeat watcher"
    return parts[1].split("\n    async def ", 1)[0]


def test_the_connector_captures_what_it_is_running_at_startup(_tmp):
    """Captured once, at import time -- later reads describe the install."""
    assert "running_fingerprint()" in _connector_source()


def test_the_heartbeat_carries_the_running_version(_tmp):
    body = _heartbeat_watcher_source()
    assert "'event': 'heartbeat'" in body, "heartbeat no longer emitted here"
    assert "running_version" in body, (
        "the heartbeat says nothing about which code emitted it")


def test_the_connector_checks_the_installed_package_on_the_heartbeat_loop(_tmp):
    body = _heartbeat_watcher_source()
    assert "package_fingerprint()" in body, (
        "nothing re-reads the installed package, so a new install is invisible")
    assert "package_change_event" in body, (
        "the installed package is read but never compared to the running one")


def test_the_connector_does_not_restart_itself_when_the_install_moves(_tmp):
    """Replacing a live gateway connection is the owning session's decision."""
    body = _heartbeat_watcher_source()
    for forbidden in ("os.execv", "os.execl", "sys.exit", "client.close",
                      "os._exit"):
        assert forbidden not in body, f"{forbidden} in the version check"


def test_extension_list_reports_the_running_and_installed_versions(_tmp):
    """The on-demand half: one call answers "have I drifted?"."""
    parts = _connector_source().split("async def op_extension_list", 1)
    assert len(parts) == 2, "connector no longer serves extension-list"
    body = parts[1].split("\n    async def ", 1)[0]
    assert "running_version" in body, "extension list names no running version"
    assert "installed_version" in body, (
        "extension list cannot show a divergence without the installed version")


# ------------------------------------------------------ ctx.on subscription
# discord.Client has no add_listener (that is commands.Bot). These pin the
# attribute-dispatch shape that a plain Client actually uses.

class _FakeClient:
    """A plain object, exactly like discord.Client: no add_listener at all."""


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_on_subscribes_without_add_listener(tmp):
    m = _mod(tmp)
    ctx = _ctx(m, tmp)
    ctx.client = _FakeClient()
    seen = []

    async def handler(payload):
        seen.append(payload)

    ctx.on("raw_reaction_add", handler)
    assert hasattr(ctx.client, "on_raw_reaction_add")
    _run(ctx.client.on_raw_reaction_add("evt"))
    assert seen == ["evt"]


def test_two_handlers_on_one_event_both_fire(tmp):
    m = _mod(tmp)
    ctx = _ctx(m, tmp)
    ctx.client = _FakeClient()
    seen = []

    async def a(p):
        seen.append("a")

    async def b(p):
        seen.append("b")

    ctx.on("raw_reaction_add", a)
    ctx.on("raw_reaction_add", b)
    _run(ctx.client.on_raw_reaction_add("evt"))
    assert seen == ["a", "b"]


def test_existing_connector_handler_is_preserved(tmp):
    """Installing over on_message must not unhook the mailbox."""
    m = _mod(tmp)
    ctx = _ctx(m, tmp)
    ctx.client = _FakeClient()
    seen = []

    async def connector_handler(msg):
        seen.append("connector")

    ctx.client.on_message = connector_handler

    async def ext_handler(msg):
        seen.append("extension")

    ctx.on("message", ext_handler)
    _run(ctx.client.on_message("m"))
    assert seen == ["connector", "extension"]


def test_raising_handler_is_unsubscribed_and_contained(tmp):
    m = _mod(tmp)
    logged = []
    ctx = _ctx(m, tmp, log=logged.append)
    ctx.client = _FakeClient()
    seen = []

    async def boom(p):
        raise RuntimeError("boom")

    async def good(p):
        seen.append("good")

    ctx.on("raw_reaction_add", boom)
    ctx.on("raw_reaction_add", good)
    _run(ctx.client.on_raw_reaction_add("evt"))   # must not raise
    _run(ctx.client.on_raw_reaction_add("evt"))
    assert seen == ["good", "good"]               # survivor keeps firing
    assert any("unsubscribing" in line for line in logged)


def test_reloading_an_extension_does_not_stack_handlers(tmp):
    """`extension set` on a loaded extension builds a NEW context. Handlers
    must be replaced, not chained -- otherwise one click fires N times."""
    m = _mod(tmp)
    client = _FakeClient()
    listeners, installed = {}, set()
    calls = []

    def ctx_for(gen):
        c = m._ExtensionContext(client=client, identity="bob", flavor="claude",
                                log=lambda s: None, emit=lambda o: None,
                                listeners=listeners, installed=installed)

        async def handler(p):
            calls.append(gen)

        return c, handler

    c1, h1 = ctx_for("gen1")
    c1.on("raw_reaction_add", h1)

    # reload: the connector clears the outgoing generation first
    for handlers in listeners.values():
        handlers.clear()
    c2, h2 = ctx_for("gen2")
    c2.on("raw_reaction_add", h2)

    _run(client.on_raw_reaction_add("evt"))
    assert calls == ["gen2"], calls


def test_reload_keeps_the_connectors_own_handler(tmp):
    m = _mod(tmp)
    client = _FakeClient()
    listeners, installed = {}, set()
    calls = []

    async def connector_handler(msg):
        calls.append("connector")

    client.on_message = connector_handler

    for gen in ("gen1", "gen2"):
        for handlers in listeners.values():
            handlers.clear()
        c = m._ExtensionContext(client=client, identity="bob", flavor="claude",
                                log=lambda s: None, emit=lambda o: None,
                                listeners=listeners, installed=installed)

        async def handler(msg, gen=gen):
            calls.append(gen)

        c.on("message", handler)

    _run(client.on_message("m"))
    assert calls == ["connector", "gen2"], calls


# --------------------------------------------------------- spawned tasks

def test_spawn_tracks_the_task(tmp):
    import asyncio
    import types
    m = _mod(tmp)

    async def body():
        tasks = []
        client = types.SimpleNamespace(loop=asyncio.get_running_loop())
        ctx = m._ExtensionContext(client=client, identity="bob", flavor="claude",
                                  log=lambda s: None, emit=lambda o: None,
                                  tasks=tasks)

        async def forever():
            await asyncio.sleep(3600)

        ctx.spawn(forever())
        assert len(tasks) == 1
        m.cancel_tracked_tasks(tasks)

    _run(body())


def test_reload_cancels_the_previous_generations_tasks(tmp):
    """Otherwise every reload leaves another poll loop running."""
    import asyncio
    import types
    m = _mod(tmp)

    async def body():
        tasks = []
        client = types.SimpleNamespace(loop=asyncio.get_running_loop())
        ctx = m._ExtensionContext(client=client, identity="bob", flavor="claude",
                                  log=lambda s: None, emit=lambda o: None,
                                  tasks=tasks)
        ticks = []

        async def loop_body():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        task = ctx.spawn(loop_body())
        await asyncio.sleep(0.03)
        assert m.cancel_tracked_tasks(tasks) == 1
        assert tasks == []
        await asyncio.sleep(0.03)
        seen = len(ticks)
        await asyncio.sleep(0.03)
        assert len(ticks) == seen, "cancelled task kept running"
        assert task.cancelled() or task.done()

    _run(body())


def test_cancel_is_safe_on_an_empty_or_finished_list(tmp):
    m = _mod(tmp)
    assert m.cancel_tracked_tasks([]) == 0
    assert m.cancel_tracked_tasks(None) == 0


def test_deliver_hands_a_relayed_message_to_the_inbox_pipeline(tmp):
    """A relay must reach the same pipeline a mention or DM does.

    `emit` was the only delivery an extension had, and it publishes a custom
    event and nothing else: no inbox JSON, no receipt reaction, no attachment
    metadata, and no record `send --reply-to` could resolve. A member's PDF was
    silently dropped that way (issue #220).
    """
    m = _mod(tmp)
    delivered = []

    async def fake_write_inbox(msg, source='live'):
        delivered.append((msg, source))
        return Path(tmp) / "inbox" / f"{msg}.json"

    ctx = m._ExtensionContext(client=None, identity="bob", flavor="claude",
                              log=lambda s: None, emit=lambda o: None,
                              deliver=fake_write_inbox)

    async def body():
        return await ctx.deliver("msg-42")

    path = _run(body())
    assert delivered == [("msg-42", "extension")], (
        f"deliver did not reach the inbox pipeline: {delivered}")
    assert path == Path(tmp) / "inbox" / "msg-42.json", (
        "deliver did not report where the record landed")


def test_deliver_without_a_connector_writer_fails_loudly(tmp):
    """A context with no writer must say so, not fail as a None call."""
    m = _mod(tmp)
    ctx = _ctx(m, tmp)

    async def body():
        return await ctx.deliver("msg-7")

    try:
        _run(body())
    except RuntimeError as exc:
        assert "cannot deliver messages" in str(exc), exc
    else:
        raise AssertionError("deliver silently accepted a missing writer")


def test_connector_supplies_its_real_inbox_writer_to_extensions(_tmp):
    """The context is only useful if the connector wires the real writer.

    The pipeline itself cannot be exercised here -- it needs a live gateway --
    so pin the wiring and the return value that makes `deliver` reportable.
    """
    source = (Path(_util.SCRIPTS) / "discord_mb_lib" / "connector.py").read_text(
        encoding="utf-8")
    start = source.split("async def _extension_start", 1)
    assert len(start) == 2, "connector no longer starts extensions here"
    construction = start[1].split("await setup(ctx)", 1)[0]
    assert "deliver=write_inbox" in construction, (
        "extensions are built without the connector's inbox writer, so "
        "ctx.deliver cannot reach the arriving-message pipeline")

    writer = source.split("async def write_inbox", 1)
    assert len(writer) == 2, "connector no longer defines write_inbox"
    body = writer[1].split("\n    @client.event", 1)[0]
    assert "return path" in body, (
        "write_inbox reports nothing, so ctx.deliver cannot say where the "
        "record landed")


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="mbext_")


if __name__ == "__main__":
    raise SystemExit(main())
