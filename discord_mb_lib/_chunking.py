from . import _graphemes


def chunk_body(body, limit):
    """Cut at a paragraph, line or space past limit//4, else grapheme, else limit; fits: whole."""
    if limit < 1:
        raise ValueError('chunk limit must be positive')
    if len(body) <= limit:
        return [body]
    parts, rest = [], body
    while len(rest) > limit:
        window = rest[:limit + 1]
        for sep in ('\n\n', '\n', ' '):
            cut = window.rfind(sep)
            if cut > limit // 4:
                sep_len = len(sep)
                break
        else:
            cut, sep_len = _graphemes.hard_cut(body, len(body) - len(rest), limit), 0
        if piece := rest[:cut].rstrip():
            parts.append(piece)
        rest = rest[cut + sep_len:]
    if rest.strip():
        parts.append(rest)
    return parts
