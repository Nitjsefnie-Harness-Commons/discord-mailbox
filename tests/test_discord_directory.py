#!/usr/bin/env python3
"""The agent directory: what an entry names, who holds a name, and how much of
the channel each read sees.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import BOT, T0  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib._directory import _Directory as D  # noqa: E402

M, L = D.MAP_SEPARATORS, D.LIST_SEPARATORS
PROBE, MALLORY, PLAIN, RAW = 980000000000000002, 980000000000000003, 980000000000000004, 980000000000000005
CONFLICT = f"directory: 'probe' is held by user {PROBE}; ignoring the later entry from user {MALLORY}"


def test_an_entry_names_its_first_mention_and_bold_token_or_separated_word(tmp):
    rows = [  # content, map result, list-agents result
        ("**alpha** · <@11>", ("alpha", 11), ("alpha", 11)),
        ("<@!11> then **alpha** and **beta**", ("alpha", 11), ("alpha", 11)),
        ("**alpha** <@11> <@22>", ("alpha", 11), ("alpha", 11)),
        ("**alpha**", None, None),
        (None, None, None),
        ("** ** <@11>", None, None),
        ("alpha: <@11>", ("alpha", 11), ("alpha", 11)),
        ("\n  - alpha = x\n<@11>", ("alpha", 11), ("alpha", 11)),
        ("no separator here\nalpha: <@11>", ("alpha", 11), ("alpha", 11)),
        (": empty\nalpha: <@11>", ("alpha", 11), ("alpha", 11)),
        # The one difference: only the map splits on the header's ' · '.
        ("alpha · <@11>", ("alpha", 11), None),
    ]
    for content, mapped, listed in rows:
        assert D.parse_entry(content, M) == mapped, (content, D.parse_entry(content, M))
        assert D.parse_entry(content, L) == listed, (content, D.parse_entry(content, L))


def test_a_name_is_held_once_and_never_as_a_user_prefix(tmp):
    del tmp
    logs = []
    # A holder repeating its own entry is no conflict; `user:` is refused for a bound entry only.
    entries = [("probe", PROBE, PROBE), ("probe", MALLORY, MALLORY), ("probe", PROBE, PROBE),
               ("user:eve", PROBE, MALLORY), ("USER:eve", MALLORY, MALLORY), ("user", PROBE, PROBE)]
    assert D(logs.append).bind_names(entries) == {"probe": PROBE, "user": PROBE}, "a `user:` name was bound"
    assert logs[:1] == [CONFLICT] and len(logs) == 2 and "USER:eve" in logs[1] and str(PROBE) not in logs[1], logs


# --- the real connector -----------------------------------------------------

_msg, _inbox = _gateway.message, _gateway.inbox


def _run(tmp, entries, script, outbox=False):
    """-> (client, connector log); skipped without discord.py."""
    client = _gateway.run(tmp, script, entries, identity="dir-test", outbox=outbox)
    return client, client.log


def _send(to, **fields):
    return {"op": "send", "to": to, "subject": "s", "body": "b", **fields}


def test_a_newer_entry_from_another_account_does_not_take_a_name(tmp):
    entries = [_msg(6, PLAIN, f"**{RAW}** · <@{PLAIN}>"),         # newest first; an all-digit name stays RAW's id
               _msg(5, PLAIN, f"plain · <@{PLAIN}>"),             # the map alone splits on ' · '
               _msg(4, MALLORY, f"**probe** · <@{MALLORY}>"),
               _msg(3, MALLORY, f"**helper** · <@{PROBE}>"),
               _msg(2, PROBE, f"**probe** · <@{PROBE}>"),
               _msg(1, BOT, f"**dir-test** · <@{BOT}>"),
               _msg(0, MALLORY, f"**probe** · <@{MALLORY}>")]
    entries[-1].pinned = True                                     # skipped by every read

    async def script(client):
        on_message = client.handlers["on_message"]
        await on_message(_msg(101, MALLORY, f"<@{BOT}> hi", client.bridge, mentions=(BOT,)))
        await on_message(_msg(102, PROBE, f"<@{BOT}> hi", client.bridge, mentions=(BOT,)))
        await client.ask({"mention": _send("probe"), "dm": _send("probe", dm=True),
                          "helper": _send("helper"), "plain": _send("plain"), "raw": _send(str(RAW)), "list": {"op": "list-agents"}})

    client, log = _run(tmp, entries, script)
    assert _inbox(tmp, 101)["from"] == f"user:user{MALLORY}", _inbox(tmp, 101)
    assert _inbox(tmp, 102)["from"] == "probe", _inbox(tmp, 102)
    r = client.responses
    mentioned = sorted(s.split(" ", 1)[0] for s in client.bridge.sent)     # the watcher's order is free
    assert r["mention"]["ok"] and r["plain"]["ok"] and r["raw"]["ok"] and mentioned == [f"<@{PROBE}>", f"<@{PLAIN}>", f"<@{RAW}>"], (r, mentioned)
    assert r["dm"]["ok"] and list(client.dms) == [PROBE] and client.dms[PROBE].sent, (r, client.dms)
    assert not r["helper"]["ok"] and "'helper'" in r["helper"]["error"], r["helper"]
    assert [(a["identity"], a["user_id"], a["author_id"]) for a in r["list"]["agents"]] == [
        (str(RAW), str(PLAIN), str(PLAIN)), ("probe", str(MALLORY), str(MALLORY)), ("helper", str(PROBE), str(MALLORY)),
        ("probe", str(PROBE), str(PROBE)), ("dir-test", str(BOT), str(BOT))], r["list"]
    conflicts = [line for line in log.splitlines() if "held by" in line]
    assert len(conflicts) == 1 and conflicts[0].endswith(CONFLICT), log


def test_every_directory_read_sees_entries_older_than_its_newest_messages(tmp):
    entries = [_msg(1000 + i, MALLORY, f"chatter {i}") for i in range(300)] + [
        _msg(2, PROBE, f"**probe** · <@{PROBE}>"), _msg(1, BOT, f"**dir-test** · <@{BOT}>\n\nprofile")]

    async def script(client):
        await client.ask({"send": _send("probe"), "list": {"op": "list-agents"},
                          "register": {"op": "register", "body": "new profile"}})

    client, _ = _run(tmp, entries, script)
    r = client.responses
    assert set(client.directory.reads) == {None}, client.directory.reads
    assert r["send"]["ok"], r["send"]
    assert [a["identity"] for a in r["list"]["agents"]] == ["probe", "dir-test"], r["list"]
    assert r["register"] == {"op": "register", "ok": True, "action": "edited", "msg_id": "1"}, r
    assert client.directory.sent == [], "self-registration or register posted a second entry"
    assert client.directory.edits == [(1, f"**dir-test** · <@{BOT}>\n\nnew profile")]


def test_a_miss_rescans_only_once_the_last_scan_is_old_but_startup_always_scans(tmp):
    entries = [_msg(1, PROBE, f"**probe** · <@{PROBE}>")]       # no entry of its own yet

    async def script(client):
        reads, client.steps = client.directory.reads, []
        assert len(reads) == 2 and len(client.directory.sent) == 1, reads  # both forced at T0
        await client.handlers["on_ready"]()                               # a reconnect scans too
        assert len(reads) == 3 and len(client.directory.sent) == 1, reads
        client.directory.msgs.insert(0, _msg(9, MALLORY, f"**carol** · <@{MALLORY}>", client.directory))
        for t, name, payload, fail in (
                (14.9, "early", _send("carol"), False), (15, "due", _send("carol"), False),
                (15, "cached", _send("carol"), False), (29.9, "dm-early", _send("ghost", dm=True), False),
                (30, "dm-due", _send("ghost", dm=True), False), (100, "failed", _send("ghost"), True),
                (100, "probe-kept", _send("probe", dm=True), True),
                (100, "after-failure", _send("ghost"), False)):
            client.clock[0], client.directory.fail = T0 + t, fail
            await client.ask({name: payload})
            client.steps.append((name, client.responses[name]["ok"], len(reads)))

    client, _ = _run(tmp, entries, script)
    # A failed scan completed nothing, so the next miss scans again.
    assert client.steps == [("early", False, 3), ("due", True, 4), ("cached", True, 4),
                            ("dm-early", False, 4), ("dm-due", False, 5), ("failed", False, 6),
                            ("probe-kept", True, 6), ("after-failure", False, 7)], client.steps


def test_an_outbox_send_is_retried_while_the_last_scan_is_fresh_and_dropped_after_a_real_miss(tmp):
    box, log = Path(tmp) / "state" / "outbox", Path(tmp) / "connector.log"

    async def until(done):                               # the watcher ticks once a second
        for _ in range(200):
            if done():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out: {log.read_text(encoding='utf-8')}")

    async def script(client):
        reads, client.steps = client.directory.reads, []
        client.directory.msgs.insert(0, _msg(9, MALLORY, f"**carol** · <@{MALLORY}>", client.directory))
        for t, files in ((1, {"dm": _send("carol", dm=True), "mention": _send("carol")}),
                         (15, {}), (100, {"ghost": _send("ghost")})):
            client.clock[0] = T0 + t
            for name, payload in files.items():
                (box / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
            if t == 1:                                   # startup scanned at T0: the miss reuses that scan
                await until(lambda: log.read_text(encoding="utf-8").count("send retry") == 2)
                client.steps.append((sorted(p.stem for p in box.iterdir()), len(reads)))
            else:
                await until(lambda: not any(box.iterdir()))
                client.steps.append((len(reads), len(client.bridge.sent), len(client.dms[MALLORY].sent)))

    client, _ = _run(tmp, [_msg(1, PROBE, f"**probe** · <@{PROBE}>")], script, outbox=True)
    failed = [(e["to"], e["error"]) for e in client.events if e["event"] == "send_failed"]
    # Kept inside the window; a due scan finds the name and both go out; a real scan that still misses drops.
    assert client.steps == [(["dm", "mention"], 2), (3, 1, 1), (4, 1, 1)], client.steps
    assert failed == [("ghost", "unknown recipient identities ['ghost']")], failed


def test_startup_keeps_the_entry_the_bot_wrote_though_an_older_account_holds_its_name(tmp):
    async def script(client):
        pass

    client, _ = _run(tmp, [_msg(2, BOT, f"**dir-test** · <@{BOT}>"),        # newest first
                           _msg(1, MALLORY, f"**dir-test** · <@{MALLORY}>")], script)
    assert len(client.directory.reads) == 1 and client.directory.sent == [], (client.directory.reads, client.directory.sent)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="directory_")


if __name__ == "__main__":
    raise SystemExit(main())
