"""Discord gateway connector and read-only leech lifecycles."""

from .core import *
from .storage import *
# Named explicitly as well as through the wildcard above: `import *`
# does bind these at runtime, because __all__ lists them, but a linter
# reading the source applies the plain no-underscore rule and reports
# every use as undefined. Spelling them out also says which module
# each one comes from.
from .core import _ExtensionContext
from .storage import (_ConnectorLogWriter, _ConnectorOwnership,
                      _ConnectorOwnershipError, _EventStreamReader,
                      _EventStreamWriter, _LeechLogWriter)


def leech_main(identity, claude_pid=None, log_path=None, flavor=None):
    '''Read-only companion to a RUNNING connector ("leech"): lets a SECOND
    session receive this identity's traffic. The master connector streams every
    event it emits (messages, send failures, leech announcements) to a
    generation-segmented event log under <state>/; the leech follows it from
    its attach point (no history replay) with a (generation, byte offset)
    cursor and passes each complete line through verbatim on its own stdout
    for its session's Monitor. Retention deletes whole retired generations,
    which the cursor reports as an explicit gap. It never
    touches Discord, the outbox, or the meta dirs — sends and control-plane
    ops still go through the master from any session.

    Registration: writes <state>/leeches/<pid>.json so the master's
    leech_watcher announces attach/detach to the session that owns the
    connector. Lifecycle: exits when the master stops (pidfile gone / PID dead
    / PID changed — a replacement master is a NEW attachment, re-leech
    deliberately) or when this session's own parent CLI dies.'''
    sd = state_dir(identity)
    pidfile = sd / 'connector.pid'
    log_path = log_path or (sd / 'leech.log')
    log_fh = _LeechLogWriter(log_path)

    def log(msg):
        # Mirrors the connector: human logs to stderr + file, stdout reserved
        # for the JSON event stream. Multiple leeches share leech.log, hence
        # the pid tag — and hence the writer's cross-process rotation lock.
        line = f'[{time.strftime("%Y-%m-%dT%H:%M:%S")}] [leech {os.getpid()}] {msg}'
        log_fh.write(line)          # the writer terminates each record itself
        print(line, file=sys.stderr, flush=True)

    def emit_event(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def master_pid():
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
        if not pid_alive(pid):
            return None
        if is_connector_process(pid, identity) is False:
            log(f'connector.pid for {identity} names PID {pid}, which is not a '
                f'discord_mb connector — a recycled PID behind a stale lock. '
                f'Refusing to leech onto it; start a real connector instead.')
            return None
        return pid

    master = master_pid()
    if master is None:
        log(f'no running connector for {identity!r} to leech onto — start a real one first')
        sys.exit(1)

    # Flavor only drives the parent-CLI walkup here (no token needed to leech).
    flavor = flavor or resolve_token_and_flavor(identity)[1]
    if claude_pid is None:
        claude_pid = find_parent_pid_from(os.getppid(), flavor)
        if claude_pid:
            log(f'auto-detected {flavor} parent PID: {claude_pid}')
        else:
            log(f'no {flavor} parent PID detected — leech runs until the master dies')

    ldir = sd / 'leeches'
    ldir.mkdir(exist_ok=True)
    reg = ldir / f'{os.getpid()}.json'
    tmp = reg.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({'pid': os.getpid(), 'claude_pid': claude_pid,
                               'started': time.strftime('%Y-%m-%dT%H:%M:%S')}),
                   encoding='utf-8')
    tmp.replace(reg)

    # Attach point: the live end of the newest generation, so no history is
    # replayed.  Retention gaps are reported through the human log rather than
    # being inferred from an ambiguous size decrease.
    events = _EventStreamReader.attach(sd, on_gap=log)
    if events.generation is None:
        log('no event stream yet — following the master\'s first generation '
            'from its start once it appears')
    else:
        log(f'leeching onto connector PID {master} (event generation '
            f'{events.generation}, byte {events.offset})')
    emit_event({'event': 'leech_started', 'identity': identity,
                'master_pid': master, 'pid': os.getpid()})

    reason = 'unknown'
    tick = 0
    try:
        while True:
            cur = master_pid()
            if cur != master:
                reason = (f'master connector PID {master} gone' if cur is None
                          else f'master connector replaced (PID {master} -> {cur})')
                break
            tick += 1
            if claude_pid is not None and tick % 5 == 0 and not pid_alive(claude_pid):
                reason = f'parent PID {claude_pid} gone'
                break
            try:
                lines = events.read()   # complete records only; rest next tick
                if lines:
                    for line in lines:
                        sys.stdout.buffer.write(line + b'\n')
                    sys.stdout.buffer.flush()
            except Exception as e:
                log(f'leech tail error: {type(e).__name__}: {e}')
            time.sleep(1.0)
    except KeyboardInterrupt:
        reason = 'interrupted'
    finally:
        log(f'leech exiting: {reason}')
        try:
            reg.unlink(missing_ok=True)
        except OSError:
            pass
        log_fh.close()


def _run_connector(identity, claude_pid=None, token=None, log_path=None,
                   flavor=None):
    import discord  # noqa: E402

    sd = state_dir(identity)
    pidfile = sd / 'connector.pid'
    log_path = log_path or (sd / 'connector.log')

    if flavor not in (None, 'claude', 'kimi', 'codex'):
        raise ValueError(f'unknown connector flavor: {flavor!r}')

    # Claim the identity before opening its human log.  This is an OS lock,
    # not a check-then-write PID race; it also protects callers that choose
    # different --log paths for the same identity.
    try:
        process_owner = _ConnectorOwnership(pidfile)
    except _ConnectorOwnershipError as e:
        print(f'Connector for {identity} already running ({e})',
              file=sys.stderr, flush=True)
        sys.exit(1)

    _connector_started_at = time.time()
    startup_messages = []
    log_fh = None
    _events: dict[str, Any] = {'writer': None}  # opened after the duplicate guard

    def _report_cleanup_error(error):
        if error is None:
            return
        try:
            print(f'connector startup cleanup failed: {type(error).__name__}: {error}',
                  file=sys.stderr, flush=True)
        except BaseException:
            pass

    def cleanup_startup():
        """Release every resource acquired before ``client.run`` starts.

        Each release is independently BaseException-safe.  The first cleanup
        failure is returned to the caller so it can be logged and attached to
        the startup exception without replacing that primary failure.
        """
        errors = []
        try:
            if _events['writer'] is not None:
                try:
                    _events['writer'].close()
                finally:
                    _events['writer'] = None
        except BaseException as exc:
            errors.append(exc)
        try:
            if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except BaseException as exc:
            errors.append(exc)
        try:
            if log_fh is not None:
                log_fh.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            process_owner.close()
        except BaseException as exc:
            errors.append(exc)
        return errors[0] if errors else None

    def raise_startup_failure(primary, primary_tb):
        cleanup_error = cleanup_startup()
        if cleanup_error is not None:
            _report_cleanup_error(cleanup_error)
            try:
                primary.add_note(
                    f'secondary startup cleanup failure: {type(cleanup_error).__name__}: '
                    f'{cleanup_error}')
            except BaseException:
                pass
            raise primary.with_traceback(primary_tb) from cleanup_error
        raise primary.with_traceback(primary_tb)

    def startup_log(msg, stderr=False):
        startup_messages.append(msg)
        if stderr:
            line = f'[{time.strftime("%Y-%m-%dT%H:%M:%S")}] {msg}'
            print(line, file=sys.stderr, flush=True)

    # Keep the legacy PID/cmdline guard for stale locks and connectors started
    # by an older bundle version that did not have the OS sidecar yet.  It runs
    # before the log writer so a live master's log is never migrated or opened.
    if pidfile.exists():
        try:
            old = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            old = None
        if old is not None and old != os.getpid() and pid_alive(old):
            if is_connector_process(old, identity) is False:
                startup_log(f'stale connector.pid for {identity}: PID {old} is alive but '
                            f'is not a discord_mb connector (the OS recycled the number) '
                            f'— clearing the lock and starting fresh')
                try:
                    pidfile.unlink()
                except OSError:
                    pass
            else:
                startup_log(f'Connector for {identity} already running (PID {old}), exiting',
                            stderr=True)
                cleanup_error = cleanup_startup()
                _report_cleanup_error(cleanup_error)
                sys.exit(1)

    try:
        log_fh = _ConnectorLogWriter(log_path)
    except _ConnectorOwnershipError as e:
        cleanup_error = cleanup_startup()
        _report_cleanup_error(cleanup_error)
        print(f'Connector log for {identity} is already owned ({e})',
              file=sys.stderr, flush=True)
        sys.exit(1)
    except BaseException as exc:
        raise_startup_failure(exc, sys.exc_info()[2])

    def log(msg):
        # Human logs go to stderr + log file. stdout is reserved for JSON event
        # stream consumed by Monitor (v0.6+).
        line = f'[{time.strftime("%Y-%m-%dT%H:%M:%S")}] {msg}'
        # Callable before the log file is opened -- the token resolution below
        # logs its own failure. stderr still carries the line either way.
        if log_fh is not None:
            log_fh.write(line)
        print(line, file=sys.stderr, flush=True)

    def emit_event(event):
        '''Emit a single JSON line on stdout (Monitor delivers each as a
        task-notification) AND append it to the segmented event stream that
        leeches follow to mirror this connector's events into their own
        sessions.'''
        line = json.dumps(event, ensure_ascii=False)
        print(line, flush=True)
        if _events['writer'] is not None:
            try:
                _events['writer'].write(line)
            except OSError:
                pass

    try:
        # Delayed warnings are intentionally emitted inside this guard.  A
        # failure on the first warning write must release every resource that
        # the constructor acquired, including a stale PID identity lock.
        for startup_message in startup_messages:
            log(startup_message)

        # Resolve the token + its flavor (which dir it came from) up front: the flavor
        # drives both the parent-CLI PID walkup and the default status plugin.
        if not token:
            token = os.environ.get('DISCORD_TOKEN')
        if not token:
            token, flavor = resolve_token_and_flavor(identity, flavor)
        else:
            # Composed watcher calls always pass this explicitly. Legacy invocations
            # with an env/flag token remain Claude-flavored for compatibility.
            flavor = flavor or 'claude'
        if not token:
            log(f'No token. Set DISCORD_TOKEN env or populate '
                f'{TOKEN_DIR}/{identity}.token')
            raise SystemExit(1)
        log(f'identity flavor: {flavor}')

        if claude_pid is None:
            claude_pid = find_parent_pid_from(os.getppid(), flavor)
            if claude_pid:
                log(f'auto-detected {flavor} parent PID: {claude_pid}')
            else:
                log(f'no {flavor} parent PID detected — connector will run until killed')

        pidfile.write_text(str(os.getpid()))

        # Event log: one generation per master — a fresh segment now that we
        # own the identity, with the predecessor's segments retired. Leeches
        # attach at its live end, so nothing is replayed, and continuous
        # traffic stays inside the documented segment window instead of
        # growing one file forever.
        try:
            _events['writer'] = _EventStreamWriter(sd)
        except OSError as e:
            log(f'event stream unavailable ({e}) — leeches will see no stream')

        # Status plugins are session-scoped: a fresh connector must never inherit one
        # a predecessor left behind (e.g. after kill -9). Sweep the slot before doing
        # anything else — this is the real "uninstall on ANY shutdown reason" guarantee.
        try:
            sweep_status_plugin(identity)
        except Exception as e:
            log(f'status-plugin startup sweep failed: {type(e).__name__}: {e}')

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        client = discord.Client(intents=intents)
    except BaseException as exc:
        raise_startup_failure(exc, sys.exc_info()[2])

    state = {'identity_map': {}, 'own_dir_msg_id': None, 'channels': {}, 'roles': {}, 'guild': None, 'watchers_started': False, 'extension': None, 'extension_ctx': None, 'extension_error': None, 'ext_listeners': {}, 'ext_installed': set(), 'ext_tasks': [], 'pins_cache': {}, 'forum_pins_cache': {},
             'status_task': None, 'status_state': 'empty', 'status_last': None, 'status_error': None,
             'usage_period': None,
             # Captured now, while the imported code and the installed files
             # are still the same thing; every later read describes the
             # install, so the two can be compared.
             'running_package': running_fingerprint(), 'reported_package': None}

    def ch(key):
        return state['channels'].get(key)

    def role_id(key):
        r = state['roles'].get(key)
        return r.id if r else None

    async def resolve_topology():
        if not client.guilds:
            log('no guild — bot has not joined any server')
            return
        # "Main" is the server holding our infra channels — identified by the
        # bridge channel NAME, not guild ordering (the bot may be in additional
        # servers, and guilds[0] is arbitrary). Other servers are reachable by
        # numeric channel id (send/topic/pins/conversation --channel); only the
        # named infra channels and the broadcast role are main-only.
        guild = next((g for g in client.guilds
                      if discord.utils.get(g.text_channels, name=BRIDGE_CHANNEL_NAME)), None)
        if guild is None:
            guild = client.guilds[0]
            log(f'WARN: no guild has a #{BRIDGE_CHANNEL_NAME} channel; defaulting main to {guild.name!r}')
        state['guild'] = guild
        if len(client.guilds) > 1:
            others = [g.name for g in client.guilds if g.id != guild.id]
            log(f'main guild: {guild.name!r}; also in {len(others)} other server(s): {others} '
                f'(reach their channels by numeric --channel id)')
        names = {
            'bridge': BRIDGE_CHANNEL_NAME,
            'directory': DIRECTORY_CHANNEL_NAME,
            'attachments': ATTACHMENTS_CHANNEL_NAME,
            'credentials': CREDS_CHANNEL_NAME,
        }
        for key, name in names.items():
            state['channels'][key] = discord.utils.get(guild.text_channels, name=name)
        state['roles']['broadcast'] = discord.utils.get(guild.roles, name=BROADCAST_ROLE_NAME)
        missing = [k for k, v in state['channels'].items() if v is None]
        if missing:
            log(f'WARN: missing channels (by name): {missing} — run `discord_mb.py setup` or create manually')
        if state['roles']['broadcast'] is None:
            log(f'WARN: broadcast role {BROADCAST_ROLE_NAME!r} not found in guild')

    async def parent_watchdog():
        if claude_pid is None:
            return
        while True:
            await asyncio.sleep(5)
            if not pid_alive(claude_pid):
                log(f'Parent PID {claude_pid} gone, shutting down')
                await client.close()
                return

    async def outbox_watcher():
        outbox = outbox_dir(identity)
        while not client.is_closed():
            try:
                files = sorted(outbox.glob('*.json'))
                for f in files:
                    await dispatch_outbox_file(f)
            except Exception as e:
                log(f'outbox_watcher error: {e}')
            await asyncio.sleep(1.0)

    async def meta_watcher():
        in_dir = meta_in_dir(identity)
        out_dir = meta_out_dir(identity)
        while not client.is_closed():
            try:
                for f in sorted(in_dir.glob('*.json')):
                    await dispatch_meta_file(f, out_dir)
            except Exception as e:
                log(f'meta_watcher error: {e}')
            await asyncio.sleep(0.5)

    async def leech_watcher():
        '''Announce leech attach/detach to THIS session (the connector owner):
        leeches register as <state>/leeches/<pid>.json; each new live one gets
        a leech_attached event on our stdout so the owner knows the connector
        is shared, each vanished/dead one a leech_detached. Files whose PID is
        dead are swept (a crashed leech can't unregister itself).'''
        ldir = sd / 'leeches'
        ldir.mkdir(exist_ok=True)
        known = {}
        while not client.is_closed():
            try:
                cur = {}
                for f in ldir.glob('*.json'):
                    try:
                        info = json.loads(f.read_text(encoding='utf-8'))
                        pid = int(info.get('pid') or 0)
                    except (OSError, ValueError):
                        continue
                    if pid and pid_alive(pid):
                        cur[pid] = info
                    else:
                        try:
                            f.unlink(missing_ok=True)
                        except OSError:
                            pass
                for pid, info in cur.items():
                    if pid not in known:
                        log(f'leech attached: PID {pid} (parent CLI PID {info.get("claude_pid")})')
                        emit_event({'event': 'leech_attached', 'identity': identity,
                                    'pid': pid, 'claude_pid': info.get('claude_pid')})
                for pid in known:
                    if pid not in cur:
                        log(f'leech detached: PID {pid}')
                        emit_event({'event': 'leech_detached', 'identity': identity, 'pid': pid})
                known = cur
            except Exception as e:
                log(f'leech_watcher error: {e}')
            await asyncio.sleep(2.0)

    async def dispatch_meta_file(f, out_dir):
        try:
            req = json.loads(f.read_text(encoding='utf-8'))
        except Exception as e:
            log(f'unreadable meta file {f.name}: {e}; deleting')
            try:
                f.unlink()
            except OSError:
                pass
            return
        op = req.get('op')
        resp = {'op': op, 'ok': False}
        try:
            if op == 'send':
                # send --wait: same payload as an outbox file, but the caller
                # blocks for the result (msg_id/channel/chunks/pin outcome).
                # SendRetry/SendError both surface as the error response.
                resp.update(await perform_send(req))
                resp['ok'] = True
            elif op == 'list-agents':
                resp['agents'] = await op_list_agents()
                resp['ok'] = True
            elif op == 'register':
                result = await op_register(req.get('body', ''))
                resp.update(result)
                resp['ok'] = True
            elif op == 'attachment-list':
                resp['entries'] = await op_attachment_list(int(req.get('limit', 50)))
                resp['ok'] = True
            elif op == 'attachment-upload':
                result = await op_attachment_upload(req.get('path', ''), label=req.get('label'), channel_ref=req.get('channel'), paths=req.get('paths'))
                resp.update(result)
                resp['ok'] = True
            elif op == 'attachment-download':
                result = await op_attachment_download(req.get('msg_id', ''), dest_dir=req.get('dest_dir', ''), rename=req.get('rename'), channel_ref=req.get('channel'))
                resp.update(result)
                resp['ok'] = True
            elif op == 'creds-list':
                resp['entries'] = await op_creds_list()
                resp['ok'] = True
            elif op == 'creds-get':
                resp['entries'] = await op_creds_get(req.get('domain', ''), login=req.get('login'))
                resp['ok'] = True
            elif op == 'creds-add':
                result = await op_creds_add(req.get('domain', ''), req.get('login', ''), req.get('password', ''), upsert=bool(req.get('upsert')))
                resp.update(result)
                resp['ok'] = True
            elif op == 'conversation':
                resp['messages'] = await op_conversation(
                    int(req.get('limit', 20)),
                    channel_id=int(req.get('channel_id', 0)) or (ch('bridge').id if ch('bridge') else None),
                    before_id=req.get('before_id'),
                    after_id=req.get('after_id'),
                    grep=req.get('grep'),
                )
                resp['ok'] = True
            elif op == 'get-topic':
                resp.update(await op_get_topic(req.get('channel')))
                resp['ok'] = True
            elif op == 'set-topic':
                resp.update(await op_set_topic(req.get('channel'), req.get('topic', '')))
                resp['ok'] = True
            elif op == 'pins-list':
                resp['entries'] = await op_pins_list(req.get('channel'), int(req.get('limit', 50)))
                resp['ok'] = True
            elif op == 'pin':
                resp.update(await op_pin(req.get('channel'), req.get('msg_id', '')))
                resp['ok'] = True
            elif op == 'unpin':
                resp.update(await op_unpin(req.get('channel'), req.get('msg_id', '')))
                resp['ok'] = True
            elif op == 'context':
                resp.update(await op_context(req.get('channel')))
                resp['ok'] = True
            elif op == 'forum-list':
                resp.update(await op_forum_list(req.get('channel'), include_archived=bool(req.get('archived')), limit=int(req.get('limit', 100))))
                resp['ok'] = True
            elif op == 'forum-create':
                resp.update(await op_forum_create(req.get('channel'), req.get('name', ''), req.get('content', ''), tags=req.get('tags')))
                resp['ok'] = True
            elif op == 'forum-delete':
                resp.update(await op_forum_delete(req.get('channel')))
                resp['ok'] = True
            elif op == 'message-edit':
                resp.update(await op_message_edit(req.get('channel'), req.get('msg_id', ''), req.get('content', '')))
                resp['ok'] = True
            elif op == 'message-delete':
                resp.update(await op_message_delete(req.get('channel'), req.get('msg_id', '')))
                resp['ok'] = True
            elif op == 'move':
                resp.update(await op_move(
                    req.get('source'), int(req.get('limit', 1)),
                    req.get('dest'), req.get('dest_kind', 'channel'),
                    title=req.get('title'), keep=bool(req.get('keep')),
                    before_id=req.get('before_id'), dry_run=bool(req.get('dry_run')),
                ))
                resp['ok'] = True
            elif op == 'list-servers':
                resp['servers'] = await op_list_servers()
                resp['ok'] = True
            elif op == 'list-channels':
                resp.update(await op_list_channels(req.get('server')))
                resp['ok'] = True
            elif op == 'channel-create':
                resp.update(await op_channel_create(req.get('server'), req.get('ctype', 'text'), req.get('name', ''), category_id=req.get('category'), topic=req.get('topic')))
                resp['ok'] = True
            elif op == 'channel-delete':
                resp.update(await op_channel_delete(req.get('channel')))
                resp['ok'] = True
            elif op == 'thread-edit':
                resp.update(await op_thread_edit(req.get('channel'), name=req.get('name'), archived=req.get('archived'), locked=req.get('locked'), pinned=req.get('pinned'), tags=req.get('tags')))
                resp['ok'] = True
            elif op == 'channel-edit':
                resp.update(await op_channel_edit(req.get('channel'), name=req.get('name'), category_id=req.get('category'), topic=req.get('topic')))
                resp['ok'] = True
            elif op == 'emoji-list':
                resp.update(await op_emoji_list(req.get('server')))
                resp['ok'] = True
            elif op == 'emoji-upload':
                resp.update(await op_emoji_upload(req.get('server'), req.get('name', ''), req.get('image', '')))
                resp['ok'] = True
            elif op == 'emoji-delete':
                resp.update(await op_emoji_delete(req.get('server'), req.get('ref', '')))
                resp['ok'] = True
            elif op == 'emoji-rename':
                resp.update(await op_emoji_rename(req.get('server'), req.get('ref', ''), req.get('name', '')))
                resp['ok'] = True
            elif op == 'message-reactions':
                resp.update(await op_message_reactions(req.get('channel'), req.get('msg_id', '')))
                resp['ok'] = True
            elif op == 'message-react':
                resp.update(await op_message_react(req.get('channel'), req.get('msg_id', ''), req.get('emoji', ''), remove=bool(req.get('remove'))))
                resp['ok'] = True
            elif op == 'extension-set':
                resp.update(await op_extension_set(req.get('path', '')))
            elif op == 'extension-remove':
                resp.update(await op_extension_remove())
            elif op == 'extension-list':
                resp.update(await op_extension_list())
            elif op == 'extension-call':
                resp.update(await op_extension_call(req.get('argv') or []))
            elif op == 'status-plugin-set':
                resp.update(await op_status_plugin_set(req.get('path', '')))
                resp['ok'] = True
            elif op == 'status-plugin-remove':
                resp.update(await op_status_plugin_remove())
                resp['ok'] = True
            elif op == 'status-plugin-list':
                resp.update(await op_status_plugin_list())
                resp['ok'] = True
            else:
                resp['error'] = f'unknown op: {op!r}'
        except Exception as e:
            resp['error'] = f'{type(e).__name__}: {e}'
        out_path = out_dir / f.name
        tmp = out_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(resp, indent=2), encoding='utf-8')
        tmp.replace(out_path)
        try:
            f.unlink()
        except OSError:
            pass

    async def op_attachment_list(limit):
        'List recent messages in #attachments that carry file attachments. Includes user uploads.'
        channel = ch('attachments')
        if channel is None:
            raise RuntimeError(f'attachments channel #{ATTACHMENTS_CHANNEL_NAME} not resolved')
        limit = min(max(limit, 1), 200)
        entries = []
        async for m in channel.history(limit=limit):
            if not m.attachments:
                continue
            from_name = reverse_identity(m.author.id) or m.author.name
            for a in m.attachments:
                entries.append({
                    'msg_id': str(m.id),
                    'from': from_name,
                    'from_user_id': str(m.author.id),
                    'created': m.created_at.isoformat(),
                    'filename': a.filename,
                    'size': a.size,
                    'url': a.url,
                    'content_type': a.content_type,
                    'note': m.content or '',
                })
        entries.reverse()  # chronological
        return entries

    async def op_attachment_upload(path, label=None, channel_ref=None, paths=None):
        '''Upload local file(s) as attachments on ONE message (Discord caps:
        10 files per message, ~25MB each).

        Default target is #attachments (binary side-channel on the main server).
        Pass channel_ref — a numeric channel id (any server the bot is in) or a
        connector-known name (bridge/attachments/credentials/directory) — to post
        the file into another channel instead, e.g. the channel a tracked task
        was assigned in so the deliverable lands where the task lives.'''
        all_paths = [str(a) for a in (paths or ([path] if path else []))]
        if not all_paths:
            raise ValueError('path required')
        if len(all_paths) > 10:
            raise ValueError(f'{len(all_paths)} files — Discord allows at most 10 per message')
        ps = []
        for a in all_paths:
            p = Path(a)
            if not p.is_file():
                raise FileNotFoundError(f'not a file: {a}')
            ps.append(p)
        channel: Any = None  # duck-typed like resolve_channel_ref's result
        if channel_ref is None or channel_ref == '':
            channel = ch('attachments')
            if channel is None:
                raise RuntimeError(f'attachments channel #{ATTACHMENTS_CHANNEL_NAME} not resolved — run `discord_mb.py setup` first')
        else:
            cref = str(channel_ref).strip()
            if cref.isdigit():
                cid = int(cref)
                channel = client.get_channel(cid)
                if channel is None:
                    try:
                        channel = await client.fetch_channel(cid)
                    except Exception as e:
                        raise RuntimeError(f'fetch_channel({cid}) failed: {e}') from e
            else:
                channel = ch(cref)
                if channel is None:
                    raise RuntimeError(f'channel #{cref} not resolved')
        total = sum(p.stat().st_size for p in ps)
        content = (f'[{identity}] {label}' if label
                   else f'[{identity}] ' + ', '.join(p.name for p in ps))
        fhs = [open(p, 'rb') for p in ps]
        try:
            files = [discord.File(fh, filename=p.name) for fh, p in zip(fhs, ps)]
            msg = await channel.send(content=content, files=files,
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            if getattr(e, 'status', None) == 413:
                raise RuntimeError(f'payload too large ({total} bytes across {len(ps)} '
                                   f'file(s)) — Discord upload limit exceeded') from e
            raise
        finally:
            for fh in fhs:
                try:
                    fh.close()
                except OSError:
                    pass
        entries = [{'filename': a.filename, 'size': a.size, 'url': a.url}
                   for a in msg.attachments]
        first = entries[0] if entries else {'filename': ps[0].name,
                                            'size': ps[0].stat().st_size, 'url': None}
        return {
            'msg_id': str(msg.id),
            'channel_id': str(channel.id),
            'files': entries,
            # legacy single-file keys = first attachment (old CLIs read these)
            'filename': first['filename'],
            'size': first['size'],
            'url': first['url'],
        }

    async def op_attachment_download(msg_id, dest_dir, rename=None, channel_ref=None):
        'Fetch the message and save all its attachments into dest_dir.'
        if not msg_id:
            raise ValueError('msg_id required')
        if not dest_dir:
            raise ValueError('dest_dir required')
        channel: Any = None  # duck-typed like resolve_channel_ref's result
        if channel_ref is None or channel_ref == '':
            channel = ch('attachments')
            if channel is None:
                raise RuntimeError(f'attachments channel #{ATTACHMENTS_CHANNEL_NAME} not resolved')
        else:
            cref = str(channel_ref).strip()
            if cref.isdigit():
                cid = int(cref)
                channel = client.get_channel(cid)
                if channel is None:
                    try:
                        channel = await client.fetch_channel(cid)
                    except Exception as e:
                        raise RuntimeError(f'fetch_channel({cid}) failed: {e}') from e
            else:
                channel = ch(cref)
                if channel is None:
                    raise RuntimeError(f'channel #{cref} not resolved')
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        if not msg.attachments:
            return {'saved': [], 'reason': 'message has no attachments'}
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for a in msg.attachments:
            target_name = rename if (rename and len(msg.attachments) == 1) else a.filename
            out = dest / target_name
            await a.save(out)
            saved.append({'path': str(out), 'filename': a.filename, 'size': a.size, 'url': a.url})
        return {'saved': saved}

    def _parse_cred(content):
        'Extract {domain, login, password} from a message with exactly 3 fenced code blocks.'
        import re as _re
        # Non-greedy capture between ``` fences, with optional language tag
        # on the opening fence. Earlier version used .*? + DOTALL which was
        # already non-greedy but combined badly when a message contained
        # 4+ fenced blocks (the parser would silently misalign by treating
        # the 4th+ blocks as continuation).
        blocks = _re.findall(r'```(?:[^\n]*\n)?(.*?)```', content or '', _re.DOTALL)
        if len(blocks) != 3:
            return None
        return {
            'domain': blocks[0].strip(),
            'login': blocks[1].strip(),
            'password': blocks[2].strip(),
        }

    async def _fetch_creds():
        channel = ch('credentials')
        if channel is None:
            raise RuntimeError(f'credentials channel #{CREDS_CHANNEL_NAME} not resolved')
        entries = []
        async for m in channel.history(limit=500):
            parsed = _parse_cred(m.content or '')
            if parsed is None:
                continue
            parsed['msg_id'] = str(m.id)
            parsed['author'] = reverse_identity(m.author.id) or m.author.name
            parsed['created'] = m.created_at.isoformat()
            entries.append(parsed)
        # newest-first from history; reverse for chronological
        entries.reverse()
        return entries

    async def op_creds_list():
        return await _fetch_creds()

    async def op_creds_get(domain, login=None):
        'Fetch credential(s) for a domain, optionally filtered by login. Returns all matches (list).'
        if not domain:
            raise ValueError('domain required')
        domain_lc = domain.strip().lower()
        login_lc = login.strip().lower() if login else None
        entries = await _fetch_creds()
        matches = [
            e for e in entries
            if e['domain'].lower() == domain_lc
            and (login_lc is None or e['login'].lower() == login_lc)
        ]
        return matches

    async def op_creds_add(domain, login, password, upsert=False):
        if not (domain and login and password):
            raise ValueError('domain, login, password all required')
        channel = ch('credentials')
        if channel is None:
            raise RuntimeError(f'credentials channel #{CREDS_CHANNEL_NAME} not resolved')
        # Dedup on (domain, login) pair — a single platform can have many logins.
        existing = await op_creds_get(domain, login=login)
        if existing and not upsert:
            return {
                'action': 'skipped',
                'reason': 'already exists (same domain + login)',
                'existing_msg_ids': [e['msg_id'] for e in existing],
            }
        content = CREDS_TEMPLATE.format(domain=domain.strip(), login=login.strip(), password=password.strip())
        msg = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        return {'action': 'added', 'msg_id': str(msg.id)}

    async def op_conversation(limit, channel_id=None, before_id=None, after_id=None, grep=None):
        'Fetch messages from any channel. Defaults to #agents. Supports cursor (before/after) and optional server-side grep.'
        import re as _re
        if channel_id:
            cid = channel_id
            channel = client.get_channel(cid)
            if channel is None:
                try:
                    channel = await client.fetch_channel(cid)
                except Exception as e:
                    raise RuntimeError(f'channel {cid} unavailable: {e}') from e
        else:
            channel = ch('bridge')
            if channel is None:
                raise RuntimeError(f'bridge channel #{BRIDGE_CHANNEL_NAME} not resolved')
        limit = min(max(limit, 1), 100)
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel, discord.GroupChannel, discord.VoiceChannel)):
            raise RuntimeError(f'channel {channel!r} does not support .history()')
        kwargs: dict[str, Any] = {'limit': limit}
        if before_id:
            kwargs['before'] = discord.Object(id=int(before_id))
        if after_id:
            kwargs['after'] = discord.Object(id=int(after_id))
            kwargs['oldest_first'] = True
        pattern = _re.compile(grep) if grep else None
        msgs = []
        async for m in channel.history(**kwargs):
            content = m.content or ''
            from_name = reverse_identity(m.author.id) or m.author.name
            record = attach_extras({
                'msg_id': str(m.id),
                'channel_id': str(m.channel.id),
                'from': from_name,
                'from_user_id': str(m.author.id),
                'created': m.created_at.isoformat(),
                'content': content,
                'attachments': [{'url': a.url, 'filename': a.filename, 'size': a.size} for a in m.attachments],
                'mentions': [str(u.id) for u in m.mentions],
                'role_mentions': [str(r.id) for r in m.role_mentions],
            }, m, body_key='content')
            # --grep matches the RENDERED text too: an embed-only or
            # Components-V2 message has empty content, so grepping content
            # alone silently skips exactly the messages this fix is about.
            if pattern and not pattern.search(content) and not pattern.search(
                    record.get('content_rendered') or ''):
                continue
            msgs.append(record)
            try:
                save_message(m)  # browsed => cached => reply-able (resolve_reply_target)
            except OSError:
                pass
        if not after_id:
            msgs.reverse()
        return msgs

    async def op_register(body):
        'Find existing self-authored non-pinned directory message; edit it. Else post new.'
        channel = ch('directory')
        if channel is None:
            raise RuntimeError(f'directory channel #{DIRECTORY_CHANNEL_NAME} not resolved')
        if client.user is None:
            raise RuntimeError('client.user is None — connector not ready')
        self_user_id = client.user.id
        header = f'**{identity}** · <@{self_user_id}>'
        body = (body or '').strip()
        content = header + ('\n\n' + body if body else '')
        if len(content) > 2000:
            raise ValueError(f'profile too long: {len(content)} > 2000')
        existing = None
        async for msg in channel.history(limit=200):
            if msg.pinned:
                continue
            if msg.author.id == self_user_id:
                existing = msg
                break
        if existing is not None:
            await existing.edit(content=content,
                                allowed_mentions=discord.AllowedMentions.none())
            log(f'register: edited existing entry msg_id={existing.id}')
            return {'action': 'edited', 'msg_id': str(existing.id)}
        new = await channel.send(content,
                                 allowed_mentions=discord.AllowedMentions.none())
        log(f'register: created new entry msg_id={new.id}')
        return {'action': 'created', 'msg_id': str(new.id)}

    async def op_list_agents():
        channel = ch('directory')
        if channel is None:
            raise RuntimeError(f'directory channel #{DIRECTORY_CHANNEL_NAME} not resolved')
        agents = []
        async for msg in channel.history(limit=200):
            if msg.pinned:
                continue
            content = msg.content or ''
            uid_match = re.search(r'<@!?(\d+)>', content)
            if not uid_match:
                continue
            uid = int(uid_match.group(1))
            name = None
            bold = re.search(r'\*\*([^*\n]+)\*\*', content)
            if bold:
                name = bold.group(1).strip()
            else:
                for line in content.splitlines():
                    line = line.strip().lstrip('-').strip()
                    if not line:
                        continue
                    for sep in (':', '='):
                        if sep in line:
                            name = line.split(sep, 1)[0].strip()
                            break
                    if name:
                        break
            if not name:
                continue
            agents.append({
                'identity': name,
                'user_id': str(uid),
                'author_id': str(msg.author.id),
                'msg_id': str(msg.id),
                'created': msg.created_at.isoformat(),
                'body': content,
            })
        return agents

    async def resolve_channel_ref(channel_ref, default_key='bridge') -> Any:
        # -> Any: callers duck-type the result (send/pins/edit/fetch_message)
        # behind hasattr/isinstance guards; the concrete channel class varies.
        '''Resolve a channel from a numeric id, a connector-known name
        (bridge / attachments / credentials / directory), or fall back to
        default_key when the ref is empty/None.'''
        if channel_ref is None or str(channel_ref).strip() == '':
            channel = ch(default_key)
            if channel is None:
                raise RuntimeError(f'default channel {default_key!r} not resolved')
            return channel
        cref = str(channel_ref).strip()
        if cref.isdigit():
            channel = client.get_channel(int(cref))
            if channel is None:
                channel = await client.fetch_channel(int(cref))
            return channel
        channel = ch(cref)
        if channel is None:
            raise RuntimeError(f'channel {cref!r} not resolved (known names: {list(state["channels"])})')
        return channel

    async def op_get_topic(channel_ref):
        channel = await resolve_channel_ref(channel_ref)
        return {
            'channel_id': str(channel.id),
            'channel_name': getattr(channel, 'name', None),
            'type': getattr(getattr(channel, 'type', None), 'name', None),
            'topic': getattr(channel, 'topic', None),
        }

    async def op_set_topic(channel_ref, topic):
        channel = await resolve_channel_ref(channel_ref)
        if not hasattr(channel, 'topic'):
            raise RuntimeError(f'{type(channel).__name__} has no topic to set (only text/forum/stage channels do)')
        await channel.edit(topic=topic, reason=f'topic set via discord_mb by {identity}')
        return {
            'channel_id': str(channel.id),
            'channel_name': getattr(channel, 'name', None),
            'topic': topic,
        }

    async def op_pins_list(channel_ref, limit):
        channel = await resolve_channel_ref(channel_ref)
        limit = max(1, min(int(limit), 50))
        entries = []
        async for p in channel.pins(limit=limit):
            entries.append(attach_extras({
                'msg_id': str(p.id),
                'from': reverse_identity(p.author.id) or p.author.name,
                'created': p.created_at.isoformat(),
                'content': p.content,
                'attachments': [{'filename': a.filename, 'url': a.url, 'size': a.size,
                                 'content_type': a.content_type} for a in p.attachments],
            }, p, body_key='content'))
        entries.reverse()  # chronological (pins() yields newest-first)
        return entries

    async def op_pin(channel_ref, msg_id):
        if not msg_id:
            raise ValueError('msg_id required')
        channel = await resolve_channel_ref(channel_ref)
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        await msg.pin(reason=f'pinned via discord_mb by {identity}')
        return {'msg_id': str(msg.id), 'channel_id': str(channel.id), 'pinned': True}

    async def op_unpin(channel_ref, msg_id):
        if not msg_id:
            raise ValueError('msg_id required')
        channel = await resolve_channel_ref(channel_ref)
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        await msg.unpin(reason=f'unpinned via discord_mb by {identity}')
        return {'msg_id': str(msg.id), 'channel_id': str(channel.id), 'pinned': False}

    async def op_context(channel_ref):
        '''On-demand channel "memory": the same enrichment the inbound path builds,
        but for any channel without needing a message. Returns channel descriptor
        (type/name/topic/category/parent), guild, pinned messages (downloaded to
        pins/), and pinned forum posts with history (downloaded to messages/).'''
        channel = await resolve_channel_ref(channel_ref)
        info = channel_info(channel)
        g = getattr(channel, 'guild', None)
        guild_info = {'id': str(g.id), 'name': g.name} if g is not None else None
        pinned = await resolve_pins(channel) if hasattr(channel, 'pins') else []
        # Pinned forum posts: this channel's own if it IS a forum, else the parent
        # forum's if this is a forum post (thread under a forum/media channel).
        forum = None
        if isinstance(channel, discord.ForumChannel):
            forum = channel
        elif isinstance(channel, discord.Thread) and channel.parent is not None \
                and getattr(getattr(channel.parent, 'type', None), 'name', None) in ('forum', 'media'):
            forum = channel.parent
        pinned_forum_posts = await resolve_pinned_forum_posts(forum) if forum is not None else []
        return {
            'channel': info,
            'guild': guild_info,
            'pinned_messages': pinned,
            'pinned_forum_posts': pinned_forum_posts,
        }

    def _as_forum(channel):
        'Return the ForumChannel for a forum ref (the channel itself, or a post\'s parent forum). Raises otherwise.'
        if isinstance(channel, discord.ForumChannel):
            return channel
        if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
            return channel.parent
        raise RuntimeError(f'{type(channel).__name__} is not a forum (or a post in one)')

    async def op_forum_list(channel_ref, include_archived=False, limit=100):
        'List a forum\'s posts (threads): pinned + active always; archived when asked. Active posts come from cache; archived are fetched.'
        forum = _as_forum(await resolve_channel_ref(channel_ref))
        limit = max(1, min(int(limit), 200))
        tag_name = {t.id: t.name for t in getattr(forum, 'available_tags', [])}

        def entry(t):
            tags = [getattr(tg, 'name', None) or tag_name.get(getattr(tg, 'id', tg)) or str(tg)
                    for tg in (getattr(t, 'applied_tags', None) or [])]
            mc = getattr(t, 'message_count', None)
            return {
                'thread_id': str(t.id),
                'name': t.name,
                'pinned': bool(getattr(getattr(t, 'flags', None), 'pinned', False)),
                'archived': bool(getattr(t, 'archived', False)),
                'locked': bool(getattr(t, 'locked', False)),
                # Discord's message_count excludes the initial/starter post; +1 to count it.
                'message_count': mc + 1 if isinstance(mc, int) else mc,
                'created': t.created_at.isoformat() if t.created_at else None,
                'owner_id': str(t.owner_id) if getattr(t, 'owner_id', None) else None,
                'tags': tags,
            }
        seen, posts = set(), []
        for t in getattr(forum, 'threads', []):
            if t.id not in seen:
                seen.add(t.id)
                posts.append(entry(t))
        if include_archived:
            try:
                async for t in forum.archived_threads(limit=limit):
                    if t.id not in seen:
                        seen.add(t.id)
                        posts.append(entry(t))
            except Exception as e:
                log(f'archived_threads fetch failed for forum {forum.id}: {type(e).__name__}: {e}')
        posts.sort(key=lambda p: p['created'] or '', reverse=True)   # newest first
        posts.sort(key=lambda p: 0 if p['pinned'] else 1)            # pinned first (stable)
        posts = posts[:limit]
        return {'forum_id': str(forum.id), 'forum_name': forum.name, 'count': len(posts), 'posts': posts}

    async def op_forum_create(channel_ref, name, content, tags=None):
        'Create a new forum post (thread + starter message). Optional applied tags by name.'
        if not name:
            raise ValueError('name (post title) required')
        if not content:
            raise ValueError('content (starter message) required')
        forum = _as_forum(await resolve_channel_ref(channel_ref))
        kwargs: dict[str, Any] = {
            'name': name,
            'content': content,
            'allowed_mentions': discord.AllowedMentions.none(),
            'reason': f'forum post created via discord_mb by {identity}',
        }
        if tags:
            by_name = {t.name.lower(): t for t in getattr(forum, 'available_tags', [])}
            want = [s.strip() for s in tags if s.strip()]
            unknown = [w for w in want if w.lower() not in by_name]
            if unknown:
                raise RuntimeError(f'unknown forum tag(s) {unknown}; available: {[t.name for t in forum.available_tags]}')
            kwargs['applied_tags'] = [by_name[w.lower()] for w in want]
        tm = await forum.create_thread(**kwargs)
        thread, starter = tm.thread, tm.message
        return {
            'thread_id': str(thread.id),
            'name': thread.name,
            'forum_id': str(forum.id),
            'starter_msg_id': str(starter.id) if starter else None,
            'jump_url': getattr(thread, 'jump_url', None),
        }

    async def op_forum_delete(channel_ref):
        'Delete a forum post (its thread). Deleting the starter message does NOT remove a post — this does.'
        channel = await resolve_channel_ref(channel_ref)
        if not isinstance(channel, discord.Thread):
            raise RuntimeError(f'{type(channel).__name__} is not a forum post/thread')
        tid = str(channel.id)
        await channel.delete(reason=f'forum post deleted via discord_mb by {identity}')
        return {'thread_id': tid, 'deleted': True}

    async def op_message_edit(channel_ref, msg_id, content):
        'Edit a message. Discord only permits editing messages THIS bot authored.'
        if not msg_id:
            raise ValueError('msg_id required')
        if not content:
            raise ValueError('content required')
        channel = await resolve_channel_ref(channel_ref)
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        if client.user is None or msg.author.id != client.user.id:
            raise RuntimeError('can only edit messages authored by this bot')
        await msg.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
        return {'msg_id': str(msg.id), 'channel_id': str(channel.id), 'edited': True}

    async def op_message_delete(channel_ref, msg_id):
        'Delete a message (own always; others need Manage Messages — Discord enforces).'
        if not msg_id:
            raise ValueError('msg_id required')
        channel = await resolve_channel_ref(channel_ref)
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        await msg.delete()
        return {'msg_id': str(msg_id), 'channel_id': str(channel.id), 'deleted': True}

    async def op_move(source, limit, dest, dest_kind, title=None, keep=False,
                      before_id=None, dry_run=False):
        '''Relocate the last N messages of a channel to another destination.

        Discord has no move primitive, so each message is reposted by the bot
        (author + timestamp in the body, attachments re-uploaded) and the
        original is deleted afterwards. The delete happens per message and only
        once THAT message's copy landed, so an interruption mid-run can leave a
        copy without the delete, but never a delete without the copy.

        dest_kind: 'channel' (id/name, incl. an existing thread), 'dm'
        (identity or raw user id), or 'forum' (creates a post named `title`).
        '''
        src = await resolve_channel_ref(source)
        if not hasattr(src, 'history'):
            raise RuntimeError(f'{type(src).__name__} has no history to move from')
        limit = max(1, min(int(limit), 100))
        kwargs: dict[str, Any] = {'limit': limit}
        if before_id:
            kwargs['before'] = discord.Object(id=int(before_id))
        msgs = [m async for m in src.history(**kwargs)]
        msgs.reverse()          # chronological: they must land in reading order

        plan = [{'msg_id': str(m.id),
                 'from': reverse_identity(m.author.id) or m.author.name,
                 'created': m.created_at.isoformat(),
                 'preview': (m.content or '')[:120],
                 'attachments': len(m.attachments)} for m in msgs]
        if dry_run:
            return {'dry_run': True, 'source_id': str(src.id),
                    'source_name': getattr(src, 'name', None),
                    'count': len(plan), 'messages': plan}
        if not msgs:
            return {'source_id': str(src.id), 'count': 0, 'moved': 0,
                    'deleted': 0, 'messages': [], 'failures': []}

        # --- destination ---
        created_thread = None
        if dest_kind == 'dm':
            uid = state['identity_map'].get(dest)
            if uid is None and str(dest).isdigit():
                uid = int(dest)
            if uid is None:
                await refresh_directory()
                uid = state['identity_map'].get(dest)
            if uid is None:
                raise RuntimeError(f'unknown identity {dest!r} for a DM destination')
            user = client.get_user(uid) or await client.fetch_user(uid)
            target = user.dm_channel or await user.create_dm()
        elif dest_kind == 'forum':
            if not title:
                raise ValueError('title required when moving into a forum')
            forum = _as_forum(await resolve_channel_ref(dest))
            src_label = f'#{getattr(src, "name", src.id)}'
            tm = await forum.create_thread(
                name=title,
                content=f'Moved {len(msgs)} message(s) from {src_label}.',
                allowed_mentions=discord.AllowedMentions.none(),
                reason=f'messages moved via discord_mb by {identity}',
            )
            target = created_thread = tm.thread
        else:
            target = await resolve_channel_ref(dest)
        if not hasattr(target, 'send'):
            raise RuntimeError(f'{type(target).__name__} cannot receive messages')

        moved, failures = [], []
        for m in msgs:
            author = reverse_identity(m.author.id) or m.author.name
            files, overflow = [], []
            for a in m.attachments:
                try:
                    files.append(discord.File(io.BytesIO(await a.read()),
                                              filename=a.filename))
                except Exception as e:                  # too large, or gone
                    log(f'move: attachment {a.filename} not re-uploaded: {e}')
                    overflow.append(a.url)
            try:
                new_ids = []
                for i, frame in enumerate(moved_frames(
                        author, m.created_at.isoformat(), m.content, overflow)):
                    kw: dict[str, Any] = {
                        'content': frame,
                        'allowed_mentions': discord.AllowedMentions.none(),
                    }
                    if i == 0 and files:        # attachments ride the first frame
                        kw['files'] = files
                    sent = await target.send(**kw)
                    new_ids.append(str(sent.id))
            except Exception as e:
                # copy failed -> leave the original alone and keep going
                failures.append({'msg_id': str(m.id), 'stage': 'copy', 'error': str(e)})
                continue

            entry = {'msg_id': str(m.id), 'new_msg_ids': new_ids,
                     'from': author, 'deleted': False}
            if not keep:
                try:
                    await m.delete()
                    entry['deleted'] = True
                except Exception as e:
                    # copy already landed; report rather than abort
                    failures.append({'msg_id': str(m.id), 'stage': 'delete',
                                     'error': str(e)})
            moved.append(entry)

        return {
            'source_id': str(src.id),
            'source_name': getattr(src, 'name', None),
            'dest_id': str(target.id),
            'dest_kind': dest_kind,
            'thread_id': str(created_thread.id) if created_thread else None,
            'jump_url': getattr(created_thread, 'jump_url', None),
            'count': len(msgs),
            'moved': len(moved),
            'deleted': sum(1 for e in moved if e['deleted']),
            'kept': bool(keep),
            'messages': moved,
            'failures': failures,
        }

    async def op_list_servers():
        'Every guild the bot is in, main flagged.'
        main_id = state['guild'].id if state.get('guild') else None
        out = []
        for g in client.guilds:
            out.append({
                'id': str(g.id),
                'name': g.name,
                'member_count': getattr(g, 'member_count', None),
                'owner_id': str(g.owner_id) if getattr(g, 'owner_id', None) else None,
                'channel_count': len(g.channels),
                'is_main': g.id == main_id,
            })
        return out

    def _resolve_guild(server_ref=None):
        'Guild by id/name, or the main guild when unspecified.'
        if server_ref:
            sref = str(server_ref).strip()
            guild = client.get_guild(int(sref)) if sref.isdigit() else discord.utils.get(client.guilds, name=sref)
            if guild is None:
                raise RuntimeError(f'server {server_ref!r} not found')
            return guild
        guild = state.get('guild') or (client.guilds[0] if client.guilds else None)
        if guild is None:
            raise RuntimeError('no guild available')
        return guild

    async def op_list_channels(server_ref=None):
        'Channels of a guild (default main): type + category + active (cached) thread counts.'
        guild = _resolve_guild(server_ref)
        chans = []
        for c in guild.channels:
            entry = {
                'id': str(c.id),
                'name': getattr(c, 'name', None),
                'type': getattr(getattr(c, 'type', None), 'name', None),
                'category': getattr(getattr(c, 'category', None), 'name', None),
            }
            threads = getattr(c, 'threads', None)
            if threads is not None and not isinstance(c, discord.CategoryChannel):
                entry['active_thread_count'] = len(threads)
            chans.append(entry)
        return {'server_id': str(guild.id), 'server_name': guild.name, 'count': len(chans), 'channels': chans}

    async def op_channel_create(server_ref, ctype, name, category_id=None, topic=None):
        'Create a text/forum/voice channel or a category in a guild (default main).'
        if not name:
            raise ValueError('name required')
        guild = _resolve_guild(server_ref)
        ctype = (ctype or 'text').lower()
        category = None
        if category_id and ctype != 'category':
            cref = str(category_id).strip()
            category = guild.get_channel(int(cref)) if cref.isdigit() else discord.utils.get(guild.categories, name=cref)
            if not isinstance(category, discord.CategoryChannel):
                raise RuntimeError(f'category {category_id!r} not found in {guild.name!r}')
        kwargs: dict[str, Any] = {'reason': f'created via discord_mb by {identity}'}
        if category is not None:
            kwargs['category'] = category
        if ctype == 'text':
            if topic:
                kwargs['topic'] = topic
            ch = await guild.create_text_channel(name, **kwargs)
        elif ctype == 'forum':
            if topic:
                kwargs['topic'] = topic
            ch = await guild.create_forum(name, **kwargs)
        elif ctype == 'voice':
            ch = await guild.create_voice_channel(name, **kwargs)
        elif ctype == 'category':
            kwargs.pop('category', None)
            ch = await guild.create_category(name, **kwargs)
        else:
            raise ValueError(f'unknown channel type {ctype!r} (text/forum/voice/category)')
        return {'id': str(ch.id), 'name': ch.name,
                'type': getattr(getattr(ch, 'type', None), 'name', None),
                'category': category.name if category else None}

    async def op_channel_delete(channel_ref):
        'Delete a guild channel or category (NOT a DM). Deleting a category orphans its children.'
        channel = await resolve_channel_ref(channel_ref)
        if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
            raise RuntimeError('cannot delete a DM channel')
        cid, name = str(channel.id), getattr(channel, 'name', None)
        await channel.delete(reason=f'deleted via discord_mb by {identity}')
        return {'id': cid, 'name': name, 'deleted': True}

    async def op_thread_edit(channel_ref, name=None, archived=None, locked=None, pinned=None, tags=None):
        'Edit a thread / forum post: rename, archive/unarchive, lock/unlock, pin/unpin (forum top), set applied tags.'
        channel = await resolve_channel_ref(channel_ref)
        if not isinstance(channel, discord.Thread):
            raise RuntimeError(f'{type(channel).__name__} is not a thread/forum post')
        kwargs: dict[str, Any] = {'reason': f'thread edit via discord_mb by {identity}'}
        if name is not None:
            kwargs['name'] = name
        if archived is not None:
            kwargs['archived'] = archived
        if locked is not None:
            kwargs['locked'] = locked
        if pinned is not None:
            kwargs['pinned'] = pinned
        if tags is not None:
            parent = channel.parent
            avail = {t.name.lower(): t for t in getattr(parent, 'available_tags', [])} if parent else {}
            want = [s.strip() for s in tags if s.strip()]
            unknown = [w for w in want if w.lower() not in avail]
            if unknown:
                raise RuntimeError(f'unknown forum tag(s) {unknown}; available: {[t.name for t in getattr(parent, "available_tags", [])]}')
            kwargs['applied_tags'] = [avail[w.lower()] for w in want]
        if len(kwargs) == 1:
            raise ValueError('nothing to edit')
        await channel.edit(**kwargs)
        return {'thread_id': str(channel.id), 'edited': sorted(k for k in kwargs if k != 'reason')}

    async def op_channel_edit(channel_ref, name=None, category_id=None, topic=None):
        'Edit a guild channel: rename, move to a category, or set topic.'
        channel = await resolve_channel_ref(channel_ref)
        if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
            raise RuntimeError('cannot edit a DM channel')
        kwargs: dict[str, Any] = {'reason': f'channel edit via discord_mb by {identity}'}
        if name is not None:
            kwargs['name'] = name
        if topic is not None:
            kwargs['topic'] = topic
        if category_id is not None:
            guild = channel.guild
            cref = str(category_id).strip()
            cat = guild.get_channel(int(cref)) if cref.isdigit() else discord.utils.get(guild.categories, name=cref)
            if not isinstance(cat, discord.CategoryChannel):
                raise RuntimeError(f'category {category_id!r} not found')
            kwargs['category'] = cat
        if len(kwargs) == 1:
            raise ValueError('nothing to edit (give --name / --category / --topic)')
        await channel.edit(**kwargs)
        return {'id': str(channel.id), 'name': getattr(channel, 'name', None),
                'edited': sorted(k for k in kwargs if k != 'reason')}

    # ---- custom emoji -----------------------------------------------------
    # Guild emoji are how an agent gets to have a FACE here: the Clawd set on
    # the main server is shared vocabulary, not decoration. Listing is the
    # common call by far -- you look up what exists, then use it inline.
    async def op_emoji_list(server_ref=None):
        'Every custom emoji in a guild (default main), with ready-to-paste refs.'
        guild = _resolve_guild(server_ref)
        out = []
        for e in guild.emojis:
            out.append({
                'id': str(e.id),
                'name': e.name,
                'animated': bool(e.animated),
                'available': bool(getattr(e, 'available', True)),
                'url': str(e.url),
                # what you actually paste into a message
                'ref': f'<{"a" if e.animated else ""}:{e.name}:{e.id}>',
                'shortcode': f':{e.name}:',
            })
        out.sort(key=lambda x: x['name'].lower())
        anim = sum(1 for e in out if e['animated'])
        return {'server_id': str(guild.id), 'server_name': guild.name,
                'count': len(out), 'animated': anim, 'static': len(out) - anim,
                'emoji': out}

    def _resolve_emoji(guild, ref):
        'Emoji by id or by name (case-insensitive), within one guild.'
        r = str(ref).strip().lstrip(':').rstrip(':')
        if r.isdigit():
            hit = discord.utils.get(guild.emojis, id=int(r))
        else:
            hit = next((e for e in guild.emojis if e.name.lower() == r.lower()), None)
        if hit is None:
            raise RuntimeError(f'emoji {ref!r} not found in {guild.name!r}')
        return hit

    async def op_emoji_upload(server_ref, name, image_b64):
        'Create a custom emoji. Discord caps the image at 256 KB.'
        guild = _resolve_guild(server_ref)
        raw = base64.b64decode(image_b64)
        if len(raw) > 256 * 1024:
            raise ValueError(f'image is {len(raw)/1024:.0f} KB, over Discord\'s 256 KB emoji cap')
        e = await guild.create_custom_emoji(
            name=name, image=raw, reason=f'uploaded via discord_mb by {identity}')
        return {'id': str(e.id), 'name': e.name, 'animated': bool(e.animated),
                'ref': f'<{"a" if e.animated else ""}:{e.name}:{e.id}>',
                'bytes': len(raw), 'server_name': guild.name}

    async def op_emoji_delete(server_ref, ref):
        'Delete a custom emoji. NOTE: this breaks <:name:id> refs in old messages.'
        guild = _resolve_guild(server_ref)
        e = _resolve_emoji(guild, ref)
        info = {'id': str(e.id), 'name': e.name, 'server_name': guild.name}
        await e.delete(reason=f'deleted via discord_mb by {identity}')
        return info

    async def op_emoji_rename(server_ref, ref, new_name):
        'Rename a custom emoji in place -- the id (and old refs) survive.'
        guild = _resolve_guild(server_ref)
        e = _resolve_emoji(guild, ref)
        old = e.name
        e = await e.edit(name=new_name, reason=f'renamed via discord_mb by {identity}')
        return {'id': str(e.id), 'old_name': old, 'name': e.name,
                'ref': f'<{"a" if e.animated else ""}:{e.name}:{e.id}>',
                'server_name': guild.name}

    async def op_message_reactions(channel_ref, msg_id):
        """Read one message's reactions without fetching it for anything else."""
        if not msg_id:
            raise ValueError('msg_id required')
        channel = await resolve_channel_ref(channel_ref)
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        return {'msg_id': str(msg.id), 'channel_id': str(channel.id),
                'reactions': reaction_records(msg)}

    async def op_message_react(channel_ref, msg_id, emoji, remove=False):
        'Add or remove (the bot\'s own) reaction on a message.'
        if not msg_id:
            raise ValueError('msg_id required')
        if not emoji:
            raise ValueError('emoji required')
        channel = await resolve_channel_ref(channel_ref)
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception as e:
            raise RuntimeError(f'fetch_message({msg_id}) failed: {e}') from e
        if remove:
            if client.user is None:
                raise RuntimeError('client.user is None')
            await msg.remove_reaction(emoji, client.user)
        else:
            await msg.add_reaction(emoji)
        return {'msg_id': str(msg.id), 'channel_id': str(channel.id), 'emoji': emoji, 'removed': bool(remove)}

    # --- status-plugin: in-loop runtime (presence is set from THIS gateway session) ---
    class _StatusContext:
        '''Handle passed to a plugin\'s run(ctx). The plugin never touches the
        discord client directly — it only sets presence through this thin surface,
        so we can validate/clamp and the plugin stays decoupled from discord.py.'''

        def __init__(self):
            self.identity = identity

        async def set_status(self, text, kind='playing', url=None, status=None):
            'Set the bot presence. kind: playing/listening/watching/competing/custom/streaming.'
            activity = build_activity(text, kind=kind, url=url)
            st = getattr(discord.Status, str(status).lower(), None) if status else None
            await status_plugin_gateway_call(
                client.change_presence(activity=activity, status=st))
            state['status_last'] = status_presence_record(
                text, kind=kind, url=url, status=status)

        async def clear(self):
            'Clear presence (back to online, no activity).'
            await status_plugin_gateway_call(
                client.change_presence(activity=None))
            state['status_last'] = None

        async def sleep(self, seconds):
            'Non-blocking sleep — ALWAYS use this (or asyncio.sleep), never time.sleep.'
            await asyncio.sleep(seconds)

        def log(self, msg):
            log(f'[status-plugin] {msg}')

    async def _run_status_plugin(run_fn):
        'Wrapper task: isolates plugin exceptions so a crash never kills the connector.'
        async def _finished():
            log('[status-plugin] run() returned; plugin finished (presence left as last set)')
            state['status_state'] = 'finished'

        async def _transport_failed(e):
            cause = e.__cause__ or e
            log(f'[status-plugin] transport interrupted; retry after reconnect: '
                f'{type(cause).__name__}: {cause}')
            state['status_state'] = 'retrying'
            state['status_error'] = f'{type(cause).__name__}: {cause}'

        async def _plugin_failed(e):
            log(f'[status-plugin] crashed: {type(e).__name__}: {e}')
            state['status_state'] = 'crashed'
            state['status_error'] = f'{type(e).__name__}: {e}'
            state['status_last'] = None
            try:
                await client.change_presence(activity=None)
            except Exception:
                pass
            sweep_status_plugin(identity)  # a broken plugin auto-uninstalls

        await run_status_plugin_task(
            run_fn, _StatusContext(), finished=_finished,
            transport_failed=_transport_failed, plugin_failed=_plugin_failed)

    async def _stop_status_plugin(clear=True):
        task = state.get('status_task')
        state['status_task'] = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if clear:
            try:
                await client.change_presence(activity=None)
            except Exception:
                pass
            state['status_last'] = None

    async def _start_status_plugin(run_fn):
        await _stop_status_plugin(clear=False)  # replacing the active one; about to set anew
        state['status_state'] = 'running'
        state['status_error'] = None
        state['status_task'] = client.loop.create_task(_run_status_plugin(run_fn))

    async def _restart_status_plugin_after_reconnect():
        '''Restart only a transport-interrupted installed plugin.'''
        if state.get('status_state') != 'retrying':
            return False
        task = state.get('status_task')
        if task is not None and not task.done():
            return None
        slot_py = status_plugin_slot(identity) / STATUS_PLUGIN_NAME
        if read_status_manifest(identity) is None or not slot_py.is_file():
            state['status_state'] = 'empty'
            state['status_task'] = None
            return False
        try:
            run_fn = load_status_plugin(str(slot_py))
            await _start_status_plugin(run_fn)
        except Exception as e:
            log(f'[status-plugin] reconnect restart failed: {type(e).__name__}: {e}')
            state['status_state'] = 'crashed'
            state['status_error'] = f'{type(e).__name__}: {e}'
            sweep_status_plugin(identity)
            return False
        return True

    async def _recover_status_after_gateway():
        async def _replay():
            try:
                return await replay_last_presence(client, state)
            except Exception as e:
                log(f'status presence replay failed: {type(e).__name__}: {e}')
                return False

        async def _clear():
            try:
                await client.change_presence(activity=None)
            except Exception as e:
                log(f'status presence clear failed after reconnect: '
                    f'{type(e).__name__}: {e}')
                return False
            return True

        outcome = await recover_status_plugin_after_gateway(
            state, restart=_restart_status_plugin_after_reconnect,
            replay=_replay, clear=_clear)
        if outcome == 'restarted':
            log('status plugin restarted after gateway reconnect')
        elif outcome == 'cleared':
            log('cleared stale status presence after plugin recovery failed')

    async def _extension_start(path, *, persist):
        '''Load + setup an extension. `persist` records it in the registry so
        the next connector start reloads it; a startup reload passes False.'''
        module, setup, command = load_extension(path)
        # Drop the outgoing generation's handlers; the installed dispatchers
        # read this same dict, so they pick the new ones up with no re-setattr.
        for _handlers in state['ext_listeners'].values():
            _handlers.clear()
        _cancelled = cancel_tracked_tasks(state['ext_tasks'])
        if _cancelled:
            log(f'cancelled {_cancelled} task(s) from the previous extension load')
        ctx = _ExtensionContext(client, identity, flavor, log, emit_event,
                                listeners=state['ext_listeners'],
                                installed=state['ext_installed'],
                                tasks=state['ext_tasks'],
                                deliver=write_inbox)
        # Resolved off the loaded extension module, so its type is not
        # knowable from this source.
        # pylint: disable-next=not-callable
        await setup(ctx)
        state['extension'] = {'path': str(path), 'command': command, 'module': module}
        state['extension_ctx'] = ctx
        state['extension_error'] = None
        if persist:
            reg = read_extension_registry(identity, flavor)
            reg['path'] = str(path)
            write_extension_registry(identity, reg, flavor)
        log(f'extension loaded: {path}')

    async def op_extension_set(path):
        try:
            await _extension_start(path, persist=True)
        except Exception as e:
            state['extension_error'] = f'{type(e).__name__}: {e}'
            return {'error': state['extension_error']}
        return {'ok': True, 'path': str(path)}

    async def op_extension_remove():
        reg = read_extension_registry(identity, flavor)
        reg.pop('path', None)
        write_extension_registry(identity, reg, flavor)
        cancel_tracked_tasks(state['ext_tasks'])
        state['extension'] = None
        state['extension_ctx'] = None
        state['extension_error'] = None
        # discord.py has no "drop every listener this module added", so the
        # outgoing handlers live until the process does. Say so rather than
        # pretending the unload was complete.
        return {'ok': True, 'restart_required': True}

    async def op_extension_list():
        reg = read_extension_registry(identity, flavor)
        return {'ok': True,
                'registered': reg.get('path'),
                'loaded': bool(state['extension']),
                'last_error': state['extension_error'],
                'store_keys': sorted((reg.get('store') or {}).keys()),
                'last_heartbeat': reg.get('last_heartbeat'),
                # What this process is executing against what a restart would
                # pick up, so one call answers "has the library moved under
                # me?" without an out-of-band watch on site-packages.
                'running_version': state['running_package'].get('version'),
                'installed_version': package_fingerprint().get('version')}

    async def op_extension_call(argv):
        ext = state['extension']
        if not ext:
            return {'error': 'no extension loaded'}
        if not ext.get('command'):
            return {'error': 'extension defines no command(ctx, argv)'}
        try:
            # ext is the extension state dict, checked for 'command' just
            # above.
            # pylint: disable-next=unsubscriptable-object
            result = await ext['command'](state['extension_ctx'],
                                          list(argv or []))
        except Exception as e:
            return {'error': f'{type(e).__name__}: {e}'}
        return {'ok': True, 'result': result}

    async def heartbeat_watcher():
        '''One event per UTC calendar day, so a connector that has silently
        stopped becomes visible. Calendar-aligned rather than 24h-since-start,
        so connectors started at different times do not drift apart.

        Also the periodic place where the installed package is compared with
        the one this process imported: an install replaces the files under a
        live connector without touching the process, and nothing else here
        would ever notice. Independently guarded, so a failing heartbeat does
        not take the comparison down with it.'''
        while not client.is_closed():
            try:
                today = time.strftime('%Y-%m-%d', time.gmtime())
                reg = read_extension_registry(identity, flavor)
                if heartbeat_due(reg.get('last_heartbeat'), today):
                    reg['last_heartbeat'] = today
                    write_extension_registry(identity, reg, flavor)
                    emit_event({'event': 'heartbeat', 'identity': identity,
                                'date': today,
                                'running_version': state['running_package'].get('version'),
                                'uptime_s': int(time.time() - _connector_started_at)})
            except Exception as e:
                log(f'heartbeat failed: {type(e).__name__}: {e}')
            try:
                installed = package_fingerprint()
                changed = package_change_event(identity, state['running_package'],
                                               installed, state['reported_package'])
                if changed:
                    # Said, not acted on: dropping a live gateway connection
                    # is the owning session's decision, not this loop's.
                    state['reported_package'] = installed
                    emit_event(changed)
                    log(f"installed package changed: {changed['running']} -> "
                        f"{changed['installed']}; restart to run it")
            except Exception as e:
                log(f'installed-version check failed: {type(e).__name__}: {e}')
            await asyncio.sleep(300)

    async def op_status_plugin_set(path):
        if not path:
            raise ValueError('path required')
        if not Path(path).is_file():
            raise FileNotFoundError(f'no such file: {path}')
        slot_py = install_status_plugin(identity, path)  # sweep + copy (source untouched)
        try:
            run_fn = load_status_plugin(slot_py)
        except Exception:
            sweep_status_plugin(identity)  # bad plugin: don't leave a half-install
            raise
        manifest = {'source': str(Path(path).resolve()), 'name': Path(path).name,
                    'installed_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        write_status_manifest(identity, manifest)
        await _start_status_plugin(run_fn)
        return {'installed': True, 'name': manifest['name'], 'source': manifest['source']}

    async def op_status_plugin_remove():
        had = read_status_manifest(identity) is not None or state.get('status_task') is not None
        await _stop_status_plugin(clear=True)
        sweep_status_plugin(identity)
        state['status_state'] = 'empty'
        return {'removed': bool(had)}

    async def op_status_plugin_list():
        man = read_status_manifest(identity)
        task = state.get('status_task')
        return {
            'installed': man is not None,
            'manifest': man,
            'state': state.get('status_state', 'empty'),
            'running': bool(task is not None and not task.done()),
            'last_status': state.get('status_last'),
            'error': state.get('status_error'),
        }

    async def resolve_reply_target(reply_to):
        '''Server-side reply_to → (channel_id, is_dm_or_None) via local caches:
        the message cache, the pins cache, and every identity's inbox on this
        host. No live guild scan: an unresolvable id is a loud SendError, never
        a silent reroute to #agents.

        The message cache holds three things, and the third used to be missing:
        messages op_conversation returned (so anything BROWSED is reply-able),
        messages fetched as reply/link targets, and — since perform_send caches
        each chunk it sends — messages this host SENT. Without that last one,
        replying to your own just-sent message failed here and the only fix was
        to browse the channel to warm a cache the connector had already been
        holding the message for.'''
        rid = str(int(reply_to))
        for root in (msg_cache_dir(), pins_dir()):
            for p in root.glob(f'*/{rid}.json'):
                return int(p.parent.name), None
        for p in STATE_ROOT.glob(f'*/inbox/{rid}.json'):
            try:
                o = json.loads(p.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if o.get('channel_id'):
                return int(o['channel_id']), bool(o.get('is_dm'))
        raise SendError(
            f'reply_to {rid} not found in local caches — reply to a message that '
            f'reached an inbox on this host, run `conversation` on its channel first '
            f'(results are cached and become reply-able), or pass --channel explicitly')

    async def perform_send(data):
        '''Shared send core for the outbox watcher (fire-and-forget) and the
        `send --wait` meta op. Handles reply routing, proactive DMs, recipient
        mentions, auto-chunking (>MAX_BODY bodies become a numbered chunk
        train; mention + reply-ref + attachments ride the first chunk), and
        pin-on-send. Returns {'msg_id', 'msg_ids', 'channel_id', 'chunks',
        'pinned'}. Raises SendRetry (transient — caller may retry the same
        payload) or SendError (permanent — report and drop).'''
        to = data.get('to', '')
        subject = data.get('subject', '')
        body = data.get('body', '')
        reply_to = data.get('reply_to')
        reply_channel_id = data.get('reply_channel_id')
        attach = data.get('attach') or []

        # --- resolve the target channel ---
        channel = None
        if data.get('dm') and not reply_to:
            uid = state['identity_map'].get(to)
            if uid is None and to.isdigit():
                uid = int(to)                   # raw user id: DM a non-registered human
            if uid is None:
                await refresh_directory()
                uid = state['identity_map'].get(to)
            if uid is None:
                raise SendError(f'unknown identity {to!r} for --dm')
            try:
                user = client.get_user(uid) or await client.fetch_user(uid)
                channel = user.dm_channel or await user.create_dm()
            except Exception as e:
                raise SendRetry(f'DM channel for {to!r} unavailable: {e}') from e
        elif reply_to:
            if not reply_channel_id:
                reply_channel_id, _ = await resolve_reply_target(reply_to)
            try:
                channel = (client.get_channel(int(reply_channel_id))
                           or await client.fetch_channel(int(reply_channel_id)))
            except Exception as e:
                raise SendRetry(f'fetch reply channel {reply_channel_id} failed: {e}') from e
        elif data.get('channel'):
            try:
                channel = await resolve_channel_ref(data['channel'])
            except Exception as e:
                raise SendError(f'send target channel {data["channel"]!r} unresolved: {e}') from e
        else:
            channel = ch('bridge')
            if channel is None:
                raise SendRetry(f'bridge channel #{BRIDGE_CHANNEL_NAME} not resolved yet')

        dm_mode = isinstance(channel, (discord.DMChannel, discord.GroupChannel))
        if not dm_mode and not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise SendError(f'channel {getattr(channel, "id", "?")} is '
                            f'{type(channel).__name__}; cannot send there')

        # --- mention + frame ---
        if dm_mode:
            mention = ''
            frame = f'**[{identity}]** {subject}'.rstrip() if subject else f'**[{identity}]**'
            allowed = discord.AllowedMentions.none()
        else:
            if to == 'nobody':
                mention = ''  # user-less send: post with no mention
            elif to == 'all':
                rid = role_id('broadcast')
                if rid is None:
                    raise SendRetry(f'broadcast role {BROADCAST_ROLE_NAME!r} not resolved')
                mention = f'<@&{rid}>'
            else:
                tos = [t.strip() for t in to.split(',') if t.strip()]
                mentions, unknown, refreshed = [], [], False
                for t in tos:
                    if t.isdigit():
                        # Raw Discord user id — mention directly, no directory
                        # lookup (reaches non-registered humans).
                        mentions.append(f'<@{t}>')
                        continue
                    uid = state['identity_map'].get(t)
                    if uid is None and not refreshed:
                        await refresh_directory()
                        refreshed = True
                        uid = state['identity_map'].get(t)
                    if uid is None:
                        unknown.append(t)
                    else:
                        mentions.append(f'<@{uid}>')
                if unknown:
                    raise SendError(f'unknown recipient identities {unknown!r}')
                mention = ' '.join(mentions)
            frame = (f'**[{identity}→{to}]** {subject}'.rstrip() if subject
                     else f'**[{identity}→{to}]**')
            allowed = discord.AllowedMentions(roles=True, users=True)
        prefix = f'{mention} ' if mention else ''

        # --- attachments (first chunk only; rebuilt per attempt — a failed
        # send exhausts discord.File handles) ---
        for a in attach:
            p = Path(a)
            if not p.is_file():
                raise SendError(f'attachment not found: {a}')
            if p.stat().st_size > 9_500_000:
                raise SendError(f'attachment over the ~10MB Discord cap: {a} '
                                f'(use `attachments upload` for the R2/side-channel route)')

        def make_files():
            return [discord.File(str(Path(a))) for a in attach] or None

        # --- chunk the body to fit under Discord's 2000-char message cap ---
        overhead = max(len(f'{prefix}{frame}'), len(f'{frame} (99/99)')) + 2
        parts = chunk_body(body, 2000 - overhead) or ['']

        ref = None
        if reply_to:
            try:
                ref_msg = await channel.fetch_message(int(reply_to))
                ref = ref_msg.to_reference(fail_if_not_exists=False)
            except Exception:
                ref = None  # right channel already; reply-ref is best-effort

        async def send_once(kwargs, files):
            if files:
                kwargs = {**kwargs, 'files': files}
            return await channel.send(**kwargs)

        sent_ids = []
        first_sent = None
        n = len(parts)
        for i, part in enumerate(parts):
            head = f'{prefix}{frame}' if i == 0 else f'{frame} ({i + 1}/{n})'
            kwargs: dict[str, Any] = {
                'content': f'{head}\n{part}' if part else head,
                'allowed_mentions': allowed,
            }
            if i == 0 and ref is not None:
                # Quiet reply: the content mention is the ONE ping; the reply
                # reference must not add a second notification.
                kwargs['reference'] = ref
                kwargs['mention_author'] = False
            try:
                sent = await send_once(kwargs, make_files() if i == 0 else None)
            except discord.HTTPException as e:
                if getattr(e, 'status', None) == 429:
                    retry = float(getattr(e, 'retry_after', 5) or 5)
                    log(f'rate limited, sleeping {retry}s')
                    await asyncio.sleep(retry)
                    try:
                        sent = await send_once(kwargs, make_files() if i == 0 else None)
                    except Exception as e2:
                        if sent_ids:
                            raise SendError(f'chunk {i + 1}/{n} failed after rate-limit '
                                            f'retry: {e2} (already sent: {sent_ids})') from e2
                        raise SendRetry(f'rate limited twice: {e2}') from e2
                elif sent_ids:
                    raise SendError(f'chunk {i + 1}/{n} failed: {e} (already sent: {sent_ids})') from e
                elif isinstance(e, discord.Forbidden):
                    raise SendError(f'forbidden: {e}') from e
                else:
                    raise SendRetry(f'send failed: {e}') from e
            except (SendError, SendRetry):
                raise
            except Exception as e:
                if sent_ids:
                    raise SendError(f'chunk {i + 1}/{n} failed: {e} (already sent: {sent_ids})') from e
                raise SendRetry(f'send error: {e}') from e
            sent_ids.append(str(sent.id))
            # Cache our OWN sends, so a later --reply-to on one of them resolves
            # without a `conversation` round-trip first. resolve_reply_target()
            # globs exactly the path save_message() writes, but until now that
            # cache only ever held messages we RECEIVED or BROWSED — never ones
            # we sent. Replying to a message this very host had just delivered
            # therefore failed with "not found in local caches", which is a
            # baffling thing to be told about your own message, and the
            # documented workaround (run `conversation` on the channel to warm
            # the cache) was pure ceremony: the connector already held the
            # message object.
            #
            # Every chunk is cached, not just the first, because a chunked send
            # produces several real message ids and any of them is a legitimate
            # reply target.
            #
            # Best-effort by design: the message is already delivered by this
            # point, so a cache-write failure must never propagate. Raising here
            # would turn a SUCCESSFUL send into a reported failure and, on the
            # fire-and-forget path, into a retry that double-posts.
            try:
                save_message(sent)
            except Exception as e:
                log(f'send-cache write failed for {sent.id}: {type(e).__name__}: {e}')
            if i == 0:
                first_sent = sent

        pinned = None
        if data.get('pin') and first_sent is not None:
            try:
                await first_sent.pin(reason=f'pinned on send by {identity}')
                pinned = True
            except Exception as e:
                log(f'pin-on-send failed for {first_sent.id}: {type(e).__name__}: {e}')
                pinned = False

        return {'msg_id': sent_ids[0], 'msg_ids': sent_ids,
                'channel_id': str(channel.id), 'chunks': n, 'pinned': pinned}

    async def dispatch_outbox_file(f):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception as e:
            log(f'unreadable outbox file {f.name}: {e}; deleting')
            try:
                f.unlink()
            except OSError:
                pass
            return
        try:
            result = await perform_send(data)
        except SendRetry as e:
            log(f'send retry ({f.name}): {e}')  # leave file; next tick retries
            return
        except SendError as e:
            # Permanent: drop the file and tell the session via the event
            # stream — never silently reroute or retry forever.
            log(f'send FAILED ({f.name}): {e}')
            emit_event(cap_event_subject({
                'event': 'send_failed', 'to': data.get('to'),
                'subject': data.get('subject'), 'error': str(e)[:300]}))
            try:
                f.unlink()
            except OSError:
                pass
            return
        if result['chunks'] > 1:
            log(f'sent {result["chunks"]}-chunk message {result["msg_id"]} to {data.get("to")}')
        try:
            f.unlink()
        except OSError:
            pass

    def routing_match(msg):
        if client.user is None:
            return False
        if msg.author.id == client.user.id:
            return False
        # DMs are 1:1 with the bot — any non-self message in a DMChannel is for us.
        if isinstance(msg.channel, discord.DMChannel):
            return True
        rid = role_id('broadcast')
        if rid is not None and any(r.id == rid for r in msg.role_mentions):
            return True
        if any(u.id == client.user.id for u in msg.mentions):
            return True
        # Continuation chunks of a train addressed to us carry no mention (it
        # rides chunk 1 for the one-ping property) — route on the frame instead.
        return continuation_chunk_for(msg.content, identity)

    def reverse_identity(user_id):
        for name, uid in state['identity_map'].items():
            if uid == user_id:
                return name
        return None

    async def resolve_pins(chobj):
        '''Pinned messages for a channel, TTL-cached 60s per channel id.

        Downloads each pinned message's full content to pins/<pin_msg_id>.json and
        returns a list of {msg_id, from, created, path}. The pins() iterator already
        yields full Message objects, so there is no extra fetch per pin. Needs
        read_message_history; on failure caches the prior value (or []) to avoid
        hammering. Discord caps pins at 50 per channel.'''
        cache = state['pins_cache']
        cid = chobj.id
        now = time.time()
        hit = cache.get(cid)
        if hit and now - hit[0] < 60:
            return hit[1]
        pdir = pins_dir() / str(cid)
        pdir.mkdir(parents=True, exist_ok=True)
        entries = []
        try:
            async for p in chobj.pins(limit=50):
                pfrom = reverse_identity(p.author.id) or p.author.name
                created = p.created_at.isoformat()
                pin_payload = attach_extras({
                    'msg_id': str(p.id),
                    'from': pfrom,
                    'from_user_id': str(p.author.id),
                    'created': created,
                    'body': p.content,
                    'channel_id': str(p.channel.id),
                    'attachments': [{'filename': a.filename, 'url': a.url, 'size': a.size,
                                     'content_type': a.content_type} for a in p.attachments],
                }, p)
                ppath = pdir / f'{p.id}.json'
                ptmp = ppath.with_suffix('.json.tmp')
                ptmp.write_text(json.dumps(pin_payload, indent=2), encoding='utf-8')
                ptmp.replace(ppath)
                entries.append({'msg_id': str(p.id), 'from': pfrom,
                                'created': created, 'path': str(ppath)})
        except Exception as e:
            log(f'pins() failed for channel {cid}: {type(e).__name__}: {e}')
            entries = hit[1] if hit else []
        cache[cid] = (now, entries)
        return entries

    def save_message(m):
        '''Persist a fetched message (reply-target / linked) to
        messages/<channel_id>/<msg_id>.json; return a summary with its path.
        Pure file write, no network.'''
        mdir = msg_cache_dir() / str(m.channel.id)
        mdir.mkdir(parents=True, exist_ok=True)
        payload = attach_extras({
            'msg_id': str(m.id),
            'from': reverse_identity(m.author.id) or getattr(m.author, 'name', '?'),
            'from_user_id': str(m.author.id),
            'created': m.created_at.isoformat(),
            'body': getattr(m, 'content', ''),
            'channel_id': str(m.channel.id),
            'attachments': [{'filename': a.filename, 'url': a.url, 'size': a.size,
                             'content_type': a.content_type} for a in getattr(m, 'attachments', [])],
        }, m)
        mpath = mdir / f'{m.id}.json'
        mtmp = mpath.with_suffix('.json.tmp')
        mtmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        mtmp.replace(mpath)
        return {'msg_id': str(m.id), 'channel_id': str(m.channel.id),
                'from': payload['from'], 'created': payload['created'], 'path': str(mpath)}

    async def fetch_and_save_message(channel_id, message_id):
        'Resolve a channel by id, fetch a message, persist it. Returns summary dict or raises.'
        chan: Any = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        m = await chan.fetch_message(int(message_id))
        return save_message(m)

    async def resolve_pinned_forum_posts(forum):
        '''For a forum/media channel, find its pinned posts (pinned threads) and
        download each post's message history to messages/<thread_id>/<msg_id>.json.
        TTL-cached 60s per forum id. Best-effort; pinned threads come from the
        forum's cached active threads (a pinned forum post is never archived).'''
        cache = state['forum_pins_cache']
        fid = forum.id
        now = time.time()
        hit = cache.get(fid)
        if hit and now - hit[0] < 60:
            return hit[1]
        posts = []
        try:
            pinned = [t for t in getattr(forum, 'threads', [])
                      if getattr(getattr(t, 'flags', None), 'pinned', False)]
            for t in pinned:
                msgs = []
                try:
                    async for hm in t.history(limit=FORUM_POST_HISTORY_LIMIT):
                        msgs.append(save_message(hm))
                except Exception as e:
                    log(f'forum-post history fetch failed for thread {t.id}: {type(e).__name__}: {e}')
                msgs.reverse()  # chronological (history yields newest-first)
                posts.append({
                    'thread_id': str(t.id),
                    'name': t.name,
                    'created': t.created_at.isoformat() if t.created_at else None,
                    'message_count': len(msgs),
                    'messages': msgs,
                })
        except Exception as e:
            log(f'resolve_pinned_forum_posts failed for forum {fid}: {type(e).__name__}: {e}')
            posts = hit[1] if hit else []
        cache[fid] = (now, posts)
        return posts

    def channel_info(chobj):
        '''Channel descriptor: id / type / kind (dm/group_dm/thread/forum_post/
        channel) / name / topic / parent_* / category. Pure, no awaits. Shared by
        the inbound resolve_context and the on-demand `context` read op.'''
        ctype = getattr(getattr(chobj, 'type', None), 'name', None)
        info: dict[str, Any] = {'id': str(chobj.id), 'type': ctype}
        if isinstance(chobj, discord.DMChannel):
            info['kind'] = 'dm'
        elif isinstance(chobj, discord.GroupChannel):
            info['kind'] = 'group_dm'
            info['name'] = chobj.name  # may be None for an unnamed group DM
        elif isinstance(chobj, discord.Thread):
            parent = chobj.parent
            parent_type = getattr(getattr(parent, 'type', None), 'name', None)
            info['kind'] = 'forum_post' if parent_type in ('forum', 'media') else 'thread'
            info['name'] = chobj.name                       # thread / forum-post title
            info['parent_id'] = str(chobj.parent_id) if chobj.parent_id else None
            if parent is not None:
                info['parent_name'] = parent.name           # forum / text channel name
                info['parent_type'] = parent_type
                info['topic'] = getattr(parent, 'topic', None)
        else:
            # TextChannel / VoiceChannel / StageChannel / ForumChannel / news
            info['kind'] = 'channel'
            info['name'] = getattr(chobj, 'name', None)
            info['topic'] = getattr(chobj, 'topic', None)
        cat = getattr(chobj, 'category', None)  # TextChannel & Thread expose this
        info['category'] = {'id': str(cat.id), 'name': cat.name} if cat is not None else None
        return info

    async def resolve_context(msg):
        '''Resolve as much channel / guild / identity / reference context as cheaply
        possible. Best-effort: lands in the inbox-file payload (NOT the compact
        event, which is cap-bound). Resolves channel type + human "kind", channel /
        forum-post names, topic, parent + category, guild id+name, pinned messages,
        the reply-target message (downloaded), any Discord message links in the body
        (downloaded), forwarded snapshots, author identities/roles, and message meta
        (edited_at, type, jump_url, pinned, mention_everyone, stickers, embeds).
        Referenced + linked messages are saved to messages/<chan>/<msg>.json.'''
        import re as _re
        chobj = msg.channel
        info = channel_info(chobj)

        g = getattr(msg, 'guild', None)
        guild_info = {'id': str(g.id), 'name': g.name} if g is not None else None

        pinned = await resolve_pins(chobj)

        # Reply target — download the message this one replies to.
        reply_info = None
        ref = getattr(msg, 'reference', None)
        if ref is not None and getattr(ref, 'message_id', None):
            resolved = getattr(ref, 'resolved', None)
            if isinstance(resolved, discord.Message):
                try:
                    reply_info = save_message(resolved)
                except Exception as e:
                    reply_info = {'msg_id': str(ref.message_id), 'error': f'{type(e).__name__}: {e}'}
            elif type(resolved).__name__ == 'DeletedReferencedMessage':
                reply_info = {'msg_id': str(ref.message_id), 'deleted': True}
            else:
                try:
                    reply_info = await fetch_and_save_message(ref.channel_id or chobj.id, ref.message_id)
                except Exception as e:
                    reply_info = {'msg_id': str(ref.message_id),
                                  'channel_id': str(ref.channel_id) if ref.channel_id else None,
                                  'error': f'{type(e).__name__}: {e}'}

        # Linked messages — any Discord message links in the body get downloaded too.
        linked = []
        seen = set()
        for gpart, lchan, lmsg in _re.findall(
                r'https?://(?:\w+\.)?discord(?:app)?\.com/channels/(@me|\d+)/(\d+)/(\d+)', msg.content or ''):
            if lmsg in seen:
                continue
            seen.add(lmsg)
            url = f'https://discord.com/channels/{gpart}/{lchan}/{lmsg}'
            try:
                entry = await fetch_and_save_message(lchan, lmsg)
            except Exception as e:
                entry = {'msg_id': lmsg, 'channel_id': lchan, 'error': f'{type(e).__name__}: {e}'}
            entry['url'] = url
            linked.append(entry)

        # Forwarded snapshots are delivered inline (no fetch needed). A snapshot
        # is a whole message, so it carries embeds and Components V2 of its own —
        # and for a forwarded bot post that is ALL it carries, content being ''.
        # Best-effort like the rest of resolve_context: a malformed component or
        # embed must cost its own line, not the whole context block (channel,
        # pins, reply_to) for this message.
        try:
            extras = message_extras(msg)
        except Exception as e:
            log(f'message_extras failed for {msg.id}: {type(e).__name__}: {e}')
            extras = {'embeds': [], 'components': [], 'poll': None,
                      'forwarded': [], 'rendered': ''}
        forwarded = extras['forwarded']

        # Forum post → also pull the parent forum's PINNED posts + their history.
        pinned_forum_posts = []
        if info.get('kind') == 'forum_post' and isinstance(chobj, discord.Thread) and chobj.parent is not None:
            pinned_forum_posts = await resolve_pinned_forum_posts(chobj.parent)

        author = msg.author
        author_roles = ([r.name for r in author.roles if r.name != '@everyone']
                        if isinstance(author, discord.Member) else None)
        identities = [name for name, mapped in state['identity_map'].items() if mapped == author.id]
        return {
            'channel': info,
            'guild': guild_info,
            'pinned_messages': pinned,
            'pinned_forum_posts': pinned_forum_posts,
            'reply_to': reply_info,
            'linked_messages': linked,
            'forwarded': forwarded,
            'author_identities': identities,
            'author_has_multiple_identities': len(identities) > 1,
            'author_display_name': getattr(author, 'display_name', None),
            'author_username': getattr(author, 'name', None),
            'author_roles': author_roles,
            'edited_at': msg.edited_at.isoformat() if getattr(msg, 'edited_at', None) else None,
            'message_type': getattr(getattr(msg, 'type', None), 'name', None),
            'jump_url': getattr(msg, 'jump_url', None),
            'pinned': bool(getattr(msg, 'pinned', False)),
            'mention_everyone': bool(getattr(msg, 'mention_everyone', False)),
            'stickers': [s.name for s in getattr(msg, 'stickers', [])],
            'embed_count': len(getattr(msg, 'embeds', [])),
            # The payloads themselves, not just a count. `body_rendered` is
            # content + embeds + components + poll + forwards as one readable
            # block, and it is present ONLY when it differs from the raw body —
            # so its presence means "the body alone is not the message".
            'embeds': extras['embeds'],
            'components': extras['components'],
            'poll': extras['poll'],
            'body_rendered': (extras['rendered']
                              if extras['rendered'] != (msg.content or '').strip() else None),
        }

    async def write_inbox(msg, source='live'):
        frm = reverse_identity(msg.author.id) or msg.author.name
        body = msg.content
        inbox = inbox_dir(identity)
        path = inbox / f'{msg.id}.json'
        if path.exists():
            # dedup: already seen (e.g. backfill overlap with live on_message,
            # or an extension relaying a message that also mentions us).
            return path
        is_dm = isinstance(msg.channel, discord.DMChannel)
        to_label, subject = parse_message_header(body, identity)
        # Resolve channel / guild / identity context. Best-effort: it enriches the
        # inbox-file payload, so any failure is logged and skipped, never blocking
        # the write or the event.
        try:
            context = await resolve_context(msg)
        except Exception as e:
            log(f'resolve_context failed for {msg.id}: {type(e).__name__}: {e}')
            context = {}
        # An embed-only, Components-V2 or forwarded message has content '' — so
        # its subject is '' too, and the notification announces nothing at all.
        # Fall back to the rendered payload, which is the actual message.
        if not subject and context.get('body_rendered'):
            _, subject = parse_message_header(context['body_rendered'], identity)
        payload = {
            'from': frm,
            'from_user_id': str(msg.author.id),
            'subject': subject,
            'body': body,
            'attachments': [{'filename': a.filename, 'url': a.url, 'size': a.size,
                             'content_type': a.content_type} for a in msg.attachments],
            'created': msg.created_at.isoformat(),
            'msg_id': str(msg.id),
            'channel_id': str(msg.channel.id),
            'is_dm': is_dm,
            **context,
        }
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        tmp.replace(path)
        # Compact event: the event carries only time, header (from→to + subject)
        # and the path; the body AND all resolved context live in the inbox file
        # at `path`. Subject trimmed to the Monitor cap by cap_event_subject.
        emit_event(cap_event_subject({
            'event': 'message',
            'source': source,
            'created': msg.created_at.isoformat(),
            'from': frm,
            'to': to_label if len(to_label) <= 50 else to_label[:50],
            'subject': subject,
            'msg_id': str(msg.id),
            'path': str(path),
        }))
        # The receipt is attempted AFTER the record is durable, so a
        # delivery is never marked ✅ that was not written. Its outcome is then
        # recorded on that same record -- without this the ✅ is documented as
        # part of the delivery guarantee with no surface to confirm it on.
        receipt = {'emoji': RECEIPT_EMOJI, 'ok': True, 'error': None}
        try:
            await msg.add_reaction(RECEIPT_EMOJI)
        except Exception as e:
            receipt = {'emoji': RECEIPT_EMOJI, 'ok': False,
                       'error': f'{type(e).__name__}: {e}'}
            log(f'react failed on {msg.id}: {e}')
            emit_event({'event': 'receipt_failed', 'msg_id': str(msg.id),
                        'emoji': RECEIPT_EMOJI, 'error': receipt['error'],
                        'path': str(path)})
        payload['receipt'] = receipt
        tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        tmp.replace(path)
        return path

    @client.event
    async def on_message(msg):
        if not routing_match(msg):
            return
        try:
            await write_inbox(msg)
        except Exception as e:
            log(f'on_message error: {e}')

    async def refresh_directory():
        channel = ch('directory')
        if channel is None:
            return
        new_map = {}
        self_has_entry = False
        self_user_id = client.user.id if client.user is not None else None
        try:
            async for msg in channel.history(limit=200):
                if msg.pinned:
                    continue
                content = msg.content or ''
                uid_match = re.search(r'<@!?(\d+)>', content)
                if not uid_match:
                    continue
                uid = int(uid_match.group(1))
                # Identity: first **bold** token on any line, else first non-empty line's
                # leading word before ':' / '=' / ' · '
                name = None
                bold = re.search(r'\*\*([^*\n]+)\*\*', content)
                if bold:
                    name = bold.group(1).strip()
                else:
                    for line in content.splitlines():
                        line = line.strip().lstrip('-').strip()
                        if not line:
                            continue
                        for sep in (':', '=', ' · '):
                            if sep in line:
                                name = line.split(sep, 1)[0].strip()
                                break
                        if name:
                            break
                if not name:
                    continue
                # Last-writer-wins isn't right since history is newest-first; keep first seen.
                new_map.setdefault(name, uid)
                if self_user_id is not None and msg.author.id == self_user_id:
                    self_has_entry = True
        except Exception as e:
            log(f'refresh_directory failed: {e}')
            return
        state['identity_map'] = new_map
        state['self_has_entry'] = self_has_entry
        if new_map:
            log(f'directory map: {list(new_map.keys())}')

    async def self_register():
        channel = ch('directory')
        if channel is None:
            log(f'directory channel #{DIRECTORY_CHANNEL_NAME} not resolved; skipping self-register')
            return
        await refresh_directory()
        if state.get('self_has_entry'):
            return
        if client.user is None:
            log('client.user is None; skipping self-register')
            return
        self_user_id = client.user.id
        content = (
            f'**{identity}** · <@{self_user_id}>\n\n'
            f'- host: {sys.platform}\n'
            f'- python: {sys.version.split()[0]}\n'
            f'- started: {time.strftime("%Y-%m-%dT%H:%M:%S")}\n\n'
            f'_Edit this message in Discord to add role, repo, capabilities, notes._\n'
        )
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            log(f'self-registered as {identity} → {self_user_id}')
            await refresh_directory()
        except Exception as e:
            log(f'self-register failed: {e}')

    def usage_guild_targets(guild):
        """One guild's status channels -> ({provider: channel}, claim_channel).

        Matched on the word in a VOICE channel name, not on an id or an exact
        title, because this code renames them — `claude status` becomes
        `claude · 5h 🟢 0% · …` and must still resolve on the next pass. First
        match per provider wins.

        Scanned per guild rather than only on the bridge guild: the bot sits in
        several servers, each of which may carry its own board, and each is
        published independently. A server without these channels is simply
        skipped, which is what makes "create the channels" the entire
        enablement step and "delete them" the off switch.
        """
        targets, claim = {}, None
        for c in getattr(guild, 'voice_channels', []):
            low = (getattr(c, 'name', '') or '').lower()
            if 'claim' in low:
                claim = claim or c       # the lease channel, not a status one
                continue
            for p in USAGE_PROVIDERS:
                if p in low and p not in targets:
                    targets[p] = c
        return targets, claim

    async def acquire_usage_claim(chan):
        """True when this connector may publish TO THIS GUILD's board.

        The lease is per guild for the same reason the local gate is: an
        identity holding the lease on the main server must not gate a server it
        is not a member of. No claim channel in that guild -> always True.

        The lease is advisory and deliberately cheap: it is re-stamped every
        USAGE_CLAIM_REFRESH, not every period, so it costs one rename per 10
        minutes rather than one per update. A lease that has gone quiet for
        USAGE_CLAIM_TTL is taken over, which bounds how long a dead owner can
        freeze the display — the tradeoff being that a crashed owner leaves the
        numbers stale for up to that long, so TTL is minutes, not hours.
        """
        if chan is None:
            return True                  # no lease channel -> name-equality only
        me = f'{identity}@{HOSTNAME}'
        owner, age = parse_claim_name(getattr(chan, 'name', ''))
        if owner == me and age is not None and age < USAGE_CLAIM_REFRESH:
            return True                  # ours and fresh — nothing to write
        if owner and owner != me and age is not None and age < USAGE_CLAIM_TTL:
            return False                 # someone else is alive and publishing
        # Free, expired, or ours-but-stale. Jitter, then re-read: another host
        # deciding the same thing at the same second will have written by now,
        # and last-writer-wins is settled by what the gateway shows afterwards.
        await asyncio.sleep(random.uniform(0, USAGE_STATUS_JITTER))
        owner, age = parse_claim_name(getattr(chan, 'name', ''))
        if owner and owner != me and age is not None and age < USAGE_CLAIM_TTL:
            return False
        try:
            await chan.edit(name=render_claim_name(me), reason='usage status lease')
            log(f'usage status: lease taken by {me} in {chan.guild.name}')
        except Exception as e:
            log(f'usage status: lease write failed: {type(e).__name__}: {e}')
            return False                 # could not claim -> do not publish
        return True

    async def update_usage_status():
        """One update period, from a connector that may be one of many machines.

        Every step is ordered to make the fleet-wide outcome "one rename per
        period" without any shared storage:

          1. period bucket — the same integer on every host right now, and this
             process handles each one at most once (in-memory guard).
          2. local claim file — stops the OTHER identities on this box from
             each spawning a usage_query subprocess for the same period.
          3. jitter — spreads hosts that woke on the same boundary, so the
             winner is settled by the gateway instead of a photo finish.
          4. name equality, re-read immediately before the edit — the actual
             cross-host interlock. A connector on another machine computing the
             same string from the same account usage has already published it,
             and this one then does nothing.
        """
        period = usage_period()
        if state.get('usage_period') == period:
            return                       # this process already handled it
        state['usage_period'] = period
        data = None                      # fetched lazily, ONCE, reused per guild
        for guild in list(client.guilds):
            targets, claim_chan = usage_guild_targets(guild)
            if not targets:
                continue                 # no board here -> feature is off
            if not claim_usage_slot(period, guild_id=guild.id):
                continue                 # another identity on this box has it
            if not await acquire_usage_claim(claim_chan):
                continue                 # another HOST holds this guild's lease
            if data is None:
                data = await asyncio.to_thread(fetch_usage)
                if not data:
                    log('usage status: no usage data this period')
                    return
            pending = []
            for provider, chan in sorted(targets.items()):
                name = render_usage_name(provider, data.get(provider))
                if name and name != getattr(chan, 'name', None):
                    pending.append((chan, name))
            if not pending:
                continue                 # already correct — nothing to publish
            await asyncio.sleep(random.uniform(0, USAGE_STATUS_JITTER))
            for chan, name in pending:
                # Re-read the live name AFTER the jitter: another host may have
                # published this exact string while we waited, and renaming to a
                # value the channel already carries just burns rate-limit budget.
                if name == getattr(chan, 'name', None):
                    continue
                try:
                    await chan.edit(name=name, reason='usage status refresh')
                    log(f'usage status: [{guild.name}] {chan.id} -> {name}')
                except Exception as e:
                    # Forbidden (no Manage Channels here) or a 429 that outlived
                    # its retries: that is this guild's problem, not the pass's.
                    log(f'usage status: rename failed for {chan.id} in '
                        f'{guild.name}: {type(e).__name__}: {e}')

    async def usage_status_watcher():
        """Re-check every USAGE_STATUS_POLL; act at most once per period.

        The poll is deliberately shorter than the period so a connector that
        starts mid-period, or reconnects, publishes as soon as it can rather
        than waiting out a full boundary — the failure mode being avoided is a
        stale name, which is the whole point of the feature.
        """
        await asyncio.sleep(5)           # let on_ready finish its own work first
        while not client.is_closed():
            try:
                await update_usage_status()
            except Exception as e:
                log(f'usage_status_watcher: {type(e).__name__}: {e}')
            await asyncio.sleep(USAGE_STATUS_POLL)

    @client.event
    async def on_ready():
        if client.user is None:
            log('on_ready fired but client.user is None — unexpected')
            return
        log(f'connected as {client.user} (id={client.user.id})')
        try:
            await resolve_topology()
            await self_register()
        except Exception as e:
            log(f'startup tasks failed: {e}')
        await _recover_status_after_gateway()
        # discord.py fires on_ready on every reconnect, but the watcher tasks
        # from the previous connection keep running (they only check
        # client.is_closed(), which stays False across reconnects). Spawning
        # again here would race N watchers on the same outbox files and post
        # each message N times. Guard so they start exactly once per process.
        if not state['watchers_started']:
            state['watchers_started'] = True
            client.loop.create_task(outbox_watcher())
            client.loop.create_task(meta_watcher())
            client.loop.create_task(parent_watchdog())
            client.loop.create_task(leech_watcher())
            client.loop.create_task(usage_status_watcher())
            client.loop.create_task(heartbeat_watcher())
            # Built-in default status plugin: load it once at startup if the user
            # hasn't installed one. A later `status-plugin set` replaces it (single
            # slot). Best-effort — a failure here must never break the connector.
            try:
                _default_plugin = default_status_plugin(flavor)
                if state.get('status_task') is None and _default_plugin.is_file():
                    await op_status_plugin_set(str(_default_plugin))
                    log(f'default status plugin loaded ({_default_plugin.name}, flavor={flavor})')
            except Exception as e:
                log(f'default status plugin load failed: {type(e).__name__}: {e}')
            # A registered extension is reloaded on EVERY connector start: its
            # registration is durable by design, unlike a status plugin, which
            # is swept at startup. Best-effort — a broken extension must not
            # stop the connector coming up.
            try:
                _ext_path = read_extension_registry(identity, flavor).get('path')
                if _ext_path:
                    await _extension_start(_ext_path, persist=False)
            except Exception as e:
                state['extension_error'] = f'{type(e).__name__}: {e}'
                log(f'extension load failed: {state["extension_error"]}')

    @client.event
    async def on_resumed():
        # discord.py dispatches RESUMED without READY.  Use the same idempotent
        # recovery path so a plugin interrupted during a gateway resume cannot
        # remain installed-but-dead indefinitely.
        await _recover_status_after_gateway()

    primary = None
    primary_tb = None
    if not token:
        raise SystemExit('connector reached the gateway with no token')
    try:
        client.run(token, log_handler=None)
    except BaseException as exc:
        primary = exc
        primary_tb = sys.exc_info()[2]

    # Uninstall on shutdown (best-effort; the loop is closed so we can't await a
    # presence clear here — the dying session + the next connector's fresh connect
    # clears it, and the startup sweep is the hard guarantee).  BaseException is
    # intentional: a plugin's SystemExit/KeyboardInterrupt cannot skip cleanup.
    sweep_error = None
    sweep_tb = None
    try:
        sweep_status_plugin(identity)
    except BaseException as exc:
        sweep_error = exc
        sweep_tb = sys.exc_info()[2]
    cleanup_error = cleanup_startup()

    # Never let cleanup/sweep errors replace the connector's original failure.
    if primary is not None:
        raise primary.with_traceback(primary_tb)
    if sweep_error is not None:
        raise sweep_error.with_traceback(sweep_tb)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)


class ConnectorApp:
    """Configuration and execution boundary for one connector lifecycle.

    The gateway implementation deliberately remains in ``_run_connector`` so
    this refactor does not perturb its deeply tested cleanup and recovery
    ordering.  Keeping invocation state here gives callers and future
    lifecycle extractions an object boundary without changing the procedural
    compatibility API.
    """

    __slots__ = ('identity', 'claude_pid', 'token', 'log_path', 'flavor')

    def __init__(self, identity, claude_pid=None, token=None, log_path=None,
                 flavor=None):
        self.identity = identity
        self.claude_pid = claude_pid
        self.token = token
        self.log_path = log_path
        self.flavor = flavor

    def run(self):
        return _run_connector(
            self.identity,
            claude_pid=self.claude_pid,
            token=self.token,
            log_path=self.log_path,
            flavor=self.flavor,
        )


def connector_main(identity, claude_pid=None, token=None, log_path=None,
                   flavor=None):
    """Run one connector lifecycle through the object-oriented boundary."""
    return ConnectorApp(
        identity,
        claude_pid=claude_pid,
        token=token,
        log_path=log_path,
        flavor=flavor,
    ).run()


__all__ = [
    'ConnectorApp',
    '_run_connector',
    'connector_main',
    'leech_main',
]
