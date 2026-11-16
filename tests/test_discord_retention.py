#!/usr/bin/env python3
"""Local message records follow deletions on Discord, on the REAL connector."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE, BOT, BRIDGE, GUILD  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
MB = os.path.join(_util.SCRIPTS, "discord_mb.py")

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
        os.utime(root / f"{rel}.json", (0, 0))   # an age rule would remove all of them


def stored(root):   # os.walk lists a link but never descends through one
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")[:-5]
                  for d, _, files in os.walk(root) for f in files if f.endswith(".json"))


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


async def deliver_during_delete(client, msg_id, held):
    """Deliver `msg_id` and delete it on Discord while `held` ("pins" or "reaction") is awaited."""
    import discord
    reached, release = asyncio.Event(), asyncio.Event()

    async def hold():
        reached.set()
        await release.wait()

    async def pins(limit=50):
        await hold()
        for pinned in ()[:limit]:
            yield pinned

    async def reaction(emoji):
        await hold()
        raise RuntimeError(f"404 Unknown Message: {emoji}")   # a deleted message takes no reaction
    msg = client.message(msg_id, ALICE, f"<@{BOT}> hi", client.bridge, mentions=(BOT,))
    if held == "pins":
        client.bridge.pins = pins
    else:
        msg.add_reaction = reaction
    task = asyncio.ensure_future(client.on_message(msg))
    await reached.wait()
    await client.on_raw_message_delete(discord.RawMessageDeleteEvent({"id": msg_id, "channel_id": BRIDGE}))
    release.set()
    await task
    vars(client.bridge).pop("pins", None)


def test_a_message_deleted_while_its_record_is_written_leaves_no_record(tmp):
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    from discord_mb_lib import connector
    inbox = Path(tmp, "mailbox", "gate-test", "inbox")

    async def script(client):
        inbox.mkdir(parents=True)
        connector.inbox_dir = lambda identity: inbox   # the inbox the deletion handler sweeps
        for msg_id, held in ((801, "pins"), (802, "reaction")):
            await deliver_during_delete(client, msg_id, held)
        await client.on_message(client.message(803, ALICE, f"<@{BOT}> kept", client.bridge, mentions=(BOT,)))
    _gateway.run(tmp, script)
    assert sorted(p.name for p in inbox.iterdir()) == ["803.json"], list(inbox.iterdir())


def mb(tmp, *argv):
    env = {**os.environ, **dict.fromkeys(("TMPDIR", "TEMP", "TMP"), tmp)}
    return subprocess.run([sys.executable, MB, *argv], capture_output=True, text=True, env=env, check=False)


def test_erase_removes_every_record_the_author_wrote_and_nothing_else(tmp):
    from discord_mb_lib import _temp_provenance
    root, outside, saved = Path(tmp, "discord-mailbox"), Path(tmp, "outside"), tempfile.tempdir
    try:
        tempfile.tempdir = tmp
        _temp_provenance.ensure_owned_temp_dir(root, "shared")   # the root `send` accepts
    finally:
        tempfile.tempdir = saved
    seed(root)
    seed(outside)
    (root / "one/outbox").mkdir()
    (root / "one/outbox/705.json").write_text(json.dumps({"from_user_id": str(A)}), encoding="utf-8")
    links = {"three": "two", f"messages/{C1 + 7}": f"messages/{C1}", "one/inbox/709.json": "one/inbox/703.json"}
    for link, target in links.items() if os.name != "nt" else ():
        os.symlink(outside / target, root / link)
    for refused in ("12a", "", "١٢", f" {A}"):
        assert mb(tmp, "erase", "--user-id", refused).returncode == 2, refused
    done = mb(tmp, "erase", "--user-id", str(A))
    assert (done.returncode, json.loads(done.stdout)) == (0, {"user_id": str(A), "removed": 7}), done
    kept = [rel for rel in STORED if rel.endswith(("702", "704"))] + ["one/outbox/705"]
    assert stored(root) == sorted(kept + (["one/inbox/709"] if os.name != "nt" else [])), stored(root)
    assert stored(outside) == sorted(STORED), stored(outside)
    for reply_to in ("702", "701"):
        assert mb(tmp, "send", "one", "nobody", "s", "b", "--reply-to", reply_to).returncode == 0
    sent = sorted((json.loads(p.read_text(encoding="utf-8")) for p in (root / "one/outbox").glob("*-*.json")),
                  key=lambda m: m["reply_to"])
    assert [(m.get("reply_channel_id"), m.get("is_dm")) for m in sent] == [(None, None), (str(C1), True)], sent
    if os.name != "nt":
        before = stored(root)
        Path(tmp, "linked").mkdir()
        os.symlink(root, Path(tmp, "linked", "discord-mailbox"))
        assert mb(str(Path(tmp, "linked")), "erase", "--user-id", str(B)).returncode == 1
        assert stored(root) == before, stored(root)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="retention_")


if __name__ == "__main__":
    raise SystemExit(main())
