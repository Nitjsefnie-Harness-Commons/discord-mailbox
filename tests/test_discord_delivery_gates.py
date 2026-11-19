#!/usr/bin/env python3
"""Delivery gates: continuation frames, the allow-lists, the per-sender cap."""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE, BOT, GUILD, T0  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _delivery as D  # noqa: E402

RAW, MALLORY, OTHER = 940000000000000003, 940000000000000004, 940000000000000005
OTHER_GUILD = 950000000000000002
CHAN, OTHER_CHAN = 960000000000000001, 960000000000000002
DIRECTORY = {"bridge-bot": BOT, "alice": ALICE}
CHUNK1 = f"<@{BOT}> **[probe→alpha]** report\nfirst part"
CHUNK2 = "**[probe→alpha]** report (2/3)\nsecond part"
ENV = ("DISCORD_MAILBOX_ALLOWED_USER_IDS", "DISCORD_MAILBOX_ALLOWED_GUILD_IDS",
       "DISCORD_MAILBOX_SENDER_RATE_LIMIT", "DISCORD_MAILBOX_GATED_GUILDS")


def _msg(author, content="hi", guild=GUILD, channel=CHAN, msg_id=1):
    return types.SimpleNamespace(id=msg_id, content=content, author=types.SimpleNamespace(id=author),
                                 channel=types.SimpleNamespace(id=channel),
                                 guild=None if guild is None else types.SimpleNamespace(id=guild))


def _gates(users=(), guilds=(), rate=None):
    logs = []
    return D._DeliveryGates(frozenset(users), frozenset(guilds), rate, logs.append), logs


# --- settings ---------------------------------------------------------------

def test_settings_parse_and_an_invalid_value_names_its_setting(tmp):
    assert D.parse_ids("X", None) == frozenset()
    assert D.parse_ids("X", " 11, 22 ,33,") == {11, 22, 33}
    assert D.parse_rate("X", None) is None
    assert D.parse_rate("X", " 3/60 ") == (3, 60)
    for raw in ("abc", "1;2", ",", "1,x", "-5", "²", "1 2"):
        _refused(lambda raw=raw: D.parse_ids(ENV[0], raw), ENV[0])
    for raw in ("0/60", "3/0", "3", "3/60s", "a/b", "-1/5", "3/60/2"):
        _refused(lambda raw=raw: D.parse_rate(ENV[2], raw), ENV[2])


def _refused(call, name):
    try:
        call()
    except ValueError as e:
        assert name in str(e), e
    else:
        raise AssertionError(f"{name}: an invalid value was accepted")


# --- allow-lists ------------------------------------------------------------

def test_the_allow_lists_combine_by_or_and_a_dm_passes_only_by_user(tmp):
    rows = [  # users, guilds, author, guild, delivered
        ((), (), RAW, None, True),
        ((ALICE,), (), ALICE, None, True), ((ALICE,), (), RAW, None, False),
        ((), (GUILD,), RAW, GUILD, True), ((), (GUILD,), RAW, OTHER_GUILD, False),
        ((), (GUILD,), RAW, None, False),
        ((ALICE,), (GUILD,), ALICE, OTHER_GUILD, True), ((ALICE,), (GUILD,), RAW, GUILD, True),
        ((ALICE,), (GUILD,), RAW, OTHER_GUILD, False),
        # Each list is read for its own field only: an id in the other list never admits.
        ((GUILD,), (), RAW, GUILD, False), ((), (ALICE,), ALICE, None, False),
    ]
    for users, guilds, author, guild, delivered in rows:
        gates, logs = _gates(users, guilds)
        refused = gates.refuses(_msg(author, guild=guild), False, DIRECTORY, now=T0)
        assert refused is not delivered, (users, guilds, author, guild)
        assert len(logs) == (0 if delivered else 1), logs
        assert delivered or ("allow-list" in logs[0] and str(author) in logs[0]), logs


# --- continuation frames ----------------------------------------------------

def _train(steps):
    """Run (author, content, continuation[, channel]) steps -> delivered flags."""
    gates, logs = _gates()
    out = []
    for author, content, continuation, *channel in steps:
        msg = _msg(author, content, channel=channel[0] if channel else CHAN)
        out.append(not gates.refuses(msg, continuation, DIRECTORY))
    return out, logs


def test_a_continuation_frame_needs_a_registered_author_or_the_chunk_one_author(tmp):
    rows = [  # steps, delivered flags
        ([(RAW, CHUNK2, True)], [False]),
        ([(ALICE, CHUNK2, True)], [True]),
        ([(RAW, CHUNK1, False), (RAW, CHUNK2, True)], [True, True]),
        ([(RAW, CHUNK1, False), (MALLORY, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace(" report", " report (1/3)"), False), (RAW, CHUNK2, True)], [True, True]),
        # Every key component separates trains: channel, sender, recipients, subject.
        ([(RAW, CHUNK1, False, OTHER_CHAN), (RAW, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace("probe→", "bridge→"), False), (RAW, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace("→alpha", "→alpha,beta"), False), (RAW, CHUNK2, True)], [True, False]),
        ([(RAW, CHUNK1.replace(" report", " other"), False), (RAW, CHUNK2, True)], [True, False]),
        # A delivered chunk 2+ frame is not a chunk 1, so it vouches for nothing.
        ([(RAW, CHUNK2, False), (RAW, CHUNK2.replace("(2/3)", "(3/3)"), True)], [True, False]),
        # The send path's other chunk-1 prefixes: a role mention (`all`) and several user mentions.
        ([(RAW, CHUNK1.replace("<@", "<@&"), False), (RAW, CHUNK2, True)], [True, True]),
        ([(RAW, CHUNK1.replace(" **", f" <@{ALICE}> **"), False), (RAW, CHUNK2, True)], [True, True]),
        # A chunk 1 for the key from a second author leaves no author vouching, for good;
        # the same author posting it again keeps vouching, and a registered sender is unaffected.
        ([(RAW, CHUNK1, False), (RAW, CHUNK1, False), (RAW, CHUNK2, True)], [True, True, True]),
        ([(RAW, CHUNK1, False), (MALLORY, CHUNK1, False), (RAW, CHUNK2, True), (MALLORY, CHUNK2, True)],
         [True, True, False, False]),
        ([(RAW, CHUNK1, False), (MALLORY, CHUNK1, False), (RAW, CHUNK1, False), (RAW, CHUNK2, True)],
         [True, True, True, False]),
        ([(RAW, CHUNK1, False), (MALLORY, CHUNK1, False), (ALICE, CHUNK2, True)], [True, True, True]),
    ]
    for steps, expected in rows:
        got, logs = _train(steps)
        assert got == expected, (steps, got)
    _, logs = _train([(RAW, CHUNK2, True)])
    assert len(logs) == 1 and "continuation" in logs[0], logs
    # A chunk 1 the cap refused was not delivered, so it vouches for nothing either.
    gates, _ = _gates(rate=(1, 60))
    got = [not gates.refuses(_msg(RAW, content), cont, DIRECTORY, now=now)
           for content, cont, now in (("hi", False, T0), (CHUNK1, False, T0 + 1), (CHUNK2, True, T0 + 61))]
    assert got == [True, False, False], got


def test_the_remembered_chunk_one_authors_are_bounded_oldest_first(tmp):
    gates, _ = _gates()

    def chunk(i, k):
        frame = f"**[probe→alpha]** part{i}"
        return _msg(RAW, frame if k == 1 else f"{frame} (2/2)")

    for i in range(D.TRAIN_MEMORY + 1):
        assert not gates.refuses(chunk(i, 1), False, DIRECTORY)
        if i == D.TRAIN_MEMORY - 1:
            assert not gates.refuses(chunk(0, 1), False, DIRECTORY)   # 0 is now newest
    assert not gates.refuses(chunk(0, 2), True, DIRECTORY)
    assert gates.refuses(chunk(1, 2), True, DIRECTORY), "the oldest train was kept"
    assert not gates.refuses(chunk(2, 2), True, DIRECTORY)


# --- per-sender cap ---------------------------------------------------------

def test_the_cap_counts_deliveries_per_sender_in_a_trailing_window(tmp):
    gates, logs = _gates(rate=(2, 60))

    def delivered(author, now):
        return not gates.refuses(_msg(author), False, DIRECTORY, now=now)

    assert [delivered(RAW, T0), delivered(RAW, T0 + 1), delivered(RAW, T0 + 2)] == [True, True, False]
    assert delivered(ALICE, T0 + 2), "one sender's cap refused another"
    assert not delivered(RAW, T0 + 59.9) and not delivered(RAW, T0 + 30)
    assert delivered(RAW, T0 + 60), "a delivery leaves the window after `seconds`"
    assert not delivered(RAW, T0 + 60.5)
    assert len(logs) == 1 and "rate limit" in logs[0] and str(RAW) in logs[0], logs
    assert delivered(RAW, T0 + 61) and not delivered(RAW, T0 + 61.9) and len(logs) == 1
    assert not delivered(RAW, T0 + 62) and len(logs) == 2, "logged again once a window passed"


def test_a_message_refused_by_another_gate_does_not_spend_the_cap(tmp):
    gates, _ = _gates(rate=(1, 60))
    assert gates.refuses(_msg(RAW, CHUNK2), True, DIRECTORY, now=T0)
    assert not gates.refuses(_msg(RAW), False, DIRECTORY, now=T0 + 1)


def test_the_cap_forgets_senders_once_their_window_has_passed(tmp):
    gates, _ = _gates(rate=(1, 60))
    for author in range(1, 1001):
        gates.refuses(_msg(author), False, DIRECTORY, now=T0)
        gates.refuses(_msg(author), False, DIRECTORY, now=T0 + 1)    # refused and logged
    assert len(gates._delivered) == 1000 and len(gates._warned) == 1000
    gates.refuses(_msg(RAW), False, DIRECTORY, now=T0 + 61)
    assert list(gates._delivered) == [RAW] and gates._warned == {}, (
        len(gates._delivered), len(gates._warned))


# --- the real connector -----------------------------------------------------

def _run(tmp, env, script):
    """-> (inbox ids, message-event msg_ids, reacted msg_ids, log), client; the settings come from `env` alone."""
    client = _gateway.run(tmp, script, env={**dict.fromkeys(ENV, ""), **env}, extension=True)
    return (_gateway.inbox(tmp), [int(e["msg_id"]) for e in client.events if e.get("event") == "message"],
            [m for m, _ in client.reactions], client.log), client


def test_the_connector_applies_the_allow_lists_and_the_cap_to_both_writers(tmp):
    async def script(client):
        on_message, dm, k = client.handlers["on_message"], client.dm(970000000000000001), client
        await on_message(k.message(101, ALICE, "listed user", dm))
        await on_message(k.message(102, MALLORY, "unlisted user, no guild", dm))
        await on_message(k.message(103, ALICE, "second inside the window", dm))
        await on_message(k.message(104, MALLORY, "listed guild", k.bridge, mentions=(BOT,)))
        client.relayed = [await client.ext_ctx.deliver(k.message(m, a, "relayed", dm))
                          for m, a in ((105, OTHER), (106, RAW), (106, RAW), (107, RAW))]

    (inbox, events, reactions, log), client = _run(
        tmp, {ENV[0]: f"{ALICE},{RAW}", ENV[1]: str(GUILD), ENV[2]: "1/3600"}, script)
    assert inbox == {101, 104, 106}, inbox
    assert events == [101, 104, 106] and reactions == [101, 104, 106], (events, reactions)
    assert [p and Path(p).name for p in client.relayed] == [None, "106.json", "106.json", None], (
        client.relayed)
    assert log.count("allow-list") == 2 and log.count("rate limit") == 2, log
    assert "not delivered: 106 " not in log, "a duplicate was judged by the gates"


def test_a_continuation_reaches_the_inbox_from_the_chunk_one_author_only(tmp):
    async def script(client):
        on_message, k = client.handlers["on_message"], client
        chunk2 = CHUNK2.replace("alpha", "gate-test")
        await on_message(k.message(201, MALLORY, chunk2, k.bridge))
        await on_message(k.message(202, RAW, CHUNK1.replace("alpha", "gate-test"), k.bridge,
                                   mentions=(BOT,)))
        await on_message(k.message(203, RAW, chunk2, k.bridge))
        await on_message(k.message(204, MALLORY, chunk2.replace("(2/3)", "(3/3)"), k.bridge))
        await on_message(k.message(205, ALICE, chunk2.replace("report", "other"), k.bridge))
        await client.ext_ctx.deliver(k.message(206, MALLORY, chunk2, k.bridge))

    (inbox, events, reactions, log), _ = _run(tmp, {}, script)
    assert inbox == {202, 203, 205, 206}, inbox
    assert events == [202, 203, 205, 206] and reactions == events, (events, reactions)
    assert log.count("not delivered") == 2 and "continuation" in log, log


def test_an_invalid_setting_stops_the_connector_at_startup(tmp):
    async def script(client):
        raise AssertionError("the connector reached the gateway")

    for name in ENV:
        (Path(tmp) / name).mkdir()
        try:
            _run(Path(tmp) / name, {name: "10 per minute"}, script)
        except SystemExit as e:
            assert e.code == 1 and name in (Path(tmp) / name / "connector.log").read_text(encoding="utf-8"), e.code
        else:
            raise AssertionError(f"the connector started with an invalid {name}")


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="delivery_")


if __name__ == "__main__":
    raise SystemExit(main())
