"""Board windows: scoped ones only when no top-level one renders; names fit 100 UTF-16 units."""
from typing import Any

NAME_LIMIT = 100


def _has_pct(window: Any) -> bool:
    return isinstance(window, dict) and window.get('pct') is not None


def windows(block: dict, top_level: tuple) -> list:
    pairs = [(label, block[key]) for key, label in top_level if _has_pct(block.get(key))]
    scoped = block.get('_scoped')
    if not pairs and isinstance(scoped, list):
        pairs = [(w['label'].strip(), w) for w in scoped
                 if _has_pct(w) and isinstance(w.get('label'), str) and w['label'].strip()]
    return pairs


def name(provider: str, segments: list) -> str | None:
    out = None
    for seg in segments:
        cand = f'{out or provider} · {seg}'
        if len(cand.encode('utf-16-le')) // 2 > NAME_LIMIT:
            break
        out = cand
    return out
