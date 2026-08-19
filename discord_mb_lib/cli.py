"""Argument parser and command dispatch for the Discord mailbox."""

from . import core as _core
from .core import *
from .core import __version__
from .storage import *
from .connector import *

# --- CLI ---


def _cli():
    for _stream in (sys.stdout, sys.stderr):
        _reconf = getattr(_stream, 'reconfigure', None)  # absent on non-TextIOWrapper streams
        if _reconf is not None:
            try:
                _reconf(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # not a reconfigurable stream (redirected/piped)
    p = argparse.ArgumentParser(prog='discord_mb.py', description=__doc__)
    p.add_argument('--version', action='version',
                   version=f'discord_mb.py {__version__}')
    sub = p.add_subparsers(dest='command')

    s = sub.add_parser('send', help='Queue a message for delivery via connector',
                       description='Queue a text message. Body is text-only (no attachment flag here); '
                                   'use `discord_mb.py attachments upload <path>` to post a file to #attachments, '
                                   'then reference its message-id or url in the body if needed.')
    s.add_argument('identity', help='Sending identity (used to pick outbox dir)')
    s.add_argument('to', help='Recipient identity, comma-separated list ("a,b,c"), "all" for broadcast, or "nobody" for a user-less send (posts with no mention)')
    s.add_argument('subject', help='Subject line')
    s.add_argument('body', help=f'Body (<= {MAX_BODY_TOTAL} chars; over {MAX_BODY} auto-chunks into a numbered train)')
    s.add_argument('--reply-to', dest='reply_to', default=None,
                   help='Discord message ID to reply to. The reply is posted in the '
                        'channel the original message lives in (resolved from the '
                        'inbox entry): a DM original gets a same-DM reply without '
                        'recipient tagging; a guild-channel original gets a reply in '
                        'that same channel with the usual <@uid> mention and reply '
                        'ref. Only non-reply sends default to #agents.')
    s.add_argument('--channel', dest='channel', default=None,
                   help='Target channel for a NON-reply send: a numeric channel id or a '
                        'connector-known name (bridge/attachments/credentials/directory). '
                        'Default: #agents bridge. Ignored when --reply-to is set (a reply '
                        'always lands in the original message\'s channel).')
    s.add_argument('--dm', action='store_true',
                   help='Send as a direct message (one recipient: identity or raw user id)')
    s.add_argument('--attach', action='append', default=None, metavar='PATH',
                   help='Attach a local file (repeatable; ~10MB Discord cap each; rides the first chunk)')
    s.add_argument('--wait', action='store_true',
                   help='Send via the connector meta channel and print msg_id/channel_id (surfaces pin/send failures)')
    s.add_argument('--timeout', type=float, default=60.0,
                   help='Max wait for the --wait response (default 60s)')
    s.add_argument('--pin', action='store_true',
                   help='Pin the message in its channel after sending (needs Manage Messages).')

    at = sub.add_parser('attachments', help='#attachments channel — upload/download/list files (agents AND user uploads)')
    at.add_argument('identity', help='Identity whose connector serves the request')
    at_sub = at.add_subparsers(dest='att_action', required=True)
    at_ls = at_sub.add_parser('list', help='List recent attachments in the channel (includes user uploads)')
    at_ls.add_argument('--limit', type=int, default=50, help='Max messages scanned (1-200, default 50)')
    at_ls.add_argument('--json', action='store_true')
    at_up = at_sub.add_parser('upload', help='Upload local file(s) on one message (max 10; default #attachments; --channel to target another)')
    at_up.add_argument('path', nargs='+', help='Absolute path(s) to local file(s) — up to 10 per message')
    at_up.add_argument('--label', default=None, help='Free-form note posted with the attachment')
    at_up.add_argument('--channel', default=None, help='Target channel: numeric id (any server) or connector-known name (bridge/attachments/credentials/directory). Default #attachments. For a task with its own channel, pass that channel id so the file lands where the task lives.')
    at_dl = at_sub.add_parser('download', help='Download all attachments from a specific message')
    at_dl.add_argument('msg_id', help='Discord message ID in the target channel (default: #attachments)')
    at_dl.add_argument('dest_dir', help='Local directory to save into (created if missing)')
    at_dl.add_argument('--rename', default=None, help='Override filename (only applies if the message has exactly one attachment)')
    at_dl.add_argument('--channel', dest='channel', default=None, help='Channel to fetch from. Accepts a numeric channel ID or a channel name resolvable by the connector (e.g. "bridge"). Defaults to #attachments.')
    at.add_argument('--timeout', type=float, default=60.0, help='Meta-request timeout (uploads/downloads can be slow)')

    cr = sub.add_parser('creds', help='Credential DB stored in #credentials — list/get/add')
    cr.add_argument('identity', help='Identity whose connector serves the request')
    cr_sub = cr.add_subparsers(dest='creds_action', required=True)
    cr_ls = cr_sub.add_parser('list', help='List all credentials')
    cr_ls.add_argument('--show-passwords', action='store_true', help='Print passwords (default: hidden)')
    cr_ls.add_argument('--json', action='store_true')
    cr_get = cr_sub.add_parser('get', help='Fetch entries for a domain (all logins); filter with --login')
    cr_get.add_argument('domain')
    cr_get.add_argument('--login', default=None, help='Filter to a specific login (exact, case-insensitive)')
    cr_get.add_argument('--json', action='store_true')
    cr_add = cr_sub.add_parser('add', help='Post a new credential (skips if domain already present unless --upsert)')
    cr_add.add_argument('domain')
    cr_add.add_argument('login')
    cr_add.add_argument('password')
    cr_add.add_argument('--upsert', action='store_true', help='Post anyway if an entry exists; latest post wins on get')
    cr.add_argument('--timeout', type=float, default=30.0)

    cv = sub.add_parser('conversation', help='Fetch last N messages from any channel (default #agents)')
    cv.add_argument('identity', help='Identity whose connector serves the request')
    cv.add_argument('n', type=int, help='Number of messages per page (1-100)')
    cv.add_argument('--channel', dest='channel_id', default=None, help='Channel ID (default: #agents bridge)')
    cv.add_argument('--before', dest='before_id', default=None, help='Fetch messages strictly before this message ID (for pagination)')
    cv.add_argument('--after', dest='after_id', default=None, help='Fetch messages strictly after this message ID (oldest-first)')
    cv.add_argument('--grep', default=None, help='Regex filter applied server-side to message content')
    cv.add_argument('--timeout', type=float, default=15.0)
    cv.add_argument('--json', action='store_true')

    rg = sub.add_parser('register', help='Set/replace this identity\'s directory profile (edits if exists, posts if not)')
    rg.add_argument('identity', help='Identity whose connector serves the request (also the bold name in the entry)')
    body_src = rg.add_mutually_exclusive_group(required=True)
    body_src.add_argument('--body', help='Inline freeform markdown body')
    body_src.add_argument('--body-file', help='Read body from file (use - for stdin)')
    rg.add_argument('--timeout', type=float, default=15.0)

    la = sub.add_parser('list-agents', help='Fetch agent directory via this session\'s connector')
    la.add_argument('identity', help='Identity whose connector should serve the request')
    la.add_argument('--timeout', type=float, default=30.0, help='Max wait for connector response (covers cold-start connector bind delay)')
    la.add_argument('--json', action='store_true', help='Emit raw JSON instead of human-readable')
    la.add_argument('--short', action='store_true',
                    help='Names only; identities sharing a user_id collapse to one comma-separated row')

    tp = sub.add_parser('topic', help='Get or set a channel topic (default #agents)')
    tp.add_argument('identity', help='Identity whose connector serves the request')
    tp.add_argument('--timeout', type=float, default=15.0)
    tp_sub = tp.add_subparsers(dest='topic_action', required=True)
    tp_get = tp_sub.add_parser('get', help='Show a channel topic')
    tp_get.add_argument('--channel', dest='channel', default=None,
                        help='Channel id or connector-known name (bridge/attachments/credentials/directory). Default: #agents bridge.')
    tp_get.add_argument('--json', action='store_true')
    tp_set = tp_sub.add_parser('set', help='Set a channel topic (needs Manage Channels)')
    tp_set.add_argument('topic', help='New topic text')
    tp_set.add_argument('--channel', dest='channel', default=None,
                        help='Channel id or connector-known name. Default: #agents bridge.')

    pn = sub.add_parser('pins', help='List / pin / unpin messages in a channel (default #agents)')
    pn.add_argument('identity', help='Identity whose connector serves the request')
    pn.add_argument('--timeout', type=float, default=15.0)
    pn_sub = pn.add_subparsers(dest='pins_action', required=True)
    pn_ls = pn_sub.add_parser('list', help='List pinned messages')
    pn_ls.add_argument('--channel', dest='channel', default=None,
                       help='Channel id or connector-known name. Default: #agents bridge.')
    pn_ls.add_argument('--limit', type=int, default=50, help='Max pins to list (1-50)')
    pn_ls.add_argument('--json', action='store_true')
    pn_pin = pn_sub.add_parser('pin', help='Pin a message by id')
    pn_pin.add_argument('msg_id')
    pn_pin.add_argument('--channel', dest='channel', default=None,
                        help='Channel id or connector-known name. Default: #agents bridge.')
    pn_unpin = pn_sub.add_parser('unpin', help='Unpin a message by id')
    pn_unpin.add_argument('msg_id')
    pn_unpin.add_argument('--channel', dest='channel', default=None,
                          help='Channel id or connector-known name. Default: #agents bridge.')

    cx = sub.add_parser('context', help='Read a channel\'s "memory": topic + category + pins (with bodies) + pinned forum posts (with history)')
    cx.add_argument('identity', help='Identity whose connector serves the request')
    cx.add_argument('--channel', dest='channel', default=None,
                    help='Channel id or connector-known name (bridge/attachments/credentials/directory). Default: #agents bridge.')
    cx.add_argument('--timeout', type=float, default=30.0)
    cx.add_argument('--json', action='store_true', help='Emit the full resolved JSON instead of a summary')

    fm = sub.add_parser('forum', help='Forum channels — list / create posts (threads)')
    fm.add_argument('identity', help='Identity whose connector serves the request')
    fm.add_argument('--timeout', type=float, default=20.0)
    fm_sub = fm.add_subparsers(dest='forum_action', required=True)
    fm_ls = fm_sub.add_parser('list', help="List a forum's posts (pinned/active; + archived with --archived)")
    fm_ls.add_argument('--channel', dest='channel', required=True, help='Forum channel id/name (or a post in it)')
    fm_ls.add_argument('--archived', action='store_true', help='Also include archived posts (fetched, not just cached)')
    fm_ls.add_argument('--limit', type=int, default=100, help='Max posts (1-200)')
    fm_ls.add_argument('--json', action='store_true')
    fm_cr = fm_sub.add_parser('create', help='Create a new forum post (thread + starter message)')
    fm_cr.add_argument('--channel', dest='channel', required=True, help='Forum channel id/name')
    fm_cr.add_argument('name', help='Post title')
    fm_cr.add_argument('content', help='Starter message body')
    fm_cr.add_argument('--tags', default=None, help='Comma-separated forum tag names to apply (must exist in the forum)')
    fm_del = fm_sub.add_parser('delete', help='Delete a forum post (its thread)')
    fm_del.add_argument('--channel', dest='channel', required=True, help='Forum post (thread) id/name to delete')

    mv = sub.add_parser('move', help='Move the last N messages of a channel elsewhere (repost + delete)')
    mv.add_argument('identity')
    mv.add_argument('n', type=int, help='how many of the most recent messages to move (1-100)')
    mv.add_argument('--from', dest='source', default='',
                    help='source channel id or known name (default #agents)')
    mv_dest = mv.add_mutually_exclusive_group(required=True)
    mv_dest.add_argument('--to', help='destination channel or existing thread (id or known name)')
    mv_dest.add_argument('--to-dm', help='destination DM (registered identity or raw user id)')
    mv_dest.add_argument('--to-forum', help='destination forum (id or name) — creates a new post')
    mv.add_argument('--title', help='post title; required with --to-forum')
    mv.add_argument('--before', help='only move messages older than this msg_id')
    mv.add_argument('--keep', action='store_true',
                    help='copy instead of move (leave the originals in place)')
    mv.add_argument('--dry-run', action='store_true',
                    help='list what would move and change nothing')
    mv.add_argument('--json', action='store_true')
    mv.add_argument('--timeout', type=float, default=120.0,
                    help='seconds to wait (re-uploading attachments is slow)')

    mg = sub.add_parser('message', help='Edit / delete / react to a message')
    mg.add_argument('identity', help='Identity whose connector serves the request')
    mg.add_argument('--timeout', type=float, default=15.0)
    mg_sub = mg.add_subparsers(dest='message_action', required=True)
    mg_ed = mg_sub.add_parser('edit', help='Edit a message (bot-authored only — Discord restriction)')
    mg_ed.add_argument('msg_id')
    mg_ed.add_argument('content', help='New message content')
    mg_ed.add_argument('--channel', dest='channel', required=True, help='Channel id/name the message lives in')
    mg_del = mg_sub.add_parser('delete', help='Delete a message (own always; others need Manage Messages)')
    mg_del.add_argument('msg_id')
    mg_del.add_argument('--channel', dest='channel', required=True, help='Channel id/name the message lives in')
    mg_re = mg_sub.add_parser('react', help='Add a reaction (emoji) to a message')
    mg_re.add_argument('msg_id')
    mg_re.add_argument('emoji', help='Unicode emoji (e.g. ✅) or custom emoji as name:id')
    mg_re.add_argument('--channel', dest='channel', required=True, help='Channel id/name the message lives in')
    mg_un = mg_sub.add_parser('unreact', help="Remove the bot's own reaction from a message")
    mg_un.add_argument('msg_id')
    mg_un.add_argument('emoji')
    mg_un.add_argument('--channel', dest='channel', required=True, help='Channel id/name the message lives in')

    th = sub.add_parser('thread', help='Edit a thread / forum post: pin/archive/lock/rename/tags')
    th.add_argument('identity', help='Identity whose connector serves the request')
    th.add_argument('--timeout', type=float, default=15.0)
    th_sub = th.add_subparsers(dest='thread_action', required=True)
    for _verb, _help in (('pin', 'Pin a forum post to the top of its forum'),
                         ('unpin', 'Unpin a forum post'),
                         ('archive', 'Archive a thread/post'),
                         ('unarchive', 'Unarchive a thread/post'),
                         ('lock', 'Lock a thread/post'),
                         ('unlock', 'Unlock a thread/post')):
        _sp = th_sub.add_parser(_verb, help=_help)
        _sp.add_argument('--channel', dest='channel', required=True, help='Thread/post id or name')
    th_rn = th_sub.add_parser('rename', help='Rename a thread/post')
    th_rn.add_argument('name')
    th_rn.add_argument('--channel', dest='channel', required=True, help='Thread/post id or name')
    th_tg = th_sub.add_parser('tags', help="Set a forum post's applied tags (must exist in the forum)")
    th_tg.add_argument('tags', help='Comma-separated tag names')
    th_tg.add_argument('--channel', dest='channel', required=True, help='Forum post id or name')

    em = sub.add_parser('emoji', help='Custom guild emoji — list / upload / delete / rename')
    em.add_argument('identity')
    em.add_argument('--timeout', type=float, default=30.0)
    em.add_argument('--server', help='Server id or name (default: main)')
    em_sub = em.add_subparsers(dest='emoji_action', required=True)
    em_ls = em_sub.add_parser('list', help='List custom emoji with paste-ready refs')
    em_ls.add_argument('--grep', help='Filter by name (regex, case-insensitive)')
    em_ls.add_argument('--animated', action='store_true', help='Animated only')
    em_ls.add_argument('--json', action='store_true')
    em_up = em_sub.add_parser('upload', help='Upload an image as a custom emoji (<=256 KB)')
    em_up.add_argument('path')
    em_up.add_argument('--name', help='Emoji name (default: the filename stem)')
    em_del = em_sub.add_parser('delete', help='Delete a custom emoji by id or name')
    em_del.add_argument('emoji')
    em_rn = em_sub.add_parser('rename', help='Rename a custom emoji (id survives)')
    em_rn.add_argument('emoji')
    em_rn.add_argument('name')

    sv = sub.add_parser('servers', help='List servers (guilds) the bot is in')
    sv.add_argument('identity', help='Identity whose connector serves the request')
    sv.add_argument('--timeout', type=float, default=20.0)
    sv.add_argument('--json', action='store_true')

    cl = sub.add_parser('channels', help='List / create / delete channels and categories')
    cl.add_argument('identity', help='Identity whose connector serves the request')
    cl.add_argument('--timeout', type=float, default=20.0)
    cl_sub = cl.add_subparsers(dest='channels_action', required=True)
    cl_ls = cl_sub.add_parser('list', help="List a server's channels (type + active thread counts)")
    cl_ls.add_argument('--server', dest='server', default=None, help='Server id or name (default: main)')
    cl_ls.add_argument('--json', action='store_true')
    cl_cr = cl_sub.add_parser('create', help='Create a channel or category')
    cl_cr.add_argument('--type', dest='ctype', choices=['text', 'forum', 'voice', 'category'], default='text', help='Channel type (default: text)')
    cl_cr.add_argument('name', help='Channel/category name')
    cl_cr.add_argument('--category', dest='category', default=None, help='Parent category id/name (ignored for --type category)')
    cl_cr.add_argument('--topic', dest='topic', default=None, help='Topic (text/forum only)')
    cl_cr.add_argument('--server', dest='server', default=None, help='Server id or name (default: main)')
    cl_del = cl_sub.add_parser('delete', help='Delete a channel or category')
    cl_del.add_argument('--channel', dest='channel', required=True, help='Channel/category id or name to delete')
    cl_ed = cl_sub.add_parser('edit', help='Edit a channel: rename, move to a category, or set topic')
    cl_ed.add_argument('--channel', dest='channel', required=True, help='Channel/category id or name to edit')
    cl_ed.add_argument('--name', dest='name', default=None, help='New name')
    cl_ed.add_argument('--category', dest='category', default=None, help='Move under this category id/name')
    cl_ed.add_argument('--topic', dest='topic', default=None, help='New topic (text/forum)')

    sp = sub.add_parser('status-plugin',
                        help='Install/remove a Discord-presence plugin (one active, session-scoped)',
                        description='Install a status plugin: a .py file defining `async def run(ctx)` that '
                                    'loops and pushes the bot\'s Discord presence via `await ctx.set_status(text, kind=...)`. '
                                    'One active at a time (set replaces). Session-scoped: the plugin is uninstalled when '
                                    'the connector stops for ANY reason (swept on the next connector startup), so re-drop '
                                    'after a restart. Runs IN the connector\'s event loop — plugins MUST be async/'
                                    'non-blocking (use ctx.sleep, not time.sleep; a blocking call freezes the gateway).')
    sp.add_argument('identity', help='Identity whose connector serves the request')
    sp.add_argument('--timeout', type=float, default=15.0)
    sp_sub = sp.add_subparsers(dest='status_plugin_action', required=True)
    sp_set = sp_sub.add_parser('set', help='Install + run a status plugin (replaces any active one)')
    sp_set.add_argument('path', help='Path to the plugin .py (copied into a session slot; the original is never touched)')
    sp_sub.add_parser('remove', help='Stop + uninstall the active status plugin and clear presence')
    sp_sub.add_parser('list', help='Show the installed status plugin and its run state')

    xt = sub.add_parser('extension',
                        help='Install/remove an identity-scoped connector extension',
                        description='Register an extension: a .py defining `async def setup(ctx)` and '
                                    'optionally `async def command(ctx, argv)`. Unlike a status plugin, '
                                    'registration is DURABLE — it lives beside the identity token and is '
                                    'reloaded on every connector start. ctx gives client/identity/flavor, '
                                    'log(), on(event, handler), store + save(), and emit(). Runs IN the '
                                    "connector's event loop, so handlers must be async and non-blocking.")
    xt.add_argument('identity', help='Identity whose connector serves the request')
    xt.add_argument('--timeout', type=float, default=15.0)
    xt_sub = xt.add_subparsers(dest='extension_action', required=True)
    xt_set = xt_sub.add_parser('set', help='Register + load an extension (replaces any active one)')
    xt_set.add_argument('path', help='Path to the extension .py (loaded in place; registration is durable)')
    xt_sub.add_parser('remove', help='Unregister the active extension (listeners drop on next restart)')
    xt_sub.add_parser('list', help='Show the registered extension, load state and last error')
    xt_call = xt_sub.add_parser('call', help="Invoke the extension's own command handler")
    xt_call.add_argument('argv', nargs=argparse.REMAINDER,
                         help='Arguments passed verbatim to command(ctx, argv)')

    u = sub.add_parser('setup', help='One-time server setup: create missing channels')
    u.add_argument('--token', default=None,
                   help='Bot token (needs Manage Channels perm)')

    c = sub.add_parser('connector', help='Long-lived gateway connector (one per identity)')
    c.add_argument('identity')
    c.add_argument('--claude-pid', dest='claude_pid', type=int, default=None,
                   help='Parent harness CLI PID. When dead, connector exits.')
    c.add_argument('--token', default=None,
                   help='Bot token (else DISCORD_TOKEN env or the shared Discord directory)')
    c.add_argument('--log', dest='log_path', default=None,
                   help='Human log path (default: <temp>/discord-mailbox/<identity>/connector.log)')
    c.add_argument('--flavor', choices=('claude', 'kimi', 'codex'), default=None,
                   help='Native harness owner; selects token directory, parent watchdog, and default status plugin')

    le = sub.add_parser(
        'leech',
        help='Attach to the already-running connector as a read-only event tap',
        description='Companion to a RUNNING connector: replays each NEW inbound '
                    'message as a stdout event for THIS session\'s Monitor, and '
                    'announces itself to the connector-owning session '
                    '(leech_attached/leech_detached on the master\'s stdout). '
                    'Launch under Monitor like the connector. No Discord access: '
                    'sends/meta ops keep going through the master from any session. '
                    'Exits when the master connector stops or this session\'s CLI dies.')
    le.add_argument('identity')
    le.add_argument('--claude-pid', dest='claude_pid', type=int, default=None,
                    help='Parent CLI PID. When dead, leech exits.')
    le.add_argument('--flavor', choices=('claude', 'kimi', 'codex'), default=None,
                    help='Native harness owner for token-directory and parent-watchdog selection')

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(0)

    if args.command == 'send':
        send(args.identity, args.to, args.subject, args.body,
             reply_to=args.reply_to, pin=args.pin, channel=args.channel,
             dm=args.dm, attach=args.attach, wait=args.wait, timeout=args.timeout)
    elif args.command == 'attachments':
        attachments_cli(args)
    elif args.command == 'creds':
        creds_cli(args)
    elif args.command == 'conversation':
        conversation_cli(args.identity, args.n,
                         channel_id=args.channel_id,
                         before_id=args.before_id,
                         after_id=args.after_id,
                         grep=args.grep,
                         timeout=args.timeout,
                         as_json=args.json)
    elif args.command == 'register':
        if args.body_file == '-':
            body = sys.stdin.read()
        elif args.body_file:
            body = open(args.body_file, 'r', encoding='utf-8').read()
        else:
            body = args.body
        register_cli(args.identity, body, timeout=args.timeout)
    elif args.command == 'topic':
        topic_cli(args)
    elif args.command == 'pins':
        pins_cli(args)
    elif args.command == 'context':
        context_cli(args)
    elif args.command == 'move':
        move_cli(args)
    elif args.command == 'forum':
        forum_cli(args)
    elif args.command == 'message':
        message_cli(args)
    elif args.command == 'thread':
        thread_cli(args)
    elif args.command == 'emoji':
        return emoji_cli(args)
    elif args.command == 'servers':
        servers_cli(args)
    elif args.command == 'channels':
        channels_cli(args)
    elif args.command == 'status-plugin':
        status_plugin_cli(args)
    elif args.command == 'extension':
        return extension_cli(args)
    elif args.command == 'list-agents':
        list_agents_cli(args.identity, timeout=args.timeout, as_json=args.json, short=args.short)
    elif args.command == 'setup':
        setup_main(token=args.token)
    elif args.command == 'connector':
        ConnectorApp(
            args.identity, claude_pid=args.claude_pid, token=args.token,
            log_path=args.log_path, flavor=args.flavor).run()
    elif args.command == 'leech':
        leech_main(args.identity, claude_pid=args.claude_pid, flavor=args.flavor)


__all__ = [name for name in globals() if not name.startswith('__')]
