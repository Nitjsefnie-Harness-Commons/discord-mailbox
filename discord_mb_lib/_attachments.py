"""`attachments download`: where each attachment is saved, or why it is skipped."""
import os
from pathlib import Path
from typing import Any


def _target(dest: Path, filename: str, targets: list[Path]) -> Path | str:
    name = filename.replace('\\', '/').rsplit('/', 1)[-1]
    if name in ('', '.', '..'):
        return f'file name reduces to {name!r}'
    if any(t.name == name for t in targets):
        return f'an earlier attachment on this message is saved as {name!r}'
    target = dest / name
    # both sides resolved, as macOS /var is a symlink; strictly inside, as `C:` on Windows names dest itself
    if dest.resolve() not in target.resolve().parents:
        return f'{name!r} does not resolve inside dest_dir'
    if target.is_dir():
        return f'{name!r} is a directory'
    if target.exists() and any(os.path.samefile(target, t) for t in targets):
        return f'{name!r} is the file an earlier attachment on this message is saved as'
    targets.append(target)
    return target


async def save_all(attachments: list[Any], dest: Path, rename: str | None) -> dict[str, list[dict[str, Any]]]:
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    targets: list[Path] = []
    for a in attachments:
        # the operator's own --rename is taken as given
        out = dest / rename if (rename and len(attachments) == 1) else _target(dest, a.filename, targets)
        if isinstance(out, str):
            skipped.append({'filename': a.filename, 'reason': out})
            continue
        await a.save(out)
        saved.append({'path': str(out), 'filename': a.filename, 'size': a.size, 'url': a.url})
    return {'saved': saved, 'skipped': skipped}
