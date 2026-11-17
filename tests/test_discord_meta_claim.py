#!/usr/bin/env python3
"""A meta request is claimed by the connector or withdrawn by the CLI, never both,
and a claimed one is never reported as a dead connector. Its files are private, and
a response left past the bound is swept. Stdlib and discord.py only, OS-agnostic."""
import asyncio
import contextlib
import io
import json
import os
import secrets
import sys
import time
import types
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from test_discord_conversation_dm import BRIDGE_ID, _ask, _connector, _conv  # noqa: E402
sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _control_files, core  # noqa: E402
from discord_mb_lib._meta_claim import MetaRequestPending, _claim, _withdraw  # noqa: E402


@contextlib.contextmanager
def _cli_side(tmp, step):
    """core with its meta dirs under tmp; each sleep jumps the clock and runs step(in, out)."""
    in_dir, out_dir = Path(tmp) / "in", Path(tmp) / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    clock = types.SimpleNamespace(now=time.time())  # the connector judges a request's age by the real clock
    clock.time = lambda: clock.now

    def sleep(delay):
        clock.now += delay
        step(in_dir, out_dir)

    clock.sleep = sleep
    saved = core.time, core.meta_in_dir, core.meta_out_dir
    core.time, core.meta_in_dir, core.meta_out_dir = clock, lambda _: in_dir, lambda _: out_dir
    try:
        yield in_dir, out_dir
    finally:
        core.time, core.meta_in_dir, core.meta_out_dir = saved


def test_an_unclaimed_request_is_withdrawn_and_reported_as_before(tmp):
    err = None
    with _cli_side(tmp, lambda i, o: None) as (in_dir, _):
        try:
            core._meta_request("me", {"op": "thread-edit"}, 0.2)
        except TimeoutError as e:
            err = e
    assert str(err) == "connector did not respond within 0.2s (is it running for 'me'?)", err
    assert not any(in_dir.iterdir()), list(in_dir.iterdir())


def test_a_response_landing_after_the_claim_is_returned(tmp):
    with contextlib.ExitStack() as connector:
        def claim_and_answer(in_dir, out_dir):
            for p in in_dir.glob("*.json"):
                connector.enter_context(_claim(p, out_dir))
                (out_dir / p.name).write_text(json.dumps({"ok": True, "late": 1}), encoding="utf-8")

        with _cli_side(tmp, claim_and_answer) as (_, out_dir):
            assert core._meta_request("me", {"op": "thread-edit"}, 0.2) == {"ok": True, "late": 1}
        assert not any(out_dir.iterdir()), list(out_dir.iterdir())


def test_a_claimed_rename_past_its_deadline_exits_75_as_pending(tmp):
    if _connector()[0] is None:
        return
    from discord_mb_lib import cli
    held, stderr, argv, code = [], io.StringIO(), sys.argv, None
    with contextlib.ExitStack() as connector:
        def claim(in_dir, out_dir):
            for p in in_dir.glob("*.json"):
                held.append(connector.enter_context(_claim(p, out_dir)))

        with _cli_side(tmp, claim) as (in_dir, out_dir), contextlib.redirect_stderr(stderr):
            sys.argv = ["discord_mb.py", "thread", "me", "--timeout", "0.2",
                        "rename", "new-name", "--channel", "1"]
            try:
                cli._cli()
            except SystemExit as e:
                code = e.code
            finally:
                sys.argv = argv
        assert code == 75, (code, stderr.getvalue())
        assert held and list(in_dir.iterdir()) == held, "the claimed request was withdrawn"
        assert str(out_dir / held[0].name.removesuffix(".claimed")) in stderr.getvalue(), stderr.getvalue()
        assert "did not respond" not in stderr.getvalue(), stderr.getvalue()


def test_the_connector_runs_only_what_it_claimed_and_clears_every_claim(tmp):
    in_dir, out_dir, outcomes = Path(tmp) / "state" / "in", Path(tmp) / "state" / "out", {}

    def busy(world):
        # While the connector works on "a", the CLI's deadline fires for "a" and "b".
        real = world.alice_dm.history

        async def history(**kwargs):
            for name in ("a", "b"):
                try:
                    _withdraw(in_dir / f"{name}.json", out_dir / f"{name}.json", 1, "dm-test", core.pid_alive, core.is_connector_process)
                except (MetaRequestPending, TimeoutError) as e:
                    outcomes[name] = type(e)
            async for m in real(**kwargs):
                yield m

        world.alice_dm.history = history

    responses, world = _ask(tmp, {"a": _conv(dm_user="alice"), "b": _conv(channel_id=str(BRIDGE_ID)),
                                  "c": _conv(dm_user="ghost")}, busy)
    if responses is None:
        return
    assert outcomes == {"a": MetaRequestPending, "b": TimeoutError}, outcomes
    assert responses["a"]["ok"] and not responses["c"]["ok"] and "b" not in responses, responses
    assert world.bridge.calls == [], "the withdrawn request ran"
    assert not any(in_dir.iterdir()), list(in_dir.iterdir())


def test_a_creds_add_request_carrying_a_password_is_private(tmp):
    password, seen = "pw" + secrets.token_urlsafe(8), []

    def answer(in_dir, out_dir):
        for p in in_dir.glob("*.json"):
            seen.append((p.read_text(encoding="utf-8"), p.stat().st_mode & 0o777))
            (out_dir / p.name).write_text(json.dumps({"ok": True, "action": "added"}), encoding="utf-8")

    args = types.SimpleNamespace(creds_action="add", identity="me", domain="a.example", login="login",
                                 password=password, upsert=False, timeout=1)
    with _cli_side(tmp, answer), contextlib.redirect_stdout(io.StringIO()):
        core.creds_cli(args)
    assert len(seen) == 1 and password in seen[0][0], seen
    if _util.POSIX_MODES:
        assert seen[0][1] == 0o600, oct(seen[0][1])


def test_write_private_publishes_a_fresh_private_inode_and_leaves_nothing_when_it_fails(tmp):
    target, twin, seen, refused, raised = Path(tmp) / "r.json", Path(tmp) / "twin", [], [], []
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    os.link(target, twin)       # a write through the old inode would show here
    real = {attr: getattr(os, attr) for attr in ("replace", "fdopen")}

    def spy(src, dst):
        seen.append((os.path.basename(src), os.stat(src).st_mode & 0o777))
        real["replace"](src, dst)

    def refuse(first, *args, **kwargs):
        refused.append(first)
        raise OSError("refused")

    for attr, stand_in, text in (("replace", spy, "one"), ("replace", spy, "two"), ("replace", refuse, "three"),
                                 ("fdopen", refuse, "three"), ("replace", spy, "\udc80")):  # the write fails
        setattr(os, attr, stand_in)
        try:
            _control_files.write_private(target, text)
        except (OSError, UnicodeError):
            raised.append(text)
        finally:
            setattr(os, attr, real[attr])
    assert raised == ["three", "three", "\udc80"], raised
    assert sorted(os.listdir(tmp)) == ["r.json", "twin"], os.listdir(tmp)
    assert len(seen) == 2 and seen[0][0] != seen[1][0], seen
    assert (target.read_text(encoding="utf-8"), twin.read_text(encoding="utf-8")) == ("two", "old")
    try:
        os.fstat(refused[1])
    except OSError:
        pass
    else:
        raise AssertionError("the descriptor fdopen refused stayed open")
    if _util.POSIX_MODES:
        assert [mode for _, mode in seen] == [0o600, 0o600], seen


def _aged(directory, name, age):
    path = directory / name
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def test_the_connector_sweeps_stale_responses_at_start_and_on_every_pass(tmp):
    out, old = Path(tmp) / "state" / "out", 15 * 60 + 30
    out.mkdir(parents=True)

    def name(first):        # a response's name with its first character fixed, to order the sweep
        return first + str(uuid.uuid4())[1:] + ".json"

    def temps(first):       # a plain-suffix temp and write_private's
        return name(first) + ".tmp", "." + name(first) + ".k2j_x9a1.tmp"

    kept = [*(_aged(out, n, 15 * 60 - 30) for n in (name("a"), *temps("a"))), *(_aged(out, n, old) for n in (
        "notes.json", "x" + name("a"), name("a") + ".old.json", "." + name("a") + ".tmp"))]
    gone = [_aged(out, name(c), old) for c in "135"] + [_aged(out, n, old) for n in temps("6")]
    faults = {name("0"): ("lstat", PermissionError), name("2"): ("unlink", FileNotFoundError),
              name("4"): ("lstat", FileNotFoundError)}           # unreadable, vanished, vanished
    kept += [_aged(out, n, old) for n in faults]
    once = _aged(out, name("f"), old)           # raises once, past the sweep's own catch, at start
    faults[once.name] = ("lstat", RuntimeError)
    real, fired, later = {op: getattr(os, op) for op in ("lstat", "unlink")}, set(), []

    def faulty(op):
        def call(path, *args, **kwargs):
            fault = faults.get(os.path.basename(path))
            if fault and fault[0] == op and not (fault[1] is RuntimeError and once.name in fired):
                fired.add(os.path.basename(path))
                raise fault[1](path)
            return real[op](path, *args, **kwargs)
        return call

    async def script(client):
        later.append(sorted(out.iterdir()))         # before the meta watcher's first pass
        await client.ask({"first": {"op": "nope"}})
        later.append(sorted(out.iterdir()))
        late = _aged(out, name("b"), old)
        await client.ask({"second": {"op": "nope"}})
        later.append(late.exists())

    for op in real:
        setattr(os, op, faulty(op))
    try:
        _gateway.run(tmp, script)
    finally:
        for op, f in real.items():
            setattr(os, op, f)
    assert later[0] == sorted([*kept, once]), (later[0], gone)
    assert later[1] == sorted([*kept, out / "first.json"]), later[1]
    assert later[2] is False, "a response gone stale after start was kept"
    assert fired == set(faults), fired


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _request(**fields):
    return {"op": "conversation", "limit": 1, **fields}


def test_a_request_cancelled_mid_operation_is_answered_as_interrupted_and_not_run_again(tmp):
    state, runs, seen = Path(tmp) / "state", [], {}

    async def hang(*, limit=100):       # the operation: a bridge read that never finishes
        runs.append(limit)
        await asyncio.sleep(3600)
        yield

    async def script(client):
        client.bridge.history = hang
        (state / "in" / "r.json").write_text(json.dumps(_request()), encoding="utf-8")
        for _ in range(100):            # bounded: a request that never reaches the operation fails, not hangs
            if runs:
                break
            await asyncio.sleep(0.05)
        watcher = next(t for t in asyncio.all_tasks() if t.get_coro().__name__ == "meta_watcher")
        watcher.cancel()
        await asyncio.sleep(0.2)
        seen.update(response=_read(state / "out" / "r.json"), left=list((state / "in").iterdir()),
                    cancelled=watcher.cancelled())

    try:
        _gateway.run(tmp, script)
    except asyncio.CancelledError:      # the cancelled watcher fails the harness's gather
        pass
    assert seen["response"]["ok"] is False and "outcome is unknown" in seen["response"]["error"], seen
    assert seen["left"] == [] and seen["cancelled"] and runs == [1], (seen, runs)


def test_a_claim_a_killed_connector_left_is_answered_at_startup_and_never_run(tmp):
    state, seen = Path(tmp) / "state", {}
    (state / "in").mkdir(parents=True)
    (state / "in" / "dead.json.claimed").write_text(json.dumps(_request()), encoding="utf-8")
    (state / "in" / "done.json.claimed").write_text(json.dumps(_request()), encoding="utf-8")
    (state / "out").mkdir()
    (state / "out" / "done.json").write_text('{"ok": true}', encoding="utf-8")     # answered before the kill

    async def script(client):
        seen.update(response=_read(state / "out" / "dead.json"), left=list((state / "in").iterdir()),
                    done=_read(state / "out" / "done.json"))
        await client.ask({"live": {"op": "nope"}})      # the watcher has made a pass

    client = _gateway.run(tmp, script)
    assert seen["response"]["ok"] is False and "outcome is unknown" in seen["response"]["error"], seen
    assert seen["done"] == {"ok": True} and seen["left"] == [] and client.bridge.reads == [], (seen, client.bridge.reads)


def test_a_request_older_than_its_callers_wait_is_answered_with_an_expiry_and_not_run(tmp):
    now = time.time()
    fields = {"stale": {"created": now - 100, "timeout": 10}, "just-over": {"created": now - 11, "timeout": 10},
              "just-under": {"created": now - 9, "timeout": 10}, "fresh": {"created": now, "timeout": 60},
              "no-timeout": {"created": now - 1e6}, "no-created": {"timeout": 1}, "legacy": {},
              "bad-type": {"created": "yesterday", "timeout": 10}}

    async def script(client):
        await client.ask({name: _request(**extra) for name, extra in fields.items()})

    client = _gateway.run(tmp, script)
    oks = {name: resp["ok"] for name, resp in client.responses.items()}
    assert oks == {n: n not in ("stale", "just-over") for n in fields}, client.responses
    assert "expired" in client.responses["stale"]["error"], client.responses
    assert len(client.bridge.reads) == 6 and not any((Path(tmp) / "state" / "in").iterdir()), client.bridge.reads


def test_a_cli_request_carries_its_creation_time_and_its_wait(tmp):
    seen = []

    def read(in_dir, out_dir):
        for p in in_dir.glob("*.json"):
            seen.append(_read(p))
            (out_dir / p.name).write_text(json.dumps({"ok": True}), encoding="utf-8")

    with _cli_side(tmp, read):
        core._meta_request("me", {"op": "list-agents"}, 7)
    assert [(r["op"], r["timeout"], abs(r["created"] - time.time()) < 60) for r in seen] == [("list-agents", 7, True)], seen


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="metaclaim_")


if __name__ == "__main__":
    sys.exit(main())
