"""Deployment values for the canonical scripts: env first, settings.json second.

WHY THIS EXISTS
---------------
Values that describe THIS deployment — R2 credentials, the docs-hub host, the
bundle mirror repo, the Discord channel layout — were literals inside the
scripts that use them. That is wrong twice over:

  * the scripts ship in the bundle and are mirrored to git, so every one of
    those values travelled with the code, and
  * changing one meant editing a canonical script, which is the thing the
    canonical-scripts rule exists to discourage.

Configuration belongs in `~/.agent-bundle/settings.json`, a neutral
per-machine home. Claude Code receives the same shared env block in its native
settings and exports it into sessions. Outside a session — cron, a systemd
unit, or a bare `python3 script.py` — this reader uses the neutral file, so a
Kimi-only installation has the same configuration contract.

Precedence: os.environ -> ~/.agent-bundle/settings.local.json `env` ->
~/.agent-bundle/settings.json `env` -> legacy ~/.claude settings -> the
caller's default. Local-over-base mirrors how Claude Code itself merges the two
files; the legacy fallback preserves existing machines through migration.

Stdlib only, OS-agnostic (SETUP.md edit discipline).
"""
import json
import os
from pathlib import Path

SETTINGS_DIR = Path.home() / '.agent-bundle'
LEGACY_SETTINGS_DIR = Path.home() / '.claude'
# settings.local.json wins over settings.json — same order Claude Code uses.
_FILES = ('settings.json', 'settings.local.json')

_cache = None


def _file_env(force=False):
    """The merged `env` blocks from the settings files, read at most once.

    Cached because a short-lived CLI reads several keys and a long-lived
    connector reads them at startup; a settings edit therefore takes effect on
    the next run, which is the same contract as the env vars this falls back
    from. `force` exists for tests.
    """
    global _cache
    if _cache is None or force:
        merged = {}
        # Legacy first, neutral second: an already-installed shared setting
        # wins while an older Claude-only install remains a safe fallback.
        for settings_dir in (LEGACY_SETTINGS_DIR, SETTINGS_DIR):
            for name in _FILES:
                try:
                    with open(settings_dir / name, encoding='utf-8') as f:
                        block = (json.load(f) or {}).get('env') or {}
                except (OSError, ValueError):
                    continue
                if isinstance(block, dict):
                    merged.update({k: v for k, v in block.items()
                                   if isinstance(v, str)})
        _cache = merged
    return _cache


def setting(name, default=None):
    """Value for `name`: environment, then settings.json, then `default`.

    An empty environment variable counts as unset — an exported-but-blank value
    is how a shell accident looks, and silently adopting "" would take a
    deployment value down to nothing without a word.
    """
    value = os.environ.get(name)
    if value:
        return value
    value = _file_env().get(name)
    return value if value else default


def required(name, hint=''):
    """Like `setting`, but raises when absent. For values with no safe default.

    Credentials and deployment identity have no sensible built-in — a script
    that invents one silently talks to the wrong place, or worse, half-works.
    Failing loudly names the exact key to add and where.
    """
    value = setting(name)
    if not value:
        raise SystemExit(
            f"{name} is not set.\n"
            f"Add it to the \"env\" block of {SETTINGS_DIR / 'settings.json'} "
            f"(or export it){': ' + hint if hint else ''}")
    return value
