#!/usr/bin/env python3
"""discord_mb.py Codex flavor: token dir, watchdog pattern, status adapter.

A connector owned by a Codex session used to have no flavor of its own, so it
had to be started as `--flavor claude` or `--flavor kimi`. The flavor does not
only pick a token directory — it also picks the default status plugin, so a
Codex session running under a foreign flavor loaded that provider's adapter and
latched presence onto that provider's transcript, reporting activity from a
session that was not its own.

These pin the three things the flavor selects, and the failure direction that
matters most: a flavor must never fall back to another provider's adapter,
because a wrong adapter reports confidently wrong presence rather than none.

Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")


def _mb():
    return _util.load(MB, "discord_mb_codex_flavor")


def test_all_flavors_use_the_shared_discord_home(_tmp):
    mb = _mb()
    shared = Path.home() / ".agent-bundle" / "discord"
    assert mb.TOKEN_DIR == shared
    assert mb.KIMI_TOKEN_DIR == shared
    assert mb.CODEX_TOKEN_DIR == shared


def test_explicit_codex_flavor_uses_shared_token_and_keeps_codex_behavior(tmp):
    mb = _mb()
    shared = Path(tmp) / "discord"
    shared.mkdir()
    (shared / "shared_id.token").write_text("shared-token", encoding="utf-8")
    mb.TOKEN_DIR = mb.KIMI_TOKEN_DIR = mb.CODEX_TOKEN_DIR = shared

    token, flavor = mb.resolve_token_and_flavor("shared_id", "codex")
    assert token == "shared-token"
    assert flavor == "codex"


def test_shared_token_defaults_to_claude_without_an_explicit_flavor(tmp):
    mb = _mb()
    shared = Path(tmp) / "discord"
    shared.mkdir()
    (shared / "shared_id.token").write_text("shared-token", encoding="utf-8")
    mb.TOKEN_DIR = mb.KIMI_TOKEN_DIR = mb.CODEX_TOKEN_DIR = shared

    token, flavor = mb.resolve_token_and_flavor("shared_id")
    assert (token, flavor) == ("shared-token", "claude")


def test_unknown_flavor_is_still_rejected(_tmp):
    mb = _mb()
    for bad in ("gemini", "CODEX", "codex "):
        try:
            mb.resolve_token_and_flavor("whoever", bad)
        except ValueError:
            continue
        raise AssertionError(f"unknown flavor accepted: {bad!r}")


def test_codex_has_a_parent_watchdog_pattern(_tmp):
    """The watchdog pattern must match the codex binary, bare or full-path."""
    import re
    mb = _mb()
    rx = re.compile(mb._PARENT_CMD_PATTERNS["codex"])
    for cmdline in ("codex", "codex exec --agent implementer",
                    "/usr/local/bin/codex", "/usr/local/bin/codex exec"):
        assert rx.search(cmdline), f"watchdog missed a codex cmdline: {cmdline}"
    for cmdline in ("claude", "kimi", "codexify --serve", "my-codex-helper"):
        assert not rx.search(cmdline), f"watchdog over-matched: {cmdline}"


def test_each_flavor_gets_its_own_status_adapter(_tmp):
    """A flavor must never inherit a foreign provider's status plugin."""
    mb = _mb()
    assert mb.default_status_plugin("codex") == mb.CODEX_STATUS_PLUGIN
    assert mb.default_status_plugin("kimi") == mb.KIMI_STATUS_PLUGIN
    assert mb.default_status_plugin("claude") == mb.DEFAULT_STATUS_PLUGIN
    assert mb.CODEX_STATUS_PLUGIN != mb.KIMI_STATUS_PLUGIN
    assert mb.CODEX_STATUS_PLUGIN != mb.DEFAULT_STATUS_PLUGIN
    assert mb.CODEX_STATUS_PLUGIN.name == "discord_status_codex.py"
    assert ".codex" in mb.CODEX_STATUS_PLUGIN.as_posix()


def test_codex_flavor_is_accepted_by_the_cli_surface(_tmp):
    """Both watcher subcommands must offer the flavor the connector accepts."""
    package = Path(MB).with_name("discord_mb_lib")
    source = "\n".join(
        (package / name).read_text(encoding="utf-8")
        for name in ("cli.py", "connector.py")
    )
    assert source.count("choices=('claude', 'kimi', 'codex')") == 2, (
        "connector and leech must both accept --flavor codex")
    assert "if flavor not in (None, 'claude', 'kimi', 'codex'):" in source, (
        "connector_main still rejects the codex flavor")


def main():
    return _util.runner(_util.collect(globals()), "codexflavor_")


if __name__ == "__main__":
    raise SystemExit(main())
