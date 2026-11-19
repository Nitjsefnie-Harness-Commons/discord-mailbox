#!/usr/bin/env python3
"""The member gate: a configured server's members reach the connector only from its list."""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE, BOT, GUILD, T0  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _delivery as D  # noqa: E402

GATED, USERS = "DISCORD_MAILBOX_GATED_GUILDS", "DISCORD_MAILBOX_ALLOWED_USER_IDS"
RAW, MALLORY, OUTSIDER, FLAKY = (940000000000000010 + i for i in range(4))
G2, OTHER, ABSENT = 950000000000000011, 950000000000000012, 950000000000000013
DM, REFUSED, RECEIPT = None, "❎", "✅"


def _discord():
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    import discord  # pylint: disable=import-outside-toplevel
    return discord


def _guild(guild_id, members, lookups):
    """A guild whose member fetch answers like Discord's: the member, 404 for anyone else, 500 for FLAKY."""
    discord = _discord()

    async def fetch_member(user_id):
        lookups.append((guild_id, user_id))
        if user_id == FLAKY:
            raise discord.HTTPException(types.SimpleNamespace(status=500, reason="Server Error"), "down")
        if user_id not in members:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"),
                                   {"code": 10007, "message": "Unknown Member"})
        return types.SimpleNamespace(id=user_id)
    return types.SimpleNamespace(id=guild_id, fetch_member=fetch_member)


def _client(lookups, members=(RAW, MALLORY, ALICE), g2_members=(ALICE,)):
    guilds = {GUILD: _guild(GUILD, members, lookups), G2: _guild(G2, g2_members, lookups)}
    return types.SimpleNamespace(get_guild=guilds.get)


def _msg(author, guild, content="hello\nthere", msg_id=1, reactions=None, react_error=None):
    async def add_reaction(emoji):
        if react_error:
            raise react_error
        reactions.append(emoji)
    return types.SimpleNamespace(id=msg_id, content=content, author=types.SimpleNamespace(id=author),
                                 channel=types.SimpleNamespace(id=960000000000000001), add_reaction=add_reaction,
                                 guild=None if guild is None else types.SimpleNamespace(id=guild))


def _judge(gates, msg, client, now=T0):
    return asyncio.run(gates.judge(msg, False, {}, client, now=now))


def _gates(gated, users=(), rate=None):
    logs = []
    return D._DeliveryGates(frozenset(users), frozenset(), rate, logs.append, gated), logs


# --- the setting --------------------------------------------------------------

def test_the_setting_parses_and_an_invalid_value_names_it(tmp):
    assert D.parse_gated(GATED, None) == {}
    assert D.parse_gated(GATED, f"{GUILD}:{ALICE},{RAW}") == {GUILD: {ALICE, RAW}}
    assert D.parse_gated(GATED, f" {GUILD} : {ALICE} , ; {G2}: ;") == {GUILD: {ALICE}, G2: frozenset()}
    for raw in ("1", ":2", "a:2", "1:x", "1:2;1:3", ";", " ; ", "1:2 3", "1;2", "²:1", "1:2:3", "-1:2"):
        try:
            D.parse_gated(GATED, raw)
        except ValueError as e:
            assert GATED in str(e), e
        else:
            raise AssertionError(f"{raw!r} was accepted")


# --- the gate -------------------------------------------------------------------

def test_the_gate_judges_every_place_and_every_lookup_answer(tmp):
    one, two = {GUILD: frozenset({ALICE})}, {GUILD: frozenset({ALICE}), G2: frozenset()}
    rows = [  # gated, author, written in, delivered, lookups
        (one, RAW, GUILD, False, []), (one, ALICE, GUILD, True, []), (one, ALICE, DM, True, []),
        (one, ALICE, OTHER, True, []),
        (one, MALLORY, DM, False, [(GUILD, MALLORY)]), (one, MALLORY, OTHER, False, [(GUILD, MALLORY)]),
        (one, OUTSIDER, DM, True, [(GUILD, OUTSIDER)]), (one, OUTSIDER, OTHER, True, [(GUILD, OUTSIDER)]),
        (one, FLAKY, DM, True, [(GUILD, FLAKY)]),
        ({ABSENT: frozenset()}, RAW, DM, True, []),
        # Each configured server is judged by its own list; the one written in is judged first, with no lookup.
        (two, ALICE, GUILD, False, [(G2, ALICE)]), (two, RAW, GUILD, False, []),
        (two, OUTSIDER, GUILD, False, []), (two, ALICE, G2, False, []),
        (two, MALLORY, G2, False, []), (two, OUTSIDER, DM, True, [(GUILD, OUTSIDER), (G2, OUTSIDER)]),
        (two, FLAKY, DM, True, [(GUILD, FLAKY), (G2, FLAKY)]),
        ({GUILD: frozenset({ALICE}), G2: frozenset({RAW})}, RAW, G2, False, [(GUILD, RAW)]),
    ]
    for gated, author, place, delivered, expected in rows:
        lookups, reactions = [], []
        gates, logs = _gates(gated)
        refused = _judge(gates, _msg(author, place, reactions=reactions), _client(lookups))
        row = (sorted(gated), author, place)
        assert refused is not delivered and lookups == expected, (row, refused, lookups)
        assert reactions == ([] if delivered else [REFUSED]), (row, reactions)
        refusals = [line for line in logs if "not delivered" in line]
        assert len(refusals) == (not delivered) and all(
            line.startswith(f"not delivered: 1 from user {author}: member gate: a member of guild ")
            and line.endswith(f" not on its {GATED} list; body 'hello there'") for line in refusals), (row, logs)
        failed = [line for line in logs if "lookup" in line]
        assert len(failed) == (author == FLAKY or ABSENT in gated) * len(gated), (row, logs)
        assert all(str(author) in line for line in failed), logs
        assert ABSENT not in gated or "LookupError: the bot is not in that guild" in failed[0], logs


def test_a_refusal_logs_a_short_one_line_excerpt_and_survives_a_failed_reaction(tmp):
    gates, logs = _gates({GUILD: frozenset()})
    msg = _msg(RAW, GUILD, content="x" * 50 + "\n" + "y" * 200, react_error=RuntimeError("gone"))
    assert _judge(gates, msg, _client([]))
    assert logs[0].endswith(f"body {'x' * 50 + ' ' + 'y' * 9!r}"), logs
    assert len(logs) == 2 and "react failed on 1" in logs[1] and "gone" in logs[1], logs


def test_a_lookup_answer_is_cached_for_the_ttl_and_a_failure_is_not(tmp):
    lookups = []
    gates, _ = _gates({GUILD: frozenset()})
    client = _client(lookups)
    for author, now in ((OUTSIDER, T0), (OUTSIDER, T0 + D.MEMBER_TTL - 1), (MALLORY, T0 + 1), (MALLORY, T0 + 2),
                        (FLAKY, T0 + 3), (FLAKY, T0 + 4), (OUTSIDER, T0 + D.MEMBER_TTL)):
        _judge(gates, _msg(author, DM, reactions=[]), client, now=now)
    assert lookups == [(GUILD, a) for a in (OUTSIDER, MALLORY, FLAKY, FLAKY, OUTSIDER)], lookups
    assert set(gates._members) == {(GUILD, OUTSIDER), (GUILD, MALLORY)}, gates._members
    _judge(gates, _msg(ALICE, DM, reactions=[]), client, now=T0 + 3 * D.MEMBER_TTL)
    assert set(gates._members) == {(GUILD, ALICE)}, "expired answers were kept"


def test_the_other_gates_judge_first_and_a_member_refusal_spends_no_cap(tmp):
    lookups, reactions = [], []
    gates, logs = _gates({GUILD: frozenset()}, users=(MALLORY,), rate=(1, 3600))
    client = _client(lookups)
    assert _judge(gates, _msg(OUTSIDER, DM, reactions=reactions), client)
    assert lookups == [] and reactions == [] and "allow-list" in logs[-1], (lookups, logs)
    assert _judge(gates, _msg(MALLORY, DM, reactions=reactions), client) and reactions == [REFUSED]
    gates._members.clear()
    lookups.clear()
    client = _client(lookups, members=())
    assert not _judge(gates, _msg(MALLORY, DM, reactions=reactions), client, now=T0 + 1), "the refusal spent the cap"
    assert _judge(gates, _msg(MALLORY, DM, reactions=reactions), client, now=T0 + 2)
    assert reactions == [REFUSED] and "rate limit" in logs[-1], (reactions, logs)


def test_without_the_setting_nothing_is_looked_up_or_refused(tmp):
    for gated in ({}, None):
        lookups, reactions = [], []
        gates = D._DeliveryGates(frozenset(), frozenset(), None, [].append, gated)
        for author, place in ((RAW, GUILD), (MALLORY, DM), (FLAKY, OTHER)):
            assert not _judge(gates, _msg(author, place, reactions=reactions), _client(lookups))
        assert lookups == [] and reactions == [], (lookups, reactions)


# --- the real connector -------------------------------------------------------

def _run(tmp, env, script, members=(RAW, MALLORY, ALICE, BOT)):
    lookups, other = [], types.SimpleNamespace(id=OTHER, name="other", text_channels=[])

    async def driven(client):
        client.guild.fetch_member = _guild(GUILD, members, lookups).fetch_member
        await script(client, other)

    client = _gateway.run(tmp, driven, env={GATED: "", USERS: "", **env}, extension=True, others=(other,))
    events = [int(e["msg_id"]) for e in client.events if e.get("event") == "message"]
    return _gateway.inbox(tmp), events, client.reactions, lookups, client


def test_the_connector_refuses_a_gated_member_wherever_they_write(tmp):
    async def script(client, other):
        on_message, dm, k = client.handlers["on_message"], client.dm(970000000000000001), client
        await on_message(k.message(301, RAW, "in the server", k.bridge, mentions=(BOT,)))
        await on_message(k.message(302, ALICE, "allowed in the server", k.bridge, mentions=(BOT,)))
        await on_message(k.message(303, MALLORY, "a dm", dm))
        await on_message(k.message(304, MALLORY, "another server", k.bridge, guild=other, mentions=(BOT,)))
        await on_message(k.message(305, OUTSIDER, "not a member", dm))
        await on_message(k.message(306, FLAKY, "lookup fails", dm))
        client.relayed = await client.ext_ctx.deliver(k.message(307, MALLORY, "relayed", dm))

    inbox, events, reactions, lookups, client = _run(tmp, {GATED: f"{GUILD}:{ALICE}"}, script)
    assert inbox == {302, 305, 306} and events == [302, 305, 306], (inbox, events)
    assert reactions == [(301, REFUSED), (302, RECEIPT), (303, REFUSED), (304, REFUSED), (305, RECEIPT),
                         (306, RECEIPT), (307, REFUSED)], reactions
    assert lookups == [(GUILD, a) for a in (MALLORY, OUTSIDER, FLAKY)], lookups
    assert client.relayed is None
    for msg_id, author, body in ((301, RAW, "in the server"), (303, MALLORY, "a dm"), (307, MALLORY, "relayed")):
        assert f"not delivered: {msg_id} from user {author}: member gate: " in client.log, client.log
        assert f"body {body!r}" in client.log
    assert client.log.count("member gate:") == 5 and f"lookup of user {FLAKY} in guild {GUILD} failed" in client.log


def test_the_connector_delivers_only_what_every_gate_passes(tmp):
    async def script(client, other):
        on_message, dm, k = client.handlers["on_message"], client.dm(970000000000000001), client
        await on_message(k.message(401, MALLORY, "on the allow-list, gated", dm))
        await on_message(k.message(402, RAW, "neither list", dm))
        await on_message(k.message(403, OUTSIDER, "allow-listed non-member", dm))

    inbox, events, reactions, lookups, client = _run(
        tmp, {GATED: f"{GUILD}:{ALICE}", USERS: f"{MALLORY},{OUTSIDER}"}, script)
    assert inbox == {403} and events == [403], (inbox, events)
    assert reactions == [(401, REFUSED), (403, RECEIPT)], reactions
    assert lookups == [(GUILD, MALLORY), (GUILD, OUTSIDER)], lookups
    assert "not delivered: 402 " in client.log and "allow-list" in client.log


def test_an_unconfigured_connector_delivers_a_members_messages_as_before(tmp):
    async def script(client, other):
        on_message, dm, k = client.handlers["on_message"], client.dm(970000000000000001), client
        await on_message(k.message(501, RAW, "in the server", k.bridge, mentions=(BOT,)))
        await on_message(k.message(502, MALLORY, "a dm", dm))
        await on_message(k.message(503, MALLORY, "another server", k.bridge, guild=other, mentions=(BOT,)))

    inbox, events, reactions, lookups, client = _run(tmp, {}, script)
    assert inbox == {501, 502, 503} and events == [501, 502, 503], (inbox, events)
    assert reactions == [(501, RECEIPT), (502, RECEIPT), (503, RECEIPT)] and lookups == [], (reactions, lookups)
    assert "member gate" not in client.log and "not delivered" not in client.log


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="member_gate_")


if __name__ == "__main__":
    raise SystemExit(main())
