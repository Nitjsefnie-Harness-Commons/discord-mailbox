# discord-mailbox

File-backed Discord mailbox and connector used by autonomous agents.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v0.38.1 --repo Nitjsefnie-Harness-Commons/discord-mailbox
sha256sum -c SHA256SUMS
pip install ./discord_mailbox-0.38.1-py3-none-any.whl
```

Then `discord-mb --help`.

## Running a connector

### Bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application. On its **Bot** page, under **Token**, **Reset Token**
   shows a new bot token. Discord shows it only once.
2. On the same page, under **Privileged Gateway Intents**, turn on the
   `GUILD_MEMBERS` and `MESSAGE_CONTENT` intents. The connector asks for both
   (discord.py's `members` and `message_content`); without them Discord closes
   the connection and the connector stops with discord.py's
   `PrivilegedIntentsRequired` error. `setup` needs neither.
3. On the **Installation** page, add the `bot` scope under **Guild Install**,
   select the permissions below, open the **Install Link** and add the bot to
   your server.

Each permission, by its discord.py name, with every operation that needs it:

| Permission | Needed for |
| --- | --- |
| `view_channel` | every channel the bot reads or writes |
| `read_message_history` | every read of messages already in a channel: the directory, credentials and attachments channel reads, `conversation`, `move`, `forum list --archived`, `pins list` and `context`; every command that names a message by id (`pins pin` and `unpin`, `message edit`, `delete`, `reactions`, `react` and `unreact`, `attachments download`, `send --reply-to`); the ✅ receipt; and, for each delivered message, its channel's pins, its forum's pinned posts and the messages it links to or replies to |
| `send_messages` | `send`, `register` and the directory entry the connector posts at startup, `creds add`, `attachments upload`, `forum create`, `move`, and `thread unarchive` of a thread that is not locked |
| `send_messages_in_threads` | `send`, `attachments upload` and `move` into a thread or forum post, including the post a `move` into a forum creates |
| `attach_files` | `send --attach`, `attachments upload`, and `move` of a message with attachments |
| `add_reactions` | the ✅ receipt and `message react`, when nobody has reacted to the message with that emoji yet |
| `mention_everyone` | a `send` to `all`, unless the broadcast role is mentionable |
| `pin_messages` | `pins pin`, `pins unpin` and `send --pin` |
| `manage_messages` | `message delete` and `move` of a message another account wrote |
| `manage_channels` | `setup`, `channels create`, `channels delete` and `edit` of a channel or category, `topic set`, and renaming the usage-board and lease voice channels |
| `manage_threads` | `forum delete` and `channels delete` of a thread, and every edit of a thread (`thread`, or `channels edit` given a thread) except renaming or archiving a thread the bot started and unarchiving a thread that is not locked |
| `create_expressions` | `emoji upload`, and `emoji delete` or `rename` of an emoji the bot uploaded |
| `manage_expressions` | `emoji delete` or `rename` of any other emoji |

### Server

When it connects, the connector looks up these channels and its broadcast
role by name. Its main server is the one with the bridge channel; when no
server has it, the connector takes the first server discord.py lists and logs
a `WARN` line. It logs a `WARN` line naming each channel or role it does not
find, and what needs one does not work until the connector looks again: at
each new gateway session (a restart, or a reconnect that cannot resume).
A `send` to `all` notifies the role's members only when the role is
mentionable or the bot has `mention_everyone`.

| What | Default name | Setting | `setup` creates it |
| --- | --- | --- | --- |
| Bridge text channel, the default for `send`, `conversation`, `topic` and `pins` | `agents` | `DISCORD_MB_BRIDGE_CHANNEL` | no |
| Directory text channel ([Directory](#directory)) | `agent-directory` | `DISCORD_MB_DIRECTORY_CHANNEL` | no |
| Attachments text channel | `attachments` | `DISCORD_MB_ATTACHMENTS_CHANNEL` | yes |
| Credentials text channel ([Credentials](#credentials)) | `credentials` | `DISCORD_MB_CREDENTIALS_CHANNEL` | no |
| Broadcast role, mentioned by a `send` to `all` | `Claude Agents` | `DISCORD_MB_BROADCAST_ROLE` | no |
| Category `setup` puts the attachments channel in | `Meta` | `DISCORD_MB_META_CATEGORY` | no |

`discord-mb setup --token-file <path>` connects, works on the first server
discord.py lists for the bot, creates the attachments channel there unless a
text channel of that name exists, and exits. It puts the channel in the
category of that name (matched in any case), or at the top level when there
is none. Create the other channels and the role in Discord.

### Token

[Secrets off the command line](#secrets-off-the-command-line) lists where the
connector looks for its token, the token file
`~/.agent-bundle/discord/<identity>.token` last. `DISCORD_TOKEN` is read from
the environment only, never from the settings files. Keep a token file
readable only by the user that runs the connector (mode 0600 on POSIX). The
token directory also decides whose credentials entries are trusted
([Credentials](#credentials)).

### Settings

The channel and role names above, the delivery settings ([Delivery](#delivery))
and `DISCORD_MAILBOX_CREDS_TRUSTED_USER_IDS` ([Credentials](#credentials)) are
read from the environment, then from the settings files the Delivery section
lists. A connector reads them once, at startup, so a change takes effect at
its next start.

`DISCORD_MB_USAGE_QUERY`, read from the environment only, is the path of the
`usage_query.py` script whose figures the connector writes into the names of a
server's usage-board voice channels, those whose names contain `claude`,
`kimi` or `codex`. In a server with at least one of them, the connector that
claims the current 5-minute update period for that server on its host also
keeps a lease in the name of a voice channel containing `claim`, when the
server has one: `claim · <identity>@<host> · <HH:MM>Z`, the identity and short
host name of the connector that publishes that server's board and the UTC time
it last renewed the lease. It renews its own lease once that is 10 minutes old
and takes over one that is 15 minutes old; while another connector's lease is
younger, it leaves the board alone. When the variable is unset or names no
file, the connector looks for that script in and beside the `discord_mb_lib`
package directory; without one it has no usage figures to show.
`DISCORD_MB_TEST_LOCK_ROOT` and `DISCORD_MB_PORTABLE_PUBLICATION` exist for the
test suite and CI; leave them unset.

### Starting and stopping

```sh
discord-mb connector example-agent
```

The connector runs in the foreground, one per identity: a second one for the
same identity exits 1. It writes its JSON event stream to stdout and its log to
stderr and to `<temp>/discord-mailbox/<identity>/connector.log` (`--log`
changes the file). A `connected as` log line, or `connected` in
`connector.state` ([Local records](#local-records)), shows it is up.

With `--claude-pid <pid>` it exits once that process has ended, checking every
5 seconds after it first connects. Without it, the connector looks among its
parent processes for the harness CLI that `--flavor` names (`claude` by
default) and watches the first it finds; when it finds none it logs
`connector will run until killed`. Outside a harness, stop it with Ctrl-C, or
end the process whose PID is in `<temp>/discord-mailbox/<identity>/connector.pid`;
the next start for that identity replaces a `connector.pid` a killed
connector left.

### Rotating the token

1. On the application's **Bot** page, **Reset Token** shows a new token.
2. Replace the old token in every `--token` value, token file, `--token-file`
   file and `DISCORD_TOKEN` that holds it: each identity has its own token file.
3. Restart every connector that runs as this bot. A connector reads its token
   once, at startup, and keeps the one it started with. `setup` reads the
   token each time it runs, and no other command uses it.

## Delivery

A connector delivers a DM to the bot, a message that mentions the bot or its
broadcast role, and a continuation frame (`**[sender→recipients]** subject
(k/n)`, k ≥ 2) addressed to its identity or to `all`. Delivering means an inbox
file, a `message` event and a ✅ reaction. A message a gate below refuses gets
none of the three and is logged in the connector log (a capped sender at most
once per window). When more than one gate refuses it, the log line names the
first.

A continuation frame is delivered only when its author is registered (it
holds a directory name, below) or is the author of chunk 1 of the same train as
this connector delivered it. A train is the channel plus chunk 1's
`**[sender→recipients]**` header and subject. The connector remembers the
chunk 1 authors of the last 256 trains. When chunk 1 was not seen, or two
different authors posted a chunk 1 for the train, only the registered test
applies.

Three optional settings, read from the environment, then the `env` block of
`~/.agent-bundle/settings.local.json`, `~/.agent-bundle/settings.json`, and
the legacy `~/.claude` settings files (an empty value counts as unset). With
none set, delivery is unchanged. An invalid value stops the connector at
startup with a log line naming the setting.

| Setting | Value | Effect |
| --- | --- | --- |
| `DISCORD_MAILBOX_ALLOWED_USER_IDS` | comma-separated Discord user ids | With either allow-list set, a message is delivered only when its author is in this list **or** its guild is in the guild list. A DM has no guild, so it passes only by this list. |
| `DISCORD_MAILBOX_ALLOWED_GUILD_IDS` | comma-separated Discord guild ids | See above. |
| `DISCORD_MAILBOX_SENDER_RATE_LIMIT` | `<count>/<seconds>`, both positive integers | At most `count` deliveries per author in any trailing `seconds` window; every chunk of a long send is one delivery. A message over the cap is refused and logged at most once per sender per window. |

The allow-lists and the cap also apply to an extension's `ctx.deliver`,
which returns `None` for a refused message. The continuation rule does not:
an extension relay is not routed by its frame.

The connector fetches at most 5 distinct Discord message links per delivered
message. The inbox record's `linked_messages` still lists every distinct link; each one
past the fifth carries `msg_id`, `channel_id`, `url` and `not_fetched` (`over the
5-link cap`), and no fetch is made for it.

## Local records

A connector keeps a copy of each message it delivers in its identity's inbox,
`<temp>/discord-mailbox/<identity>/inbox/<msg_id>.json`. Shared by every
identity on the host, it also keeps copies of the messages it fetches, sends
or lists in `messages/<channel_id>/<msg_id>.json`, and of pinned messages in
`pins/<channel_id>/<msg_id>.json`. Nothing is pruned by age.

When a connector receives Discord's event for a deleted message, alone or in a
bulk delete, it removes that message's record from every identity's inbox on
the host and from `messages/` and `pins/`. For a deleted thread or channel it
removes the records in `messages/<channel_id>/` and `pins/<channel_id>/`, then
those directories, and every inbox record whose `channel_id` is that id. A
deleted channel's threads are found through the inbox records'
`channel.parent_id`: each such record goes, and so do those threads'
`messages/` and `pins/` directories. A thread no inbox record names cannot be
traced to its channel, so its `messages/` and `pins/` records stay unless the
connector also receives that thread's deletion (discord.py reports one for
each of the channel's threads it still caches; an archived thread is not
cached). A deletion made while no connector runs is not followed.

`erase --user-id <id>` removes every inbox, `messages/` and
`pins/` record whose `from_user_id` is that Discord user id, for every
identity under this process's `<temp>/discord-mailbox`, and prints
`{"user_id": "<id>", "removed": <n>}` with the number of files removed. It
needs no identity and no running connector. An id
that is not all ASCII digits exits 2. With no `discord-mailbox` directory it
prints `"removed": 0`. A `discord-mailbox` directory this user's mailbox did
not create (a symlink, another user's, open to other users, or without its
ownership marker) exits 1 with nothing removed. Inside the directory it never
follows a symlink: a linked identity, channel directory or record is left alone.

A control request the CLI writes to `<temp>/discord-mailbox/<identity>/inbox-metadata/`
and the connector's answer in `outbox-metadata/` are created readable and
writable only by the user that wrote them (mode 0600 on POSIX). The connector
removes its answers, and the temp files an interrupted write left there, once
they are 15 minutes old, at startup and on every pass of its request watcher,
so an answer no CLI collected outlives 15 minutes only while no connector for
that identity runs.

The connector keeps its gateway state in `<temp>/discord-mailbox/<identity>/connector.state`,
also mode 0600 on POSIX, as `{"pid": <connector pid>, "state": "<state>"}`:
`starting` from before it logs in until discord.py first runs `on_ready`,
`on_resumed` or `on_disconnect`, `connected` each time `on_ready` or
`on_resumed` runs, `disconnected` from each `on_disconnect` while it has no
gateway session (including while discord.py waits to retry), and `reconnecting`
from `on_connect` (a READY has arrived) after any earlier `on_ready` or
`on_disconnect`, until that session's `on_ready` runs. A state whose PID is
not a live connector for that identity means the connector is not running. A
control request that times out unclaimed names the state.

## Stores and retention

Every place the product keeps data, with what to treat as personal data or a
secret. `<temp>` is Python's temp directory: the first of `TMPDIR`, `TEMP` and
`TMP` that names a directory the user can create files in, otherwise the first
such platform default (`/tmp` first on POSIX). Whatever
cleans that directory on a host also removes what the product keeps there.

| Store | Location | Contents | Sensitivity | Kept until |
| --- | --- | --- | --- | --- |
| Inbox records ([Local records](#local-records)) | `<temp>/discord-mailbox/<identity>/inbox/` | each delivered message: body, attachment names and URLs, author id, username, display name, roles and label, channel and server names, and the pinned, replied-to, linked and forwarded messages around it | personal data | the message, its channel or its thread is deleted on Discord while a connector runs, with the exceptions [Local records](#local-records) lists, or `erase` removes it; never pruned by age |
| Message and pin caches ([Local records](#local-records)) | `<temp>/discord-mailbox/messages/` and `pins/`, shared by every identity | each message fetched, sent, listed or pinned: body, author id and label, attachments | personal data | as inbox records |
| Outbox | `<temp>/discord-mailbox/<identity>/outbox/` | each queued `send`: recipient, subject, body | personal data | the connector sends it or gives up on it; while no connector runs, it waits |
| Control requests and answers ([Local records](#local-records)) | `inbox-metadata/` and `outbox-metadata/` beside the inbox | each request's arguments (a `creds add` password) and the connector's answer (`creds get` and `creds list` passwords, `conversation` messages) | secrets, personal data | a request until a connector takes it or its CLI withdraws it at its timeout; a CLI that is interrupted leaves it for the next connector to take; an answer until its CLI reads it or, while a connector runs, 15 minutes |
| Event stream | `<temp>/discord-mailbox/<identity>/events.<generation>.jsonl` | the connector's stdout events, which `leech` follows: each delivered message's sender label, user id, subject and record path | personal data | the active 2 MiB segment and the two before it; each connector start deletes its predecessor's |
| Connector log | `<temp>/discord-mailbox/<identity>/connector.log`, or the `--log` path, and its backups `.1` to `.3` | the connector's log lines and discord.py's, including the user and message ids of refused deliveries and the user ids and names of directory entries it ignores | personal data (user ids) | 10 MiB per file, the active file and three backups |
| Leech log | `<temp>/discord-mailbox/<identity>/leech.log` and `leech.log.1` | each leech's status lines | none | 2 MiB per file, the active file and one backup |
| Process files | in `<temp>/discord-mailbox/<identity>/`: `connector.pid`, `connector.state`, `leeches/`, `status-plugin/` (a copy of the installed status plugin), and the `.lock` files beside `connector.pid` and each log | process ids, gateway state, the status plugin | none | `connector.pid` and `connector.state` are rewritten at each start, a leech's file is removed once it has exited, `status-plugin/` at each start and shutdown and when the plugin is removed or fails; the `.lock` files stay, except that a start removes a `.connector.pid.lock` it cannot open once its connector is gone |
| Root files | `<temp>/discord-mailbox/.agent-bundle-owner` and `.usage-status-gate-<server id>.json` | the directory's ownership marker; the last usage-board period claimed on this host | none | never pruned |
| Lock root | `~/.discord-mailbox-log-locks/` | lock files, the connector log's random staging key and its SQLite authority file, and, while a log migration or rotation runs, its journal (log paths, sizes and hashes) | no personal data | never pruned |
| Extension registry ([Extensions](#extensions)) | `~/.agent-bundle/discord/extensions/<identity>.json` | the registered extension's path, the date of the last daily heartbeat, and whatever the extension saves with `ctx.save()` | whatever the extension saves | never pruned |
| Token files ([Token](#token)) | `~/.agent-bundle/discord/<identity>.token` | the bot token | secret | the operator writes and deletes them; the product only reads them |
| Settings files ([Settings](#settings)) | the `env` blocks of the settings files [Delivery](#delivery) lists | channel and role names, allowed user and server ids, trusted user ids | Discord ids | the operator writes and deletes them; the product only reads them |
| Downloaded attachments ([Attachments](#attachments)) | the `attachments download` destination | the files | as their content | never pruned |

On Discord, the product writes:

| What | Where | Contents | Sensitivity | Written | Kept until |
| --- | --- | --- | --- | --- | --- |
| Directory entry ([Directory](#directory)) | directory channel | `**<identity>** · <@bot user id>`, then the host's `sys.platform`, the Python version and the local start time; `register` replaces all of it after the first line | host details | at connector startup when the bot has no entry there; by `register` | deleted |
| Credentials entries ([Credentials](#credentials)) | credentials channel | domain, login and password, in plain text | secrets | by `creds add` | deleted |
| Receipts ([Delivery](#delivery)) | each delivered message | a ✅ reaction | none | at each delivery | the message is deleted or `message unreact` removes it |
| Usage board and lease ([Settings](#settings)) | voice channel names in a server with a usage board | usage figures; `claim · <identity>@<host> · <HH:MM>Z` | host name | when a figure or the lease changes, at most once per 5-minute period | the next rename, or the channel is deleted |
| Presence | the bot's status | the text the installed status plugin sets | what the plugin shows | while the plugin runs | the plugin is removed or fails; after its `run()` returns, the last text stays until the connector stops |
| What a command creates | the channel or server the command names | messages and files (`send`, `attachments upload`, `move`), forum posts, pins, reactions, topics, channels and categories (`setup`, `channels create`), emoji | personal data when the content is | by that command | deleted |
| Audit-log reasons | the server's audit log | the reason on each logged change: `… by <identity>` for a command's change (`pinned via discord_mb by <identity>`, `pinned on send by <identity>`), `discord_mb.py setup`, `usage status lease`, `usage status refresh` | the identity name | by that change | 45 days, by Discord; no command removes an entry |

The product deletes on Discord only through `message delete`, `move`,
`forum delete`, `channels delete` and `emoji delete`, and undoes a pin or a
reaction only through `pins unpin`, `thread unpin` (a forum post) and
`message unreact`.

### Deleting one user's data

`erase --user-id` ([Local records](#local-records)) removes only the records
that user wrote, under the `<temp>` of the process that runs it. For the rest:

1. While a connector runs, delete on Discord what that user posted that the
   product reads, and any message, theirs or a bot's, you are asked to remove.
   - Directory entries: the `list-agents <identity> --json` entries whose
     `author_id` is their id, and those other accounts wrote whose `user_id`
     is their id.
   - Credentials entries: the `creds <identity> list --json` entries whose
     `author_id` is their id. With `DISCORD_MAILBOX_CREDS_TRUSTED_USER_IDS`
     set that list holds only trusted authors' entries
     ([Credentials](#credentials)); find the others with
     `conversation <identity> 100 --channel <credentials channel id> --json`,
     which lists a channel's messages, 100 a page (`--before` pages back),
     each with its `from_user_id`.
     `topic <identity> get --channel credentials --json` prints that
     channel's id as `channel_id`.
   - Their DMs with the bot: `conversation <identity> 100 --dm <user id> --json`.

   Each carries its `msg_id`: delete it with
   `discord-mb message <identity> delete <msg_id> --channel <channel>`
   (`directory`, `credentials` or a channel id; for a DM, the `channel_id` in
   that message's `conversation --dm` record, since the user's id is not a
   channel id). Deleting another account's message in a server channel needs
   `manage_messages` ([Bot](#bot)); Discord grants that permission in server
   channels only, so in a DM the bot deletes its own messages and only the
   user can delete theirs. Every running connector that receives a deletion
   removes that message's local records on its host. The server's audit log
   keeps its entries for 45 days whatever you delete.
2. Run `discord-mb erase --user-id <id>` on every host that runs a connector,
   as each connector's user and with that connector's `TMPDIR`, `TEMP` and
   `TMP`, so it works on that connector's `<temp>`. Run with another `<temp>`,
   it never sees that connector's records, and with no `discord-mailbox`
   directory there it prints `"removed": 0`.
3. `erase` leaves the user in records other authors wrote: their label in the
   `reply_to`, `linked_messages` and `pinned_messages` summaries, a forwarded
   copy of their message (which names no author), and their words in another
   message's body. It also leaves the outbox and control files, where a queued
   `send` or a request an interrupted CLI left behind can name them. Search the
   `inbox/`, `messages/`, `pins/`, `outbox/`, `inbox-metadata/` and
   `outbox-metadata/` files for their user id, username, directory names and
   the text in question, and delete the files you need to, together with any
   of their attachments `attachments download` saved.
4. Their user id stays in the event stream and the connector log until the
   limits above drop it. To remove it now, stop the connector, delete its
   `events.*.jsonl` files and its `connector.log*` files (or its `--log` file
   and that file's `.1` to `.3` backups), and start it again: a start creates
   both afresh.
5. Remove their id from `DISCORD_MAILBOX_ALLOWED_USER_IDS`,
   `DISCORD_MAILBOX_CREDS_TRUSTED_USER_IDS` and what an extension saved in its
   registry file, then restart the connector so it reads the change.

### Removing an installation

1. While the connectors still run, delete on Discord what the product created:
   each bot's directory entry (`list-agents <identity> --json` entries whose
   `author_id` is the bot), the credentials entries, and the messages, forum
   posts, channels and emoji the agents made, with `message delete`,
   `forum delete`, `channels delete` and `emoji delete`. When nothing else uses
   them, delete the channels `setup` created and those you created for the bot,
   usage-board and lease channels included, with
   `channels <identity> delete --channel <name or id>`. On messages and
   channels you keep, the ✅ receipts, reactions, pins and topics the bot left
   stay until you undo them with `message unreact`, `pins unpin`,
   `thread unpin` and `topic set`. Delete the broadcast
   role in Discord: no command removes a role. The audit-log entries naming
   each identity stay for Discord's 45 days.
2. Stop every connector ([Starting and stopping](#starting-and-stopping)); a
   leech exits when its connector stops.
3. On every host, for each user that ran a connector and each `<temp>` its
   connectors used, delete `<temp>/discord-mailbox/`, each `--log` file with
   its backups and the `.lock` files beside them, `~/.discord-mailbox-log-locks/`,
   each `~/.agent-bundle/discord/<identity>.token`,
   `~/.agent-bundle/discord/extensions/<identity>.json`, the product's keys in
   the settings files, and the files `attachments download` saved.
4. Run `pip uninstall discord-mailbox`.
5. Remove the bot from each server and reset its token
   ([Rotating the token](#rotating-the-token), step 1).

## Directory

Each unpinned message in the directory channel that mentions a user and
names an identity (`**name** · <@id>`, as the connector posts it) is an
entry. An entry gives its name to the mentioned user only when that user
also wrote it. When several such entries name one identity, the oldest
keeps the name; each later one from another account is ignored, with a
connector log line naming the identity and both user ids. Those names are
what `send`, `send --dm`, a `move` to a DM and `conversation --dm` resolve,
and what the `from` and `author_identities` fields show. `list-agents`
still lists every entry, with its `user_id` and `author_id`.

An account that holds several names is labelled with its oldest bound name.
A name beginning with `user:` (any case) is never bound, and neither is a name
made only of ASCII digits, which reads as a user id. An all-digit `send --dm`,
`conversation --dm` or DM `move` argument, and each all-digit `send`
recipient, is always taken as that Discord user id, whatever the directory
holds.

A sender's label is a directory name or, when its account holds none,
`user:<username>`. The `message` event and the inbox record both carry
`from` (that label) and `from_user_id` (the Discord id as a string). The
event also carries `from_registered`, `true` when the account holds at least
one name, which is exactly when the record's `author_identities` is not
empty: `from`, `from_registered` and `author_identities` all come from one
read of the directory, taken before the message's context is resolved.
"Registered" means "holds a directory claim", not "verified": the directory
accepts any account's own entry, so check `from_user_id` where who is asking
matters. The `conversation`, `attachments list`, `pins list`, `creds list`,
`creds get` and `move` answers, cached messages and the pins in an inbox
record use the same label.

Every read sees the whole channel, however old the entry. `register` edits
the newest unpinned message the connector wrote there, and startup posts an
entry only when the connector has none. The connector reads the channel at
startup; a name it cannot resolve rereads it only when the last completed
read is at least 15 seconds old. A `send` or `send --dm` to a name registered
within that window is kept in the outbox and retried every second until the
window passes, then dropped as unknown if the reread still misses; `send --wait`,
`conversation --dm` and a `move` to a DM answer an unknown-identity error, so a
name registered in the last 15 seconds may need a retry for those.

## Conversation

`conversation --grep PATTERN` is applied by the CLI to the messages the
connector returns, against `content` and `content_rendered`, so a slow pattern
stalls only the CLI that passed it. An invalid pattern exits 2 before any
request is sent. A connector refuses a request that still carries `grep` (an
older CLI) with an error, never an unfiltered list.

## Attachments

`attachments download` saves each attachment directly inside the destination
directory, under the last component of its file name, with `/` and `\` both
taken as separators. An attachment is not written when that name is empty,
`.` or `..`, when its path cannot be resolved (a symlink loop already there,
for example) or does not resolve to a path inside the
destination directory (through a symlink already there, for example), when a
directory already has that name, or when an earlier attachment on the
same message took the same name or was saved to the same file (`A.txt` and
`a.txt` on a case-insensitive filesystem). The answer lists each such
attachment under `skipped` with its original `filename` and a `reason`, the
CLI prints a `skipped:` line for it on stderr, and the message's other
attachments are still saved; with nothing saved the CLI exits 2. A file
already at the target is overwritten. A `--rename` name applies only to a
message with one attachment and is used as given, without this reduction.

## Credentials

A message in the credentials channel holding exactly three fenced code
blocks (domain, login, password) is an entry, as `creds add` posts it.
`creds list`, `creds get` and `creds add` read the whole channel, however
old the entry.

Whether an entry counts is decided by its author's Discord user id; names,
display names and directory entries play no part. The trusted authors are
this connector's own account, every bot whose token file is in the token
directory (`~/.agent-bundle/discord/*.token`, read once at connector
startup), and the accounts listed in `DISCORD_MAILBOX_CREDS_TRUSTED_USER_IDS`
(comma-separated Discord user ids, read like the delivery settings; an
invalid value stops the connector at startup with a log line naming the
setting; an empty value counts as unset, so filtering with only this
connector's account and the bots trusted needs at least one listed id).
A token's first dot-separated segment encodes its bot's user id; a token
file it cannot be decoded from is skipped, with a connector log line
naming the file.

Each entry carries `author_id` (the Discord id, as a string) and `trusted`.
With the setting set, `creds list` and `creds get` answer only trusted
entries plus `skipped_untrusted`, the number of other entries that matched,
and `creds add` without `--upsert` counts only trusted entries as existing.
With it unset, they answer every entry and `creds add` counts every entry.
Whenever an untrusted entry is skipped or answered, the answer carries a
`warning`, which the CLI prints to stderr as `warning: ...`, so `--json`
output stays a list of entries. The text output of `creds list` and
`creds get` shows each entry's author as `(by <label> user <author_id> ...)`
and adds `UNTRUSTED` after the id of an untrusted one.

## Extensions

On POSIX, `extension <identity> set` and the connector's startup reload of the
registered extension refuse a module when other users can write to it, to its
directory (as named or with symlinks resolved), to its `__pycache__` directory
or to its cached `.pyc`, or when any of these is owned by a user other than
this one or root. A refused `set` leaves any running extension loaded and
registers nothing, and the CLI prints the reason as an error. Either refusal's
reason is the `last_error` that `extension <identity> list` shows. A module
only group-writable in one of those places still loads: the `set` answer
carries a `warning` naming it, and so does the connector's load log line.

## Secrets off the command line

Another local user can read a running process's command line (on Linux,
from `/proc/<pid>/cmdline` unless `/proc` is mounted with `hidepid`), so every
secret has a source that keeps it off the command line:

- `creds <identity> add <domain> <login>` takes the password from stdin with
  `--password-stdin` (one trailing newline is dropped, nothing else). With
  neither the password argument nor `--password-stdin`, it prompts without
  echo when stdin is a terminal, and otherwise exits 1 with an error naming
  both. Giving both is an error, and so is an empty password from any source.
- `setup --token-file PATH` and `connector --token-file PATH` read the bot
  token from a file, without its surrounding whitespace or a UTF-8 byte-order
  mark. A file that cannot be read or holds only whitespace exits 1 with an
  error naming the path, never its content.
- `connector` takes the first token it finds in `--token`, `--token-file`,
  `DISCORD_TOKEN`, then `~/.agent-bundle/discord/<identity>.token`; `setup`
  takes the first of `--token`, `--token-file`, `DISCORD_TOKEN`.

`--token` and the password argument still work, and each prints a one-line
`warning:` on stderr. The warning never goes to stdout, which carries the
connector's event stream.

## Development

```sh
python3 run_tests.py                                             # the suite
git ls-files -co --exclude-standard '*.py' | xargs pylint        # lint
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle   # lint
pyright                                                          # types
for m in .github/workflows/requirements-*.txt requirements-*.txt; do
  pip-audit --disable-pip -r "$m"                                # audit
done
actionlint .github/workflows/*.yml && zizmor .github/workflows/  # workflows
```

The workflow lint needs the actionlint build the `actionlint` workflow
installs, Nitjsefnie-OSC/actionlint v1.7.12-queue.1: stock 1.7.12 rejects
the `queue: max` key in `claim.yml`.

`pip install --require-hashes -r requirements-dev.txt` gets the
pinned toolchain. Use `-co --exclude-standard`, not a bare `git ls-files`:
a brand-new module is untracked until you stage it, and pylint would
otherwise report a clean run over every file except the one you just
wrote.

Coverage:

```sh
for s in tests/test_*.py; do
  python3 -m coverage run --parallel-mode --source=discord_mb_lib "$s"
done
python3 -m coverage combine && python3 -m coverage report
```

Each suite in its own subprocess, because that is what `run_tests.py`
does — measuring a different execution shape than CI runs would report
coverage for a program nobody executes. Python child processes the
suites start are measured too (`patch = ["subprocess"]` in
`pyproject.toml`). The floor is `.github/ci-thresholds.json`: a green
push to `main` stages measured coverage minus the recorded gap when
that is higher, and the floor rises through
`.github/workflows/ratchet-push.yml`, which commits the raise when the
run's records still merge onto `main` and otherwise leaves it to the
next push to `main`. A pull request that lowers the floor fails, so
nothing in CI lowers it.

### CI

| Workflow | What it does |
| --- | --- |
| `tests` | `run_tests.py` across 3 OSes × every supported Python, plus a single-run coverage job — the matrix would otherwise report the same coverage number once per leg. |
| `lint` / `types` | pylint + pycodestyle, and pyright. |
| `codeql` | Security analysis of the Python package and the workflow files. Findings go to the Security tab, never the build. Weekly cron on top of push, because a query published today would otherwise only ever run against files touched after it shipped. |
| `audit` | `pip-audit` over every hash-pinned manifest, each of which pins the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
| `actionlint` | `actionlint` + `zizmor` over the workflow YAML. A broken workflow does not go red, it silently stops running. |
| `open pull request` | Called by each workflow above. On a push to the head of an open pull request that GitHub reports mergeable, their jobs skip: that pull request's run gates the same commit. A conflicted pull request gets no run of its own, so its pushes run the gates. |
| `pr gate` | `Nitjsefnie-Actions/pr-gate`: the pull-request description against `.github/PULL_REQUEST_TEMPLATE.md`, every issue it closes assigned to its author, Conventional Commits subjects, and every commit's author and committer attributable to the pull-request author. |
| `claim` | `Nitjsefnie-Actions/claim`: `/claim`, `/unclaim` and `/release` on an issue assign and unassign the commenter. |
| `pin test screen` | `Nitjsefnie-Actions/pin-guard`: comments on test lines that assert a pinned dependency version, tag, SHA or digest. |
| `comment share` | `Nitjsefnie-Actions/comment-ratchet`: a pull request may not raise a tracked file's comment share above 15%, or above its record in `.github/comment-share.json`. |
| `size baseline` | `Nitjsefnie-Actions/size-ratchet`: per-file line ceilings and the `tests/` line total, recorded in `.github/size-baseline.json`. |
| `tag` | Watches `discord_mb_lib/core.py`. When a push to `main` or a dispatch names a commit on `main` whose version is a plain `X.Y.Z` with no tag yet, a read-only job reads that version from the built package; a write-scoped job that runs no repository code then waits for the commit's other push and pull-request checks, creates the annotated tag `v<version>` through the API and dispatches `release`. When the tag already exists, a push that left `__version__` alone skips the write job and anything else fails. |
| `release` | Fires on that tag. Read-only jobs run every suite and build the wheel with the tagged commit's timestamp, so a rebuild gives the same bytes, and refuse a tag that is not `v` plus the wheel's version. A write-scoped job that runs no repository code then waits for the commit's other push and pull-request checks, checks the wheel against the build's `SHA256SUMS` and publishes both. A published asset never changes: on an existing release it uploads only missing assets, and fails without uploading if one already there differs. |

**`perf` counts work, not time.** It runs each named workload in
`scripts/ci/perf_workloads.py` under callgrind and counts its
instructions minus those of a run that only starts up and imports what
the workload needs. Wall-clock speed is not gated: the dashboard repos'
speed comparator needs pytest's `--junitxml`, and pytest cannot run
these suites (the tests are plain functions taking helper arguments,
which pytest reads as fixture requests). Budgets live in
`.github/perf-budget.json`. A pull request fails when a workload
measures above budget × (1 + tolerance), when a budgeted workload is
missing or fails to measure, and when a measured workload has no budget.
Each push to `main` lowers a budget whose workload got cheaper by more
than the tolerance and commits it through `ratchet-push.yml`. A raise is
its own commit touching only the budget file, with a `Perf-Budget-Raise:
<workload>, ...` trailer (`*` for the tolerance or the Python version).

**Releasing = bumping `__version__`.** `tag` creates the tag, `release`
publishes from it. Nothing bumps the version automatically: deciding
patch-vs-minor is a judgement about what changed.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
run with a repository token. Dependabot keeps the hashes current.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. `build/`, `dist/` and `*.egg-info/` need no rules at all —
they are simply never named back. A new file of an unlisted type is
invisible to git and will NOT appear in `git status`;
`git check-ignore -v <path>` names the rule hiding it. Never "fix" it by
loosening the leading `*`.

**The comment-share and size records** are kept by their actions.
Per-file records are never raised; the `tests/` tree budget rises only in a
pull request carrying a `feat` commit. With the deploy key of the `main-push`
environment, each push to `main` tightens `.github/comment-share.json` and
`.github/size-baseline.json` and commits any change itself. The adoption pull
request seeded `.github/comment-share.json` by running the action's tighten
over its own head
(`COMMENT_RATCHET_MODE=tighten GITHUB_WORKSPACE="$PWD" python3 -I
<comment-ratchet checkout>/run.py`); the first push to `main` seeded the size
records.
