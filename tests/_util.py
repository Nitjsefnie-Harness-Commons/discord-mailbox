"""Shared helpers for the bundle's source-only test suites.

Not a suite itself -- run_tests.py only loads `test_*.py`.

Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
import contextlib
import importlib.util
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
# This project IS the mailbox, so its "scripts" root resolves to its root.
SCRIPTS = str(_PROJECT_ROOT)
RENAMED = ("DISCORD_TOKEN", "DISCORD_MB_USAGE_QUERY", "DISCORD_MB_PORTABLE_PUBLICATION", "DISCORD_MB_BRIDGE_CHANNEL", "DISCORD_MB_DIRECTORY_CHANNEL",
           "DISCORD_MB_ATTACHMENTS_CHANNEL", "DISCORD_MB_CREDENTIALS_CHANNEL", "DISCORD_MB_BROADCAST_ROLE", "DISCORD_MB_META_CATEGORY")
for _name in (*RENAMED, *("DISCORD_MAILBOX_" + name for name in RENAMED)):    # a test sets one name, never meets the other
    os.environ.pop(_name, None)


def script(name):
    return str(_PROJECT_ROOT / name)


def load(path, name=None):
    """Import a script by path without running its __main__ block."""
    name = name or ("mod_" + os.path.splitext(os.path.basename(path))[0]
                    .replace("-", "_"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(call, answer):
    """Run `call()` with the connector's meta channel answering `answer` -> (exit code or None, stdout, stderr, (identity, payload) pairs sent)."""
    from discord_mb_lib import core
    sent, out, err, code = [], io.StringIO(), io.StringIO(), None
    stand_in = mock.patch.object(core, "_meta_request", lambda identity, payload, timeout: sent.append((identity, payload)) or answer)
    with stand_in, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            call()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue(), sent


POSIX_MODES = os.name != "nt"
"""False where st_mode carries no group/other bits worth asserting on.

Windows reports 0o666 for a file opened with 0o600, so a mode assertion there
is testing the C runtime, not this package. The code knows: _posix_mode_exposed
returns False on nt and confidentiality comes from the ACL work instead."""


class Skipped(Exception):
    """Raised by a test that cannot hold on this platform."""


def skip(reason):
    """End the running test as skipped, with a reason the log will show."""
    raise Skipped(reason)


def _assertion_site(exc):
    """`file:line: source` of the assert that failed, for bare asserts."""
    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.lineno}: {last.line}"


def runner(tests, tmp_prefix="hooktests_"):
    """Shared main(): run every callable, print PASS/FAIL, return exit code.

    Each test takes one argument: an isolated temp dir.

    The directory is handed over fully resolved. The connector operates on the
    realpath of everything it is given, so on a platform whose temporary root
    is itself a symlink -- macOS resolves /var to /private/var -- an unresolved
    directory makes every `writer_path == test_path` comparison in the suites
    false and silently disarms the failure injections built on them."""
    failed = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix=tmp_prefix) as tmp:
        tmp = os.path.realpath(tmp)
        for t in tests:
            d = os.path.join(tmp, t.__name__)
            os.makedirs(d, exist_ok=True)
            try:
                t(d)
                print(f"  PASS  {t.__name__}")
            except Skipped as e:
                skipped.append(t.__name__)
                print(f"  SKIP  {t.__name__}: {e}")
            except AssertionError as e:
                failed.append(t.__name__)
                # A bare `assert x == y` carries no message, which on a
                # platform you cannot run locally leaves nothing to go on.
                detail = str(e) or _assertion_site(e)
                print(f"  FAIL  {t.__name__}: {detail}")
            except Exception as e:  # noqa: BLE001
                failed.append(t.__name__)
                print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    passed = len(tests) - len(failed) - len(skipped)
    summary = f"\n{passed}/{len(tests)} passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    print(summary)
    return 1 if failed else 0


def collect(namespace):
    return [v for k, v in sorted(namespace.items())
            if k.startswith("test_") and callable(v)]
