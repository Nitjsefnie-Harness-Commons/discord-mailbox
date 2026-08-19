#!/usr/bin/env python3
"""discord_mb.py connector lock: a PID is not an identity.

The lock recorded a PID and validated it with "does this PID exist". After a
crash the number goes back in the pool and the OS reissues it, so three days
later the lock pointed at an unrelated system process. The identity was locked
out of its own mailbox ("already running (PID 6548)") and `leech` attached to
the corpse, tailing an events file that would never grow — an outage that looks
healthy from outside.

These pin the identity check that closes it, and just as importantly its
failure direction: when the cmdline cannot be read the answer is "unknown", and
unknown must behave as "still a connector". Guessing "stale" starts a SECOND
connector on one identity, which is worse than the stale lock.

Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
import os
import sys
from io import BytesIO, StringIO
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")

CONNECTOR = "/usr/bin/python3 /opt/mailbox/discord_mb.py connector agent_dev_kimi"
OTHER_IDENTITY = "/usr/bin/python3 /opt/mailbox/discord_mb.py connector agent_dev"
RECYCLED = "C:\\WINDOWS\\system32\\svchost.exe -k netsvcs -p -s WpnService"


def _mod():
    """discord_mb with pid_cmdline stubbable, or None when discord.py is absent."""
    try:
        return _util.load(MB, "mb_connector_lock")
    except ImportError:
        return None                      # no discord.py on this host


def _verdict(m, cmdline, identity):
    m.pid_cmdline = lambda pid: cmdline
    return m.is_connector_process(4242, identity)


def test_recycled_pid_is_not_a_connector(tmp):
    """The reported incident: the PID now belongs to an unrelated service."""
    m = _mod()
    if m is None:
        return
    assert _verdict(m, RECYCLED, "agent_dev_kimi") is False


def test_own_connector_is_recognised(tmp):
    """The lock must still hold against a genuinely running connector."""
    m = _mod()
    if m is None:
        return
    assert _verdict(m, CONNECTOR, "agent_dev_kimi") is True


def test_another_identitys_connector_does_not_hold_this_lock(tmp):
    m = _mod()
    if m is None:
        return
    assert _verdict(m, OTHER_IDENTITY, "agent_dev_kimi") is False


def test_identity_match_is_a_whole_argv_token(tmp):
    """`agent_dev` must not match the `agent_dev_kimi` connector.

    A substring test would have one identity silently holding another's lock —
    the same class of bug, one layer down."""
    m = _mod()
    if m is None:
        return
    assert _verdict(m, CONNECTOR, "agent_dev") is False


def test_unreadable_cmdline_is_unknown_not_stale(tmp):
    """None, never False — the call sites only clear a lock on a definite False.

    A wrong "stale" verdict starts a second connector on the same identity."""
    m = _mod()
    if m is None:
        return
    assert _verdict(m, None, "agent_dev_kimi") is None


def test_pid_cmdline_reads_this_process(tmp):
    """The reader works on a real PID on this platform — not just the stub."""
    m = _mod()
    if m is None:
        return
    cmd = m.pid_cmdline(os.getpid())
    if cmd is None:
        return                           # unreadable here; unknown is a valid answer
    assert "python" in cmd.lower(), cmd


def test_explicit_flavor_selects_behavior_with_shared_token(tmp):
    """Flavor selects connector behavior, while every harness shares auth."""
    m = _mod()
    if m is None:
        return
    shared = Path(tmp) / "discord"
    shared.mkdir()
    (shared / "worker.token").write_text("shared-token")
    m.TOKEN_DIR = shared
    m.KIMI_TOKEN_DIR = shared
    m.CODEX_TOKEN_DIR = shared

    assert m.resolve_token_and_flavor("worker", "claude") == \
        ("shared-token", "claude")
    assert m.resolve_token_and_flavor("worker", "kimi") == \
        ("shared-token", "kimi")
    assert m.resolve_token_and_flavor("worker", "codex") == \
        ("shared-token", "codex")

    (shared / "worker.token").unlink()
    assert m.resolve_token_and_flavor("worker", "kimi") == (None, "kimi"), \
        "an explicit flavor must survive a missing shared credential"


def test_parent_walk_parses_ppid_after_final_comm_paren(tmp):
    """Linux stat comm may contain spaces and parentheses. The PPID is field
    four after the *final* ')' delimiter, not split token 3."""
    m = _mod()
    if m is None:
        return

    files = {
        "/proc/100/cmdline": b"python3\0worker.py\0",
        "/proc/100/stat": "100 (worker (pool)) S 200 0 0 0\n",
        "/proc/200/cmdline": b"node\0/opt/apps/kimi-code/dist/main.mjs\0",
    }

    def fake_open(path, mode="r", *args, **kwargs):
        value = files[str(path)]
        return BytesIO(value) if "b" in mode else StringIO(value)

    old_platform = m.sys.platform
    sentinel = object()
    old_open = getattr(m, "open", sentinel)
    try:
        m.sys.platform = "linux"
        m.open = fake_open
        assert m.find_parent_pid_from(100, "kimi") == 200
    finally:
        m.sys.platform = old_platform
        if old_open is sentinel:
            del m.open
        else:
            m.open = old_open


def main():
    return _util.runner(_util.collect(globals()), "connlock_")


if __name__ == "__main__":
    raise SystemExit(main())
