#!/usr/bin/env python3
"""discord_mb.py usage-status board: pace colours, ETA, period bucket, leases.

The board publishes Claude/Kimi/Codex rate-limit utilization into
voice-channel names. Everything asserted here is a PURE function — no gateway, no network —
so the suite is offline and safe to run anywhere. The live end of the feature
(renaming a real channel) is exercised by running a connector, not by tests.

Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
# pylint: disable=subprocess-run-check
# Every subprocess.run below is a probe whose exit status is the
# assertion; check=True would raise before the test could read it.
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")


def _mb():
    """Import discord_mb, or None when discord.py is absent on this host.

    A consumer box that never runs a connector has no discord.py, and a missing
    optional dependency must read as "not applicable here", not as a failure.
    """
    try:
        return _util.load(MB, "discord_mb_under_test")
    except ImportError:
        return None


# --------------------------------------------------------------------------
# colours: pace, not level
# --------------------------------------------------------------------------

def test_pace_colours(_tmp):
    mb = _mb()
    if mb is None:
        return
    assert mb.pace_dot(40.0, 35.9) == "🔴", "over pace must be red"
    assert mb.pace_dot(0.0, 2.5) == "🟢", "under pace must be green"
    assert mb.pace_dot(36.3, 35.9) == "🟡", "+0.4pp is inside the band"
    assert mb.pace_dot(34.9, 35.9) == "🟡", "-1.0pp is the band edge"
    assert mb.pace_dot(37.0, 35.9) == "🔴", "+1.1pp is outside the band"
    assert mb.pace_dot(40.0, None) == "⚪", "unknown pace must not read as bad"
    assert mb.pace_dot(None, 1.0) == "⚪"


# --------------------------------------------------------------------------
# the ETA on a red window
# --------------------------------------------------------------------------

def test_duration_parsing(_tmp):
    mb = _mb()
    if mb is None:
        return
    assert mb.duration_secs("6h54m") == 6 * 3600 + 54 * 60
    assert mb.duration_secs("4d11h") == 4 * 86400 + 11 * 3600
    assert mb.duration_secs("45m") == 2700
    assert mb.duration_secs("soon") is None, "unparseable must not become 0"
    assert mb.duration_secs(None) is None


def test_coarse_duration(_tmp):
    """Biggest unit only, remainder ALWAYS up, minutes the floor unit."""
    mb = _mb()
    if mb is None:
        return
    assert mb.coarse_duration(8 * 3600 + 7 * 60) == "9h"
    assert mb.coarse_duration(2 * 3600) == "2h", "exact hours must not inflate"
    assert mb.coarse_duration(4 * 86400 + 11 * 3600) == "5d"
    assert mb.coarse_duration(3 * 86400) == "3d"
    assert mb.coarse_duration(40 * 60 + 20) == "41m"
    assert mb.coarse_duration(45) == "1m", "sub-minute floors to a minute"
    # rounding that fills the unit is promoted, not printed as 60m / 24h
    assert mb.coarse_duration(59 * 60 + 1) == "1h"
    assert mb.coarse_duration(23 * 3600 + 30 * 60) == "1d"
    assert mb.coarse_duration(None) is None


def test_recovery_picks_the_nearer_clock(_tmp):
    """Whichever ends the red first: pace catching up, or the window resetting."""
    mb = _mb()
    if mb is None:
        return
    assert mb.recovery_label(
        {"recover_in": "6h54m", "resets_in": "4d11h"}) == "7h"
    # a reset that lands first is the real answer -- advertising a 3-day
    # recovery on a window that rolls over in 2h is a wait that never happens
    assert mb.recovery_label(
        {"recover_in": "3d2h", "resets_in": "2h10m"}) == "3h"
    # mixed units: the shorter wait is not the smaller string, so this is
    # decided numerically
    assert mb.recovery_label(
        {"recover_in": "1h50m", "resets_in": "5h"}) == "2h"
    assert mb.recovery_label({"recover_in": None, "resets_in": "40m"}) == "40m"
    assert mb.recovery_label({}) is None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_render_usage_name(_tmp):
    mb = _mb()
    if mb is None:
        return
    both = {"five_hour": {"pct": 5.0, "pace_pct": 9.0, "resets_in": "4h31m"},
            "weekly": {"pct": 41.0, "pace_pct": 36.0,
                       "recover_in": "6h54m", "resets_in": "4d11h"}}
    name = mb.render_usage_name("claude", both)
    assert name == "claude · 5h 🟢 5% · 7d 🔴 41% →🟢 7h", name
    assert len(name) <= 100, "Discord caps a channel name at 100 chars"
    # the provider word must survive the rename or the next pass cannot find
    # the channel it just renamed
    assert "claude" in name.lower()

    green = {"five_hour": {"pct": 5.0, "pace_pct": 9.0, "resets_in": "4h31m"}}
    assert mb.render_usage_name("claude", green) == "claude · 5h 🟢 5%", \
        "green has nothing to wait for"
    yellow = {"weekly": {"pct": 1.0, "pace_pct": 0.2, "recover_in": "1h20m"}}
    assert mb.render_usage_name("kimi", yellow) == "kimi · 7d 🟡 1%"
    # red with no clock at all still shows its colour rather than blanking
    assert mb.render_usage_name(
        "claude", {"weekly": {"pct": 90.0, "pace_pct": 10.0}}) == \
        "claude · 7d 🔴 90%"


def test_render_returns_none_without_data(_tmp):
    """No trustworthy number -> None, so the caller leaves the channel alone."""
    mb = _mb()
    if mb is None:
        return
    assert mb.render_usage_name("kimi", {"five_hour": {}, "weekly": {}}) is None
    assert mb.render_usage_name("kimi", None) is None
    assert mb.render_usage_name("kimi", {}) is None


def test_usage_fetch_keeps_partial_results_on_provider_failure(tmp):
    """A missing optional Codex login must not erase Claude/Kimi status."""
    mb = _mb()
    if mb is None:
        return
    # Installed as a wheel there is no sibling helper, so point the resolver at
    # one; without it fetch_usage returns early and never reaches the mock.
    helper = Path(tmp) / "usage_query.py"
    helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    os.environ["DISCORD_MB_USAGE_QUERY"] = str(helper)
    response = SimpleNamespace(
        returncode=1,
        stdout=json.dumps({"usage": {"claude": {"weekly": {"pct": 12}}},
                           "errors": {"codex": "not logged in"}}),
        stderr="")
    try:
        with mock.patch.object(mb.subprocess, "run", return_value=response):
            got = mb.fetch_usage()
    finally:
        os.environ.pop("DISCORD_MB_USAGE_QUERY", None)
    assert got == {"claude": {"weekly": {"pct": 12}}}


# --------------------------------------------------------------------------
# cadence: the period bucket and the per-guild claim
# --------------------------------------------------------------------------

def test_period_bucket_is_absolute_time(_tmp):
    """The same integer on every machine — this is what syncs hosts."""
    mb = _mb()
    if mb is None:
        return
    now = 1785300000.0
    assert mb.usage_period(now) == int(now // mb.USAGE_STATUS_INTERVAL)
    assert mb.usage_period(now + 299) == mb.usage_period(now)
    assert mb.usage_period(now + 300) == mb.usage_period(now) + 1


def test_claim_slot_is_per_guild_and_monotonic(tmp):
    mb = _mb()
    if mb is None:
        return
    now = 1785300000.0
    p = mb.usage_period(now)
    assert mb.claim_usage_slot(p, guild_id=111, root=tmp, now=now) is True
    assert mb.claim_usage_slot(p, guild_id=111, root=tmp, now=now) is False, \
        "a second identity on this box must not re-run the same period"
    # another guild is independent: the identities here are separate bot users
    # in different sets of servers
    assert mb.claim_usage_slot(p, guild_id=222, root=tmp, now=now) is True
    assert mb.claim_usage_slot(p + 1, guild_id=111, root=tmp,
                               now=now + 300) is True, "next period is claimable"
    # a backwards clock step must not re-grant a window already published
    assert mb.claim_usage_slot(p, guild_id=111, root=tmp, now=now) is False
    _, lock = mb.usage_gate_paths(tmp, 111)
    assert not lock.exists(), "claim lock must be released"


def test_orphaned_claim_lock_is_stolen(tmp):
    """A killed process mid-claim must not wedge the updater forever."""
    mb = _mb()
    if mb is None:
        return
    now = 1785300000.0
    _, lock = mb.usage_gate_paths(tmp, 333)
    lock.write_text("stale")
    old = time.time() - mb.USAGE_STATUS_LOCK_TTL - 5
    os.utime(lock, (old, old))
    assert mb.claim_usage_slot(mb.usage_period(now), guild_id=333, root=tmp,
                               now=now) is True


# --------------------------------------------------------------------------
# the cross-host lease
# --------------------------------------------------------------------------

def test_lease_round_trip_and_expiry(_tmp):
    mb = _mb()
    if mb is None:
        return
    import calendar
    now = calendar.timegm(time.strptime("2026-07-29 13:25:30",
                                        "%Y-%m-%d %H:%M:%S"))
    name = mb.render_claim_name("analyst@host-a", now)
    assert name == "claim · analyst@host-a · 13:25Z", name
    assert "claim" in name.lower(), "lease must stay findable after the rename"

    owner, age = mb.parse_claim_name(name, now)
    assert owner == "analyst@host-a"
    assert age < mb.USAGE_CLAIM_REFRESH, "a just-written lease is fresh"

    # past refresh, inside TTL: the owner re-stamps, everyone else defers
    o2, a2 = mb.parse_claim_name(
        mb.render_claim_name("agent_dev@host-b", now - 12 * 60), now)
    assert o2 == "agent_dev@host-b"
    assert mb.USAGE_CLAIM_REFRESH < a2 < mb.USAGE_CLAIM_TTL

    # past TTL: the owner is presumed dead and the lease is takeable
    _, a3 = mb.parse_claim_name(
        mb.render_claim_name("gone@somehost", now - 20 * 60), now)
    assert a3 > mb.USAGE_CLAIM_TTL


def test_lease_day_rollover(_tmp):
    """23:55Z read at 00:05Z is 10 minutes old, not 23h50m."""
    mb = _mb()
    if mb is None:
        return
    import calendar
    at = calendar.timegm(time.strptime("2026-07-30 00:05:00",
                                       "%Y-%m-%d %H:%M:%S"))
    _, age = mb.parse_claim_name("claim · analyst@box · 23:55Z", at)
    assert 590 <= age <= 610, age


def test_non_lease_names_are_not_leases(_tmp):
    """A status channel must never be mistaken for the lease channel."""
    mb = _mb()
    if mb is None:
        return
    assert mb.parse_claim_name("General") == (None, None)
    assert mb.parse_claim_name("") == (None, None)
    assert mb.parse_claim_name("claude · 5h 🟢 1% · 7d 🔴 41%") == (None, None)


def test_codex_is_a_board_provider(_tmp):
    """Codex belongs on the board on the same terms as Claude and Kimi.

    usage_query already reports Codex under the same `five_hour`/`weekly` keys
    the board renders, but the provider tuple listed only Claude and Kimi, so a
    `codex` voice channel was never matched and a Codex account's burn was
    invisible beside the other two (issue #221).
    """
    mb = _mb()
    if mb is None:
        return
    assert "codex" in mb.USAGE_PROVIDERS, (
        "codex is not a usage board provider, so its channel is never matched")

    # The renderer is provider-agnostic; prove it for codex rather than
    # assuming the name is only ever a label.
    block = {"five_hour": {"pct": 2.0, "pace_pct": 40.0},
             "weekly": {"pct": 12.0, "pace_pct": 50.0}}
    assert mb.render_usage_name("codex", block) == "codex · 5h 🟢 2% · 7d 🟢 12%"


def test_the_provider_tuple_is_the_only_board_control_point(_tmp):
    """Adding a provider to the tuple must be enough to give it a channel.

    `usage_guild_targets` is a closure in the connector's run scope, so it
    cannot be called without a live gateway. Pin instead that it matches by
    iterating USAGE_PROVIDERS -- that is what makes the tuple the whole switch,
    and a hardcoded provider there would silently strand the new entry.
    """
    source = (Path(_util.SCRIPTS) / "discord_mb_lib" / "connector.py").read_text(
        encoding="utf-8")
    body = source.split("def usage_guild_targets", 1)
    assert len(body) == 2, "connector no longer resolves usage channels here"
    matcher = body[1].split("return targets, claim", 1)[0]
    assert "for p in USAGE_PROVIDERS" in matcher, (
        "usage channels are not matched from USAGE_PROVIDERS, so adding a "
        "provider to that tuple does not give it a board")
    for hardcoded in ("'claude'", '"claude"', "'kimi'", '"kimi"'):
        assert hardcoded not in matcher, (
            f"usage channel matching hardcodes {hardcoded} beside the tuple")


# --------------------------------------------------------------------------
# staleness: a board that cannot be refreshed must stop asserting
# --------------------------------------------------------------------------

LIVE = "claude · 5h 🟢 1% · 7d 🟢 43%"
READING = {"weekly": {"pct": 68.0, "pace_pct": 65.0}}


def test_a_single_dataless_period_leaves_the_board_alone(_tmp):
    """One usage_query timeout must not blank a board that will be right again
    in five minutes — the figure is only a few minutes old at that point."""
    mb = _mb()
    if mb is None:
        return
    name, misses, marked = mb.usage_board_plan("claude", None, LIVE)
    assert name is None, f"one empty period already renamed the board to {name!r}"
    assert (misses, marked) == (1, False)


def test_a_board_with_no_reading_stops_asserting_a_figure(_tmp):
    """Issue #10: the board kept publishing its last numbers indefinitely.

    Observed live: `7d 🟢 43%` stayed up for over twelve hours while the account
    was actually at 68%, and nothing in the name said the reading was old.
    """
    mb = _mb()
    if mb is None:
        return
    misses, marked, live = 0, False, LIVE
    for _ in range(mb.USAGE_STALE_PERIODS - 1):
        name, misses, marked = mb.usage_board_plan("claude", None, live,
                                                   misses, marked)
        assert name is None, "blanked before the threshold"
    name, misses, marked = mb.usage_board_plan("claude", None, live, misses,
                                               marked)
    assert name == mb.render_stale_usage_name("claude"), (
        f"a board with no reading for {mb.USAGE_STALE_PERIODS} periods still "
        f"asserts a figure: {name!r}")
    assert marked is True


def test_the_stale_name_is_published_once_per_episode(_tmp):
    """The bound on a disagreement between hosts.

    A host whose fetch is broken while another host's still works would blank a
    board the working host immediately republishes. Announcing once per episode
    caps that at a single exchange instead of two renames every period, which
    would sit outside Discord's 2-per-10-minutes budget.
    """
    mb = _mb()
    if mb is None:
        return
    stale = mb.render_stale_usage_name("claude")
    # already announced, and another host has since put a figure back up
    name, _, marked = mb.usage_board_plan("claude", None, LIVE,
                                          mb.USAGE_STALE_PERIODS, True)
    assert name is None, f"re-announced over a live figure: {name!r}"
    assert marked is True
    # and it does not rewrite a name the channel already carries
    name, _, marked = mb.usage_board_plan("claude", None, stale,
                                          mb.USAGE_STALE_PERIODS, False)
    assert name is None, "renamed a channel to the name it already had"
    assert marked is True, "an already-stale board must count as announced"


def test_a_reading_ends_the_episode_and_re_arms_the_marker(_tmp):
    """Data coming back is what re-arms it — otherwise the second outage of a
    connector's life would go unannounced."""
    mb = _mb()
    if mb is None:
        return
    stale = mb.render_stale_usage_name("claude")
    name, misses, marked = mb.usage_board_plan(
        "claude", READING, stale, mb.USAGE_STALE_PERIODS, True)
    assert name == "claude · 7d 🔴 68%", name
    assert (misses, marked) == (0, False), "the episode did not end"
    # ...and the next outage is announced again
    for _ in range(mb.USAGE_STALE_PERIODS - 1):
        name, misses, marked = mb.usage_board_plan("claude", None, name,
                                                   misses, marked)
        assert name is None
    name, _, _ = mb.usage_board_plan("claude", None, "claude · 7d 🔴 68%",
                                     misses, marked)
    assert name == stale, "the marker did not re-arm after a reading"


def test_a_live_figure_that_needs_no_rename_is_not_stale(_tmp):
    """Nothing to write is not the same as nothing to read."""
    mb = _mb()
    if mb is None:
        return
    current = mb.render_usage_name("claude", READING)
    name, misses, marked = mb.usage_board_plan("claude", READING, current, 2,
                                               False)
    assert name is None, "rewrote a name the channel already carried"
    assert (misses, marked) == (0, False), (
        "a board showing a current figure was left counted as stale")


def test_staleness_is_decided_per_provider(_tmp):
    """usage_query returns partial results — Claude and Kimi on a box with no
    Codex login. One provider dropping out must not blank the others."""
    mb = _mb()
    if mb is None:
        return
    data = {"claude": READING}
    n_claude, _, _ = mb.usage_board_plan("claude", data.get("claude"), LIVE,
                                         mb.USAGE_STALE_PERIODS, False)
    n_codex, _, _ = mb.usage_board_plan("codex", data.get("codex"),
                                        "codex · 7d 🟢 15%",
                                        mb.USAGE_STALE_PERIODS, False)
    assert n_claude == "claude · 7d 🔴 68%", n_claude
    assert n_codex == mb.render_stale_usage_name("codex"), n_codex


def test_the_stale_name_carries_no_figure_and_stays_matchable(_tmp):
    """It has to survive the round trip: the channel is found again by the
    provider word in whatever this code last renamed it to."""
    mb = _mb()
    if mb is None:
        return
    for provider in mb.USAGE_PROVIDERS:
        name = mb.render_stale_usage_name(provider)
        assert provider in name.lower(), (
            f"{name!r} no longer matches its own provider, so the board can "
            "never come back")
        assert "%" not in name, f"the stale name still asserts a figure: {name!r}"
        assert not any(ch.isdigit() for ch in name), name
        assert "claim" not in name.lower(), (
            f"{name!r} would be mistaken for the lease channel")
        assert mb.parse_claim_name(name) == (None, None)
        assert len(name) <= 100, "Discord caps a channel name at 100 characters"


def test_an_empty_fetch_no_longer_ends_the_pass(_tmp):
    """`update_usage_status` is a closure in the connector's run scope and
    cannot be called without a live gateway, so pin the shape instead.

    It used to `return` the moment `fetch_usage` came back empty, which is what
    left every board holding its last figures. The empty read has to reach the
    per-channel planning below it.
    """
    source = (Path(_util.SCRIPTS) / "discord_mb_lib" / "connector.py").read_text(
        encoding="utf-8")
    body = source.split("async def update_usage_status", 1)
    assert len(body) == 2, "the connector no longer publishes the board here"
    body = body[1].split("async def usage_status_watcher", 1)[0]
    empty = body.split("if not data:", 1)
    assert len(empty) == 2, "the empty-read branch is gone"
    branch = empty[1].split("pending = []", 1)[0]
    # Statements only: a comment saying the branch does not return is not the
    # branch not returning, and the two must not be able to satisfy each other.
    branch = "\n".join(line for line in branch.splitlines()
                       if not line.lstrip().startswith("#"))
    assert "return" not in branch, (
        "an empty usage read still ends the pass, so every board keeps the "
        "figures it was last given")
    assert "usage_board_plan(" in body, (
        "the connector does not consult usage_board_plan, so nothing decides "
        "when a board has gone stale")


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="usagestatus_")


if __name__ == "__main__":
    sys.exit(main())
