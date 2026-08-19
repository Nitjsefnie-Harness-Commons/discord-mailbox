#!/usr/bin/env python3
"""discord_mb `move` — attribution rendering and frame splitting.

Discord has no move primitive, so `move` reposts each message as the bot and
deletes the original. That makes the rendered body the ONLY place the original
author and timestamp survive — if it silently drops either, the moved history
becomes an anonymous wall of bot messages and the originals are already gone.
These pin the rendering and the chunking that carries it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

try:
    MB = _util.load(os.path.join(_util.SCRIPTS, "discord_mb.py"))
except ImportError:
    MB = None  # discord.py missing — not applicable on this box


def test_body_carries_author_and_timestamp(tmp):
    if MB is None:
        return
    out = MB.moved_body('kotvas', '2026-08-04T12:30:14+00:00', 'hello there')
    assert out.startswith('**kotvas**'), out
    assert '2026-08-04 12:30:14' in out, out
    assert 'hello there' in out, out


def test_missing_pieces_do_not_crash_or_fabricate(tmp):
    if MB is None:
        return
    # no author -> a marker, never a blank attribution
    assert MB.moved_body(None, None, 'x').startswith('**?**')
    # empty content is legitimate (an attachment-only message)
    out = MB.moved_body('a', '2026-08-04T00:00:00', '')
    assert out.strip().endswith('00:00:00'), out
    assert MB.moved_body('a', None, None) == '**a**'


def test_unuploadable_attachments_survive_as_links(tmp):
    if MB is None:
        return
    out = MB.moved_body('a', '2026-08-04T00:00:00', 'see file',
                        ['https://cdn.example/x.zip'])
    assert '[attachment] https://cdn.example/x.zip' in out, out


def test_long_message_splits_and_keeps_attribution_on_frame_one(tmp):
    if MB is None:
        return
    frames = MB.moved_frames('a', '2026-08-04T00:00:00', 'word ' * 900)
    assert len(frames) > 1, len(frames)
    assert frames[0].startswith('**a**'), frames[0]
    assert all(len(f) <= MB.MAX_BODY for f in frames), [len(f) for f in frames]


def test_short_message_stays_one_frame(tmp):
    if MB is None:
        return
    assert len(MB.moved_frames('a', '2026-08-04T00:00:00', 'short')) == 1


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="dmove_")


if __name__ == "__main__":
    sys.exit(main())
