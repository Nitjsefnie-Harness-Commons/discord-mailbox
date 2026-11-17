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
import unicodedata

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


# One user-perceived character each; repeated with no whitespace, a body splits only between them.
CLUSTERS = {
    'combining mark': (0x65, 0x301),
    'spacing mark': (0x915, 0x93E),
    'zero-width-joiner sequence': (0x1F468, 0x200D, 0x1F469, 0x200D, 0x1F467),
    'variation selector': (0x2764, 0xFE0F),
    'emoji modifier': (0x1F44D, 0x1F3FD),
    'regional-indicator pair': (0x1F1E8, 0x1F1FF),
    'Hangul L L V V T T': (0x1100, 0x1100, 0x1161, 0x1161, 0x11A8, 0x11A8),
    'Hangul L LVT T': (0x1100, 0xAC01, 0x11A8),
    'Hangul LV V T': (0xAC00, 0x1161, 0x11A8),
    'Hangul L LV': (0x1100, 0xAC00),
    'Hangul LV T': (0xAC00, 0x11A8),
    'Indic conjunct': (0x915, 0x94D, 0x937),
    'prepended concatenation mark': (0x600, 0x61),
    'Thai sara am': (0xE01, 0xE33),
    'emoji tag sequence': (0x1F3F4, 0xE0067, 0xE0062, 0xE0065, 0xE006E, 0xE0067, 0xE007F),
}
if unicodedata.category(chr(0x11F02)) != 'Cn':   # assigned from Unicode 15 (Python 3.12); a Cn code point is no Prepend
    CLUSTERS['prepended Kawi sign'] = (0x11F02, 0x61)


def test_a_hard_split_lands_on_the_last_cluster_boundary_in_the_window(tmp):
    if MB is None:
        return
    for family, cps in CLUSTERS.items():
        unit = ''.join(map(chr, cps))
        for lead in range(1, len(unit) + 1):   # every offset of the unit against the limit
            body = 'x' * lead + unit * (6000 // len(unit))
            chunks = MB.chunk_body(body, 2000)
            assert ''.join(chunks) == body, (family, lead)
            ends = [sum(map(len, chunks[:k + 1])) for k in range(len(chunks) - 1)]
            assert all((e - lead) % len(unit) == 0 for e in ends), (family, lead, ends)
            assert all(2000 - len(unit) < len(c) <= 2000 for c in chunks[:-1]), (family, lead, ends)
    # A window that is one cluster splits at the limit: every chunk is non-empty.
    assert [len(c) for c in MB.chunk_body('x' + chr(0x301) * 5000, 2000)] == [2000, 2000, 1001]
    assert [len(c) for c in MB.chunk_body('x' + chr(13) + chr(10) + chr(0x301) * 3000, 2000)] == [1, 2000, 1002]
    assert [len(c) for c in MB.chunk_body(chr(9) * 2 + chr(0x301) * 3000, 2000)] == [2000, 1002]
    for ws in (chr(9), chr(0x3000), ' ', chr(10)):   # a window of whitespace is no chunk: the text after it survives and a blank body has none
        assert MB.chunk_body(ws * 5000, 2000) == [] and [c.strip() for c in MB.chunk_body(ws * 2001 + 'x', 2000)] == ['x'], ws
    for limit, body in ((0, ''), (0, 'abc'), (-3, 'abc')):   # a limit that holds no character is an error, never a hang
        try:
            MB.chunk_body(body, limit)
        except ValueError:
            continue
        raise AssertionError((body, limit))
    for lead in (0x600, 9, 0x200D):   # a first cut inside a cluster leaves the later cuts between flags
        chunks = MB.chunk_body(chr(lead) * 1899 + chr(0x1F1E8) * 6000, 1900)
        assert all(sum(map(len, chunks[:k])) % 2 for k in range(2, len(chunks))), (lead, [len(c) for c in chunks])


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="dmove_")


if __name__ == "__main__":
    sys.exit(main())
