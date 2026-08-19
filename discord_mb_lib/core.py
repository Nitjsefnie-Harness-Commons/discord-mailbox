#!/usr/bin/env python3
'Discord-backed mailbox for selected-harness agent communication. CLI reference: the composed `discord` skill.'

__version__ = "0.34.0"
# Cross-platform: must work on Linux AND Windows. No POSIX-only calls without a
# Windows fallback. Bump __version__ (SemVer) on every substantive change.

import argparse
import asyncio
import base64
import codecs
from collections import deque
import errno
import hashlib
import hmac
import io
import json
import os
import random
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Force UTF-8 on stdout (Windows consoles default to a legacy codepage that
# cannot encode the emoji this tool emits). Guarded by a marker on `sys`, which
# survives module re-execution: importing this file a SECOND time in one
# process would otherwise wrap the already-wrapped stream, and the discarded
# wrapper closes the underlying buffer as it is collected — leaving the whole
# process with a dead stdout ("I/O operation on closed file"). Any test or
# tool that loads this module more than once hits that; `sys` is the only
# namespace that persists across the re-exec, so the flag lives there.
if hasattr(sys.stdout, 'buffer') and not getattr(sys, '_discord_mb_utf8', False):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys._discord_mb_utf8 = True                      # type: ignore[attr-defined]

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_ROOT))
from ._settings import setting  # noqa: E402  (env -> ~/.agent-bundle/settings.json)
from ._temp_provenance import _linklike, ensure_owned_temp_dir  # noqa: E402

# --- Server topology (resolved by name at connector startup — no hardcoded IDs) ---
# Overridable per machine: a server that lays its channels out differently sets
# these in the `env` block of ~/.agent-bundle/settings.json instead of editing this
# canonical script. The literals are the defaults a fresh server gets from
# `discord_mb.py setup`, so an untouched install needs no configuration at all.
__SETTING_SPECS = (
    ('BRIDGE_CHANNEL_NAME', 'DISCORD_MB_BRIDGE_CHANNEL', 'agents'),
    ('DIRECTORY_CHANNEL_NAME', 'DISCORD_MB_DIRECTORY_CHANNEL',
     'agent-directory'),
    ('ATTACHMENTS_CHANNEL_NAME', 'DISCORD_MB_ATTACHMENTS_CHANNEL',
     'attachments'),
    ('CREDS_CHANNEL_NAME', 'DISCORD_MB_CREDENTIALS_CHANNEL', 'credentials'),
    ('BROADCAST_ROLE_NAME', 'DISCORD_MB_BROADCAST_ROLE', 'Claude Agents'),
    ('META_CATEGORY_NAME', 'DISCORD_MB_META_CATEGORY', 'Meta'),
)


__RELOAD_STATE_NAMES = tuple(spec[0] for spec in __SETTING_SPECS) + (
    'deque', 'Path', 'Any', 'setting', '_linklike', 'ensure_owned_temp_dir',
    'SCRIPT_ROOT', '_DEFAULT_CONNECTOR_LOCK_ROOT', 'STATE_ROOT',
    'TOKEN_DIR', 'KIMI_TOKEN_DIR', 'CODEX_TOKEN_DIR', 'HOSTNAME',
    'DEFAULT_STATUS_PLUGIN', 'KIMI_STATUS_PLUGIN', 'CODEX_STATUS_PLUGIN',
    '_PARENT_CMD_PATTERNS', '_DUR_UNITS', '_FLAVOR_STATUS_PLUGINS',
    '_EXTENSION_FLAVOR_DIRS',
)


def __refresh_dependencies():
    """Repeat the former monolith's ``from ... import ...`` bindings."""
    from collections import deque as current_deque
    from pathlib import Path as current_path
    from typing import Any as current_any
    from ._settings import setting as current_setting
    from ._temp_provenance import (
        _linklike as current_linklike,
        ensure_owned_temp_dir as current_ensure_owned_temp_dir,
    )

    return {
        'deque': current_deque,
        'Path': current_path,
        'Any': current_any,
        'setting': current_setting,
        '_linklike': current_linklike,
        'ensure_owned_temp_dir': current_ensure_owned_temp_dir,
    }


def __refresh_reload_state(dependencies=None):
    """Return all import-time state that monolith reload reconstructed."""
    dependencies = dependencies or __refresh_dependencies()
    current_path = dependencies['Path']
    current_setting = dependencies['setting']
    home = current_path.home()
    default_status_plugin = (
        home / '.claude' / 'skills' / 'discord' /
        'discord_status_default.py')
    kimi_status_plugin = (
        home / '.kimi-code' / 'skills' / 'discord' /
        'discord_status_kimi.py')
    codex_status_plugin = (
        home / '.codex' / 'skills' / 'discord' /
        'discord_status_codex.py')
    values = {
        public_name: current_setting(environment_name, default)
        for public_name, environment_name, default in __SETTING_SPECS
    }
    values.update({
        'SCRIPT_ROOT': current_path(__file__).resolve().parent.parent,
        '_DEFAULT_CONNECTOR_LOCK_ROOT':
            home / '.discord-mailbox-log-locks',
        'STATE_ROOT': current_path(tempfile.gettempdir()) / 'discord-mailbox',
        'TOKEN_DIR': home / '.agent-bundle' / 'discord',
        'KIMI_TOKEN_DIR': home / '.agent-bundle' / 'discord',
        'CODEX_TOKEN_DIR': home / '.agent-bundle' / 'discord',
        'HOSTNAME': (socket.gethostname() or '?').split('.')[0],
        'DEFAULT_STATUS_PLUGIN': default_status_plugin,
        'KIMI_STATUS_PLUGIN': kimi_status_plugin,
        'CODEX_STATUS_PLUGIN': codex_status_plugin,
        '_PARENT_CMD_PATTERNS': {
            'claude': r'^claude( |$)',
            'kimi': (r'kimi-code.*main\.mjs|kimi[_-]cli|'
                     r'(^|/)kimi(-legacy)?( |$)'),
            'codex': r'(^|/)codex( |$)',
        },
        '_DUR_UNITS': {'d': 86400, 'h': 3600, 'm': 60, 's': 1},
        '_FLAVOR_STATUS_PLUGINS': {
            'kimi': kimi_status_plugin,
            'codex': codex_status_plugin,
        },
        '_EXTENSION_FLAVOR_DIRS': {
            'claude': lambda: TOKEN_DIR,
            'kimi': lambda: TOKEN_DIR,
            'codex': lambda: TOKEN_DIR,
        },
    })
    return dependencies | values


globals().update(__refresh_reload_state())

# Credentials message template — 3 fenced code blocks: website, login, password.
CREDS_TEMPLATE = (
    "website:\n```\n{domain}\n```\n"
    "name/email/whatever is used to login:\n```\n{login}\n```\n"
    "password:\n```\n{password}\n```"
)

MAX_BODY = 1900          # single-message budget; longer bodies auto-chunk
MAX_BODY_TOTAL = 12000   # hard CLI cap (fat-finger guard, ~7 chunks)
# A connector can publish scheduled status updates even when no mailbox traffic
# arrives.  Keep the human log useful without allowing a continuously running
# identity to consume the disk: one 10 MiB active file plus three retained
# backups is an explicit ~40 MiB ceiling, including each UTF-8 line/chunk
# being written.
CONNECTOR_LOG_MAX_BYTES = 10 * 1024 * 1024
CONNECTOR_LOG_BACKUP_COUNT = 3
# The leech-facing JSON event stream is segmented, never rotated by rename:
# a leech tracks (generation, byte offset), so renaming files under it would
# silently replay or skip events.  One 2 MiB active segment plus two retained
# retired segments is an explicit ~6 MiB ceiling per identity.
EVENT_STREAM_SEGMENT_MAX_BYTES = 2 * 1024 * 1024
EVENT_STREAM_RETAINED_SEGMENTS = 2
# leech.log is shared by every leech process of an identity, so its bound is
# taken under an OS file lock rather than an in-process thread lock: one
# 2 MiB active file plus one retained backup is an explicit ~4 MiB ceiling.
LEECH_LOG_MAX_BYTES = 2 * 1024 * 1024
LEECH_LOG_BACKUP_COUNT = 1
FORUM_POST_HISTORY_LIMIT = 100  # messages downloaded per pinned forum post
RECEIPT_EMOJI = '\u2705'  # ✅
# The connector's ownership/journal namespace must not change when a launcher
# supplies a different TMPDIR.  A test may inject a private root through the
# explicit DISCORD_MB_TEST_LOCK_ROOT seam; production always uses this stable
# per-user location.
_TEST_LOCK_ROOT_ENV = 'DISCORD_MB_TEST_LOCK_ROOT'
# Every harness reads the same identity token and durable extension registry
# from the neutral runtime. Flavor is selected by --flavor and controls only
# the status adapter and parent-process watchdog. The three public directory
# names remain aliases for compatibility with callers that import them.


def _ensure_state_root():
    """Create provenance only when this process created the mailbox root."""
    return ensure_owned_temp_dir(STATE_ROOT, 'shared')


def resolve_token_and_flavor(identity, requested_flavor=None):
    '''Find ``<identity>.token`` in the shared neutral Discord directory.

    ``requested_flavor`` is authoritative when supplied. This matters when an
    identity is taken over by another harness: the credential stays shared
    while the status adapter and watchdog switch. With no explicit flavor,
    preserve the legacy Claude default for older hand-written invocations.
    '''
    if requested_flavor is not None and requested_flavor not in {
            'claude', 'kimi', 'codex'}:
        raise ValueError(f'unknown connector flavor: {requested_flavor!r}')
    flavor = requested_flavor or 'claude'
    tok = TOKEN_DIR / f'{identity}.token'
    if tok.exists():
        try:
            return tok.read_text().strip(), flavor
        except OSError:
            pass
    return None, requested_flavor or 'claude'


# --- State dir layout ---

def state_dir(identity):
    _ensure_state_root()
    d = STATE_ROOT / identity
    for sub in ('inbox', 'outbox', 'inbox-metadata', 'outbox-metadata'):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def inbox_dir(identity):
    return state_dir(identity) / 'inbox'


def pins_dir():
    '''Downloaded pinned-message bodies. A pin belongs to a CHANNEL, not to a
    recipient identity, so this lives at the mailbox-state-root level (shared
    across every identity on this host) and is grouped by channel:
    `<tempdir>/discord-mailbox/pins/<channel_id>/<msg_id>.json`. `<tempdir>` is
    `tempfile.gettempdir()` — `/tmp` on POSIX, `%TEMP%` on Windows; never
    hardcoded.'''
    _ensure_state_root()
    return STATE_ROOT / 'pins'


def msg_cache_dir():
    '''Downloaded referenced / linked / forwarded message bodies, shared across
    identities and grouped by channel:
    `<tempdir>/discord-mailbox/messages/<channel_id>/<msg_id>.json`. Same
    rationale as pins_dir() — a message belongs to a channel, not an identity.'''
    _ensure_state_root()
    return STATE_ROOT / 'messages'


def outbox_dir(identity):
    return state_dir(identity) / 'outbox'


def meta_in_dir(identity):
    'CLI writes here, connector consumes (control-plane requests).'
    return state_dir(identity) / 'inbox-metadata'


def meta_out_dir(identity):
    'Connector writes here, CLI consumes (control-plane responses).'
    return state_dir(identity) / 'outbox-metadata'


# --- Parent CLI PID detection (walk /proc ancestors, NOT pgrep which matches any) ---
# The identity flavor picks the cmdline pattern: 'claude' walks to the Claude CLI;
# 'kimi' walks to the kimi-code node process (`.../kimi-code/.../dist/main.mjs`),
# the `kimi`/`kimi-legacy` launcher, or the legacy Python kimi-cli; 'codex' walks
# to the `codex` binary, which an interactive session and `codex exec` share.
def find_parent_pid_from(start_pid, flavor='claude'):
    '''Walk the parent chain from start_pid up until a process whose cmdline
    matches the flavor CLI. Linux walks /proc; Windows walks via psutil when
    installed (pip install psutil), else returns None — caller can pass
    --claude-pid explicitly. Returns None if not found.'''
    import re as _re
    rx = _re.compile(_PARENT_CMD_PATTERNS.get(flavor, _PARENT_CMD_PATTERNS['claude']))
    if sys.platform == 'win32':
        try:
            import psutil
        except ImportError:
            return None
        try:
            for anc in psutil.Process(start_pid).parents():
                try:
                    argv = anc.cmdline() or []
                except (psutil.Error, OSError):
                    continue
                if not argv:
                    continue
                # argv[0] is a full path on Windows — normalize to a bare
                # lowercase program name so '^claude' style patterns match.
                prog = os.path.basename(argv[0]).lower()
                if prog.endswith('.exe'):
                    prog = prog[:-4]
                if rx.search(' '.join([prog] + argv[1:])):
                    return anc.pid
        except (psutil.Error, OSError):
            return None
        return None
    pid = start_pid
    for _ in range(64):  # bounded walk
        if not pid or pid == 1:
            return None
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().decode('utf-8', errors='replace').replace('\0', ' ').strip()
        except (FileNotFoundError, PermissionError):
            return None
        if rx.search(cmd):
            return pid
        try:
            with open(f'/proc/{pid}/stat') as f:
                stat = f.read()
            # ``comm`` may itself contain spaces and parentheses. Only the
            # final ')' terminates it; after that come fixed fields state, ppid.
            tail = stat[stat.rindex(')') + 1:].split()
            pid = int(tail[1])
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            return None
    return None


def find_claude_pid_from(start_pid):
    'Back-compat wrapper: walk to the Claude-flavor parent CLI.'
    return find_parent_pid_from(start_pid, 'claude')


# --- Cross-platform process check ---

def pid_cmdline(pid):
    '''Best-effort full command line of `pid` as one string.

    None means "could not read it" — no permission, no psutil on Windows, or
    the process vanished mid-read. Callers must treat None as unknown, never as
    a mismatch.'''
    if pid is None:
        return None
    if sys.platform == 'win32':
        try:
            import psutil
        except ImportError:
            return None
        try:
            return ' '.join(psutil.Process(pid).cmdline() or [])
        except Exception:
            return None
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read().decode('utf-8', errors='replace').replace('\0', ' ').strip()
    except PermissionError:
        return None
    except OSError:
        pass
    try:                                   # no /proc (macOS, BSD)
        r = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def is_connector_process(pid, identity):
    '''Is `pid` actually this identity's connector?

    True yes, False demonstrably something else, None unknown.

    A PID existing is not the same as that PID being who the lock says it is.
    After a crash the connector's number goes back in the pool, and the OS hands
    it to something unrelated — observed on a three-day-old lock whose PID had
    been recycled into a Windows service. The identity was then locked out of
    its own mailbox ("already running (PID 6548)"), and `leech` cheerfully
    attached to the corpse and tailed an events file that would never grow: an
    outage that looks healthy from outside.

    Unknown deliberately reads as "still a connector" at the call sites. A false
    "stale" starts a SECOND connector on one identity, which is worse than the
    stale lock this exists to clear.'''
    cmd = pid_cmdline(pid)
    if cmd is None:
        return None
    if 'discord_mb' not in cmd:
        return False
    # Exact argv token, so `dev` does not match a `dev_kimi` connector.
    return True if not identity else identity in cmd.split()


def pid_alive(pid):
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            r = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                               capture_output=True, text=True, timeout=5)
            return str(pid) in r.stdout
        except Exception as e:
            # fail-open: don't kill connector on tooling error
            print(f'[discord_mb] pid_alive tasklist error (assuming alive): '
                  f'{type(e).__name__}: {e}', file=sys.stderr, flush=True)
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours


# --- Inbox event header parsing ---

def parse_message_header(content, default_to):
    """Extract (to_label, subject) for a compact inbox notification.

    Mailbox sends are framed as ``<@uid> **[sender→recipients]** subject\\nbody``
    (see dispatch_outbox_file). When that frame is present, return its recipients
    and subject line. For unframed messages (a human typing in Discord, a DM with
    no framing), return ``default_to`` and the first non-empty line as the topic.
    Both returned values are caller-capped before they go into an event — this
    function does not truncate.
    """
    import re as _re
    m = _re.search(r'\*\*\[[^\]\n]*?→([^\]\n]+)\]\*\*[ \t]*([^\n]*)', content or '')
    if m:
        return m.group(1).strip(), m.group(2).strip()
    for line in (content or '').splitlines():
        line = line.strip()
        if line:
            return default_to, line
    return default_to, ''


# Continuation-chunk frame: `**[from→to]** subject (k/n)` with the (k/n) at the
# end of the header line. Chunk 1 carries the recipient mention (the one-ping
# property); chunks 2+ deliberately do NOT — so a mention-based routing gate
# drops them and the recipient's inbox saw a message ending mid-thought. The
# `to` group drives routing for those instead.
_CHUNK_FRAME_RE = re.compile(
    r'^\*\*\[[^\]\n]*→([^\]\n]*)\]\*\*[^\n]*\((\d+)/(\d+)\)[ \t]*(?:\n|$)')


def continuation_chunk_for(content, identity):
    """True when `content` is a chunk-2+ frame of a mailbox send addressed to
    `identity` (comma list ok, or 'all'). Chunk 1 frames (no (k/n) suffix, or
    (1/n)) return False — those route by their mention, not here."""
    m = _CHUNK_FRAME_RE.match(content or '')
    if not m:
        return False
    k, n = int(m.group(2)), int(m.group(3))
    if k < 2 or n < k:
        return False
    to = m.group(1).strip()
    return to == 'all' or identity in (t.strip() for t in to.split(','))


# --- Non-content message payloads (embeds, Components V2, polls, forwards) ---
#
# `Message.content` is only ONE of the ways a Discord message carries text, and
# for bot/webhook posts it is usually the empty one. A status bot posts an
# embed; a modern app posts a Components-V2 container of text displays; a
# forward carries the whole thing in a snapshot. Every one of those reached an
# agent as `body: ""` with no hint that anything had been dropped — the record
# said `embed_count` and nothing else, and Components V2 was not represented at
# all. Observed 2026-07-29: a forwarded status.claude.com post (flags 16384,
# HAS_SNAPSHOT, payload in snapshot.components) arrived with an empty body,
# empty forwarded[] and embed_count 0, and had to be read off the raw HTTP API.
#
# So: flatten everything into text ONCE, here, and let every record-writing site
# attach it. Structure is preserved alongside (embeds[] keeps its fields), but
# the flattened `rendered` is what makes an empty-content message legible.

def _flat_component(c, out, depth=0):
    """Append one component's human-readable text to `out` (recursive).

    Reads by ComponentType VALUE rather than isinstance, so a component type
    this discord.py does not model yet degrades to a labelled placeholder
    instead of vanishing. Defensive getattr throughout: a partial object from an
    older library must not take the whole ingest down.
    """
    if depth > 6:                                    # cycles are impossible, but
        return                                       # depth is free insurance
    ctype = getattr(getattr(c, 'type', None), 'value', None)
    if ctype == 10:                                  # text_display — the v2 body
        text = (getattr(c, 'content', '') or '').strip()
        if text:
            out.append(text)
        return
    if ctype in (17, 9, 1, 18):                      # container / section / row / label
        label = (getattr(c, 'label', None) or '').strip() if ctype == 18 else ''
        if label:
            out.append(label)
        for child in (getattr(c, 'children', None) or []):
            _flat_component(child, out, depth + 1)
        inner = getattr(c, 'component', None)        # label wraps a single child
        if inner is not None:
            _flat_component(inner, out, depth + 1)
        acc = getattr(c, 'accessory', None)          # section accessory (thumb/button)
        if acc is not None:
            _flat_component(acc, out, depth + 1)
        return
    if ctype == 2:                                   # button
        lab = (getattr(c, 'label', None) or '').strip()
        url = getattr(c, 'url', None)
        if lab or url:
            out.append(f'[button] {lab}{" — " + url if url else ""}'.strip())
        return
    if ctype in (3, 5, 6, 7, 8):                     # select menus
        ph = (getattr(c, 'placeholder', None) or '').strip()
        opts = [getattr(o, 'label', '') for o in (getattr(c, 'options', None) or [])]
        out.append(f'[select] {ph}{" — options: " + ", ".join(o for o in opts if o) if opts else ""}'.strip())
        return
    if ctype == 11:                                  # thumbnail
        media = getattr(c, 'media', None)
        url = getattr(media, 'url', None)
        if url:
            out.append(f'[thumbnail] {url}')
        return
    if ctype == 12:                                  # media gallery
        for item in (getattr(c, 'items', None) or []):
            url = getattr(getattr(item, 'media', None), 'url', None)
            desc = (getattr(item, 'description', None) or '').strip()
            if url:
                out.append(f'[image] {desc + " — " if desc else ""}{url}')
        return
    if ctype == 13:                                  # file
        url = getattr(getattr(c, 'media', None), 'url', None)
        name = getattr(c, 'name', None)
        out.append(f'[file] {name or ""} {url or ""}'.strip())
        return
    if ctype == 14:                                  # separator — no text
        return
    if ctype is not None:                            # modelled by Discord, not by us
        out.append(f'[component type {ctype}]')


def flatten_components(components):
    'Every text-bearing component, flattened to a list of lines.'
    out = []
    for c in components or []:
        try:
            _flat_component(c, out)
        except Exception:                            # never break ingest on one component
            out.append('[component: unreadable]')
    return [line for line in out if line]


def embed_record(e):
    'One embed as a JSON-safe dict, with a `rendered` text form of the same.'
    def _s(v):
        return v if isinstance(v, str) and v.strip() else None
    rec = {
        'title': _s(getattr(e, 'title', None)),
        'description': _s(getattr(e, 'description', None)),
        'url': _s(getattr(e, 'url', None)),
        'colour': getattr(getattr(e, 'colour', None), 'value', None),
        'author': _s(getattr(getattr(e, 'author', None), 'name', None)),
        'footer': _s(getattr(getattr(e, 'footer', None), 'text', None)),
        'timestamp': (e.timestamp.isoformat()
                      if getattr(e, 'timestamp', None) is not None else None),
        'image': _s(getattr(getattr(e, 'image', None), 'url', None)),
        'thumbnail': _s(getattr(getattr(e, 'thumbnail', None), 'url', None)),
        'fields': [{'name': f.name, 'value': f.value, 'inline': bool(f.inline)}
                   for f in (getattr(e, 'fields', None) or [])],
    }
    lines = []
    if rec['author']:
        lines.append(rec['author'])
    if rec['title']:
        lines.append(f"**{rec['title']}**" + (f" — {rec['url']}" if rec['url'] else ''))
    elif rec['url']:
        lines.append(rec['url'])
    if rec['description']:
        lines.append(rec['description'])
    for f in rec['fields']:
        lines.append(f"{f['name']}: {f['value']}")
    for key, tag in (('image', 'image'), ('thumbnail', 'thumbnail')):
        if rec[key]:
            lines.append(f'[{tag}] {rec[key]}')
    if rec['footer']:
        lines.append(rec['footer'])
    rec['rendered'] = '\n'.join(lines)
    return rec


def poll_record(poll):
    'Poll question + answers with vote counts, or None.'
    if poll is None:
        return None
    try:
        question = getattr(getattr(poll, 'question', None), 'text', None) or str(
            getattr(poll, 'question', '') or '')
        answers = []
        for a in (getattr(poll, 'answers', None) or []):
            answers.append({'text': getattr(getattr(a, 'media', None), 'text', None)
                            or str(getattr(a, 'text', '') or ''),
                            'votes': getattr(a, 'vote_count', None)})
        return {'question': question, 'answers': answers,
                'multiple': bool(getattr(poll, 'multiple', False))}
    except Exception:
        return None


def message_extras(m, depth=0):
    """Everything a message carries BESIDES content, flattened + structured.

    Returns a dict with `embeds`, `components`, `poll`, `forwarded`, and
    `rendered` — content plus every extra, as one readable block. Callers merge
    the non-empty keys into their record; `rendered` is what they show when
    `content` is empty. Recurses one level into forward snapshots, which are
    themselves full messages (content + embeds + components + attachments).
    """
    embeds = [embed_record(e) for e in (getattr(m, 'embeds', None) or [])]
    comps = flatten_components(getattr(m, 'components', None) or [])
    poll = poll_record(getattr(m, 'poll', None))
    forwarded = []
    if depth < 2:
        for s in (getattr(m, 'message_snapshots', None) or []):
            sub = message_extras(s, depth + 1)
            forwarded.append({
                'body': getattr(s, 'content', '') or '',
                'attachments': [{'filename': a.filename, 'url': a.url, 'size': a.size,
                                 'content_type': a.content_type}
                                for a in (getattr(s, 'attachments', None) or [])],
                'embeds': sub['embeds'],
                'components': sub['components'],
                'rendered': sub['rendered'],
            })
    parts = []
    content = (getattr(m, 'content', '') or '').strip()
    if content:
        parts.append(content)
    for e in embeds:
        if e['rendered']:
            parts.append(e['rendered'])
    parts += comps
    if poll:
        parts.append('[poll] ' + poll['question'] + ''.join(
            f"\n  - {a['text']}"
            + (f" ({a['votes']} votes)" if a['votes'] is not None else '')
            for a in poll['answers']))
    for f in forwarded:
        if f['rendered']:
            parts.append('[forwarded]\n' + f['rendered'])
        for a in f['attachments']:
            parts.append(f"[forwarded attachment] {a['filename']} {a['url']}")
    return {'embeds': embeds, 'components': comps, 'poll': poll,
            'forwarded': forwarded, 'rendered': '\n\n'.join(parts)}


def attach_extras(record, m, body_key='body'):
    """Merge a message's non-content payloads into `record`, in place.

    Only non-empty keys are added, so an ordinary text message's record keeps
    exactly the shape it has always had. `<body_key>_rendered` appears only when
    it differs from the plain body — its presence IS the signal that reading the
    body alone would have missed something.
    """
    x = message_extras(m)
    if x['embeds']:
        record['embeds'] = x['embeds']
    if x['components']:
        record['components'] = x['components']
    if x['poll']:
        record['poll'] = x['poll']
    if x['forwarded']:
        record['forwarded'] = x['forwarded']
    if x['rendered'] and x['rendered'] != (record.get(body_key) or '').strip():
        record[f'{body_key}_rendered'] = x['rendered']
    return record


# --- Usage status channels (voice-channel names as a live usage dashboard) ---
#
# A voice channel's NAME is freeform text, always visible in the sidebar, and
# costs the reader nothing to consult — so the fleet's Claude/Kimi rate-limit
# utilization lives there instead of in a message nobody re-reads.
#
# Two constraints shape everything below:
#
#   * Discord rate-limits channel renames hard (2 per 10 minutes per channel).
#     One rename per 5 minutes is the fastest cadence that stays inside it.
#   * Connectors run on SEVERAL MACHINES (a laptop, a server, a gpu box…), all
#     watching the same account, and nothing on disk is shared between them. So
#     the cadence cannot be coordinated by a lock file — a local file only ever
#     serializes the identities on one box.
#
# The synchronization is therefore built from two things every machine already
# has, with no shared storage at all:
#
#   1. THE WALL CLOCK, bucketed. Every connector computes
#      `period = int(now // 300)` — an integer that is the SAME on every machine
#      without anyone agreeing on anything. All of them consider an update at the
#      same absolute boundary, and each acts at most once per period.
#   2. THE CHANNEL NAME ITSELF as the shared state. A rename is skipped when the
#      computed name already equals the live one. Since every machine derives the
#      name from the same account usage, they all compute the SAME string: the
#      first to write it wins, and everyone else — on every other host — sees it
#      via the gateway within milliseconds and does nothing. One rename per
#      period across the whole fleet, with no consensus protocol.
#
# A short random jitter before the edit spreads simultaneous wake-ups so the
# winner is decided by the gateway rather than by a photo finish, and the name
# is re-read immediately before the edit so a late arrival still backs off. The
# local claim file remains, but ONLY as a same-box optimisation: it keeps N
# identities on one machine from each spawning a usage_query subprocess.
#
# The name carries no timestamp on purpose. A clock in the name would change
# every period and force a rename every 5 minutes — exactly at the rate limit,
# with no headroom for a race. Without it, a rename happens only when a number
# or a colour actually changes, which is a few times an hour.
#
# The channels are matched by the provider word in their name, NOT by id or by
# an exact title, because this code renames them: whatever the name becomes, it
# still contains "claude" / "kimi", so the next lookup still finds it. Creating
# a voice channel named "claude status" is therefore the whole enablement step,
# and deleting it is the off switch — there is no config file to drift.

USAGE_STATUS_INTERVAL = 300        # period length — the fleet-wide rename cadence
USAGE_STATUS_JITTER = 20           # seconds of spread before the edit (see above)
USAGE_STATUS_POLL = 60             # how often a connector re-checks the claim
USAGE_STATUS_LOCK_TTL = 60         # steal a claim lock older than this
USAGE_PACE_BAND = 1.0              # ±% around flat pace that reads as "on pace"
USAGE_PROVIDERS = ('claude', 'kimi', 'codex')
USAGE_WINDOWS = (('five_hour', '5h'), ('weekly', '7d'))

# Optional third channel: an explicit, VISIBLE lease saying which connector is
# publishing. The period bucket + name equality above already give one rename
# per period as long as every host computes the SAME string — which holds while
# they share an account. The moment two hosts report different numbers (separate
# accounts, one box querying a different plan) that assumption breaks and they
# would overwrite each other every period. The lease decides an owner instead,
# and doubles as provenance: the channel literally says who published last and
# when, which is how a human tells "green because it's fine" from "green because
# nothing has updated in an hour".
#
# It is enablement-by-existence like the status channels, and PER GUILD: create
# a voice channel whose name contains "claim" in a server and that server's
# board is leased; delete it and that server falls back to name-equality alone. The stamp is coarse on purpose — the lease
# is refreshed every 10 minutes, not every period, so the claim channel's own
# rename budget (2 per 10 min) is never the bottleneck.
USAGE_CLAIM_REFRESH = 600          # owner re-stamps its lease this often
USAGE_CLAIM_TTL = 900              # a lease older than this is dead; take over
_CLAIM_RE = re.compile(r'claim\s*[·:-]\s*(\S+)\s*[·:-]\s*(\d{1,2}):(\d{2})\s*Z',
                       re.IGNORECASE)


def render_claim_name(owner, now=None):
    'e.g. `claim · analyst@host-a · 13:25Z` (UTC, so hosts agree).'
    t = time.gmtime(time.time() if now is None else now)
    return f'claim · {owner} · {t.tm_hour:02d}:{t.tm_min:02d}Z'


def parse_claim_name(name, now=None):
    """(owner, age_seconds) from a claim channel name, or (None, None).

    The stamp is HH:MM UTC — no date, because the name has to stay short. A
    stamp that reads as being in the future is a day rollover (or a skewed
    clock), so it is folded back a day rather than trusted; the effect is that
    an ambiguous lease reads as OLD, and old means takeable. Erring toward
    "expired" keeps a dead owner from freezing the display, which is the failure
    that actually matters here.
    """
    m = _CLAIM_RE.search(name or '')
    if not m:
        return None, None
    now = time.time() if now is None else now
    t = time.gmtime(now)
    stamp_min = int(m.group(2)) * 60 + int(m.group(3))
    now_min = t.tm_hour * 60 + t.tm_min
    delta_min = now_min - stamp_min
    if delta_min < -1:                            # future stamp -> previous day
        delta_min += 24 * 60
    return m.group(1), max(0, delta_min * 60 + t.tm_sec)


def usage_gate_paths(root=None, guild_id=None):
    """Box-wide claim state + lock, PER GUILD, shared by every identity here.

    Per guild and not per box: the identities on this machine are separate bot
    users in different sets of servers. A single gate would let the identity
    that happens to run first consume the period for a server it is not even a
    member of, and the connector that IS in that server would then skip it.
    """
    base = Path(root) if root is not None else STATE_ROOT
    if root is None:
        _ensure_state_root()
    base.mkdir(parents=True, exist_ok=True)
    tag = f'-{guild_id}' if guild_id is not None else ''
    return (base / f'.usage-status-gate{tag}.json',
            base / f'.usage-status-gate{tag}.lock')


def usage_period(now=None, interval=USAGE_STATUS_INTERVAL):
    """The current update period as an absolute-time bucket.

    `int(now // 300)` is the same integer on every machine in the fleet at the
    same moment, so it synchronizes hosts that share no storage — which is the
    whole reason the cadence is not tracked as "seconds since I last updated".
    """
    return int((time.time() if now is None else now) // interval)


def claim_usage_slot(period=None, guild_id=None, root=None, now=None):
    """True if THIS process owns `period` for `guild_id` on THIS BOX.

    Same-box optimisation only — it stops the several identities running here
    from each spawning a usage_query subprocess for the same period. Cross-host
    coordination is the period bucket plus the channel-name check; see above.

    O_CREAT|O_EXCL on a lock file, not fcntl: this has to work on Windows, where
    the fleet's other connectors run. A lock older than USAGE_STATUS_LOCK_TTL is
    assumed orphaned (killed process) and stolen, so a crash mid-claim cannot
    wedge the updater forever.
    """
    now = time.time() if now is None else now
    period = usage_period(now) if period is None else period
    gate, lock = usage_gate_paths(root, guild_id)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            # Lock age is measured against the REAL clock, never the injected
            # `now` — that parameter exists for period arithmetic (and tests),
            # and comparing a synthetic timestamp to a filesystem mtime would
            # silently make a stale lock unstealable.
            if time.time() - os.path.getmtime(lock) < USAGE_STATUS_LOCK_TTL:
                return False                     # someone is claiming right now
            os.unlink(lock)                      # orphaned — steal it
        except OSError:
            return False
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False
    try:
        os.close(fd)
        seen = None
        try:
            with open(gate, encoding='utf-8') as f:
                seen = json.load(f).get('period')
        except (OSError, ValueError, AttributeError):
            seen = None
        # `<=`, not `==`: periods only move forward, so a request for one that
        # is already recorded — or OLDER — is a duplicate. Equality alone would
        # re-grant an earlier period after a backwards clock step, which is the
        # one way a box could publish twice inside a single window.
        if isinstance(seen, int) and period <= seen:
            return False                         # already claimed here
        tmp = gate.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'period': period, 'claimed_at': now}),
                       encoding='utf-8')
        tmp.replace(gate)
        return True
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def pace_dot(pct, pace_pct, band=USAGE_PACE_BAND):
    """🟢 under pace · 🟡 within `band`% of it · 🔴 over pace (⚪ unknown).

    Green is the GOOD state (burning slower than a flat line to reset), red the
    bad one. The band exists because a window sitting exactly on pace flickers
    between colours otherwise.
    """
    if pct is None or pace_pct is None:
        return '⚪'
    diff = float(pct) - float(pace_pct)
    if diff > band:
        return '🔴'
    if diff < -band:
        return '🟢'
    return '🟡'


_DUR_RE = re.compile(r'(\d+)\s*([dhms])', re.IGNORECASE)


def duration_secs(text):
    'Seconds in a "4d11h" / "6h54m" style duration, or None if unparseable.'
    if not isinstance(text, str):
        return None
    total = sum(int(n) * _DUR_UNITS[u.lower()] for n, u in _DUR_RE.findall(text))
    return total or None


def coarse_duration(secs):
    """Seconds -> the BIGGEST unit alone, always rounded UP. Minutes floor it.

    `8h07m` reads as two facts when the reader only wants one, and truncating
    to `8h` would understate the wait — so the remainder always rounds up, and
    a wait shorter than a minute still shows as `1m` rather than a count of
    seconds nobody is going to act on.

    Rounding up can fill the unit exactly (23h30m -> 24h, 59m01s -> 60m), which
    is promoted rather than printed: `1d` and `1h` are what those mean.
    """
    if secs is None:
        return None
    minutes = -(-int(secs) // 60)                 # ceil, integer-only
    if minutes < 60:
        return f'{max(1, minutes)}m'
    hours = -(-minutes // 60)
    if hours < 24:
        return f'{hours}h'
    return f'{-(-hours // 24)}d'


def recovery_label(window):
    """When an over-pace window goes green again — the missing half of a colour.

    A red dot says "burning too fast" and stops there, which leaves the reader
    with no idea whether relief is an hour away or four days. Two different
    clocks can end the red, and the honest answer is whichever lands FIRST:

      * `recover_in` — the flat-pace line catching up to current usage, i.e.
        when this goes green if nothing more is spent.
      * `resets_in`  — the window rolling over, which zeroes usage regardless.

    A long recovery on a window that resets sooner would otherwise advertise a
    wait that never happens. Returns None when neither clock is known.
    """
    if not isinstance(window, dict):
        return None
    best_secs = None
    for key in ('recover_in', 'resets_in'):
        secs = duration_secs(window.get(key))
        if secs is not None and (best_secs is None or secs < best_secs):
            best_secs = secs
    # Compare in seconds and publish coarse. The comparison has to be numeric
    # because the two clocks arrive as strings in different units ("3d2h" vs
    # "2h10m"), where the shorter wait is not the smaller string.
    return coarse_duration(best_secs)


def render_usage_name(provider, block):
    """Channel name for one provider, e.g. `claude · 5h 🟢 0% · 7d 🔴 40%`.

    Returns None when there is nothing trustworthy to show — the caller then
    leaves the existing name alone rather than publishing a blank or a guess.
    The provider word stays in the name so the channel is still findable after
    the rename (see the module comment above).
    """
    if not isinstance(block, dict):
        return None
    parts = []
    for key, label in USAGE_WINDOWS:
        w = block.get(key)
        if not isinstance(w, dict) or w.get('pct') is None:
            continue
        dot = pace_dot(w.get('pct'), w.get('pace_pct'))
        seg = f"{label} {dot} {float(w['pct']):.0f}%"
        if dot == '🔴':
            # Only red carries the ETA. Green and yellow have nothing to wait
            # for, and hanging a duration off every window would triple the
            # length of a name whose whole value is being readable at a glance.
            eta = recovery_label(w)
            if eta:
                seg += f' →🟢 {eta}'
        parts.append(seg)
    if not parts:
        return None
    return f"{provider} · " + ' · '.join(parts)


def fetch_usage(timeout=60):
    """`usage_query.py --json` -> every successfully queried provider.

    usage_query keeps a box-wide 30s cache of the live endpoints, so calling it
    on every update period costs a subprocess, not an API round trip — and every
    session on the box sees the same numbers. A non-zero query exit can still
    carry valid partial results (for example Claude/Kimi on a box without
    Codex), so parse stdout rather than discarding all providers together.
    """
    # Facade-bound calls see discord_mb.py's directory; package-internal calls
    # (including ConnectorApp) retain core.py's globals one level lower. Keep
    # facade relocation/``__file__`` monkeypatches working, then fall back to
    # the package's parent scripts directory.
    override = os.environ.get('DISCORD_MB_USAGE_QUERY')
    here = Path(__file__).resolve().parent
    candidates = [Path(override)] if override else []
    # Installed as a wheel there is no sibling helper at all: site-packages is
    # not the caller's scripts directory. The probe stays for a flat install,
    # but the env var is what an installer sets, and a missing helper means
    # "publish no board", never a crash.
    candidates += [here / 'usage_query.py', here.parent / 'usage_query.py']
    script = next((candidate for candidate in candidates
                   if candidate.is_file()), None)
    if script is None:
        return {}
    try:
        r = subprocess.run([sys.executable, str(script), '--json', '--quiet'],
                           capture_output=True, text=True, timeout=timeout)
        return (json.loads(r.stdout) or {}).get('usage') or {}
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


# --- Sender (short-lived CLI) ---

class SendRetry(Exception):
    'Transient send failure — the outbox file stays in place; the next tick retries.'


class SendError(Exception):
    'Permanent send failure — reported and dropped, never silently rerouted.'


def chunk_body(body, limit):
    '''Split body into <=limit pieces, preferring paragraph, then line, then
    space boundaries; hard-splits only when no boundary lands in the back 3/4
    of the window. Returns [body] unchanged when it already fits.'''
    if len(body) <= limit:
        return [body]
    parts = []
    rest = body
    while len(rest) > limit:
        window = rest[:limit + 1]
        cut, sep_len = -1, 0
        for sep in ('\n\n', '\n', ' '):
            i = window.rfind(sep)
            if i > limit // 4:
                cut, sep_len = i, len(sep)
                break
        if cut < 0:
            cut, sep_len = limit, 0
        parts.append(rest[:cut].rstrip())
        rest = rest[cut + sep_len:]
    if rest.strip():
        parts.append(rest)
    return parts


def moved_body(author, created, content, extra_urls=None):
    '''Render one relocated message: attribution line, then the original text.

    Discord has no move primitive -- a move is a repost by the bot followed by
    a delete of the original, so the author and time survive ONLY if they are
    written into the body. `extra_urls` carries attachments that could not be
    re-uploaded (too large, or unreadable): a dead link beats losing the
    reference entirely.
    '''
    stamp = str(created or '')[:19].replace('T', ' ')
    head = f'**{author or "?"}**' + (f' · {stamp}' if stamp else '')
    parts = [head]
    body = (content or '').strip()
    if body:
        parts.append(body)
    for url in extra_urls or []:
        parts.append(f'[attachment] {url}')
    return '\n'.join(parts)


def moved_frames(author, created, content, extra_urls=None, limit=MAX_BODY):
    '''moved_body split into postable frames (a moved message can exceed the
    single-message budget just as an authored one can).'''
    return chunk_body(moved_body(author, created, content, extra_urls), limit)


def cap_event_subject(ev, limit=500):
    '''Trim ev["subject"] until the whole SERIALIZED event fits the Monitor
    notification cap (500 chars, verified empirically) — a fixed subject cap
    can't guarantee that once path/to vary, and json escaping inflates length.
    Converges in 1-2 passes. The full body lives in the file at ev["path"].'''
    while ev['subject'] and len(json.dumps(ev, ensure_ascii=False)) > limit:
        over = len(json.dumps(ev, ensure_ascii=False)) - limit
        keep = max(0, len(ev['subject']) - over - 1)
        ev['subject'] = (ev['subject'][:keep].rstrip() + '…') if keep else ''
    return ev


def send(identity, to, subject, body, reply_to=None, pin=False, channel=None,
         dm=False, attach=None, wait=False, timeout=60.0):
    '''Queue a message (outbox file, fire-and-forget) or, with wait=True, send
    through the connector's meta channel and return the result (msg_id,
    channel_id, chunks, pin outcome). Bodies over MAX_BODY are auto-chunked by
    the connector; MAX_BODY_TOTAL is the hard cap.'''
    if len(body) > MAX_BODY_TOTAL:
        print(f'body too long: {len(body)} > {MAX_BODY_TOTAL} (auto-chunking covers up to '
              f'{MAX_BODY_TOTAL}; put anything bigger in a file and `attachments upload` it)',
              file=sys.stderr, flush=True)
        sys.exit(2)
    # `to` may be a comma-separated list ("a,b,c") — connector tags all of them in
    # a single Discord message. "all" anywhere in the list collapses to broadcast.
    tos = [t.strip() for t in to.split(',') if t.strip()]
    if not tos:
        print(f'no recipients in {to!r}', file=sys.stderr, flush=True)
        sys.exit(2)
    if 'all' in tos:
        canonical_to = 'all'
    elif tos == ['nobody']:
        canonical_to = 'nobody'  # user-less send: post with no mention
    else:
        canonical_to = ','.join(tos)
    if dm and (len(tos) != 1 or canonical_to in ('all', 'nobody')):
        print(f'--dm needs exactly one recipient identity or user id, got {to!r}',
              file=sys.stderr, flush=True)
        sys.exit(2)
    msg = {
        'to': canonical_to,
        'subject': subject,
        'body': body,
        'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if pin:
        msg['pin'] = True
    if channel:
        msg['channel'] = channel
    if dm:
        msg['dm'] = True
    if attach:
        paths = [str(Path(a).resolve()) for a in attach]
        missing = [a for a in paths if not Path(a).is_file()]
        if missing:
            print(f'attachment(s) not found: {missing}', file=sys.stderr, flush=True)
            sys.exit(2)
        msg['attach'] = paths
    if reply_to:
        msg['reply_to'] = reply_to
        # Best source for the reply channel: the original in OUR inbox. When it
        # isn't there, the connector resolves it from the shared caches — or
        # fails the send LOUDLY. There is no silent #agents fallback anymore.
        inbox_file = inbox_dir(identity) / f'{reply_to}.json'
        if inbox_file.exists():
            try:
                orig = json.loads(inbox_file.read_text(encoding='utf-8'))
                if orig.get('channel_id'):
                    msg['reply_channel_id'] = orig['channel_id']
                if orig.get('is_dm'):
                    msg['is_dm'] = True
            except Exception as e:
                print(f'[discord_mb] reply-to context load failed for {reply_to}: '
                      f'{type(e).__name__}: {e}', file=sys.stderr, flush=True)
    if wait:
        try:
            resp = _meta_request(identity, {'op': 'send', **msg}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr, flush=True)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'send failed: {resp.get("error", resp)}', file=sys.stderr, flush=True)
            sys.exit(1)
        pin_note = {True: ', pinned', False: ', PIN FAILED'}.get(resp.get('pinned'), '')
        chunk_note = f' in {resp["chunks"]} chunks' if resp.get('chunks', 1) > 1 else ''
        print(f'[{time.strftime("%H:%M:%S")}] Sent to {canonical_to}: {subject} '
              f'(msg_id={resp.get("msg_id")} channel_id={resp.get("channel_id")}'
              f'{chunk_note}{pin_note})', flush=True)
        return resp.get('msg_id')
    path = outbox_dir(identity) / f'{uuid.uuid4()}.json'
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(msg, indent=2), encoding='utf-8')
    tmp.replace(path)  # atomic on POSIX; best-effort on Windows
    print(f'[{time.strftime("%H:%M:%S")}] Queued for {canonical_to}: {subject}', flush=True)
    return str(path)


# --- Setup (one-shot channel bootstrap) ---

def setup_main(token=None):
    'Create missing infrastructure channels (currently: #attachments).'
    import discord

    token = token or os.environ.get('DISCORD_TOKEN')
    if not token:
        print('No token. Set DISCORD_TOKEN or pass --token.', file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            if not client.guilds:
                print('bot is not in any guild — invite it first (discord_mb.py does not handle OAuth flow)', file=sys.stderr)
                await client.close()
                return
            guild = client.guilds[0]
            # Locate or create Meta category
            meta = discord.utils.find(lambda c: c.name.lower() == META_CATEGORY_NAME.lower(),
                                      guild.categories)
            if meta is None:
                print(f'Meta category {META_CATEGORY_NAME!r} not found; placing channel at guild root')
            existing = discord.utils.find(lambda c: c.name == ATTACHMENTS_CHANNEL_NAME,
                                          guild.text_channels)
            if existing:
                print(f'#{ATTACHMENTS_CHANNEL_NAME} already exists (id={existing.id})')
            else:
                ch = await guild.create_text_channel(
                    ATTACHMENTS_CHANNEL_NAME,
                    category=meta,
                    topic='Binary side-channel for oversized mailbox payloads. See discord_mb.py.',
                    reason='discord_mb.py setup',
                )
                print(f'created #{ATTACHMENTS_CHANNEL_NAME} (id={ch.id})')
        except discord.Forbidden as e:
            print(f'missing perms (need Manage Channels): {e}', file=sys.stderr)
        except Exception as e:
            print(f'setup error: {e}', file=sys.stderr)
        finally:
            await client.close()

    client.run(token, log_handler=None)


# --- Control-plane CLI (round-trip via connector through filesystem) ---

def _meta_request(identity, payload, timeout):
    'Write request to inbox-metadata, poll outbox-metadata for response, delete, return parsed dict.'
    in_dir = meta_in_dir(identity)
    out_dir = meta_out_dir(identity)
    req_id = f'{uuid.uuid4()}.json'
    in_path = in_dir / req_id
    out_path = out_dir / req_id
    tmp = in_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload), encoding='utf-8')
    tmp.replace(in_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out_path.exists():
            data = json.loads(out_path.read_text(encoding='utf-8'))
            try:
                out_path.unlink()
            except OSError:
                pass
            return data
        time.sleep(0.2)
    # timed out; clean up our request if connector hasn't picked it up
    try:
        in_path.unlink()
    except OSError:
        pass
    raise TimeoutError(f'connector did not respond within {timeout}s (is it running for {identity!r}?)')


def attachments_cli(args):
    identity = args.identity
    timeout = args.timeout
    action = args.att_action
    if action == 'list':
        try:
            resp = _meta_request(identity, {'op': 'attachment-list', 'limit': args.limit}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        entries = resp.get('entries', [])
        if args.json:
            print(json.dumps(entries, indent=2))
            return
        if not entries:
            print('(no attachments)')
            return
        for e in entries:
            ts = e.get('created', '').replace('T', ' ')[:19]
            size_kb = e.get('size', 0) / 1024
            note = f"  note={e['note']!r}" if e.get('note') else ''
            print(f'[{ts} from {e["from"]} msg_id={e["msg_id"]}] {e["filename"]} ({size_kb:.1f} KB, {e.get("content_type", "?")}){note}')
    elif action == 'upload':
        paths = [str(Path(p).absolute()) for p in args.path]
        # 'path' kept alongside 'paths' so an old connector (pre-multi-file)
        # still uploads the first file instead of erroring.
        req = {'op': 'attachment-upload', 'path': paths[0], 'paths': paths}
        if args.label:
            req['label'] = args.label
        if getattr(args, 'channel', None):
            req['channel'] = args.channel
        try:
            resp = _meta_request(identity, req, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        files = resp.get('files') or [{'filename': resp.get('filename'),
                                       'size': resp.get('size'), 'url': resp.get('url')}]
        print(f'uploaded: msg_id={resp.get("msg_id")}  channel_id={resp.get("channel_id")}  ({len(files)} file(s))')
        for fe in files:
            print(f'  {fe["filename"]}  ({fe["size"]} bytes)  {fe["url"]}')
    elif action == 'download':
        req = {'op': 'attachment-download', 'msg_id': args.msg_id,
               'dest_dir': str(Path(args.dest_dir).absolute())}
        if args.rename:
            req['rename'] = args.rename
        if args.channel:
            req['channel'] = args.channel
        try:
            resp = _meta_request(identity, req, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        saved = resp.get('saved', [])
        if not saved:
            print(f'(no attachments in {args.msg_id}: {resp.get("reason", "")})', file=sys.stderr)
            sys.exit(2)
        for s in saved:
            print(f'saved: {s["path"]}  ({s["size"]} bytes, {s["filename"]})')


def creds_cli(args):
    identity = args.identity
    timeout = args.timeout
    action = args.creds_action
    if action == 'list':
        try:
            resp = _meta_request(identity, {'op': 'creds-list'}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        entries = resp.get('entries', [])
        if args.json:
            if not args.show_passwords:
                for e in entries:
                    e.pop('password', None)
            print(json.dumps(entries, indent=2))
            return
        if not entries:
            print('(no credentials)')
            return
        for e in entries:
            pw = e.get('password', '') if args.show_passwords else '<hidden — pass --show-passwords>'
            print(f'{e["domain"]:<30}  login={e["login"]!r:<40}  password={pw!r}  (by {e.get("author")} {e.get("created", "")[:19]})')
    elif action == 'get':
        req = {'op': 'creds-get', 'domain': args.domain}
        if args.login:
            req['login'] = args.login
        try:
            resp = _meta_request(identity, req, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        entries = resp.get('entries', [])
        if not entries:
            tgt = f'{args.domain!r}' + (f' + login {args.login!r}' if args.login else '')
            print(f'(no entry for {tgt})', file=sys.stderr)
            sys.exit(2)
        if args.json:
            print(json.dumps(entries, indent=2))
            return
        for e in entries:
            print(f'domain:   {e["domain"]}')
            print(f'login:    {e["login"]}')
            print(f'password: {e["password"]}')
            print(f'(by {e.get("author")} {e.get("created", "")[:19]})')
            print()
    elif action == 'add':
        try:
            resp = _meta_request(identity, {
                'op': 'creds-add',
                'domain': args.domain, 'login': args.login, 'password': args.password,
                'upsert': bool(args.upsert),
            }, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        print(f'{resp.get("action")}: {resp.get("msg_id", "")} {resp.get("reason", "")}'.strip())


def conversation_cli(identity, n, channel_id=None, before_id=None, after_id=None, grep=None, timeout=15.0, as_json=False):
    req = {'op': 'conversation', 'limit': n}
    if channel_id:
        req['channel_id'] = channel_id
    if before_id:
        req['before_id'] = before_id
    if after_id:
        req['after_id'] = after_id
    if grep:
        req['grep'] = grep
    try:
        resp = _meta_request(identity, req, timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error from connector: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    msgs = resp.get('messages', [])
    if as_json:
        print(json.dumps(msgs, indent=2))
        return
    if not msgs:
        print('(no messages matched)')
        return
    for m in msgs:
        ts = m.get('created', '').replace('T', ' ').split('.')[0].split('+')[0]
        att = ''
        if m.get('attachments'):
            att = '  [+' + ','.join(a['filename'] for a in m['attachments']) + ']'
        print(f'[{ts} from {m["from"]} msg_id={m["msg_id"]}]{att} {m["content"]}')


def register_cli(identity, body, timeout=15.0):
    try:
        resp = _meta_request(identity, {'op': 'register', 'body': body}, timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error from connector: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    print(f'{resp.get("action")}: msg_id={resp.get("msg_id")}', flush=True)


def list_agents_cli(identity, timeout=30.0, as_json=False, short=False):
    try:
        resp = _meta_request(identity, {'op': 'list-agents'}, timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error from connector: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    agents = resp.get('agents', [])
    if short:
        # Names only, one row per DISTINCT user: aliases sharing a user_id
        # (e.g. a human registered as zmatek/user/owner) collapse to one
        # comma-separated row, in directory order.
        groups = {}
        for a in agents:
            groups.setdefault(str(a.get('user_id')), []).append(a['identity'])
        if as_json:
            print(json.dumps(groups, indent=2))
        elif not groups:
            print('(no agents in directory)')
        else:
            for names in groups.values():
                print(', '.join(names))
        return
    if as_json:
        print(json.dumps(agents, indent=2))
        return
    if not agents:
        print('(no agents in directory)')
        return
    for a in agents:
        print(f'=== {a["identity"]} (user_id={a["user_id"]}) ===')
        print(a.get('body', '').rstrip())
        print()


def topic_cli(args):
    identity = args.identity
    timeout = args.timeout
    if args.topic_action == 'get':
        try:
            resp = _meta_request(identity, {'op': 'get-topic', 'channel': args.channel}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(resp, indent=2))
            return
        name = resp.get('channel_name') or resp.get('channel_id')
        topic = resp.get('topic')
        print(f'#{name} topic: {topic!r}' if topic else f'#{name} has no topic set')
    elif args.topic_action == 'set':
        try:
            resp = _meta_request(identity, {'op': 'set-topic', 'channel': args.channel, 'topic': args.topic}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        name = resp.get('channel_name') or resp.get('channel_id')
        print(f'set #{name} topic to: {resp.get("topic")!r}')


def pins_cli(args):
    identity = args.identity
    timeout = args.timeout
    action = args.pins_action
    if action == 'list':
        try:
            resp = _meta_request(identity, {'op': 'pins-list', 'channel': args.channel, 'limit': args.limit}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        entries = resp.get('entries', [])
        if args.json:
            print(json.dumps(entries, indent=2))
            return
        if not entries:
            print('(no pinned messages)')
            return
        for e in entries:
            ts = e.get('created', '').replace('T', ' ')[:19]
            att = '  [+' + ','.join(a['filename'] for a in e['attachments']) + ']' if e.get('attachments') else ''
            body = (e.get('content') or '').replace('\n', ' ')
            if len(body) > 120:
                body = body[:117] + '...'
            print(f'[{ts} from {e["from"]} msg_id={e["msg_id"]}]{att} {body}')
    else:  # pin / unpin
        op = 'pin' if action == 'pin' else 'unpin'
        try:
            resp = _meta_request(identity, {'op': op, 'channel': args.channel, 'msg_id': args.msg_id}, timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        print(f'{action}ned msg_id={resp.get("msg_id")} in channel {resp.get("channel_id")}')


def context_cli(args):
    identity = args.identity
    try:
        resp = _meta_request(identity, {'op': 'context', 'channel': args.channel}, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(resp, indent=2))
        return
    ch = resp.get('channel') or {}
    g = resp.get('guild') or {}
    loc = f"#{ch.get('name') or ch.get('id')} ({ch.get('kind')}/{ch.get('type')})"
    if g:
        loc += f" in {g.get('name')}"
    print(loc)
    if ch.get('category'):
        print(f"  category: {ch['category'].get('name')}")
    print(f"  topic: {ch.get('topic')!r}")
    pins = resp.get('pinned_messages', [])
    print(f"  pinned messages: {len(pins)}")
    for p in pins:
        print(f"    [{p.get('created', '')[:19]} from {p.get('from')} msg_id={p.get('msg_id')}] -> {p.get('path')}")
    posts = resp.get('pinned_forum_posts', [])
    print(f"  pinned forum posts: {len(posts)}")
    for fp in posts:
        print(f"    {fp.get('name')!r} (thread {fp.get('thread_id')}, {fp.get('message_count')} msgs)")


def move_cli(args):
    if args.to_forum and not args.title:
        print('--title is required with --to-forum (a forum post needs a name)',
              file=sys.stderr)
        sys.exit(2)
    if args.title and not args.to_forum:
        print('--title only applies to --to-forum', file=sys.stderr)
        sys.exit(2)
    if args.to_dm:
        dest, kind = args.to_dm, 'dm'
    elif args.to_forum:
        dest, kind = args.to_forum, 'forum'
    else:
        dest, kind = args.to, 'channel'

    req = {'op': 'move', 'source': args.source, 'limit': args.n,
           'dest': dest, 'dest_kind': kind, 'title': args.title,
           'keep': bool(args.keep), 'before_id': args.before,
           'dry_run': bool(args.dry_run)}
    try:
        resp = _meta_request(args.identity, req, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(resp, indent=2))
        return

    if resp.get('dry_run'):
        print(f"would move {resp.get('count')} message(s) "
              f"from #{resp.get('source_name') or resp.get('source_id')}:")
        for e in resp.get('messages', []):
            att = f"  +{e['attachments']} attachment(s)" if e.get('attachments') else ''
            print(f"  [{(e.get('created') or '')[:19]} {e.get('from')} "
                  f"msg_id={e.get('msg_id')}]{att}  {e.get('preview')!r}")
        return

    verb = 'copied' if resp.get('kept') else 'moved'
    where = resp.get('thread_id') or resp.get('dest_id')
    print(f"{verb} {resp.get('moved')}/{resp.get('count')} message(s) "
          f"from #{resp.get('source_name') or resp.get('source_id')} -> {where}"
          f" ({resp.get('deleted')} original(s) deleted)")
    if resp.get('jump_url'):
        print(f"  {resp['jump_url']}")
    for f in resp.get('failures', []):
        print(f"  FAILED [{f.get('stage')}] msg_id={f.get('msg_id')}: {f.get('error')}",
              file=sys.stderr)
    if resp.get('failures'):
        sys.exit(1)


def forum_cli(args):
    identity = args.identity
    if args.forum_action == 'list':
        req = {'op': 'forum-list', 'channel': args.channel, 'archived': bool(args.archived), 'limit': args.limit}
        try:
            resp = _meta_request(identity, req, args.timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(resp, indent=2))
            return
        posts = resp.get('posts', [])
        print(f"#{resp.get('forum_name')} ({resp.get('count')} posts):")
        for p in posts:
            flags = [f for f, on in (('PINNED', p.get('pinned')), ('archived', p.get('archived')), ('locked', p.get('locked'))) if on]
            fl = ('  [' + ','.join(flags) + ']') if flags else ''
            tags = ('  #' + ' #'.join(p['tags'])) if p.get('tags') else ''
            print(f"  {(p.get('created') or '')[:19]}  thread_id={p['thread_id']}  ({p.get('message_count')} msgs){fl}{tags}  {p['name']!r}")
    elif args.forum_action == 'create':
        # Discord's forum starter message is capped at 2000 chars and the
        # connector does NOT auto-chunk it (unlike `send`, which chunks up to
        # MAX_BODY_TOTAL) — reject client-side so an over-long body fails with
        # a named limit instead of the raw API 400 (issue #28).
        if len(args.content) > 2000:
            print(f'forum starter body too long: {len(args.content)} > 2000 '
                  f'(Discord forum starter-message limit; `send` auto-chunks but '
                  f'`forum create` does not — shorten the body, or create with the '
                  f'first 2000 chars and post the rest as follow-ups in the thread)',
                  file=sys.stderr, flush=True)
            sys.exit(2)
        tags = [t for t in (args.tags.split(',') if args.tags else []) if t.strip()]
        req = {'op': 'forum-create', 'channel': args.channel, 'name': args.name, 'content': args.content, 'tags': tags}
        try:
            resp = _meta_request(identity, req, args.timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        print(f"created forum post: thread_id={resp.get('thread_id')} "
              f"starter_msg_id={resp.get('starter_msg_id')}  {resp.get('jump_url')}")
    elif args.forum_action == 'delete':
        try:
            resp = _meta_request(identity, {'op': 'forum-delete', 'channel': args.channel}, args.timeout)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if not resp.get('ok'):
            print(f'error: {resp.get("error", resp)}', file=sys.stderr)
            sys.exit(1)
        print(f"deleted forum post thread_id={resp.get('thread_id')}")


def message_cli(args):
    identity = args.identity
    act = args.message_action
    if act == 'edit':
        req = {'op': 'message-edit', 'channel': args.channel, 'msg_id': args.msg_id, 'content': args.content}
    elif act == 'delete':
        req = {'op': 'message-delete', 'channel': args.channel, 'msg_id': args.msg_id}
    else:  # react / unreact
        req = {'op': 'message-react', 'channel': args.channel, 'msg_id': args.msg_id,
               'emoji': args.emoji, 'remove': act == 'unreact'}
    try:
        resp = _meta_request(identity, req, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    if act in ('react', 'unreact'):
        print(f"{act}ed {resp.get('emoji')} on msg_id={resp.get('msg_id')}")
    else:
        verb = 'edited' if act == 'edit' else 'deleted'
        print(f"{verb} msg_id={resp.get('msg_id')} in channel {resp.get('channel_id')}")


def thread_cli(args):
    identity = args.identity
    act = args.thread_action
    req = {'op': 'thread-edit', 'channel': args.channel}
    if act == 'pin':
        req['pinned'] = True
    elif act == 'unpin':
        req['pinned'] = False
    elif act == 'archive':
        req['archived'] = True
    elif act == 'unarchive':
        req['archived'] = False
    elif act == 'lock':
        req['locked'] = True
    elif act == 'unlock':
        req['locked'] = False
    elif act == 'rename':
        req['name'] = args.name
    elif act == 'tags':
        req['tags'] = [t for t in args.tags.split(',') if t.strip()]
    try:
        resp = _meta_request(identity, req, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    print(f"thread {resp.get('thread_id')} edited: {', '.join(resp.get('edited', []))}")


def emoji_cli(args):
    """Custom guild emoji: list / upload / delete / rename.

    `list` is the one you will reach for constantly -- it prints the exact
    `<a:name:id>` ref to paste into a message, so expressing yourself with the
    shared Clawd set is a lookup away rather than a guess."""
    action = args.emoji_action
    if action == 'list':
        resp = _meta_request(args.identity, {'op': 'emoji-list', 'server': args.server}, args.timeout)
        if not resp.get('ok'):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(resp.get('emoji', []), indent=2))
            return 0
        rows = resp.get('emoji', [])
        if args.grep:
            import re as _re
            rx = _re.compile(args.grep, _re.I)
            rows = [e for e in rows if rx.search(e['name'])]
        if args.animated:
            rows = [e for e in rows if e['animated']]
        print(f"{resp.get('server_name')}: {resp.get('count')} emoji "
              f"({resp.get('animated')} animated, {resp.get('static')} static)"
              + (f" — showing {len(rows)}" if len(rows) != resp.get('count') else ''))
        for e in rows:
            flag = 'GIF' if e['animated'] else '   '
            print(f"  {flag}  :{e['name']}:{'' if e['available'] else '  (UNAVAILABLE)'}")
            print(f"        paste: {e['ref']}")
        return 0
    if action == 'upload':
        path = os.path.abspath(os.path.expanduser(args.path))
        if not os.path.isfile(path):
            print(f'error: no such file: {path}', file=sys.stderr)
            return 1
        raw = open(path, 'rb').read()
        if len(raw) > 256 * 1024:
            print(f"error: {len(raw)/1024:.0f} KB, over Discord's 256 KB emoji cap", file=sys.stderr)
            return 1
        name = args.name or os.path.splitext(os.path.basename(path))[0]
        resp = _meta_request(args.identity, {'op': 'emoji-upload', 'server': args.server,
                                             'name': name,
                                             'image': base64.b64encode(raw).decode()}, args.timeout)
        if not resp.get('ok'):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        print(f"created :{resp['name']}: id={resp['id']} animated={resp['animated']} "
              f"({resp['bytes']} bytes) in {resp['server_name']}")
        print(f"  paste: {resp['ref']}")
        return 0
    if action == 'delete':
        resp = _meta_request(args.identity, {'op': 'emoji-delete', 'server': args.server,
                                             'ref': args.emoji}, args.timeout)
        if not resp.get('ok'):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        print(f"deleted :{resp['name']}: (id={resp['id']}) from {resp['server_name']}")
        print("  note: <:name:id> refs in existing messages now point at a dead id")
        return 0
    if action == 'rename':
        resp = _meta_request(args.identity, {'op': 'emoji-rename', 'server': args.server,
                                             'ref': args.emoji, 'name': args.name}, args.timeout)
        if not resp.get('ok'):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        print(f"renamed :{resp['old_name']}: -> :{resp['name']}: (id unchanged: {resp['id']})")
        print(f"  paste: {resp['ref']}")
        return 0
    print(f'unknown emoji action: {action}', file=sys.stderr)
    return 2


def servers_cli(args):
    try:
        resp = _meta_request(args.identity, {'op': 'list-servers'}, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    servers = resp.get('servers', [])
    if args.json:
        print(json.dumps(servers, indent=2))
        return
    if not servers:
        print('(bot is in no servers)')
        return
    for s in servers:
        main = '  [MAIN]' if s.get('is_main') else ''
        print(f"{s['id']}  {s['name']!r}  ({s.get('member_count')} members, {s.get('channel_count')} channels){main}")


def channels_cli(args):
    identity = args.identity
    action = args.channels_action
    if action == 'list':
        req = {'op': 'list-channels'}
        if args.server:
            req['server'] = args.server
    elif action == 'create':
        req = {'op': 'channel-create', 'ctype': args.ctype, 'name': args.name,
               'category': args.category, 'topic': args.topic}
        if args.server:
            req['server'] = args.server
    elif action == 'edit':
        req = {'op': 'channel-edit', 'channel': args.channel}
        if args.name is not None:
            req['name'] = args.name
        if args.category is not None:
            req['category'] = args.category
        if args.topic is not None:
            req['topic'] = args.topic
    else:  # delete
        req = {'op': 'channel-delete', 'channel': args.channel}
    try:
        resp = _meta_request(identity, req, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    if action == 'create':
        cat = f"  in [{resp.get('category')}]" if resp.get('category') else ''
        print(f"created {resp.get('type')} channel: id={resp.get('id')} {resp.get('name')!r}{cat}")
        return
    if action == 'edit':
        print(f"edited channel id={resp.get('id')} {resp.get('name')!r}: {', '.join(resp.get('edited', []))}")
        return
    if action == 'delete':
        print(f"deleted channel id={resp.get('id')} {resp.get('name')!r}")
        return
    if args.json:
        print(json.dumps(resp, indent=2))
        return
    print(f"{resp.get('server_name')} ({resp.get('count')} channels):")
    for c in resp.get('channels', []):
        tc = f"  threads={c['active_thread_count']}" if 'active_thread_count' in c else ''
        cat = f"  [{c['category']}]" if c.get('category') else ''
        print(f"  {c['id']}  {(c.get('type') or '?'):<14} {c.get('name')!r}{cat}{tc}")


# --- Status-plugin support (module-level so it is unit-testable without a gateway) ---

# Discord presence "activity" kinds a status plugin may set via ctx.set_status.
STATUS_KINDS = ('playing', 'listening', 'watching', 'competing', 'custom', 'streaming')


def build_activity(text, kind='playing', url=None):
    '''Map (text, kind) -> a discord activity object for client.change_presence.

    `playing`   -> discord.Game(text)            (the classic "Playing X")
    `listening`/`watching`/`competing` -> discord.Activity(type=..., name=text)
    `custom`    -> discord.CustomActivity(text)  (the free-form custom status line)
    `streaming` -> discord.Streaming(name=text, url=url)  (url required)
    Raises ValueError on empty text, an unknown kind, or streaming without a url.'''
    import discord
    text = (text or '').strip()
    if not text:
        raise ValueError('status text is empty')
    kind = (kind or 'playing').lower()
    if kind == 'playing':
        return discord.Game(name=text)
    if kind == 'custom':
        return discord.CustomActivity(name=text)
    if kind == 'streaming':
        if not url:
            raise ValueError('streaming status requires a url')
        return discord.Streaming(name=text, url=url)
    if kind in ('listening', 'watching', 'competing'):
        return discord.Activity(type=getattr(discord.ActivityType, kind), name=text)
    raise ValueError(f'unknown status kind {kind!r} (one of {", ".join(STATUS_KINDS)})')


def status_presence_record(text, kind='playing', url=None, status=None):
    '''Return every field needed to replay a plugin presence after reconnect.'''
    return {
        'text': text,
        'kind': kind,
        'url': url,
        'status': status,
        'set_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }


async def replay_last_presence(client, state):
    '''Re-assert the last plugin presence on a fresh gateway connection.'''
    last = state.get('status_last')
    if not last:
        return False
    import discord
    activity = build_activity(
        last.get('text'), kind=last.get('kind', 'playing'),
        url=last.get('url'))
    raw_status = last.get('status')
    status = (getattr(discord.Status, str(raw_status).lower(), None)
              if raw_status else None)
    await client.change_presence(activity=activity, status=status)
    return True


def status_plugin_failure_is_transport(exc):
    '''True when a status-plugin failure came from the gateway transport.

    Status plugins call Discord only through ``ctx.set_status``/``ctx.clear``.
    A reset from those calls says nothing about the plugin's correctness, so it
    must survive until ``on_ready`` can restart it.  Avoid importing aiohttp
    here: discord.py owns that optional dependency, and class provenance plus
    the standard connection hierarchy gives us a stable, testable boundary.
    Wrapped transport errors are common, hence the bounded cause walk.
    '''
    current = exc
    seen = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        cls = type(current)
        module = getattr(cls, '__module__', '')
        name = getattr(cls, '__name__', '')
        if module.startswith(('aiohttp.', 'discord.')) and (
                name in {'ClientConnectionError', 'ClientConnectionResetError',
                         'ClientOSError', 'ServerConnectionError',
                         'ServerDisconnectedError', 'GatewayNotFound'} or
                name.endswith(('ConnectionError', 'ConnectionResetError'))):
            return True
        current = current.__cause__ or current.__context__
    return False


class StatusPluginGatewayTransportError(Exception):
    '''Private boundary marker for a failed Discord presence call.'''


async def status_plugin_gateway_call(awaitable):
    '''Await one plugin-requested gateway call and mark transport failures.

    Only ``_StatusContext`` uses this wrapper.  A plugin's own network or timer
    exception therefore stays an ordinary plugin failure even when it has the
    same concrete type as a Discord transport failure.
    '''
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if status_plugin_failure_is_transport(exc):
            raise StatusPluginGatewayTransportError(str(exc)) from exc
        raise


async def run_status_plugin_task(run_fn, context, *, finished,
                                 transport_failed, plugin_failed):
    '''Run one plugin task with an explicit gateway-vs-plugin boundary.'''
    try:
        await run_fn(context)
    except asyncio.CancelledError:
        raise
    except StatusPluginGatewayTransportError as exc:
        await transport_failed(exc)
        return 'retrying'
    except Exception as exc:
        await plugin_failed(exc)
        return 'crashed'
    await finished()
    return 'finished'


async def recover_status_plugin_after_gateway(state, *, restart, replay, clear):
    '''Run the status-plugin recovery state machine for READY or RESUMED.

    ``restart`` returns True after starting one replacement task, None while an
    existing task is still settling, and False when the installed slot cannot
    be restarted.  The latter must clear the old presence after reconnect.
    '''
    if state.get('status_state') == 'clearing':
        if not await clear():
            return 'clear-pending'
        state['status_state'] = state.pop('status_clear_terminal', 'empty')
        state['status_last'] = None
        return 'cleared'
    if state.get('status_state') == 'retrying':
        restarted = await restart()
        if restarted is None:
            return 'pending'
        if restarted:
            return 'restarted'
        terminal = state.get('status_state')
        if terminal in (None, 'retrying', 'clearing'):
            terminal = 'empty'
        state['status_clear_terminal'] = terminal
        state['status_state'] = 'clearing'
        if not await clear():
            return 'clear-pending'
        state['status_state'] = state.pop('status_clear_terminal')
        state['status_last'] = None
        return 'cleared'
    if await replay():
        return 'replayed'
    return 'unchanged'


STATUS_PLUGIN_NAME = 'plugin.py'      # the installed copy inside the slot
STATUS_MANIFEST_NAME = 'manifest.json'
# Built-in default status plugin (ships in the bundle next to the discord skill).
# The connector loads it at startup when no status plugin is set; a `status-plugin
# set` replaces it. It is also the reference example for authoring a custom one.
# The built-in status-plugin paths and flavor map are refreshed above.


def default_status_plugin(flavor):
    """The built-in status plugin for the identity flavor.

    'kimi' -> kimi-code wire, 'codex' -> Codex rollout, else Claude transcript.
    A flavor without its own adapter must NOT fall back to another provider's:
    that is how a connector latches onto a foreign transcript and reports
    activity that is not its session's.
    """
    return _FLAVOR_STATUS_PLUGINS.get(flavor, DEFAULT_STATUS_PLUGIN)


def status_plugin_slot(identity):
    '''Single-slot dir holding the installed status plugin (plugin.py + manifest.json).
    Session-scoped: swept empty at connector startup, deleted on uninstall/shutdown,
    so a plugin never outlives the connector it was installed into.'''
    return state_dir(identity) / 'status-plugin'


def sweep_status_plugin(identity):
    'Uninstall: remove the whole slot dir. Idempotent (no error if absent).'
    import shutil
    slot = status_plugin_slot(identity)
    if slot.exists():
        shutil.rmtree(slot, ignore_errors=True)


def install_status_plugin(identity, src_path):
    '''Sweep the slot, copy src_path -> slot/plugin.py, return the slot plugin path.
    Does NOT write the manifest or load the module — the caller does that, so a load
    failure can sweep cleanly. The original src_path is never modified. Raises
    ValueError (not a .py) / FileNotFoundError (missing).'''
    import shutil
    p = Path(src_path)
    if p.suffix != '.py':
        raise ValueError(f'status plugin must be a .py file: {src_path!r}')
    if not p.is_file():
        raise FileNotFoundError(f'no such file: {src_path}')
    sweep_status_plugin(identity)
    slot = status_plugin_slot(identity)
    slot.mkdir(parents=True, exist_ok=True)
    dest = slot / STATUS_PLUGIN_NAME
    shutil.copy2(p, dest)
    return str(dest)


def write_status_manifest(identity, manifest):
    'Persist the slot manifest (atomic). Slot must already exist (install first).'
    slot = status_plugin_slot(identity)
    slot.mkdir(parents=True, exist_ok=True)
    path = slot / STATUS_MANIFEST_NAME
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    tmp.replace(path)


def read_status_manifest(identity):
    'Return the slot manifest dict, or None if absent/unreadable.'
    path = status_plugin_slot(identity) / STATUS_MANIFEST_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_status_plugin(path):
    '''Import a status plugin .py by path; return its `run` coroutine function.
    Raises FileNotFoundError (missing), ValueError (no run / run not `async def`),
    or the underlying exception for a broken module (e.g. SyntaxError). The module
    name is derived from the path so re-loading the same slot copy reuses the entry
    rather than leaking a new sys.modules object per install.'''
    import importlib.util
    import inspect as _inspect
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f'no such file: {path}')
    mod_name = f'_status_plugin_{abs(hash(str(p.resolve())))}'
    spec = importlib.util.spec_from_file_location(mod_name, str(p))
    if spec is None or spec.loader is None:
        raise ValueError(f'cannot load module spec from {path!r}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    run = getattr(module, 'run', None)
    if run is None:
        sys.modules.pop(mod_name, None)
        raise ValueError('status plugin defines no run(ctx)')
    if not _inspect.iscoroutinefunction(run):
        sys.modules.pop(mod_name, None)
        raise ValueError('status plugin run(ctx) must be `async def`')
    return run


# ---------------------------------------------------------------- extensions
# An extension is an identity-scoped module the connector loads at startup.
# Unlike a status plugin (session-scoped, presence-only, swept on every
# connector start), an extension's registration is DURABLE: it lives beside the
# identity's token, not under STATE_ROOT, because STATE_ROOT is a temp dir and a
# reboot would silently drop every binding an extension holds.

def extension_dir(flavor=None):
    base = _EXTENSION_FLAVOR_DIRS.get(flavor or 'claude', lambda: TOKEN_DIR)()
    return Path(base) / 'extensions'


def extension_registry_path(identity, flavor=None):
    return extension_dir(flavor) / f'{identity}.json'


def read_extension_registry(identity, flavor=None):
    '''Registration + extension-owned store. Never raises: a corrupt or absent
    file reads as {} so a bad write cannot wedge the connector at startup.'''
    try:
        data = json.loads(extension_registry_path(identity, flavor).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_extension_registry(identity, data, flavor=None):
    p = extension_registry_path(identity, flavor)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(p)          # atomic: a crash mid-write cannot truncate the live file
    try:
        p.chmod(0o600)
    except OSError:
        pass


def heartbeat_due(last_date, today):
    '''True when no heartbeat has been emitted for `today` (a UTC date string).
    Deliberately does NOT backfill: a connector down for a week emits one
    heartbeat on return, not seven.'''
    return last_date != today


def load_extension(path):
    '''Import an extension .py by path; return (module, setup, command).

    Raises FileNotFoundError (missing), ValueError (no setup / wrong shape), or
    the underlying exception for a broken module. The module name is derived
    from the resolved path so re-loading the same file reuses its sys.modules
    entry rather than leaking one per load.'''
    import importlib.util
    import inspect as _inspect
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f'no such file: {path}')
    mod_name = f'_mb_extension_{abs(hash(str(p.resolve())))}'
    spec = importlib.util.spec_from_file_location(mod_name, str(p))
    if spec is None or spec.loader is None:
        raise ValueError(f'cannot load module spec from {path!r}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    setup = getattr(module, 'setup', None)
    if setup is None:
        sys.modules.pop(mod_name, None)
        raise ValueError('extension defines no setup(ctx)')
    if not _inspect.iscoroutinefunction(setup):
        sys.modules.pop(mod_name, None)
        raise ValueError('extension setup(ctx) must be `async def`')
    command = getattr(module, 'command', None)
    if command is not None and not _inspect.iscoroutinefunction(command):
        sys.modules.pop(mod_name, None)
        raise ValueError('extension command(ctx, argv) must be `async def`')
    return module, setup, command


def cancel_tracked_tasks(tasks):
    """Cancel and forget every task an extension generation spawned.

    Reloading builds a new context, so without this each reload would leave the
    previous generation's loops running -- the same accumulation the listener
    registry prevents for handlers.
    """
    cancelled = 0
    for t in list(tasks or ()):
        try:
            if not t.done():
                t.cancel()
                cancelled += 1
        except Exception:
            pass
    if tasks is not None:
        tasks.clear()
    return cancelled


class _ExtensionContext:
    '''Surface handed to an extension's setup(ctx) / command(ctx, argv).

    `store` is a plain dict loaded from the identity's durable registry; the
    extension mutates it and calls `save()`. Registration keys in that file are
    preserved on save, so an extension cannot unregister itself by accident.'''

    def __init__(self, client, identity, flavor, log, emit,
                 listeners=None, installed=None, tasks=None, deliver=None):
        self.client = client
        self.identity = identity
        self.flavor = flavor
        self._log = log
        self._emit = emit
        self._deliver = deliver
        store = read_extension_registry(identity, flavor).get('store')
        self.store = store if isinstance(store, dict) else {}
        # Both live in connector state, not per-context: reloading an extension
        # builds a NEW context, and a dispatcher installed per context would
        # capture the previous dispatcher as `prior` and chain, so one click
        # would fire every generation of the handler.
        self._listeners = {} if listeners is None else listeners
        self._installed = set() if installed is None else installed
        self._tasks = [] if tasks is None else tasks

    def log(self, msg):
        self._log(f'[ext] {msg}')

    def emit(self, obj):
        self._emit(obj)

    async def deliver(self, msg, source='extension'):
        '''Hand a message to the connector's arriving-message pipeline.

        `emit` publishes a custom event and nothing else, so a relayed message
        arrived with a truncated body, no attachment metadata, no inbox JSON,
        no receipt reaction, and no record for `send --reply-to` to resolve --
        a relay silently dropped a member's PDF (issue #220). `deliver` runs
        the same path a mention or DM takes, making the message first-class:
        the enriched inbox record, a `message` event carrying its path, the
        receipt reaction, and reply-ability.

        Delivery is idempotent -- a message already in the inbox is not
        written again -- so relaying one that also mentions the bot is safe.
        Returns the inbox path.
        '''
        if self._deliver is None:
            raise RuntimeError(
                'this extension context cannot deliver messages: the '
                'connector did not supply its inbox writer')
        return await self._deliver(msg, source=source)

    def save(self):
        reg = read_extension_registry(self.identity, self.flavor)
        reg['store'] = self.store
        write_extension_registry(self.identity, reg, self.flavor)

    def spawn(self, coro):
        """Run a coroutine as a background task tied to this extension.

        Tracked so a reload cancels it: an extension that spawns a poll loop
        would otherwise leave one running per reload.
        """
        task = self.client.loop.create_task(coro)
        self._tasks.append(task)
        return task

    def on(self, event, handler):
        '''Subscribe to a gateway event by bare name ('raw_reaction_add').

        discord.Client (unlike commands.Bot) has no add_listener: it resolves
        handlers by attribute name, which is all @client.event does. So the
        first subscription for an event installs ONE dispatcher attribute and
        every later one joins its list.

        Any handler the connector itself already registered for that event is
        captured and called first -- installing a dispatcher over on_message
        would otherwise silently unhook the mailbox.

        A raising handler is logged and unsubscribed rather than propagating
        into the gateway task: a dead feature beats a dead connector.'''
        name = f'on_{event}'

        async def _guarded(*a, **kw):
            try:
                await handler(*a, **kw)
            except Exception as exc:
                self.log(f'handler {event} failed, unsubscribing: '
                         f'{type(exc).__name__}: {exc}')
                try:
                    self._listeners.get(name, []).remove(_guarded)
                except ValueError:
                    pass

        listeners = self._listeners.setdefault(name, [])
        if name not in self._installed:
            prior = getattr(self.client, name, None)
            registry = self._listeners

            async def _dispatch(*a, **kw):
                if prior is not None:
                    await prior(*a, **kw)
                for fn in list(registry.get(name, ())):
                    await fn(*a, **kw)

            setattr(self.client, name, _dispatch)
            self._installed.add(name)
        listeners.append(_guarded)
        return _guarded


def extension_cli(args):
    identity = args.identity
    act = args.extension_action
    if act == 'set':
        req = {'op': 'extension-set', 'path': str(Path(args.path).absolute())}
    elif act == 'remove':
        req = {'op': 'extension-remove'}
    elif act == 'call':
        req = {'op': 'extension-call', 'argv': list(args.argv)}
    else:
        req = {'op': 'extension-list'}
    try:
        resp = _meta_request(identity, req, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        return 1
    if resp.get('error'):
        print(f"error: {resp['error']}", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0


def status_plugin_cli(args):
    identity = args.identity
    act = args.status_plugin_action
    if act == 'set':
        req = {'op': 'status-plugin-set', 'path': str(Path(args.path).absolute())}
    elif act == 'remove':
        req = {'op': 'status-plugin-remove'}
    else:
        req = {'op': 'status-plugin-list'}
    try:
        resp = _meta_request(identity, req, args.timeout)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not resp.get('ok'):
        print(f'error: {resp.get("error", resp)}', file=sys.stderr)
        sys.exit(1)
    if act == 'set':
        print(f"installed status plugin {resp.get('name')!r}  (source: {resp.get('source')})")
    elif act == 'remove':
        print('removed status plugin' if resp.get('removed') else '(no status plugin was installed)')
    else:
        if not resp.get('installed') and not resp.get('running'):
            print('(no status plugin installed)')
            return
        man = resp.get('manifest') or {}
        print(f"name:    {man.get('name')}")
        print(f"source:  {man.get('source')}")
        print(f"installed: {man.get('installed_at')}")
        print(f"state:   {resp.get('state')}  (running={resp.get('running')})")
        ls = resp.get('last_status')
        if ls:
            print(f"status:  kind={ls.get('kind')} text={ls.get('text')!r} set_at={ls.get('set_at')}")
        if resp.get('error'):
            print(f"error:   {resp.get('error')}")


__all__ = [
    name for name in globals()
    if (not name.startswith('__')
        and not name.startswith('_discord_mb_class_')
        and name != 'SCRIPT_ROOT')]
