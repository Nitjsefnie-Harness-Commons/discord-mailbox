#!/usr/bin/env python3
"""discord_mb.continuation_chunk_for — routing for chunk-2+ frames (issue #10).

A chunked mailbox send mentions the recipient only on chunk 1 (the one-ping
property), so the mention-based routing gate produced no inbox record for
chunks 2+ — the recipient saw a message ending mid-thought. These pin the
frame-based detection that lets continuation chunks through: shape, chunk
index >= 2, and recipient match.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

try:
    MB = _util.load(os.path.join(_util.SCRIPTS, "discord_mb.py"))
except ImportError:
    MB = None  # discord.py missing — not applicable on this box

IDENT = 'agent_dev_kimi'


def _check(content, ident=IDENT):
    return MB.continuation_chunk_for(content, ident)


def test_continuation_to_us_matches(tmp):
    if MB is None:
        return
    assert _check('**[agent_server→agent_dev_kimi]** patched calamine (2/2)\nrest')
    assert _check('**[a→agent_dev_kimi]** subj (3/3)\nx')
    assert _check('**[a→all]** broadcast (2/3)\nx')
    assert _check('**[a→other, agent_dev_kimi]** subj (2/2)\nx')


def test_chunk_one_and_nonframes_do_not_match(tmp):
    if MB is None:
        return
    # chunk 1 routes by its mention, not here
    assert not _check('**[a→agent_dev_kimi]** subj\nfirst chunk body')
    assert not _check('**[a→agent_dev_kimi]** subj (1/2)\nx')
    assert not _check('plain human message with (2/3) in it')
    assert not _check('')


def test_wrong_recipient_and_malformed_do_not_match(tmp):
    if MB is None:
        return
    assert not _check('**[a→someone_else]** subj (2/2)\nx')
    assert not _check('**[a→nobody]** note (2/2)\nx')
    assert not _check('**[a→agent_dev_kimi]** subj (3/2)\nx')      # k > n
    assert not _check('**[a→agent_dev_kimi]** subj (2/3) tail\nx')  # suffix not at line end


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="chunkroute_")


if __name__ == "__main__":
    raise SystemExit(main())
