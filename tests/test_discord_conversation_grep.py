#!/usr/bin/env python3
"""`conversation --grep`: the CLI filters, the connector never runs the pattern.

The connector half drives the REAL `conversation` meta op on the stand-in
gateway; the CLI half drives `conversation_cli` over a stand-in `_meta_request`.
"""
import contextlib
import io
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
    sent, out, err = [], io.StringIO(), io.StringIO()

    def stand_in(identity, payload, timeout):
        sent.append(payload)
        return {"ok": True, "messages": MESSAGES}

    original, code = core._meta_request, None
    core._meta_request = stand_in
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                core.conversation_cli("me", 5, **kwargs)
            except SystemExit as exc:
                code = exc.code
    finally:
        core._meta_request = original
    return code, out.getvalue(), err.getvalue(), sent


def test_the_connector_refuses_a_request_that_still_carries_grep(tmp):
    responses, world = _ask(tmp, {"slow": _conv(dm_user="alice", grep="^(a+)+$"),
                                  "bad": _conv(dm_user="alice", grep="(")})
    if responses is None:
        return
    for name in ("slow", "bad"):
        resp = responses[name]
        assert not resp["ok"] and "moved to the CLI" in resp["error"], resp
        assert "messages" not in resp, "an older CLI must never get an unfiltered list"
    assert world.alice_dm.calls == [] and world.bridge.calls == [], "no history was read"


def test_the_connector_still_serves_a_conversation_without_grep(tmp):
    responses, _ = _ask(tmp, {"r": _conv(dm_user="alice")})
    if responses is None:
        return
    assert responses["r"]["ok"] and len(responses["r"]["messages"]) == 2, responses["r"]


def test_the_cli_filters_content_and_content_rendered_and_sends_no_grep(tmp):
    del tmp
    if _connector()[0] is None:
        return
    for grep, expected in (("needle", ["1", "2"]), ("embed$", ["3"]), ("^plain", ["1"]), ("absent", [])):
        code, out, _, sent = _cli(grep=grep, as_json=True)
        assert code is None and [m["msg_id"] for m in json.loads(out)] == expected, (grep, out)
        assert sent == [{"op": "conversation", "limit": 5}], sent
    _, text, _, _ = _cli(grep="absent")
    assert text == "(no messages matched)\n", text
    _, text, _, _ = _cli(grep="needle")
    assert text.count("msg_id=") == 2 and "msg_id=3" not in text, text
    _, text, _, _ = _cli()
    assert text.count("msg_id=") == 3, "no --grep keeps every message"


def test_an_invalid_pattern_fails_in_the_cli_before_any_request(tmp):
    del tmp
    if _connector()[0] is None:
        return
    code, out, err, sent = _cli(grep="(")
    assert code == 2 and out == "" and sent == [], (code, out, sent)
    assert "invalid --grep pattern '('" in err, err


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="convgrep_")


if __name__ == "__main__":
    sys.exit(main())
