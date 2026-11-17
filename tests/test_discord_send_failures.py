#!/usr/bin/env python3
"""A queued send that can never succeed is dropped with one `send_failed` event; a transient one stays queued, and a re-sent chunk keeps its nonce.

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
# keeps the file with no event, "sent" delivers without the reply reference, "referenced" with it.
CASES = {
    "reference-found": ("-", None, "referenced"),
    "channel-404": ("fetch_channel", _http(404), "dropped"), "channel-403": ("fetch_channel", _http(403), "dropped"),
    "user-404": ("fetch_user", _http(404), "dropped"), "chunk-400": ("send", _http(400), "dropped"),
    "chunk-403": ("send", _http(403), "dropped"), "channel-503": ("fetch_channel", _http(503), "queued"),
    "channel-network": ("fetch_channel", lambda: OSError("down"), "queued"), "user-503": ("fetch_user", _http(503), "queued"),
    "chunk-503": ("send", _http(503), "queued"), "chunk-network": ("send", lambda: OSError("down"), "queued"),
    "reference-404": ("fetch_message", _http(404), "sent"),
}


def test_a_send_that_can_never_succeed_is_dropped_and_a_transient_failure_keeps_the_file(tmp):
    box, log, channels, sent_with = Path(tmp) / "state" / "outbox", Path(tmp) / "connector.log", {}, {}
    ids = {n: 960000000000000001 + i for i, n in enumerate(CASES)}

    async def fetch_message(message_id):    # the reply target exists
        return types.SimpleNamespace(to_reference=lambda fail_if_not_exists: "ref")

    async def script(client):
        def case(key):
            return next(n for n, i in ids.items() if i == key)

        async def raise_for(name, call):
            if CASES[name][0] == call:
                raise CASES[name][1]()

        async def proxy(name, call, real, *args, **kwargs):
            await raise_for(name, call)
            if call == "send":
                sent_with[name] = kwargs
            return await real(*args, **kwargs)

        async def fetch_channel(channel_id):
            name = case(channel_id)
            await raise_for(name, "fetch_channel")
            channels[name] = channel = type(client.bridge)(channel_id, "c")
            channel.fetch_message = fetch_message
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
            waiting = [n for n, (_, _, o) in CASES.items() if (f"send retry ({n}.json)" not in text if o == "queued" else (box / f"{n}.json").exists())]
            if not waiting:
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out waiting on {waiting}: {text[-600:]}")

    client = _gateway.run(tmp, script, outbox=True)
    dropped, queued = ([n for n, (_, _, o) in CASES.items() if o == outcome] for outcome in ("dropped", "queued"))
    assert sorted(e["subject"] for e in client.events if e["event"] == "send_failed") == sorted(dropped)
    assert sorted(p.stem for p in box.iterdir()) == sorted(queued)
    assert len(channels["reference-404"].sent) == 1, channels["reference-404"].sent
    assert "reference" not in sent_with["reference-404"] and sent_with["reference-found"]["reference"] == "ref"


def test_a_resent_chunk_carries_the_nonce_of_its_first_attempt(tmp):
    box, calls = Path(tmp) / "state" / "outbox", []

    async def script(client):
        async def send(content, **kwargs):  # Discord accepted the first call, but its reply is lost
            calls.append((content.splitlines()[0].split("** ")[1], str(kwargs.get("nonce"))))
            if len(calls) == 1:
                raise asyncio.TimeoutError()
            return await real_send(content, **kwargs)

        real_send, client.bridge.send = client.bridge.send, send
        for name, body in (("a", "x" * 2500), ("b", "y")):    # a is a two-chunk train
            (box / f"{name}.json").write_text(json.dumps({"to": "nobody", "subject": name, "body": body}), encoding="utf-8")
        for _ in range(100):
            if len(calls) == 4 and not any(box.iterdir()):
                break
            await asyncio.sleep(0.1)
        await client.ask({f"m{i}": {"op": "send", "to": "nobody", "subject": f"m{i}", "body": "z"} for i in (1, 2)})

    _gateway.run(tmp, script, outbox=True)
    assert [label for label, _ in calls] == ["a", "b", "a", "a (2/2)", "m1", "m2"], calls
    nonces = [nonce for _, nonce in calls]
    assert nonces[0] == nonces[2], "the retry of a lost first chunk must reuse its nonce"
    assert len(set(nonces)) == 5 and all(len(n) <= 25 for n in nonces), nonces


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="send_failures_")


if __name__ == "__main__":
    raise SystemExit(main())
