#!/usr/bin/env python3
"""Credentials: whose posts count, and every entry in the channel, however old.

The pure half drives `discord_mb_lib._creds`. The connector half runs the REAL
creds-list / creds-get / creds-add closures on the stand-in gateway
(`tests/_gateway.py`), whose channel history honours `limit` and whose token
directory is the test's own. The CLI half stubs only the meta round trip.
Token-shaped strings are assembled at runtime from a synthetic id and random
segments.
"""
import base64
import contextlib
import io
import json
import os
import secrets
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE, BOT  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _creds as C  # noqa: E402

PROBE, MALLORY = 940000000000000006, 940000000000000007     # a sibling bot; an unlisted member


def _b64(raw):
    return base64.b64encode(raw).decode().rstrip("=")


def _token(head):
    return ".".join((head, secrets.token_urlsafe(4), secrets.token_urlsafe(20)))


def _entry(client, msg_id, author, domain="a.example"):
    """A template post; the password is built from the id, never a literal."""
    return client.message(msg_id, author, f"```\n{domain}\n```\n```\nlogin\n```\n```\npw-{msg_id}\n```",
                          client.credentials)


# --- pure ---------------------------------------------------------------------

def test_a_token_files_first_segment_names_its_bot_or_the_file_is_skipped(tmp):
    d = Path(tmp)
    cases = {"probe": (_token(_b64(str(PROBE).encode())) + "\n", PROBE),
             "padded": (_token(base64.b64encode(b"12").decode()), 12), "unpadded": (_token(_b64(b"12")), 12),
             "words": (_token(_b64(b"bridge-bot")), None), "underscore": (_token(_b64(b"1_2")), None),
             "spaced": (_token(_b64(b" 12")), None), "alphabet": (_token("MT!I="), None),
             "one-char": (_token("a"), None), "empty": ("", None)}
    for name, (text, _) in cases.items():
        (d / f"{name}.token").write_text(text, encoding="utf-8")
    got = {name: C.token_user_id(d / f"{name}.token") for name in [*cases, "missing"]}
    assert got == {**{n: uid for n, (_, uid) in cases.items()}, "missing": None}, got


def test_from_settings_reads_the_list_and_the_token_directory_and_fails_loudly(tmp):
    d, logs, saved = Path(tmp), [], C.setting
    junk = _token("!!!!")
    for name, text in (("probe.token", _token(_b64(str(PROBE).encode()))), ("junk.token", junk),
                       ("other.txt", _token(_b64(str(MALLORY).encode())))):
        (d / name).write_text(text, encoding="utf-8")
    try:
        C.setting = {}.get
        trust = C._CredsTrust.from_settings(d, logs.append)
        assert (trust.listed, trust.bots) == (None, {PROBE}), (trust.listed, trust.bots)
        C.setting = {C.TRUSTED: f" {ALICE}, 7,"}.get
        assert C._CredsTrust.from_settings(d / "none", logs.append).listed == {ALICE, 7}
        C.setting = {C.TRUSTED: "alice"}.get
        try:
            C._CredsTrust.from_settings(d, logs.append)
        except SystemExit as e:
            assert e.code == 1 and C.TRUSTED in logs[-1], (e.code, logs)
        else:
            raise AssertionError("an invalid trusted-author list was accepted")
    finally:
        C.setting = saved
    assert any("junk.token" in line for line in logs), logs
    assert not any(part in line for line in logs for part in junk.split(".")[1:]), "token text was logged"


# --- the real connector ---------------------------------------------------------

def test_get_list_and_add_see_an_entry_older_than_five_hundred_messages(tmp):
    async def script(client):
        chatter = [client.message(1000 + i, BOT, f"chatter {i}", client.credentials) for i in range(600)]
        client.credentials.msgs[:] = [*chatter, _entry(client, 1, BOT)]      # newest first
        await client.ask({"list": {"op": "creds-list"}, "get": {"op": "creds-get", "domain": "a.example"},
                          "add": {"op": "creds-add", "domain": "a.example", "login": "login", "password": "x"}})

    client = _gateway.run(tmp, script)
    if client is None:
        return
    r = client.responses
    assert [e["msg_id"] for e in r["list"]["entries"]] == ["1"], r["list"]
    assert [e["msg_id"] for e in r["get"]["entries"]] == ["1"], r["get"]
    assert (r["add"]["action"], r["add"]["existing_msg_ids"]) == ("skipped", ["1"]), r["add"]
    assert client.credentials.sent == [], client.credentials.sent
    assert "warning" not in r["list"] and "warning" not in r["get"], "a warning with no untrusted entry"


def test_only_trusted_authors_count_once_the_list_is_set_and_unset_marks_and_warns(tmp):
    # 1 is this connector's own user, 2 a bot whose token is in the token
    # directory, 3 a listed human, 4 and 5 an unlisted member.
    authors = {"1": BOT, "2": PROBE, "3": ALICE, "4": MALLORY, "5": MALLORY}
    for case, listed, keep, skipped, add in (
            ("unset", "", ["1", "2", "3", "4", "5"], None, "skipped"),
            ("set", str(ALICE), ["1", "2", "3"], (2, 1), "added")):
        async def script(client):
            client.credentials.msgs[:] = [_entry(client, 5, MALLORY, "m.example"), *(
                _entry(client, int(i), authors[i]) for i in "4321")]
            await client.ask({"list": {"op": "creds-list"}, "get": {"op": "creds-get", "domain": "a.example"}})
            await client.ask({"add": {"op": "creds-add", "domain": "m.example", "login": "login", "password": "x"}})

        root = Path(tmp) / case
        (root / "tokens").mkdir(parents=True)
        (root / "tokens" / "probe.token").write_text(_token(_b64(str(PROBE).encode())), encoding="utf-8")
        client = _gateway.run(root, script, env={C.TRUSTED: listed})
        if client is None:
            return
        r = client.responses
        for op, ids in (("list", keep), ("get", [i for i in keep if i != "5"])):
            got = [(e["msg_id"], e["author_id"], e["trusted"]) for e in r[op]["entries"]]
            trusted = ("1", "2", "3") if listed else ("1", "2")
            assert got == [(i, str(authors[i]), i in trusted) for i in ids], (case, op, got)
            untrusted = sum(i not in trusted for i in ids)
            if skipped is None:
                assert "skipped_untrusted" not in r[op] and f"{untrusted} " in r[op]["warning"], (case, r[op])
            else:
                count = skipped[op == "get"]
                assert r[op]["skipped_untrusted"] == count and f"{count} " in r[op]["warning"], (case, r[op])
        assert r["add"]["action"] == add, (case, r["add"])


def test_an_invalid_trusted_author_list_stops_the_connector_at_startup(tmp):
    async def script(client):
        raise AssertionError("the connector reached the gateway")

    try:
        _gateway.run(tmp, script, env={C.TRUSTED: "bridge-bot"})
    except SystemExit as e:
        assert e.code == 1 and C.TRUSTED in (Path(tmp) / "connector.log").read_text(encoding="utf-8"), e.code
    else:
        if _gateway.have_discord():
            raise AssertionError("the connector started with an invalid setting")


# --- the CLI --------------------------------------------------------------------

def test_the_cli_sends_each_request_and_prints_the_warning_on_stderr_only(tmp):
    del tmp
    from discord_mb_lib import core
    entry = {"domain": "a.example", "login": "l", "password": "p", "msg_id": "1", "author": "bridge-bot",
             "author_id": "9", "trusted": False, "created": "2026-01-01T00:00:00+00:00"}
    twin, base = {**entry, "author_id": "8", "trusted": True}, {
        "identity": "me", "timeout": 1.0, "json": True, "show_passwords": False, "login": None}
    bare = {k: v for k, v in entry.items() if k not in ("author_id", "trusted")}     # an older connector's entry
    shown = ["bridge-bot user 9 UNTRUSTED 2026-01-01T00:00:00)", "bridge-bot user 8 2026-01-01T00:00:00)",
             "bridge-bot 2026-01-01T00:00:00)"]
    for action, extra, answer, payload, code, stdout in (
            ("list", {}, [entry], {"op": "creds-list"}, None, [{k: v for k, v in entry.items() if k != "password"}]),
            ("get", {"domain": "a.example"}, [entry], {"op": "creds-get", "domain": "a.example"}, None, [entry]),
            ("list", {"json": False}, [entry, twin, bare], {"op": "creds-list"}, None, shown),
            ("get", {"domain": "a.example", "json": False}, [entry, twin, bare],
             {"op": "creds-get", "domain": "a.example"}, None, shown),
            ("get", {"domain": "b.example", "login": "l"}, [],
             {"op": "creds-get", "domain": "b.example", "login": "l"}, 2, None),
            ("add", {"domain": "d", "login": "l", "password": "p", "upsert": True}, None,
             {"op": "creds-add", "domain": "d", "login": "l", "password": "p", "upsert": True}, None, None)):
        sent, out, err, original = [], io.StringIO(), io.StringIO(), core._meta_request
        core._meta_request = lambda i, p, t, answer=answer, sent=sent: sent.append((i, p)) or {
            "ok": True, "entries": json.loads(json.dumps(answer)), "warning": "w-text", "action": "added",
            "msg_id": "5"}
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                core.creds_cli(types.SimpleNamespace(**{**base, **extra, "creds_action": action}))
        except SystemExit as e:
            assert e.code == code, (action, e.code)
        else:
            assert code is None, (action, "no exit")
        finally:
            core._meta_request = original
        assert sent == [("me", payload)], (action, sent)
        assert err.getvalue().startswith("warning: w-text\n"), (action, err.getvalue())
        if stdout is not None:      # --json parses whole; the text form is read off its `(by ...)` tails
            got = json.loads(out.getvalue()) if extra.get("json", True) else [
                line.split("(by ", 1)[1] for line in out.getvalue().splitlines() if "(by " in line]
            assert got == stdout, (action, extra, out.getvalue())
    assert out.getvalue() == "added: 5\n", out.getvalue()


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="creds_")


if __name__ == "__main__":
    raise SystemExit(main())
