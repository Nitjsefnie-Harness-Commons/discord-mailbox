#!/usr/bin/env python3
"""Secrets off the command line: token files, the token order, the stored password.

Tokens and passwords are random values built at runtime; failure messages name them, never print them.
"""
import contextlib
import io
import os
import secrets
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _secrets, cli, core  # noqa: E402


class _Tty(io.StringIO):
    def isatty(self):
        return True


@contextlib.contextmanager
def _environ(**values):
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            os.environ.pop(name)
            if value is not None:
                os.environ[name] = value


def _cli(argv, stdin=None, env=""):
    """The real parser. -> (what reached the command, stdout, stderr, prompts); `seen["exit"]` holds a SystemExit
    code. The connector entry records only the token the CLI hands it; the connector's own steps are tested below."""
    seen, out, err, prompts = {}, io.StringIO(), io.StringIO(), []

    class App:
        def __init__(self, identity, **kwargs):
            seen["connector"] = kwargs["token"]

        def run(self):
            pass

    import discord
    saved = (sys.argv, sys.stdin, cli.ConnectorApp, discord.Client, core._meta_request, _secrets.getpass)
    cli.ConnectorApp, _secrets.getpass = App, lambda prompt: prompts.append(prompt) or "typed"
    discord.Client = lambda **_: types.SimpleNamespace(event=lambda f: f, run=lambda t, **_: seen.update(setup=t))
    core._meta_request = lambda _i, payload, _t: seen.update(creds=payload) or {"ok": True, "action": "added", "msg_id": "5"}
    sys.argv, sys.stdin = ["discord_mb.py", *argv], stdin or io.StringIO()
    try:
        with _environ(DISCORD_TOKEN=env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli._cli()
    except SystemExit as e:
        seen["exit"] = e.code
    finally:
        sys.argv, sys.stdin, cli.ConnectorApp, discord.Client, core._meta_request, _secrets.getpass = saved
    return seen, out.getvalue(), err.getvalue(), prompts


def _value():
    return secrets.token_urlsafe(24)


def _hide(detail, names):
    """A failure message with every generated value replaced by its name."""
    text = repr(detail)
    for value, name in names.items():
        text = text.replace(value, f"<{name}>")
    return text


def _warned_once(err, flag, value):
    lines = err.splitlines()
    return len(lines) == 1 and lines[0].startswith("warning: ") and flag in lines[0] and value not in err


def test_the_cli_takes_the_flag_then_the_file_and_only_the_flag_warns(tmp):
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    flag, held, env = (_value() for _ in range(3))
    good, blank, binary, missing = (str(Path(tmp) / f"{n}.token") for n in ("good", "blank", "binary", "missing"))
    Path(good).write_text(f" \t{held} \n", encoding="utf-8-sig")       # a leading byte-order mark
    Path(blank).write_text(" \n\t", encoding="utf-8")
    Path(binary).write_bytes(b"\xff" + held.encode())                    # not UTF-8
    both, names = ["--token", flag, "--token-file", good], {flag: "--token", held: "--token-file", env: "env"}
    for argv, key, want in ((["connector", "agent", *both], "connector", flag),
                            (["connector", "agent", "--token-file", good], "connector", held),
                            (["connector", "agent"], "connector", None),
                            (["connector", "agent", "--token-file", blank], "exit", blank),
                            (["connector", "agent", "--token-file", binary], "exit", binary),
                            (["connector", "agent", "--token-file", missing], "exit", missing),
                            (["setup", *both], "setup", flag),
                            (["setup", "--token-file", good], "setup", held),
                            (["setup"], "setup", env)):
        seen, out, err, _ = _cli(argv, env=env)
        got = seen.get(key)
        if key == "exit":
            assert want in got and held not in got and env not in got, _hide((argv, got), names)
        else:
            assert key in seen and got == want, _hide((argv, seen), names)
        assert out == "", _hide((argv, out), names)
        if "--token" in argv:
            assert _warned_once(err, "--token", flag), _hide((argv, err), names)
        else:
            assert err == "", _hide((argv, err), names)


def test_a_given_token_beats_discord_token_which_beats_the_directory(tmp):
    given, env, stored = (_value() for _ in range(3))
    (Path(tmp) / "agent.token").write_text(stored, encoding="utf-8")
    names, saved, core.TOKEN_DIR = {given: "given", env: "env", stored: "directory"}, core.TOKEN_DIR, Path(tmp)
    try:
        for token, env_value, flavor, want in ((given, env, None, (given, "claude")),
                                               (None, env, "codex", (env, "codex")),
                                               (None, "", "kimi", (stored, "kimi"))):
            with _environ(DISCORD_TOKEN=env_value):
                got = _secrets.connector_token(token, "agent", flavor, core.resolve_token_and_flavor)
            assert got == want, _hide((token, env_value, flavor, got), names)
    finally:
        core.TOKEN_DIR = saved


def test_the_real_connector_reads_a_patched_token_directory_with_its_flavor(tmp):
    """discord_mb.py keeps module patches reaching the connector; TOKEN_DIR is one of them."""
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    m, root = _util.load(os.path.join(_util.SCRIPTS, "discord_mb.py"), "secrets_facade"), Path(tmp)
    (root / "agent.token").write_text(_value(), encoding="utf-8")

    class Reached(Exception):
        """The connector got past its token check."""

    def reached(log):
        raise Reached(log)
    m.TOKEN_DIR, m.state_dir, m._DeliveryGates = root, lambda _: root, types.SimpleNamespace(from_settings=reached)
    for identity, env, flavor in (("agent", "", "kimi"), ("unlisted", _value(), "codex")):
        try:
            with _environ(DISCORD_TOKEN=env, DISCORD_MB_TEST_LOCK_ROOT=str(root / "locks")), \
                    contextlib.redirect_stderr(io.StringIO()):
                m.connector_main(identity, claude_pid=os.getpid(), token=None, log_path=root / f"{identity}.log",
                                 flavor=flavor)
        except Reached:
            pass
        except SystemExit as e:
            raise AssertionError(f"{identity}: the connector found no token (exit {e.code})") from None
        assert f"identity flavor: {flavor}\n" in (root / f"{identity}.log").read_text(encoding="utf-8"), identity


def test_the_password_comes_from_the_argument_stdin_or_the_prompt_never_two(tmp):
    del tmp
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    pw, add = _value(), ["creds", "me", "add", "d.example", "login"]
    names = {pw: "password"}
    for extra, stdin, want, prompted, warned in (
            ([pw], None, pw, False, True),
            (["--password-stdin"], io.StringIO(f" {pw} \n\n"), f" {pw} \n", False, False),
            (["--password-stdin"], _Tty(pw), pw, False, False),
            ([], _Tty(""), "typed", True, False),
            ([], io.StringIO(pw), ("--password-stdin", "password argument"), False, False),
            ([pw, "--password-stdin"], io.StringIO(pw), ("--password-stdin", "not both"), False, False),
            ([""], None, ("empty",), False, True)):
        seen, out, err, prompts = _cli([*add, *extra], stdin)
        if isinstance(want, tuple):     # refused: nothing is sent, the error names what to do
            assert "creds" not in seen and all(w in seen["exit"] for w in want), _hide((extra, seen), names)
            assert pw not in seen["exit"] and out == "", _hide((extra, out), names)
        else:
            assert seen["creds"]["password"] == want and out == "added: 5\n", _hide((extra, seen, out), names)
        assert bool(prompts) == prompted, (extra, prompts)
        if warned:
            assert _warned_once(err, "password", pw), _hide((extra, err), names)
        else:
            assert err == "", _hide((extra, err), names)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="secrets_")


if __name__ == "__main__":
    raise SystemExit(main())
