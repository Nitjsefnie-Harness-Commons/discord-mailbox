#!/usr/bin/env python3
"""A queued send that can never succeed is dropped with one `send_failed` event; a transient one stays queued.

The REAL outbox watcher dispatches every case against a stand-in gateway that fails where the case says.
"""
import asyncio
import functools
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)


def _http(status, message=""):
    import discord
    kind = {403: discord.Forbidden, 404: discord.NotFound, 503: discord.DiscordServerError}.get(status, discord.HTTPException)
    return lambda: kind(types.SimpleNamespace(status=status, reason="r"), message)


# name -> (the call that fails, its error, outcome): "dropped" is one `send_failed` and no file, "queued"
# keeps the file with no event, "sent" delivers without the reply reference.
CASES = {
    "channel-404": ("fetch_channel", _http(404), "dropped"), "channel-403": ("fetch_channel", _http(403), "dropped"),
    "user-404": ("fetch_user", _http(404), "dropped"), "chunk-400": ("send", _http(400), "dropped"),
    "chunk-403": ("send", _http(403), "dropped"), "channel-503": ("fetch_channel", _http(503), "queued"),
    "channel-network": ("fetch_channel", lambda: OSError("down"), "queued"), "user-503": ("fetch_user", _http(503), "queued"),
    "chunk-503": ("send", _http(503), "queued"), "chunk-network": ("send", lambda: OSError("down"), "queued"),
    "reference-404": ("fetch_message", _http(404), "sent"),
}


def test_a_send_that_can_never_succeed_is_dropped_and_a_transient_failure_keeps_the_file(tmp):
    box, log, channels = Path(tmp) / "state" / "outbox", Path(tmp) / "connector.log", {}
    ids = {n: 960000000000000001 + i for i, n in enumerate(CASES)}

    async def script(client):
        def case(key):
            return next(n for n, i in ids.items() if i == key)

        async def raise_for(name, call):
            if CASES[name][0] == call:
                raise CASES[name][1]()

        async def proxy(name, call, real, *args, **kwargs):
            await raise_for(name, call)
            return await real(*args, **kwargs)

        async def fetch_channel(channel_id):
            name = case(channel_id)
            await raise_for(name, "fetch_channel")
            channels[name] = channel = type(client.bridge)(channel_id, "c")
            for call in ("send", "fetch_message"):
                setattr(channel, call, functools.partial(proxy, name, call, getattr(channel, call)))
            return channel

        async def fetch_user(user_id):
            await raise_for(case(user_id), "fetch_user")

        client.get_user, client.fetch_channel, client.fetch_user = lambda user_id: None, fetch_channel, fetch_user
        for name, (call, _, _) in CASES.items():
            to = {"dm": True, "to": str(ids[name])} if call == "fetch_user" else {"to": "nobody", "reply_to": "1", "reply_channel_id": str(ids[name])}
            (box / f"{name}.json").write_text(json.dumps({"subject": name, "body": "b", **to}), encoding="utf-8")
        for _ in range(100):            # the watcher ticks once a second
            text = log.read_text(encoding="utf-8")
            if all(f"({n}.json)" in text if o == "queued" else not (box / f"{n}.json").exists() for n, (_, _, o) in CASES.items()):
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out: {text[-600:]}")

    client = _gateway.run(tmp, script, outbox=True)
    dropped, queued = ([n for n, (_, _, o) in CASES.items() if o == outcome] for outcome in ("dropped", "queued"))
    assert sorted(e["subject"] for e in client.events if e["event"] == "send_failed") == sorted(dropped)
    assert sorted(p.stem for p in box.iterdir()) == sorted(queued)
    assert len(channels["reference-404"].sent) == 1, channels["reference-404"].sent


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="send_failures_")


if __name__ == "__main__":
    raise SystemExit(main())
