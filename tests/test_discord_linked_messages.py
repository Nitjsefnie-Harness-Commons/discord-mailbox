#!/usr/bin/env python3
"""Linked messages: at most five are fetched per inbound message.

Runs the REAL connector on the stand-in gateway, whose cache holds none of the channels.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import ALICE  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
CHANNEL = 960000000000000000     # each link adds its number
CAPPED = {"not_fetched": "over the 5-link cap"}


def test_a_message_resolves_five_distinct_links_and_only_lists_the_rest(tmp):
    # Repeats precede the fifth distinct link; the third link's channel refuses.
    order = [1, 2, 1, 3, 4, 5, 2, 6, 7, 7]
    body = " ".join(f"https://discord.com/channels/1/{CHANNEL + n}/{n}" for n in order)
    fetched, read = [], []

    async def script(client):
        async def fetch_channel(channel_id):
            fetched.append(channel_id)
            if channel_id == CHANNEL + 3:
                raise RuntimeError("Missing Access")
            channel = types.SimpleNamespace(id=channel_id)

            async def fetch_message(message_id):
                read.append(message_id)
                return client.message(message_id, ALICE, f"linked {message_id}", channel)
            channel.fetch_message = fetch_message
            return channel
        client.fetch_channel = fetch_channel
        await client.handlers["on_message"](client.message(101, ALICE, body, client.dm(970000000000000099)))

    _gateway.run(tmp, script)
    linked = _gateway.inbox(tmp, 101)["linked_messages"]
    url = "https://discord.com/channels/1/{}/{}".format
    assert fetched == [CHANNEL + n for n in (1, 2, 3, 4, 5)], fetched
    assert read == [1, 2, 4, 5], read
    assert [e["msg_id"] for e in linked] == ["1", "2", "3", "4", "5", "6", "7"], linked
    assert [e["url"] for e in linked] == [url(CHANNEL + n, n) for n in range(1, 8)], linked
    assert [("path" in e, "error" in e) for e in linked[:5]] == [(True, False)] * 2 + [(False, True)] + [
        (True, False)] * 2, linked
    assert linked[2] == {"msg_id": "3", "channel_id": str(CHANNEL + 3), "url": url(CHANNEL + 3, 3),
                         "error": "RuntimeError: Missing Access"}, linked[2]
    assert linked[5:] == [{"msg_id": str(n), "channel_id": str(CHANNEL + n), "url": url(CHANNEL + n, n), **CAPPED}
                          for n in (6, 7)], linked


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="linked_")


if __name__ == "__main__":
    raise SystemExit(main())
