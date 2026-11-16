#!/usr/bin/env python3
"""Local message records follow deletions on Discord, on the REAL connector."""
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import BRIDGE, GUILD  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)

C1, C2, A, B = BRIDGE, 960000000000000002, 980000000000000001, 980000000000000002
MESSAGES = {701: (A, C1), 702: (B, C1), 703: (A, C2), 704: (B, C2)}
# Message 701 reached both identities; 702 is the only DM.
STORED = ["one/inbox/701", "one/inbox/702", "one/inbox/703", "two/inbox/701", "two/inbox/704",
          f"messages/{C1}/701", f"messages/{C1}/702", f"messages/{C2}/703", f"messages/{C2}/704",
          f"pins/{C1}/701", f"pins/{C2}/703"]


def seed(root):
    for rel in STORED:
        author, channel = MESSAGES[int(rel.rsplit("/", 1)[1])]
        record = {"msg_id": rel[-3:], "from_user_id": str(author), "channel_id": str(channel),
                  "is_dm": rel.endswith("702")}
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / f"{rel}.json").write_text(json.dumps(record), encoding="utf-8")


def stored(root):
    return sorted(str(p.relative_to(root).with_suffix("")).replace(os.sep, "/") for p in root.rglob("*.json"))


def deleted(*ids):
    return sorted(set(STORED) - {rel for rel in STORED if any(f"/{i}" in rel for i in ids)})


def raw_thread(thread_id, parent_id):
    import discord
    return discord.RawThreadDeleteEvent({"id": thread_id, "type": 11, "guild_id": GUILD, "parent_id": parent_id})


def test_a_deletion_on_discord_removes_exactly_its_local_records(tmp):
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    import discord
    rows = [  # handler, its payload, the records left behind, a removed and a surviving reply target, a removed channel
        ("raw_message_delete", lambda: discord.RawMessageDeleteEvent({"id": 701, "channel_id": C1}), deleted(701),
         701, 702, None),
        ("raw_bulk_message_delete", lambda: discord.RawBulkMessageDeleteEvent({"ids": [701, 702], "channel_id": C1}),
         deleted(701, 702), 702, None, None),
        ("raw_thread_delete", lambda: raw_thread(C1, C2), deleted(701, 702, C1), 701, None, C1),
        ("guild_channel_delete", lambda: types.SimpleNamespace(id=C2), deleted(703, 704, C2), 703, 702, C2)]
    for event, payload, expected, gone, live, channel in rows:
        left, heard, answers = delete_on_discord(Path(tmp, event), event, payload(), {"gone": gone, "live": live})
        assert left == expected, (event, left)
        assert not channel or not list(Path(tmp, event, "mailbox").glob(f"*/{channel}")), event
        assert len(heard) == 1, (event, heard)
        assert "not found in local caches" in answers["gone"].get("error", ""), (event, answers)
        assert live is None or answers["live"].get("channel_id") == str(C1), (event, answers)


def delete_on_discord(root, event, payload, replies):
    heard = []
    root.mkdir()

    async def script(client):
        seed(root / "mailbox")

        async def extension_handler(*args):
            heard.append(args)
        client.ext_ctx.on(event, extension_handler)
        await getattr(client, f"on_{event}")(payload)
        await client.ask({name: {"op": "send", "to": "nobody", "subject": "s", "body": "b", "reply_to": str(msg)}
                          for name, msg in replies.items() if msg})
    client = _gateway.run(root, script, extension=True)
    return stored(root / "mailbox"), heard, client.responses


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="retention_")


if __name__ == "__main__":
    raise SystemExit(main())
