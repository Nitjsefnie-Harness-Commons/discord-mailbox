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

## Development

```sh
python3 run_tests.py                                             # the suite
git ls-files -co --exclude-standard '*.py' | xargs pylint        # lint
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle   # lint
pyright                                                          # types
pip-audit -r requirements-dev.txt -r requirements-test.txt       # audit
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
| `audit` | `pip-audit` over both requirements files, resolving the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
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
named back. `build/`, `dist/` and `*.egg-info/` need no rules at all now —
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
<comment-ratchet checkout>/run.py`); the first push to `main` seeds the size
records.
