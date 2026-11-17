"""Bounded UTF-8 chunks of historical connector log bytes."""
import codecs


def bounded_byte_chunks(raw, limit):
    """Yield valid UTF-8 chunks, replacing malformed history if possible."""
    limit = int(limit)
    if limit < 1:
        raise ValueError('chunk limit must be positive')
    # Only history older than this writer gets here.
    text = bytes(raw).decode('utf-8', errors='replace')
    current = bytearray()
    for char in text:
        encoded = char.encode('utf-8')
        if len(encoded) > limit:
            if current:
                yield bytes(current)
            raise ValueError(
                f'max_bytes={limit} cannot hold UTF-8 code point U+{ord(char):04X}')
        if current and len(current) + len(encoded) > limit:
            yield bytes(current)
            current = bytearray()
        current.extend(encoded)
    if current:
        yield bytes(current)


def iter_historical_chunks(path, limit):
    """Yield bounded, valid-UTF-8 historical records from ``path``."""
    limit = int(limit)
    if limit < 1:
        raise ValueError('chunk limit must be positive')
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    current = bytearray()

    def consume(text):
        nonlocal current
        for char in text:
            encoded = char.encode('utf-8')
            if len(encoded) > limit:
                if current:
                    yield bytes(current)
                    current = bytearray()
                raise ValueError(
                    f'max_bytes={limit} cannot hold UTF-8 code point '
                    f'U+{ord(char):04X}')
            if current and len(current) + len(encoded) > limit:
                yield bytes(current)
                current = bytearray()
            current.extend(encoded)
            if char == '\n':
                yield bytes(current)
                current = bytearray()

    with open(path, 'rb') as source:
        while True:
            raw = source.read(64 * 1024)
            if not raw:
                break
            yield from consume(decoder.decode(raw, final=False))
        yield from consume(decoder.decode(b'', final=True))
    if current:
        yield bytes(current)
