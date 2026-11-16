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
identity on the host, and prints `{"user_id": "<id>", "removed": <n>}` with the
number of files removed. It needs no identity and no running connector. An id
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

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
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
coverage for a program nobody executes. Gated at **48%**, a ratchet
under the current 50.3%, not a target. Raise it as coverage climbs;
never lower it to turn a build green.

### CI

| Workflow | What it does |
| --- | --- |
| `tests` | `run_tests.py` across 3 OSes × every supported Python, plus a single-run coverage job — the matrix would otherwise report the same coverage number once per leg. |
| `lint` / `types` | pylint + pycodestyle, and pyright. |
| `codeql` | Security analysis (Python only — no JS here). Findings go to the Security tab, never the build. Weekly cron on top of push, because a query published today would otherwise only ever run against files touched after it shipped. |
| `audit` | `pip-audit` over every hash-pinned manifest, each of which pins the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
| `actionlint` | `actionlint` + `zizmor` over the workflow YAML. A broken workflow does not go red, it silently stops running. |
| `open pull request` | Called by each workflow above. On a push to the head of an open pull request that GitHub reports mergeable, their jobs skip: that pull request's run gates the same commit. A conflicted pull request gets no run of its own, so its pushes run the gates. |
| `pr gate` | `Nitjsefnie-Actions/pr-gate`: the pull-request description against `.github/PULL_REQUEST_TEMPLATE.md`, every issue it closes assigned to its author, Conventional Commits subjects, and every commit's author and committer attributable to the pull-request author. |
| `claim` | `Nitjsefnie-Actions/claim`: `/claim`, `/unclaim` and `/release` on an issue assign and unassign the commenter. |
| `pin test screen` | `Nitjsefnie-Actions/pin-guard`: comments on test lines that assert a pinned dependency version, tag, SHA or digest. |
| `comment share` | `Nitjsefnie-Actions/comment-ratchet`: a pull request may not raise a tracked file's comment share above 15%, or above its record in `.github/comment-share.json`. |
| `size baseline` | `Nitjsefnie-Actions/size-ratchet`: per-file line ceilings and the `tests/` line total, recorded in `.github/size-baseline.json`. |
| `tag` | Watches `discord_mb_lib/__init__.py`. When `__version__` changes on `main`, it waits for every other check on that commit and then pushes `v<version>`. |
| `release` | Fires on that tag: runs every suite, builds the wheel, and publishes `SHA256SUMS` beside it. |

**There is deliberately no speed gate here**, unlike the dashboard repos.
pytest cannot run these suites — the tests are plain functions taking
helper arguments, which pytest reads as fixture requests — and the
comparator needs `--junitxml`, which only pytest emits. Rather than
reshape the tests to suit a gate, the gate is omitted.

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
