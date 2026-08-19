"""Shared helpers for the bundle's source-only test suites.

Not a suite itself -- run_tests.py only loads `test_*.py`.

Everything here is read-only with respect to real state: hook subprocesses get
an isolated TMPDIR so per-session flag/dedup/cache files are never touched.
Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
import ast
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CLAUDE = os.path.expanduser("~/.claude")
AGENT_BUNDLE = os.path.expanduser("~/.agent-bundle")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
# This project IS the mailbox, so every "scripts" root resolves to its root.
# The names are kept so the ported suites need no edit.
HOOKS = str(_PROJECT_ROOT)
SCRIPTS = str(_PROJECT_ROOT)
SHARED_SOURCE = str(_PROJECT_ROOT)
CLAUDE_SOURCE = str(_PROJECT_ROOT)
KIMI_SOURCE = str(_PROJECT_ROOT)
CODEX_SOURCE = str(_PROJECT_ROOT)
HAVE_PROC = os.path.isdir("/proc/self")


def hook(name):
    return str(_PROJECT_ROOT / name)


def script(name):
    return str(_PROJECT_ROOT / name)


def load(path, name=None):
    """Import a hook/script by path without running its __main__ block."""
    name = name or ("mod_" + os.path.splitext(os.path.basename(path))[0]
                    .replace("-", "_"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def transcripts(limit=None, min_size=0):
    """Real session transcripts on this box, smallest first."""
    found = [p for p in glob.glob(os.path.join(CLAUDE, "projects", "**", "*.jsonl"),
                                  recursive=True)
             if os.path.getsize(p) >= min_size]
    found.sort(key=os.path.getsize)
    return found[-limit:] if limit else found


def payload(**kw):
    d = {"session_id": "hooktests", "cwd": "/root",
         "hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": "echo hi"}}
    d.update(kw)
    return json.dumps(d).encode()


def run(path, pl, tmpdir, extra_env=None, timeout=90):
    """Run a hook as a subprocess with isolated TMPDIR. -> (code, out, err)."""
    env = dict(os.environ)
    env["TMPDIR"] = tmpdir
    env.pop("BYPASS_CURL_HOOK", None)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, path], input=pl, capture_output=True,
                       env=env, timeout=timeout, check=False)
    return (r.returncode, r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def toplevel_imports(path):
    """Module-level imported names in `path` (NOT ones inside functions).

    Used to pin lazy-import wins: these hooks run on every tool call, and a
    heavy module quietly promoted back to module scope is a silent latency
    regression that nothing else would catch."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    names = set()
    for node in tree.body:                     # module level ONLY
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


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
