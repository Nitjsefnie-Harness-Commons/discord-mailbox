#!/usr/bin/env python3
"""Renamed variables: DISCORD_MAILBOX_<old name> wins and the old name stays the fallback.

Each variable is driven through the code that reads it, once per limb: only the
prefixed name, only the old name, both (the prefix wins) and a blank prefixed
name (counts as unset).
"""
import asyncio
import contextlib
import io
import os
import secrets
import sys
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _secrets, _settings, core, storage  # noqa: E402

PREFIX = "DISCORD_MAILBOX_"
SPECS = getattr(core, "__SETTING_SPECS")
NAMES = ("DISCORD_TOKEN", "DISCORD_MB_USAGE_QUERY", "DISCORD_MB_PORTABLE_PUBLICATION", *(spec[1] for spec in SPECS))


@contextlib.contextmanager
def _environ(values):
    """The environment holds these values for the renamed variables and nothing else."""
    with mock.patch.dict(os.environ):
        for name in NAMES:
            os.environ.pop(name, None)
            os.environ.pop(PREFIX + name, None)
        os.environ.update(values)
        yield


def _limbs(name, new, old):
    """(environment, resolved value) for each limb of the two-name read of `name`."""
    return [({PREFIX + name: new}, new), ({name: old}, old), ({PREFIX + name: new, name: old}, new),
            ({PREFIX + name: "", name: old}, old)]


def _value():
    return "v" + secrets.token_urlsafe(8)


def test_the_connector_token_reads_the_prefixed_name_first(tmp):
    del tmp
    for env, want in _limbs("DISCORD_TOKEN", _value(), _value()):
        with _environ(env):
            got = _secrets.connector_token(None, "agent", None, lambda *_: (None, "directory"))[0]
        assert got == want, sorted(env)


def test_the_usage_helper_path_reads_the_prefixed_name_first(tmp):
    new, old = (Path(tmp) / name for name in ("new.py", "old.py"))
    for helper in (new, old):
        helper.write_text("", encoding="utf-8")
    ran = []
    run = mock.patch.object(core.subprocess, "run", side_effect=lambda args, **_: ran.append(args[1]) or types.SimpleNamespace(stdout="{}"))
    for env, want in _limbs("DISCORD_MB_USAGE_QUERY", str(new), str(old)):
        with _environ(env), run:
            core.fetch_usage()
        assert ran.pop() == want, sorted(env)


def test_portable_publication_reads_the_prefixed_name_first(tmp):
    del tmp
    name = "DISCORD_MB_PORTABLE_PUBLICATION"
    for new, old in (("1", "0"), ("0", "1")):
        for env, want in _limbs(name, new, old):
            # Pinned to a platform that would publish anonymously, so only the variable decides.
            with _environ(env), mock.patch.object(os, "O_TMPFILE", 1, create=True), mock.patch.object(os, "name", "posix"):
                got = "0" if storage._anonymous_publication_available() else "1"
            assert got == want, sorted(env)


def test_a_setting_reads_the_prefixed_name_from_any_source_before_the_old_name(tmp):
    del tmp
    for public, name, default in SPECS:
        new, old = _value(), _value()
        for env, file, want in (
                ({PREFIX + name: new}, {}, new), ({name: old}, {}, old), ({}, {PREFIX + name: new}, new), ({}, {name: old}, old),
                ({name: old}, {PREFIX + name: new}, new), ({PREFIX + name: new}, {name: old}, new),
                ({PREFIX + name: ""}, {name: old}, old), ({}, {}, default)):
            with _environ(env), mock.patch.object(_settings, "_cache", file):
                got = getattr(core, "__refresh_reload_state")()[public]
            assert got == want, (public, sorted(env), sorted(file))


def _setup(env):
    """`setup` under `env` with a stand-in bot -> (tokens the bot ran with, exit status, stderr)."""
    import discord
    ran, err, status = [], io.StringIO(), 0

    def bot(**_):
        client = types.SimpleNamespace(guilds=[], close=lambda: asyncio.sleep(0))
        client.event = lambda function: setattr(client, "ready", function) or function
        client.run = lambda token, **_: ran.append(token) or asyncio.run(client.ready())
        return client
    with _environ(env), mock.patch.object(discord, "Client", bot), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        try:
            core.setup_main(token=None)
        except SystemExit as stop:
            status = stop.code
    return ran, status, err.getvalue()


def test_setup_reads_the_prefixed_token_first_and_refuses_without_one(tmp):
    del tmp
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    for env, want in _limbs("DISCORD_TOKEN", _value(), _value()):
        ran, status, err = _setup(env)
        assert (ran, status) == ([want], 1), sorted(env)
        assert "setup finished without finding or creating" in err, err
    ran, status, err = _setup({})
    assert (ran, status, PREFIX + "DISCORD_TOKEN" in err) == ([], 1, True), err


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="env_prefix_")


if __name__ == "__main__":
    raise SystemExit(main())
