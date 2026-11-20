#!/usr/bin/env python3
"""A reply to one of the bot's messages reaches the connector even with its reply ping switched off."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE, BOT  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)

OTHER_BOT = 940000000000000077


def _reply(client, msg_id, to_author, mentions=()):
    msg = client.message(msg_id, ALICE, f"reply {msg_id}", client.bridge, mentions=mentions)
    resolved = None if to_author is None else types.SimpleNamespace(author=types.SimpleNamespace(id=to_author))
    msg.reference = types.SimpleNamespace(resolved=resolved)
    return msg


def test_an_unpinged_reply_to_the_bot_is_delivered_and_other_replies_are_not(tmp):
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")

    async def script(client):
        on_message = client.handlers["on_message"]
        await on_message(_reply(client, 501, BOT))                    # reply to us, ping off
        await on_message(_reply(client, 502, BOT, mentions=(BOT,)))   # reply to us, ping on
        await on_message(_reply(client, 503, OTHER_BOT))              # reply to someone else
        await on_message(_reply(client, 504, None))                   # original deleted or not resolved
        await on_message(client.message(505, ALICE, "plain", client.bridge))

    client = _gateway.run(tmp, script)
    events = [int(e["msg_id"]) for e in client.events if e.get("event") == "message"]
    assert _gateway.inbox(tmp) == {501, 502} and events == [501, 502], (_gateway.inbox(tmp), events)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="replyroute_")


if __name__ == "__main__":
    raise SystemExit(main())
