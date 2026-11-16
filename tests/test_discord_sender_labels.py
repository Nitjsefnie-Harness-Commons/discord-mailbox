#!/usr/bin/env python3
"""Sender labels: a name from the directory, or visibly `user:<username>`; and the attachment ops' answers."""
import contextlib
import io
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE, BOT  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import core  # noqa: E402
from discord_mb_lib._directory import _Directory as D  # noqa: E402

MALLORY, OTHER = 970000000000000002, 970000000000000003
FALLBACK = "user:alice"                # MALLORY's username is `alice`, a name ALICE holds
DOWNLOADS = (  # attachment name, the file it is saved as, why it is skipped
    ("../../escaped.txt", "escaped.txt", None), ("..\\..\\win.txt", "win.txt", None),
    ("sub/escaped.txt", None, "an earlier attachment on this message is saved as 'escaped.txt'"),
    ("dir/", None, "file name reduces to ''"), ("x/.", None, "file name reduces to '.'"), ("loop.txt", None, "'loop.txt' does not resolve"),
    ("x\\..", None, "file name reduces to '..'"), ("old.txt", "old.txt", None),   # already there: overwritten
    ("link.txt", None, "'link.txt' does not resolve inside dest_dir"),
    ("self", None, "'self' does not resolve inside dest_dir"), ("sub", None, "'sub' is a directory"),
    ("alias.txt", None, "'alias.txt' is the file an earlier attachment on this message is saved as"))


def test_a_sender_is_labelled_by_its_oldest_directory_name_or_user_and_username(tmp):
    del tmp
    held = {"alice": ALICE, "probe": OTHER, "alice-2": ALICE}
    assert D.names_held(held, ALICE) == ["alice", "alice-2"]
    assert D.names_held(held, MALLORY) == []
    user = types.SimpleNamespace(id=ALICE, name="x")
    assert D.label_of(held, user) == "alice"
    assert D.label_of(held, types.SimpleNamespace(id=MALLORY, name="alice")) == FALLBACK
    assert D.label_of({}, types.SimpleNamespace(id=MALLORY)) == "user:?", "a nameless user"


def test_the_message_event_and_record_say_who_sent_it_and_whether_they_hold_a_name(tmp):
    entries = [_gateway.message(7, ALICE, f"**١** · <@{ALICE}>"),                 # newest first; a digit, not an ASCII one: a name
               _gateway.message(6, MALLORY, f"**{OTHER}** · <@{MALLORY}>"),       # neither of MALLORY's entries binds
               _gateway.message(5, MALLORY, f"**user:alice** · <@{MALLORY}>"),
               _gateway.message(4, ALICE, f"**alice-2** · <@{ALICE}>"),
               _gateway.message(3, BOT, f"**gate-test** · <@{BOT}>"),
               _gateway.message(2, ALICE, f"**alice** · <@{ALICE}>")]

    async def script(client):
        dm = client.dm(970000000000000099)
        for msg_id, author in ((101, MALLORY), (102, ALICE)):
            await client.handlers["on_message"](client.message(msg_id, author, "hi", dm, username="alice"))

    client = _gateway.run(tmp, script, entries)
    events = {int(e["msg_id"]): e for e in client.events if e.get("event") == "message"}
    for msg_id, author, label, registered, identities, multiple in (
            (101, MALLORY, FALLBACK, False, [], False), (102, ALICE, "alice", True, ["alice", "alice-2", "١"], True)):
        record = _gateway.inbox(tmp, msg_id)
        assert record["author_has_multiple_identities"] is multiple, record
        assert (events[msg_id]["from"], events[msg_id]["from_user_id"], events[msg_id]["from_registered"]) == (
            label, str(author), registered), events[msg_id]
        assert (record["from"], record["from_user_id"], record["author_identities"]) == (
            label, str(author), identities), record


def test_the_flag_and_the_records_identities_come_from_one_directory_read(tmp):
    held = [_gateway.message(7, MALLORY, f"**mallory** · <@{MALLORY}>"),
            _gateway.message(3, BOT, f"**gate-test** · <@{BOT}>")]
    for n, (case, entries, rescan, breaks, registered, identities) in enumerate((
            ("a rescan binds the sender while the context is resolved", None, True, False, False, []),
            ("the context fails after its awaits", held, False, True, True, ["mallory"]))):
        async def script(client, rescan=rescan, breaks=breaks):
            dm = client.dm(970000000000000099)

            async def pins(*, limit=50):
                client.directory.msgs.insert(0, _gateway.message(
                    8, MALLORY, f"**mallory** · <@{MALLORY}>", client.directory))
                client.clock[0] += 60                       # the next name miss rescans
                await client.ask({"miss": {"op": "send", "to": "ghost", "subject": "s", "body": "b"}})
                return
                yield                                       # an async generator, yielding nothing
            if rescan:
                dm.pins = pins
            msg = client.message(101, MALLORY, "hi", dm, username="alice")
            if breaks:
                msg.stickers = 5                            # resolve_context raises on it, last
            await client.handlers["on_message"](msg)

        case_dir = Path(tmp) / str(n)
        case_dir.mkdir()
        client = _gateway.run(case_dir, script, entries)
        event = next(e for e in client.events if e.get("event") == "message")
        record = _gateway.inbox(case_dir, 101)
        assert not rescan or "mallory" in client.log.rsplit("directory map", 1)[-1], (case, "no rescan", client.log)
        assert not breaks or "resolve_context failed" in client.log, (case, client.log)
        assert (event["from_registered"], record["author_identities"],
                record["author_has_multiple_identities"]) == (registered, identities, False), (case, event, record)


def _pair(client, channel, first_id, **fields):
    """[MALLORY's message, ALICE's message], newest first; both have the username `alice`."""
    pair = [client.message(first_id + 1, MALLORY, "from mallory", channel, username="alice"),
            client.message(first_id, ALICE, "from alice", channel, username="alice")]
    for m in pair:
        for name, value in fields.items():
            setattr(m, name, value)
    return pair


def test_every_author_label_in_a_listing_follows_the_same_rule(tmp):
    async def script(client):
        file = types.SimpleNamespace(filename="f.txt", size=1, url="u", content_type="t")
        client.attachments.msgs[:] = _pair(client, client.attachments, 10, attachments=[file])
        client.credentials.msgs[:] = _pair(client, client.credentials, 20,
                                           content="```\na.example\n```\n```\nlogin\n```\n```\nsecret\n```")
        client.bridge.msgs[:] = _pair(client, client.bridge, 30)
        client.bridge.pinned = _pair(client, client.bridge, 40)
        move = {"op": "move", "source": "bridge", "dest": "attachments", "dest_kind": "channel", "limit": 10}
        await client.ask({
            "attachments": {"op": "attachment-list"}, "creds": {"op": "creds-list"},
            "conversation": {"op": "conversation", "limit": 10}, "pins": {"op": "pins-list"},
            "plan": {**move, "dry_run": True}})
        await client.ask({"move": move})
        await client.handlers["on_message"](client.message(
            90, MALLORY, f"<@{BOT}> look", client.bridge, mentions=(BOT,), username="alice"))

    client = _gateway.run(tmp, script)
    r, newest_first, oldest_first = client.responses, [FALLBACK, "alice"], ["alice", FALLBACK]
    for op, key, field in (("attachments", "entries", "from"), ("creds", "entries", "author"),
                           ("conversation", "messages", "from"), ("pins", "entries", "from"),
                           ("plan", "messages", "from"), ("move", "messages", "from")):
        assert [e[field] for e in r[op][key]] == oldest_first, (op, r[op])
    assert [s.split("**")[1] for s in client.attachments.sent] == oldest_first, client.attachments.sent
    cached = json.loads(next((Path(tmp) / "msgs").glob("*/31.json")).read_text(encoding="utf-8"))
    assert cached["from"] == FALLBACK, cached
    pinned = _gateway.inbox(tmp, 90)["pinned_messages"]
    assert [p["from"] for p in pinned] == newest_first, pinned
    saved = {p.stem: json.loads(p.read_text(encoding="utf-8"))["from"] for p in (Path(tmp) / "pins").glob("*/*.json")}
    assert saved == {"40": "alice", "41": FALLBACK}, saved


def _download(tmp, filenames, dest, **extra):
    """-> the attachment-download answer for one #attachments message carrying `filenames`."""
    def attachment(filename):
        async def save(fp):
            Path(fp).write_text(f"from {filename}", encoding="utf-8")
        return types.SimpleNamespace(filename=filename, size=1, url="u", save=save)

    async def script(client):
        async def fetch_message(msg_id):
            msg = client.message(msg_id, ALICE, "", client.attachments)
            msg.attachments = [attachment(f) for f in filenames]
            return msg
        client.attachments.fetch_message = fetch_message
        await client.ask({"dl": {"op": "attachment-download", "msg_id": "7", "dest_dir": str(dest), **extra}})
    tmp.mkdir()
    return _gateway.run(tmp, script).responses["dl"]


def test_a_download_saves_each_attachment_directly_inside_dest_dir_or_skips_it_with_a_reason(tmp):
    root = Path(tmp)
    inside = root / "real" / "inside"
    inside.mkdir(parents=True)
    (root / "via").symlink_to(root / "real", target_is_directory=True)   # dest_dir differs from its resolved form
    (inside / "old.txt").write_text("stale", encoding="utf-8")
    for link, to in (("link.txt", root / "outside.txt"), ("alias.txt", inside / "escaped.txt"), ("loop.txt", inside / "loop.txt")):
        (inside / link).symlink_to(to)
    (inside / "self").symlink_to(inside, target_is_directory=True)
    (inside / "sub").mkdir()
    dest = root / "via" / "inside"
    resp = _download(root / "gateway", [n for n, *_ in DOWNLOADS], dest)
    assert [(s["filename"], s["path"]) for s in resp["saved"]] == [(n, str(dest / f)) for n, f, _ in DOWNLOADS if f], resp
    assert resp["skipped"] == [{"filename": n, "reason": r} for n, _, r in DOWNLOADS if r], resp
    assert sorted(os.listdir(root)) == ["gateway", "real", "via"], os.listdir(root)
    resp = _download(root / "renamed", ["../../escaped.txt"], inside, rename="../renamed.txt")   # taken as given
    assert (resp["saved"][0]["path"], resp["skipped"]) == (str(inside / "../renamed.txt"), []), resp
    fresh = root / "real" / "new"                   # created; with two attachments --rename goes unused
    resp = _download(root / "two", ["a.txt", "b.txt"], fresh, rename="../renamed.txt")
    assert [s["path"] for s in resp["saved"]] == [str(fresh / "a.txt"), str(fresh / "b.txt")], resp
    assert _download(root / "empty", [], inside)["skipped"] == [], "no attachments, nothing skipped"


def test_the_download_cli_prints_every_skipped_attachment(tmp):
    del tmp
    args = types.SimpleNamespace(identity="me", timeout=1.0, att_action="download", msg_id="7", dest_dir="d", rename=None, channel=None)
    err, original, code = io.StringIO(), core._meta_request, None
    core._meta_request = lambda i, p, t: {"ok": True, "saved": [], "skipped": [
        {"filename": "x/..", "reason": "file name reduces to '..'"}]}
    try:
        with contextlib.redirect_stderr(err):
            core.attachments_cli(args)
    except SystemExit as e:
        code = e.code
    finally:
        core._meta_request = original
    assert (code, err.getvalue()) == (2, "skipped: 'x/..'  (file name reduces to '..')\n"
                                         "(nothing saved from 7: every attachment was skipped)\n"), err.getvalue()


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="labels_")


if __name__ == "__main__":
    raise SystemExit(main())
