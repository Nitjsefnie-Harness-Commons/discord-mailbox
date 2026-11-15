#!/usr/bin/env python3
"""A meta request is claimed by the connector or withdrawn by the CLI, never both,
and a claimed one is never reported as a dead connector. Stdlib and discord.py only, OS-agnostic."""
import contextlib
import io
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402
from test_discord_conversation_dm import BRIDGE_ID, _ask, _connector, _conv  # noqa: E402
sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import core  # noqa: E402
from discord_mb_lib._meta_claim import MetaRequestPending, _claim, _withdraw  # noqa: E402


@contextlib.contextmanager
def _cli_side(tmp, step):
    """core with its meta dirs under tmp; each sleep jumps the clock and runs step(in, out)."""
    in_dir, out_dir = Path(tmp) / "in", Path(tmp) / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    clock = types.SimpleNamespace(now=1e6)          # a real clock's origin, never zero
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
                connector.enter_context(_claim(p))
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
                held.append(connector.enter_context(_claim(p)))

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
                    _withdraw(in_dir / f"{name}.json", out_dir / f"{name}.json", 1, "dm-test")
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


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="metaclaim_")


if __name__ == "__main__":
    sys.exit(main())
