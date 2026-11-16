#!/usr/bin/env python3
"""Sender labels: a name from the directory, or visibly `user:<username>`.

The pure half drives `discord_mb_lib._directory`. The connector half runs the
REAL connector on the stand-in gateway (`tests/_gateway.py`): one account that
holds directory names and one that merely has a username equal to one of them,
read through the `message` event, the inbox record and every listing that
labels an author.
"""
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
from discord_mb_lib._directory import _Directory as D  # noqa: E402

MALLORY, OTHER = 970000000000000002, 970000000000000003
FALLBACK = "user:alice"                # MALLORY's username is `alice`, a name ALICE holds


def test_a_sender_is_labelled_by_its_oldest_directory_name_or_user_and_username(tmp):
    del tmp
    held = {"alice": ALICE, "probe": OTHER, "alice-2": ALICE}
    assert D.names_held(held, ALICE) == ["alice", "alice-2"]
    assert D.names_held(held, MALLORY) == []
    user = types.SimpleNamespace(id=ALICE, name="x")
    assert D.label_of(held, user) == "alice"
    assert D.label_of(held, types.SimpleNamespace(id=MALLORY, name="alice")) == FALLBACK
    assert D.label_of({}, types.SimpleNamespace(id=MALLORY)) == "user:?", "a nameless user"
    logs = []
    entries = [("user:alice", MALLORY, MALLORY), ("User:alice", MALLORY, MALLORY),
               ("fine", MALLORY, MALLORY), ("user", OTHER, OTHER)]
    assert D(logs.append).bind_names(entries) == {"fine": MALLORY, "user": OTHER}, "a `user:` name was bound"
    assert len(logs) == 2 and all("user:" in line.lower() for line in logs), logs


def test_the_message_event_and_record_say_who_sent_it_and_whether_they_hold_a_name(tmp):
    entries = [_gateway.message(5, MALLORY, f"**user:alice** · <@{MALLORY}>"),    # newest first
               _gateway.message(4, ALICE, f"**alice-2** · <@{ALICE}>"),
               _gateway.message(3, BOT, f"**gate-test** · <@{BOT}>"),
               _gateway.message(2, ALICE, f"**alice** · <@{ALICE}>")]

    async def script(client):
        dm = client.dm(970000000000000099)
        for msg_id, author in ((101, MALLORY), (102, ALICE)):
            await client.handlers["on_message"](client.message(msg_id, author, "hi", dm, username="alice"))

    client = _gateway.run(tmp, script, entries)
    if client is None:
        return
    events = {int(e["msg_id"]): e for e in client.events if e.get("event") == "message"}
    for msg_id, author, label, registered, identities in (
            (101, MALLORY, FALLBACK, False, []), (102, ALICE, "alice", True, ["alice", "alice-2"])):
        record = _gateway.inbox(tmp, msg_id)
        assert (events[msg_id]["from"], events[msg_id]["from_user_id"], events[msg_id]["from_registered"]) == (
            label, str(author), registered), events[msg_id]
        assert (record["from"], record["from_user_id"], record["author_identities"]) == (
            label, str(author), identities), record
        assert registered == bool(record["author_identities"]), "the flag is not the record's lookup"


def test_the_flag_and_the_records_identities_come_from_one_directory_read(tmp):
    held = [_gateway.message(7, MALLORY, f"**mallory** · <@{MALLORY}>"),
            _gateway.message(3, BOT, f"**gate-test** · <@{BOT}>")]
    for case, entries, rescan, breaks, registered, identities in (
            ("a rescan binds the sender while the context is resolved", None, True, False, False, []),
            ("the context fails after its awaits", held, False, True, True, ["mallory"])):
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

        case_dir = Path(tmp) / str(len(case))
        case_dir.mkdir()
        client = _gateway.run(case_dir, script, entries)
        if client is None:
            return
        event = next(e for e in client.events if e.get("event") == "message")
        record = _gateway.inbox(case_dir, 101)
        assert not rescan or "mallory" in client.log.rsplit("directory map", 1)[-1], (case, "no rescan", client.log)
        assert (breaks and "resolve_context failed" in client.log) or not breaks, (case, client.log)
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
    if client is None:
        return
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


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="labels_")


if __name__ == "__main__":
    raise SystemExit(main())
