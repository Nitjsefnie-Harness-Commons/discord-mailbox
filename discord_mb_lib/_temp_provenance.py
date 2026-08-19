"""Trustworthy ownership markers for bundle-created temporary state.

Only direct children of the platform temporary root may be marked.  Markers
are created with an exclusive, private file descriptor so a predictable
symlink or pre-existing user entry can never be adopted as bundle-owned.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _posix_mode_exposed(mode):
    """True when a POSIX mode grants group or other access.

    Always False on Windows, which has no POSIX permission bits: os.open with
    0o600 still reports st_mode 0o666 there, so a literal `S_IMODE & 0o077`
    test is unsatisfiable and every guard using it rejects the very file it
    just created. Confidentiality on Windows comes from the ACL work in
    _best_effort_private(), not from these bits.
    """
    if os.name == 'nt':
        return False
    return bool(stat.S_IMODE(mode) & 0o077)


OWNER_MARKER = ".agent-bundle-owner"
VALID_SCOPES = {"claude", "shared"}


def _marker_for(path: Path) -> Path:
    return (path / OWNER_MARKER if path.is_dir() and not path.is_symlink()
            else path.with_name(path.name + OWNER_MARKER))


def _linklike(path: Path) -> bool:
    """Recognize symlinks and Windows junction/reparse points."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def owned_temp_scope(path: str | os.PathLike[str]) -> str | None:
    """Return trusted ownership scope for one direct temporary child.

    On POSIX the entry and marker must both belong to the current user.  A
    directory namespace must also be private: accepting a predictable 0777
    mailbox root lets another local user control every file later written
    below it.  Windows has no useful ``geteuid``/mode contract, but link and
    regular-file checks still reject junction/reparse adoption.
    """
    candidate = Path(path)
    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        if candidate.parent.resolve(strict=True) != root:
            return None
        if _linklike(candidate):
            return None
        entry_info = candidate.lstat()
        if not (stat.S_ISREG(entry_info.st_mode)
                or stat.S_ISDIR(entry_info.st_mode)):
            return None
        marker = _marker_for(candidate)
        if _linklike(marker):
            return None
        marker_info = marker.lstat()
        if not stat.S_ISREG(marker_info.st_mode):
            return None
        if hasattr(os, "geteuid"):
            current_uid = os.geteuid()
            if (entry_info.st_uid != current_uid
                    or marker_info.st_uid != current_uid):
                return None
            if (stat.S_ISDIR(entry_info.st_mode)
                    and _posix_mode_exposed(entry_info.st_mode)):
                return None
            if _posix_mode_exposed(marker_info.st_mode):
                return None
        scope = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return scope if scope in VALID_SCOPES else None


def mark_created_temp(path: str | os.PathLike[str], scope: str) -> bool:
    """Mark one newly-created direct temp child without adopting collisions."""
    candidate = Path(path)
    if scope not in VALID_SCOPES:
        return False
    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        if candidate.parent.resolve(strict=True) != root:
            return False
        info = candidate.lstat()
        if _linklike(candidate):
            return False
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            return False
    except OSError:
        return False
    marker = _marker_for(candidate)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        # Never rewrite/adopt an existing marker.  It is acceptable only when
        # the complete entry+marker envelope is already exact and private.
        return owned_temp_scope(candidate) == scope
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(scope + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            marker.unlink()
        except OSError:
            pass
        return False
    return True


def ensure_owned_temp_dir(path: str | os.PathLike[str], scope: str) -> bool:
    """Create and mark a direct temp directory, without adopting old roots."""
    candidate = Path(path)
    try:
        candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError as exc:
        if owned_temp_scope(candidate) == scope:
            return True
        # The mailbox caller historically ignored a false return and then
        # wrote below the unsafe root anyway.  Refuse by exception so failure
        # cannot be accidentally converted into adoption.
        raise RuntimeError(
            f"unsafe pre-existing temporary directory: {candidate}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"could not create private temporary directory {candidate}: {exc}") from exc
    if not mark_created_temp(candidate, scope):
        try:
            candidate.rmdir()
        except OSError:
            pass
        raise RuntimeError(
            f"could not establish temporary-directory ownership: {candidate}")
    return True
