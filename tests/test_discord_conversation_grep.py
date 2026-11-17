#!/usr/bin/env python3
"""`conversation --grep`: the CLI filters, the connector never runs the pattern."""
import functools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402
from test_discord_conversation_dm import _ask, _connector, _conv  # noqa: E402

MESSAGES = [
    {"msg_id": "1", "from": "alpha", "created": "2026-01-01T12:00:00+00:00", "content": "plain needle"},
    {"msg_id": "2", "from": "alpha", "created": "2026-01-01T12:00:01+00:00", "content": "",
     "content_rendered": "embed-only needle"},
    {"msg_id": "3", "from": "alpha", "created": "2026-01-01T12:00:02+00:00", "content": "other",
     "content_rendered": "other embed"},
]


def _cli(**kwargs):
    """-> (exit code or None, stdout, stderr, requests sent) of `conversation_cli`."""
    from discord_mb_lib import core
    return _util.run_cli(functools.partial(core.conversation_cli, "me", 5, **kwargs), {"ok": True, "messages": MESSAGES})


def test_the_connector_refuses_a_request_that_still_carries_grep(tmp):
    responses, world = _ask(tmp, {"slow": _conv(dm_user="alice", grep="^(a+)+$"),
                                  "bad": _conv(dm_user="alice", grep="("),
                                  "empty": _conv(dm_user="alice", grep="")})
    if responses is None:
        _util.skip("discord.py is not installed")
    for name in ("slow", "bad", "empty"):
        resp = responses[name]
        assert not resp["ok"] and "moved to the CLI" in resp["error"], resp
        assert "messages" not in resp, "an older CLI must never get an unfiltered list"
    assert world.alice_dm.calls == [] and world.bridge.calls == [], "no history was read"


def test_the_cli_filters_content_and_content_rendered_and_sends_no_grep(tmp):
    del tmp
    if _connector()[0] is None:
        _util.skip("discord.py is not installed")
    for grep, expected in (("needle", ["1", "2"]), ("embed$", ["3"]), ("^plain", ["1"]), ("absent", []),
                           ("NEEDLE", [])):     # no flags: the match is case-sensitive
        code, out, _, sent = _cli(grep=grep, as_json=True)
        assert code is None and [m["msg_id"] for m in json.loads(out)] == expected, (grep, out)
        assert sent == [("me", {"op": "conversation", "limit": 5})], sent
    _, text, _, _ = _cli(grep="absent")
    assert text == "(no messages matched)\n", text
    _, text, _, _ = _cli(grep="needle")
    assert text.count("msg_id=") == 2 and "msg_id=3" not in text, text
    _, text, _, _ = _cli()
    assert text.count("msg_id=") == 3, "no --grep keeps every message"


def test_an_invalid_pattern_fails_in_the_cli_before_any_request(tmp):
    del tmp
    if _connector()[0] is None:
        _util.skip("discord.py is not installed")
    code, out, err, sent = _cli(grep="(")
    assert code == 2 and out == "" and sent == [], (code, out, sent)
    assert "invalid --grep pattern '('" in err, err


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="convgrep_")


if __name__ == "__main__":
    sys.exit(main())
