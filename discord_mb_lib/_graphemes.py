"""UAX #29 grapheme cluster boundaries from general category and tables (Unicode 14-17).

A pair with an unassigned code point is no boundary; the cut moves earlier.
"""
import unicodedata
from itertools import chain


def _cps(*spans: tuple[int, int]) -> frozenset[int]:
    return frozenset(chain.from_iterable(range(lo, hi + 1) for lo, hi in spans))


# Extend, ZWJ, SpacingMark not in Mn/Mc/Me.
_JOINERS = _cps((0x0E33, 0x0E33), (0x0EB3, 0x0EB3), (0x200C, 0x200D), (0xFF9E, 0xFF9F),
                (0x1F3FB, 0x1F3FF), (0xE0020, 0xE007F))
_PREPEND = _cps((0x0600, 0x0605), (0x06DD, 0x06DD), (0x070F, 0x070F), (0x0890, 0x0891),
                (0x08E2, 0x08E2), (0x0D4E, 0x0D4E), (0x110BD, 0x110BD), (0x110CD, 0x110CD),
                (0x111C2, 0x111C3), (0x113D1, 0x113D1), (0x1193F, 0x1193F), (0x11941, 0x11941),
                (0x11A3A, 0x11A3A), (0x11A84, 0x11A89), (0x11D46, 0x11D46), (0x11F02, 0x11F02))
_JAMO = ((0x1100, 0x115F, 'L'), (0xA960, 0xA97C, 'L'), (0x1160, 0x11A7, 'V'), (0xD7B0, 0xD7C6, 'V'),
         (0x16D63, 0x16D63, 'V'), (0x16D67, 0x16D6A, 'V'), (0x11A8, 0x11FF, 'T'), (0xD7CB, 0xD7FB, 'T'))
_JAMO_JOINS = {('L', 'L'), ('L', 'V'), ('L', 'LV'), ('L', 'LVT'), ('LV', 'V'), ('LV', 'T'),
               ('V', 'V'), ('V', 'T'), ('LVT', 'T'), ('T', 'T')}
_RIS = ''.join(map(chr, range(0x1F1E6, 0x1F200)))


def _joins(c: str) -> bool:
    return ord(c) in _JOINERS or unicodedata.category(c) in ('Mn', 'Mc', 'Me', 'Cn')


def _jamo(c: str) -> str:
    o = ord(c)
    if 0xAC00 <= o <= 0xD7A3:
        return 'LVT' if (o - 0xAC00) % 28 else 'LV'
    return next((kind for lo, hi, kind in _JAMO if lo <= o <= hi), '')


def _boundary(s: str, i: int) -> bool:
    a, b = s[i - 1], s[i]
    if a == '\r' and b == '\n':
        return False
    if _joins(b) or ord(a) == 0x200D or ord(a) in _PREPEND or unicodedata.category(a) == 'Cn':
        return False
    if (_jamo(a), _jamo(b)) in _JAMO_JOINS:
        return False
    if a in _RIS and b in _RIS:
        return (i - len(s[:i].rstrip(_RIS))) % 2 == 0
    j = i - 1
    if unicodedata.category(b) == 'Lo':   # GB9c: a virama joins conjuncts
        while j >= 0 and _joins(s[j]):
            if unicodedata.combining(s[j]) == 9 or unicodedata.category(s[j]) == 'Cn':
                return False
            j -= 1
    return True


def hard_cut(text: str, start: int, limit: int) -> int:
    """Last boundary of text in start+1..start+limit past leading space, as an offset from start, else limit."""
    first = limit - len(text[start:start + limit].lstrip())
    return next((i for i in range(limit, first, -1) if _boundary(text, start + i)), limit)
