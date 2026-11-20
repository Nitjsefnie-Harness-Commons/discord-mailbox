#!/usr/bin/env python3
"""discord_mb.py connector lock: a PID is not an identity.

A lock that records a PID and validates it with "does this PID exist" is not
enough. After a crash the number goes back in the pool and the OS reissues it,
so the lock can point at an unrelated system process. The identity is then
locked out of its own mailbox and `leech` attaches to the corpse, tailing an
events file that will never grow — an outage that looks healthy from outside.

These pin the identity check that closes it, and just as importantly its
failure direction: when the cmdline cannot be read the answer is "unknown", and
unknown must behave as "still a connector". Guessing "stale" starts a SECOND
connector on one identity, which is worse than the stale lock.

Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
import os
import subprocess
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


def _claim_module():
    """The connector's ownership claim, or None when discord.py is absent."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        from discord_mb_lib import connector
    except ImportError:
        return None
    return connector


def test_only_lock_contention_reads_as_already_running(tmp):
    """A lock file that cannot be opened is not another connector."""
    c = _claim_module()
    if c is None:
        return
    pidfile = Path(tmp) / "connector.pid"
    held = c._ConnectorOwnership(pidfile)
    try:
        owner, refusal = c._claim_ownership("agent", pidfile)
        assert owner is None and "already running" in refusal, refusal
    finally:
        held.close()

    real = c._ConnectorOwnership
    calls = []

    def denied_once(path):
        calls.append(path)
        if len(calls) == 1:
            raise c._OwnershipAccessError("cannot use ownership lock: Permission denied")
        return real(path)

    # The recorded owner is gone: clean up its stale state, then claim.
    pidfile.write_text("4194000\n")             # above any default pid_max
    stale_lock = Path(tmp) / ".connector.pid.lock"
    stale_lock.write_bytes(b"\0")
    c._ConnectorOwnership = denied_once
    try:
        owner, refusal = c._claim_ownership("agent", pidfile)
    finally:
        c._ConnectorOwnership = real
    assert owner is not None and refusal is None, refusal
    assert not pidfile.exists(), "the dead owner's PID file survived cleanup"
    owner.close()

    # No readable record of an owner: report the access error, delete nothing.
    pidfile.unlink(missing_ok=True)
    calls.clear()
    c._ConnectorOwnership = lambda path: (_ for _ in ()).throw(
        c._OwnershipAccessError("cannot use ownership lock: Permission denied"))
    try:
        owner, refusal = c._claim_ownership("agent", pidfile)
    finally:
        c._ConnectorOwnership = real
    assert owner is None and "Permission denied" in refusal, refusal
    assert "already running" not in refusal and "not another connector" in refusal


def test_recycled_pid_is_not_a_connector(tmp):
    """The PID now belongs to an unrelated service."""
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

    # Mirrors open(path, mode='r', ...), which is the signature it stands
    # in for.
    # pylint: disable-next=keyword-arg-before-vararg
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


def _child(code, home, *args, **env):
    """Run `code` in a child whose home is `home` and whose lock-root names are only those in `env`."""
    names = ("DISCORD_MB_TEST_LOCK_ROOT", "DISCORD_MAILBOX_LOCK_ROOT")
    child_env = {k: v for k, v in os.environ.items() if k not in names}
    child_env.update(HOME=str(home), USERPROFILE=str(home), **env)
    argv = [sys.executable, "-c", "import sys\nsys.path.insert(0, sys.argv[1])\n" + code, _util.SCRIPTS, *map(str, args)]
    return subprocess.run(argv, env=child_env, capture_output=True, text=True, check=False)


def test_lock_root_names_resolve_at_both_read_sites(tmp):
    """The prefixed name wins over the old one, blank counts as unset, and the default is under the home."""
    new, old = "DISCORD_MAILBOX_LOCK_ROOT", "DISCORD_MB_TEST_LOCK_ROOT"
    home, a, b = (str(Path(tmp) / name) for name in ("home", "a", "b"))
    probe = ("import importlib, discord_mb, discord_mb_lib.storage as s\nprint(s._ConnectorOwnership._IDENTITY_LOCK_ROOT)\n"
             "importlib.reload(discord_mb)\nprint(discord_mb._ConnectorOwnership._IDENTITY_LOCK_ROOT)")
    default = str(Path(home) / ".discord-mailbox-log-locks")
    for env, want in (({new: a}, a), ({old: b}, b), ({new: a, old: b}, a), ({new: "", old: b}, b),
                      ({new: a, old: ""}, a), ({new: "", old: ""}, default), ({}, default)):
        run = _child(probe, home, **env)
        assert run.stdout.splitlines() == [want, want], (env, run.stdout, run.stderr)


def test_connector_lock_needs_no_writable_home(tmp):
    """The setting places the lock root where the default cannot be made (a file as home); drives storage's read (`_ConnectorOwnership._IDENTITY_LOCK_ROOT`) and the log writer."""
    (home := Path(tmp) / "home").write_text("", encoding="utf-8")
    code = "import discord_mb\ndiscord_mb._ConnectorLogWriter(sys.argv[2], max_bytes=64, backup_count=1).close()"
    log = Path(tmp) / "connector.log"
    refused = _child(code, home, log)
    assert refused.returncode != 0 and "cannot establish connector lock root" in refused.stderr, refused.stderr
    placed = _child(code, home, log, DISCORD_MAILBOX_LOCK_ROOT=str(Path(tmp) / "locks"))
    assert placed.returncode == 0, placed.stderr


def main():
    return _util.runner(_util.collect(globals()), "connlock_")


if __name__ == "__main__":
    raise SystemExit(main())
