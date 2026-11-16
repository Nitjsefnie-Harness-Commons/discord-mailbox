#!/usr/bin/env python3
"""Secrets off the command line: token files, the token order, the stored password.

Each case runs the real parser. Tokens and passwords are built at runtime.
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


def _cli(argv, stdin=None, env=""):
    """-> (what reached the command, stdout, stderr, prompts); `seen["exit"]` holds a SystemExit code."""
    seen, out, err, prompts = {}, io.StringIO(), io.StringIO(), []

    class App:      # the connector resolves what it is given as below, then runs
        def __init__(self, identity, **kwargs):
            seen["connector"] = _secrets.connector_token(kwargs["token"], identity, kwargs["flavor"])

        def run(self):
            pass

    import discord
    saved = (sys.argv, sys.stdin, os.environ.get("DISCORD_TOKEN"), cli.ConnectorApp, discord.Client,
             core._meta_request, _secrets.getpass)
    cli.ConnectorApp, _secrets.getpass = App, lambda prompt: prompts.append(prompt) or "typed"
    discord.Client = lambda **_: types.SimpleNamespace(event=lambda f: f, run=lambda t, **_: seen.update(setup=t))
    core._meta_request = lambda _i, payload, _t: seen.update(creds=payload) or {"ok": True, "action": "added", "msg_id": "5"}
    sys.argv, sys.stdin, os.environ["DISCORD_TOKEN"] = ["discord_mb.py", *argv], stdin or io.StringIO(), env
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli._cli()
    except SystemExit as e:
        seen["exit"] = e.code
    finally:
        sys.argv, sys.stdin, prior, cli.ConnectorApp, discord.Client, core._meta_request, _secrets.getpass = saved
        os.environ.pop("DISCORD_TOKEN")
        if prior is not None:
            os.environ["DISCORD_TOKEN"] = prior
    return seen, out.getvalue(), err.getvalue(), prompts


def _secret():
    return secrets.token_urlsafe(24)


def _hide(detail, names):
    """A failure message with every secret replaced by its name."""
    text = repr(detail)
    for secret, name in names.items():
        text = text.replace(secret, f"<{name}>")
    return text


def _warned_once(err, flag, secret):
    lines = err.splitlines()
    return len(lines) == 1 and lines[0].startswith("warning: ") and flag in lines[0] and secret not in err


def test_each_token_source_wins_over_the_next_and_only_the_flag_warns(tmp):
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    flag, held, env, stored = (_secret() for _ in range(4))
    good, blank, binary, missing = (str(Path(tmp) / f"{n}.token") for n in ("good", "blank", "binary", "missing"))
    Path(good).write_text(f" \t{held} \n", encoding="utf-8-sig")       # a leading byte-order mark
    Path(blank).write_text(" \n\t", encoding="utf-8")
    Path(binary).write_bytes(b"\xff" + held.encode())                    # not UTF-8
    (Path(tmp) / "dir").mkdir()
    (Path(tmp) / "dir" / "agent.token").write_text(stored, encoding="utf-8")
    both, connector = ["--token", flag, "--token-file", good], ["connector", "agent"]
    names = {flag: "--token", held: "--token-file", env: "DISCORD_TOKEN", stored: "directory"}
    saved, core.TOKEN_DIR = core.TOKEN_DIR, Path(tmp) / "dir"
    try:
        for argv, env_value, key, want in (
                ([*connector, *both], env, "connector", (flag, "claude")),
                ([*connector, "--token-file", good, "--flavor", "codex"], env, "connector", (held, "codex")),
                (connector, env, "connector", (env, "claude")),
                ([*connector, "--flavor", "kimi"], "", "connector", (stored, "kimi")),
                ([*connector, "--token-file", blank], env, "exit", blank),
                ([*connector, "--token-file", binary], env, "exit", binary),
                ([*connector, "--token-file", missing], env, "exit", missing),
                (["setup", *both], env, "setup", flag),
                (["setup", "--token-file", good], env, "setup", held),
                (["setup"], env, "setup", env)):
            seen, out, err, _ = _cli(argv, env=env_value)
            got = seen.get(key)
            if key == "exit":
                assert want in got and held not in got and env not in got, _hide((argv, got), names)
            else:
                assert got == want, _hide((argv, seen), names)
            assert out == "", _hide((argv, out), names)
            assert _warned_once(err, "--token", flag) if "--token" in argv else err == "", _hide((argv, err), names)
    finally:
        core.TOKEN_DIR = saved


def test_the_password_comes_from_the_argument_stdin_or_the_prompt_never_two(tmp):
    del tmp
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    pw, add = _secret(), ["creds", "me", "add", "d.example", "login"]
    for extra, stdin, want, prompted in (
            ([pw], None, pw, False),
            (["--password-stdin"], io.StringIO(f" {pw} \n\n"), f" {pw} \n", False),
            (["--password-stdin"], _Tty(pw), pw, False),
            ([], _Tty(""), "typed", True),
            ([], io.StringIO(pw), None, False),
            ([pw, "--password-stdin"], io.StringIO(pw), None, False)):
        seen, out, err, prompts = _cli([*add, *extra], stdin)
        names = {pw: "password"}
        assert seen.get("creds", {}).get("password") == want and bool(prompts) == prompted, _hide((extra, seen), names)
        if want is None:
            assert "--password-stdin" in seen["exit"] and "password argument" in seen["exit"], _hide((extra, seen), names)
            assert pw not in seen["exit"] and out == "", _hide((extra, out), names)
        else:
            assert out == "added: 5\n", _hide((extra, out), names)
        assert _warned_once(err, "password", pw) if extra[:1] == [pw] and want else err == "", _hide((extra, err), names)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="secrets_")


if __name__ == "__main__":
    raise SystemExit(main())
