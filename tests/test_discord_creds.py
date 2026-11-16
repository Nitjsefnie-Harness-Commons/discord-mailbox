#!/usr/bin/env python3
"""Credentials: every entry in the channel, however old.

The connector half runs the REAL creds-list / creds-get / creds-add closures on
the stand-in gateway (`tests/_gateway.py`), whose channel history honours `limit`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from _gateway import BOT  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)


def _entry(client, msg_id, author, domain="a.example"):
    """A template post; the password is built from the id, never a literal."""
    return client.message(msg_id, author, f"```\n{domain}\n```\n```\nlogin\n```\n```\npw-{msg_id}\n```",
                          client.credentials)


def test_get_list_and_add_see_an_entry_older_than_five_hundred_messages(tmp):
    async def script(client):
        chatter = [client.message(1000 + i, BOT, f"chatter {i}", client.credentials) for i in range(600)]
        client.credentials.msgs[:] = [*chatter, _entry(client, 1, BOT)]      # newest first
        await client.ask({"list": {"op": "creds-list"}, "get": {"op": "creds-get", "domain": "a.example"},
                          "add": {"op": "creds-add", "domain": "a.example", "login": "login", "password": "x"}})

    client = _gateway.run(tmp, script)
    if client is None:
        return
    r = client.responses
    assert [e["msg_id"] for e in r["list"]["entries"]] == ["1"], r["list"]
    assert [e["msg_id"] for e in r["get"]["entries"]] == ["1"], r["get"]
    assert (r["add"]["action"], r["add"]["existing_msg_ids"]) == ("skipped", ["1"]), r["add"]
    assert client.credentials.sent == [], client.credentials.sent


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="creds_")


if __name__ == "__main__":
    raise SystemExit(main())
