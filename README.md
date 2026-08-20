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
