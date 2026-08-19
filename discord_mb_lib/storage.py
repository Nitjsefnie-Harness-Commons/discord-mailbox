"""Hardened connector ownership, logs, locks, and event streams."""

from .core import *


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


PORTABLE_PUBLICATION_ENV = 'DISCORD_MB_PORTABLE_PUBLICATION'


def _anonymous_publication_available():
    """True when a crash before publication can be made invisible.

    Linux hides an unfinished publication in an O_TMPFILE inode that has no
    pathname until it is complete, so no recovery ever sees a torn one. macOS
    has no O_TMPFILE and Windows has no equivalent, so both take the portable
    named path, where every intermediate state is a real directory entry that
    a crash leaves behind for the next start to reason about.

    Setting DISCORD_MB_PORTABLE_PUBLICATION=1 takes the portable path on a
    platform that does not need it. That is how CI exercises the macOS and
    Windows publication protocol on a Linux runner; nothing in normal
    operation sets it.
    """
    if os.environ.get(PORTABLE_PUBLICATION_ENV) == '1':
        return False
    return getattr(os, 'O_TMPFILE', None) is not None and os.name != 'nt'


# --- Connector storage ---
# Kept local to avoid import cost for connectorless commands (send, leech, …).

class _ConnectorOwnershipError(RuntimeError):
    """The connector identity or log path is already owned by another process."""


class _TornNamedClaimRecord(FileExistsError):
    """A fixed claim entry whose bytes are not a complete record at all.

    Distinct from a complete record that fails a check.  A record that parses
    but carries no durable authenticator is a deliberate artifact and is
    preserved; bytes that do not parse are a write that stopped partway, which
    only the portable publication path can leave behind at a fixed name.
    """


class _ConnectorOwnership:
    """Hold stable-path and file-identity interlocks for one log owner.

    The active log is deliberately never used as a lock file.  Windows CRT
    byte-range locks are mandatory enough to reject the second handle used by
    migration and rotation, and making an empty active file lockable writes a
    leading NUL.  A retained sidecar protects each stable pathname, while a
    second retained sidecar in a stable per-user namespace is keyed by the
    file identity.  The latter makes hard-link aliases contend without
    touching the active inode.

    Acquisition is always path interlocks first (lexicographically ordered),
    then the identity interlock, followed by path/identity revalidation.  This
    ordering prevents an alias from unlinking or replacing a pathname while a
    writer is migrating it and avoids lock-order cycles between aliases.
    """

    _IDENTITY_LOCK_ROOT = Path(
        os.environ.get(_TEST_LOCK_ROOT_ENV, _DEFAULT_CONNECTOR_LOCK_ROOT))

    def __init__(self, target, lock_inode=False, stable_paths=None,
                 lock_root=None):
        requested = Path(os.path.abspath(os.fspath(target)))
        self.requested = requested
        self.target = Path(os.path.realpath(str(requested)))
        self._lock_inode = bool(lock_inode)
        self._stable_paths = tuple(stable_paths or ())
        self.lock_root = Path(lock_root or self._IDENTITY_LOCK_ROOT)
        self._windows = sys.platform == 'win32'
        self._path_handles = []
        self._path_lock_paths = []
        self._identity_handle = None
        self._identity_path = None
        self.identity = None
        self.identity_token = None
        self._stable_identities = {}
        self.path = self.target.parent / f'.{self.target.name}.lock'
        self._acquire()
        if not self._windows:
            register_at_fork = getattr(os, 'register_at_fork', None)
            if register_at_fork is not None:
                # A forked child gets duplicate descriptors.  It must close
                # those descriptors without issuing LOCK_UN: unlocking in the
                # child would release the parent's shared flock as well.
                register_at_fork(after_in_child=self._after_fork_child)

    @staticmethod
    def _identity_for(path):
        try:
            stat_result = os.stat(path, follow_symlinks=True)
        except (OSError, TypeError):
            return None
        device = getattr(stat_result, 'st_dev', None)
        inode = getattr(stat_result, 'st_ino', None)
        if device is None or inode in (None, 0):
            return None
        return (str(device), str(inode))

    @staticmethod
    def _entry_identity(path):
        """Return the directory-entry identity without following links."""
        try:
            stat_result = os.lstat(path)
        except (OSError, TypeError):
            return None
        device = getattr(stat_result, 'st_dev', None)
        inode = getattr(stat_result, 'st_ino', None)
        if device is None or inode in (None, 0):
            return None
        return (str(device), str(inode))

    @staticmethod
    def _sidecar_alias_key(lock_path):
        """Pathname key that collapses OS-level aliases of one sidecar."""
        return os.path.normcase(os.path.realpath(str(lock_path)))

    def _sidecar_keys(self, lock_path, handle):
        """Names under which this attempt already holds one sidecar."""
        keys = {self._sidecar_alias_key(lock_path)}
        try:
            opened = os.fstat(handle.fileno())
        except (OSError, ValueError):
            return keys
        device = getattr(opened, 'st_dev', None)
        inode = getattr(opened, 'st_ino', None)
        if device is not None and inode not in (None, 0):
            keys.add(('id', str(device), str(inode)))
        return keys

    def _sidecar_already_held(self, lock_path, held):
        """True when this attempt already locked the file this name reaches.

        Two candidate pathnames can name a single sidecar: a Windows 8.3 short
        name beside the long name its realpath produces, or a symlinked parent
        directory on POSIX.  Byte-range locks are per file and mandatory for a
        second handle, so locking the alias contends with the lock this very
        acquisition already holds, and the caller reports its own lock as held
        by someone else (issue #217).  One lock already covers every name that
        reaches the file, so the alias is skipped rather than relocked.
        """
        if self._sidecar_alias_key(lock_path) in held:
            return True
        identity = self._entry_identity(lock_path)
        return identity is not None and ('id',) + tuple(identity) in held

    @classmethod
    def _ensure_secure_lock_root(cls, root):
        """Create/adopt only a private, same-user regular directory.

        The path is predictable by design, so plain ``mkdir(..., exist_ok=True)``
        would be an unsafe adoption primitive: a symlink/reparse point,
        foreign-owned directory, or permissive directory would redirect every
        lock and journal operation.  Validate the identity after creation and
        on every use.  The default parent is the user's home directory; the
        test seam supplies an already-created private parent.
        """
        root = Path(root)
        try:
            try:
                info_before = root.lstat()
            except FileNotFoundError:
                root.mkdir(mode=0o700, parents=False, exist_ok=False)
                info_before = root.lstat()
            if _linklike(root) or not stat.S_ISDIR(info_before.st_mode):
                raise _ConnectorOwnershipError(
                    f'connector lock root is not a private directory: {root}')
            if hasattr(os, 'geteuid'):
                if info_before.st_uid != os.geteuid():
                    raise _ConnectorOwnershipError(
                        f'connector lock root is not owned by this user: {root}')
                if _posix_mode_exposed(info_before.st_mode):
                    raise _ConnectorOwnershipError(
                        f'connector lock root is too permissive: {root}')
            info_after = root.lstat()
            before_identity = (getattr(info_before, 'st_dev', None),
                               getattr(info_before, 'st_ino', None))
            after_identity = (getattr(info_after, 'st_dev', None),
                              getattr(info_after, 'st_ino', None))
            if before_identity != after_identity or _linklike(root):
                raise _ConnectorOwnershipError(
                    f'connector lock root changed while validating: {root}')
        except _ConnectorOwnershipError:
            raise
        except FileExistsError:
            # Another creator won the race; the recursive validation above is
            # still required before the directory can be trusted.
            return cls._ensure_secure_lock_root(root)
        except OSError as exc:
            raise _ConnectorOwnershipError(
                f'cannot establish connector lock root {root}: {exc}') from exc
        return root

    @classmethod
    def _identity_lock_path(cls, identity, root=None):
        root = cls._ensure_secure_lock_root(root or cls._IDENTITY_LOCK_ROOT)
        token = ':'.join(identity)
        digest = hashlib.sha256(token.encode('ascii', 'strict')).hexdigest()
        return root / f'{digest}.lock'

    def _path_candidates(self):
        candidates = {self.requested, self.target}
        candidates.update(Path(os.path.abspath(os.fspath(path)))
                          for path in self._stable_paths)
        return tuple(sorted(candidates, key=self._path_sort_key))

    def _path_sort_key(self, path):
        path = str(path)
        return path.casefold() if self._windows else path

    def _lock_sidecar(self, lock_path):
        lock_path = Path(lock_path)
        if _linklike(lock_path):
            raise _ConnectorOwnershipError(
                f'ownership lock is link-like: {lock_path}')
        try:
            before = lock_path.lstat()
        except FileNotFoundError:
            before = None
        except OSError as exc:
            raise _ConnectorOwnershipError(
                f'cannot inspect ownership lock {lock_path}: {exc}') from exc
        if before is not None:
            if (_linklike(lock_path) or not stat.S_ISREG(before.st_mode) or
                    getattr(before, 'st_nlink', 1) != 1):
                raise _ConnectorOwnershipError(
                    f'ownership lock has an unsafe directory entry: {lock_path}')
            expected_identity = (
                str(getattr(before, 'st_dev', None)),
                str(getattr(before, 'st_ino', None)),
            )
        else:
            expected_identity = None

        flags = (os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        fd = None
        try:
            try:
                fd = os.open(str(lock_path), flags | os.O_CREAT | os.O_EXCL,
                             0o600)
            except FileExistsError as exc:
                # A path that was absent during the preflight must not be
                # adopted after a racing creator wins the O_EXCL attempt.
                if before is None:
                    raise _ConnectorOwnershipError(
                        f'ownership lock appeared during creation: {lock_path}') from exc
                fd = os.open(str(lock_path), flags)

            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            if (not stat.S_ISREG(opened.st_mode) or
                    getattr(opened, 'st_nlink', 1) != 1 or
                    (expected_identity is not None and
                     opened_identity != expected_identity)):
                raise _ConnectorOwnershipError(
                    f'ownership lock identity changed: {lock_path}')
            if hasattr(os, 'geteuid') and opened.st_uid != os.geteuid():
                raise _ConnectorOwnershipError(
                    f'ownership lock has foreign owner: {lock_path}')
            if (_linklike(lock_path) or
                    self._entry_identity(lock_path) != opened_identity):
                raise _ConnectorOwnershipError(
                    f'ownership lock directory entry changed: {lock_path}')

            fh = os.fdopen(fd, 'r+b', buffering=0)
            fd = None
            locked = False

            def revalidate():
                current = os.fstat(fh.fileno())
                current_identity = (
                    str(getattr(current, 'st_dev', None)),
                    str(getattr(current, 'st_ino', None)),
                )
                if (not stat.S_ISREG(current.st_mode) or
                        getattr(current, 'st_nlink', 1) != 1 or
                        current_identity != opened_identity or
                        _linklike(lock_path) or
                        self._entry_identity(lock_path) != opened_identity):
                    raise _ConnectorOwnershipError(
                        f'ownership lock changed while acquiring: {lock_path}')

            # The directory entry is checked immediately before the only
            # write and before taking the OS lock, closing the symlink/reparse
            # and replacement windows on both POSIX and Windows.
            revalidate()
            if self._windows:
                import msvcrt
                # msvcrt.locking() locks bytes from the current file pointer;
                # only sidecars are materialized, never the active log.
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    os.write(fh.fileno(), b'\0')
                    os.fsync(fh.fileno())
                fh.seek(0)
                revalidate()
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            else:
                import fcntl
                revalidate()
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            revalidate()
            return fh
        except _ConnectorOwnershipError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            else:
                try:
                    if 'fh' in locals():
                        if locals().get('locked', False):
                            self._release_handle(fh)
                        else:
                            fh.close()
                except BaseException:
                    pass
            raise
        except (ImportError, OSError, AssertionError) as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            else:
                try:
                    if 'fh' in locals():
                        if locals().get('locked', False):
                            self._release_handle(fh)
                        else:
                            fh.close()
                except BaseException:
                    pass
            raise _ConnectorOwnershipError(
                f'ownership lock is held for {lock_path}') from exc

    def _release_handle(self, fh):
        error = None
        try:
            if self._windows:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:
            # Closing the descriptor still releases either OS lock.
            error = exc
        finally:
            try:
                fh.close()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def _release_acquired(self):
        error = None
        if self._identity_handle is not None:
            fh, self._identity_handle = self._identity_handle, None
            try:
                self._release_handle(fh)
            except BaseException as exc:
                error = exc
        try:
            self._release_path_acquired()
        except BaseException as exc:
            if error is None:
                error = exc
        if error is not None:
            raise error

    def _release_path_acquired(self):
        handles, self._path_handles = self._path_handles, []
        self._path_lock_paths = []
        error = None
        for fh in reversed(handles):
            try:
                self._release_handle(fh)
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def _probe_identity(self):
        """Stat without locking/opening the active inode for writes."""
        try:
            with open(self.target, 'a+b') as probe:
                stat_result = os.fstat(probe.fileno())
                device = getattr(stat_result, 'st_dev', None)
                inode = getattr(stat_result, 'st_ino', None)
                if device is None or inode in (None, 0):
                    return None
                return (str(device), str(inode))
        except OSError as exc:
            raise _ConnectorOwnershipError(
                f'cannot inspect connector log {self.target}: {exc}') from exc

    def _acquire(self):
        for _attempt in range(4):
            self.target = Path(os.path.realpath(str(self.requested)))
            self.path = self.target.parent / f'.{self.target.name}.lock'
            try:
                if self._lock_inode:
                    # Validate the global identity namespace before touching
                    # the active path or its sidecar.  An unsafe predictable
                    # root must not cause collateral log-directory writes.
                    self._ensure_secure_lock_root(self.lock_root)
                self._path_handles = []
                self._path_lock_paths = []
                held_sidecars = set()
                for path in self._path_candidates():
                    lock_path = path.parent / f'.{path.name}.lock'
                    if self._sidecar_already_held(lock_path, held_sidecars):
                        continue
                    handle = self._lock_sidecar(lock_path)
                    self._path_handles.append(handle)
                    self._path_lock_paths.append(lock_path)
                    held_sidecars.update(
                        self._sidecar_keys(lock_path, handle))
                # Re-resolve after path interlocks.  A symlink replacement or
                # a pathname replacement during setup is never silently used.
                resolved = Path(os.path.realpath(str(self.requested)))
                if resolved != self.target:
                    self._release_acquired()
                    continue
                if not self._lock_inode:
                    self._snapshot_stable_identities()
                    return

                identity = self._probe_identity()
                if identity is None:
                    # Stable path interlocks still provide safe ownership on
                    # filesystems that do not expose a usable file index.
                    self._snapshot_stable_identities()
                    return
                identity_path = self._identity_lock_path(identity, self.lock_root)
                self._ensure_secure_lock_root(self.lock_root)
                self._identity_handle = self._lock_sidecar(identity_path)
                current = Path(os.path.realpath(str(self.requested)))
                current_identity = self._identity_for(current)
                if current != self.target or current_identity != identity:
                    self._release_acquired()
                    continue
                self.identity = identity
                self.identity_token = ':'.join(identity)
                self._identity_path = identity_path
                self._snapshot_stable_identities()
                return
            except BaseException:
                try:
                    self._release_acquired()
                except BaseException:
                    pass
                raise
        raise _ConnectorOwnershipError(
            f'connector log path changed while acquiring ownership: {self.requested}')

    def _snapshot_stable_identities(self):
        """Remember every locked pathname so external replacement is visible."""
        self._stable_identities = {
            path: self._identity_for(path) for path in self._path_candidates()
        }

    def _after_fork_child(self):
        handles, self._path_handles = self._path_handles, []
        self._path_lock_paths = []
        if self._identity_handle is not None:
            handles.append(self._identity_handle)
            self._identity_handle = None
        for fh in handles:
            try:
                # Closing without LOCK_UN leaves the parent's interlock held.
                fh.close()
            except BaseException:
                pass

    def manifest_stable_paths(self):
        """Return the stable names and identities held by this owner."""
        self.revalidate()
        records = []
        for path in self._path_candidates():
            identity = self._identity_for(path)
            records.append({'path': str(path),
                            'identity': list(identity) if identity else None})
        return records

    def _normalize_stable_records(self, records):
        if not isinstance(records, list) or not records:
            raise _ConnectorOwnershipError(
                'connector log journal has no stable ownership paths')
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                raise _ConnectorOwnershipError(
                    'connector log journal has an invalid stable ownership path')
            raw_path = record.get('path')
            raw_identity = record.get('identity')
            if (not isinstance(raw_path, str) or not os.path.isabs(raw_path) or
                    (raw_identity is not None and
                     (not isinstance(raw_identity, (list, tuple)) or
                      len(raw_identity) != 2))):
                raise _ConnectorOwnershipError(
                    'connector log journal has an invalid stable ownership path')
            normalized.append((Path(os.path.abspath(raw_path)),
                               (None if raw_identity is None else
                                tuple(str(value) for value in raw_identity))))
        normalized.sort(key=lambda item: self._path_sort_key(item[0]))
        return normalized

    def reacquire_stable_paths(self, records, *,
                               revalidate_identities=True):
        """Reacquire current and journal namespaces in one sorted pass.

        Recovery can be restarted through a hard-link filename different from
        the one that created the journal.  The current owner already holds the
        restart filename's interlocks, while the journal names may sort before
        them.  Holding those sets in separate acquisitions both violates the
        global order and leaves a race while recovery is acquiring the second
        set.  Keep the identity interlock held, release only path handles, and
        reacquire the union as one globally sorted set.  The pre-existing
        identities of the live current namespace are checked after the
        handoff so another owner cannot replace it during the small gap.
        """
        normalized = self._normalize_stable_records(records)

        # ``revalidate`` verifies that the namespace was stable while this
        # owner held its original path handles.  Save those identities as the
        # live namespace contract across the path-lock handoff.
        self.revalidate()
        current_paths = self._path_candidates()
        current_identities = {
            path: self._identity_for(path) for path in current_paths
        }

        # Journal records describe the names before a publish.  A journal
        # pathname may also be one of the current names, so retain the live
        # current identity for that overlap and use the journal identity only
        # for journal-only names.
        journal_identities = {}
        for path, expected in normalized:
            journal_identities.setdefault(path, expected)
        all_paths = set(current_paths)
        all_paths.update(journal_identities)
        ordered_paths = tuple(sorted(all_paths, key=self._path_sort_key))
        ordered_lock_paths = tuple(
            path.parent / f'.{path.name}.lock' for path in ordered_paths)

        # The identity handle is intentionally retained throughout this
        # release/reacquire window.  A hard-link alias therefore cannot enter
        # and mutate the active inode while this owner changes path coverage.
        self._release_path_acquired()
        handles = []
        try:
            held_sidecars = set()
            locked_lock_paths = []
            for lock_path in ordered_lock_paths:
                if self._sidecar_already_held(lock_path, held_sidecars):
                    continue
                handle = self._lock_sidecar(lock_path)
                handles.append(handle)
                locked_lock_paths.append(lock_path)
                held_sidecars.update(self._sidecar_keys(lock_path, handle))

            self._path_handles = handles
            self._path_lock_paths = locked_lock_paths
            handles = []

            resolved = Path(os.path.realpath(str(self.requested)))
            if resolved != self.target:
                raise _ConnectorOwnershipError(
                    f'connector log path changed while recovering: {self.requested}')
            if self._lock_inode and self.identity is not None:
                if self._identity_for(self.target) != self.identity:
                    raise _ConnectorOwnershipError(
                        f'connector log identity changed while recovering: {self.target}')

            # Current names are live namespace exclusions, regardless of
            # whether a journal also records the same name.  Journal-only
            # names may legitimately have changed as a generation publishes.
            for path, expected in current_identities.items():
                if self._identity_for(path) != expected:
                    raise _ConnectorOwnershipError(
                        f'connector log namespace path changed while recovering: {path}')
            if revalidate_identities:
                for path, expected in journal_identities.items():
                    if path in current_identities:
                        continue
                    if expected is not None and self._identity_for(path) != expected:
                        raise _ConnectorOwnershipError(
                            f'connector log stable path changed while recovering: {path}')
        except BaseException:
            if handles:
                cleanup = handles
            else:
                cleanup = self._path_handles
                self._path_handles = []
                self._path_lock_paths = []
            for handle in reversed(cleanup):
                try:
                    self._release_handle(handle)
                except BaseException:
                    pass
            raise

        # Keep the expanded namespace in subsequent revalidation and in any
        # future journal manifest.  The handles above remain held for the
        # writer's lifetime, so live namespace exclusion covers both aliases.
        self._stable_paths = tuple(
            path for path in ordered_paths
            if path not in (self.requested, self.target))

    def refresh_stable_identities(self):
        """Refresh namespace identities after this owner publishes new slots."""
        self._snapshot_stable_identities()

    def revalidate(self):
        """Reject a pathname/inode replacement after ownership was acquired."""
        resolved = Path(os.path.realpath(str(self.requested)))
        if resolved != self.target:
            raise _ConnectorOwnershipError(
                f'connector log path was replaced while owned: {self.requested}')
        if self._lock_inode and self.identity is not None:
            if self._identity_for(self.target) != self.identity:
                raise _ConnectorOwnershipError(
                    f'connector log identity was replaced while owned: {self.target}')
        for path, expected in self._stable_identities.items():
            current = self._identity_for(path)
            if current != expected:
                raise _ConnectorOwnershipError(
                    f'connector log namespace path was replaced while owned: {path}')

    def close(self):
        self._release_acquired()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class _ConnectorLogWriter:
    """Line-buffered UTF-8 writer with a strict byte-sized rotation window.

    A logical line is encoded first, then written with one trailing newline.
    Lines that do not fit in one record are split at Unicode code-point
    boundaries into physical lines, each no larger than ``max_bytes``. Existing
    files are migrated by retaining the newest ``backup_count + 1`` UTF-8-safe
    byte chunks, with the newest chunk becoming the active file. This makes
    every active and retained backup file obey the ceiling before the first
    new write; no character-counting text handler is involved.

    The ownership sidecar is acquired before the historical files are read or
    rotated. Closing the writer releases it, and an OS process death releases
    it automatically on both POSIX and Windows.
    """

    def __init__(self, path, max_bytes=CONNECTOR_LOG_MAX_BYTES,
                 backup_count=CONNECTOR_LOG_BACKUP_COUNT, lock_root=None):
        max_bytes = int(max_bytes)
        backup_count = int(backup_count)
        if max_bytes < 1:
            raise ValueError('max_bytes must be positive')
        if backup_count < 0:
            raise ValueError('backup_count must be non-negative')
        # Keep the requested path only for diagnostics.  All file operations
        # use the resolved target so a custom symlink remains a symlink across
        # migration and rotation.
        self._requested_path = Path(os.path.abspath(os.fspath(path)))
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        rotation_paths = []
        for base in (self._requested_path,
                     Path(os.path.realpath(str(self._requested_path)))):
            rotation_paths.extend(
                base.with_name(f'{base.name}.{index}')
                for index in range(1, self._backup_count + 1))
        self._owner = _ConnectorOwnership(
            self._requested_path, lock_inode=True, stable_paths=rotation_paths,
            lock_root=lock_root)
        self._path = self._owner.target
        self._identity_token = self._owner.identity_token or hashlib.sha256(
            str(self._path).encode('utf-8')).hexdigest()
        self._identity_digest = hashlib.sha256(
            self._identity_token.encode('utf-8')).hexdigest()[:32]
        self._staging_key_bytes = None
        self._staging_key_authority_seeded = False
        self._fh = None
        self._use_anonymous_staging = True
        import threading
        self._thread_lock = threading.RLock()
        try:
            self._staging_key_bytes = self._load_staging_key()
            self._migrate_existing()
            self._owner.refresh_stable_identities()
            self._open_active()
            self._use_anonymous_staging = False
        except BaseException:
            try:
                self._owner.close()
            except BaseException:
                # Preserve the startup/migration failure; callers still get a
                # deterministic error and the OS releases the descriptor at
                # process death if close itself is broken.
                pass
            raise

    def _backup_path(self, index):
        return self._path.with_name(f'{self._path.name}.{index}')

    def _migration_manifest_path(self):
        root = self._owner._ensure_secure_lock_root(self._owner.lock_root)
        return root / f'{self._identity_digest}.migrate.json'

    def _rotation_manifest_path(self):
        root = self._owner._ensure_secure_lock_root(self._owner.lock_root)
        return root / f'{self._identity_digest}.rotate.json'

    def _owned_temp_path(self, kind, index, directory=None):
        directory = Path(directory or self._path.parent)
        nonce = getattr(self, '_transaction_nonce', None) or uuid.uuid4().hex
        return directory / (
            f'.{self._path.name}.{kind}-{self._identity_digest}-'
            f'{nonce}-{index}.tmp')

    _TEMP_MAGIC = b'DISCORD-MB-TEMP-2'
    _LEGACY_TEMP_MAGIC = b'DISCORD-MB-TEMP-1'
    _STAGING_KEY_NAME = 'connector-staging.key'
    _STAGING_KEY_AUTHORITY_NAME = 'connector-staging-authority.sqlite3'
    _STAGING_KEY_AUTHORITY_TIMEOUT = 30.0
    _STAGING_KEY_BYTES = 32
    # Minting the durable authority and publishing the key portably makes the
    # guarded bootstrap section tens of milliseconds long, so the budget has
    # to outlast several contending virgin starts rather than fail one.
    _STAGING_KEY_GUARD_RETRIES = 600
    _STAGING_KEY_GUARD_RETRY_DELAY = 0.005
    _VERIFIED_STAGED_PAYLOADS = {}
    _VERIFIED_STAGED_REPLACEMENTS = {}

    def _staging_key_path(self):
        root = self._owner._ensure_secure_lock_root(self._owner.lock_root)
        return root / self._STAGING_KEY_NAME

    @classmethod
    def _read_secure_staging_key(cls, path):
        """Read a complete private key without adopting a rebound pathname."""
        path = Path(path)
        if _linklike(path):
            raise RuntimeError(f'connector staging key is link-like: {path}')
        try:
            before = path.lstat()
        except OSError:
            raise
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f'connector staging key is not a regular file: {path}')
        if hasattr(os, 'geteuid'):
            if before.st_uid != os.geteuid():
                raise RuntimeError(f'connector staging key has foreign owner: {path}')
            if _posix_mode_exposed(before.st_mode):
                raise RuntimeError(f'connector staging key is too permissive: {path}')
        before_identity = (
            str(getattr(before, 'st_dev', None)),
            str(getattr(before, 'st_ino', None)),
        )
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        fd = os.open(str(path), flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError(f'connector staging key is not a regular file: {path}')
            if hasattr(os, 'geteuid'):
                if opened.st_uid != os.geteuid():
                    raise RuntimeError(
                        f'connector staging key has foreign owner: {path}')
                if _posix_mode_exposed(opened.st_mode):
                    raise RuntimeError(
                        f'connector staging key is too permissive: {path}')
            if opened.st_size != cls._STAGING_KEY_BYTES:
                raise RuntimeError(f'connector staging key is partial: {path}')
            chunks = []
            remaining = cls._STAGING_KEY_BYTES
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    raise RuntimeError(f'connector staging key is partial: {path}')
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise RuntimeError(f'connector staging key has trailing data: {path}')
            after_open = os.fstat(fd)
        finally:
            os.close(fd)
        try:
            after = path.lstat()
        except OSError as exc:
            raise RuntimeError(f'connector staging key disappeared: {path}') from exc
        opened_identity = (getattr(opened, 'st_dev', None),
                           getattr(opened, 'st_ino', None))
        reopened_identity = (getattr(after_open, 'st_dev', None),
                             getattr(after_open, 'st_ino', None))
        if (_linklike(path)
                or before_identity != (str(opened_identity[0]),
                                       str(opened_identity[1]))
                or before_identity != (str(getattr(after, 'st_dev', None)),
                                       str(getattr(after, 'st_ino', None)))
                or opened_identity != reopened_identity):
            raise RuntimeError(
                f'connector staging key changed while reading: {path}')
        return b''.join(chunks)

    @staticmethod
    def _write_fd_bytes(fd, payload):
        view = memoryview(bytes(payload))
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short write while publishing connector bytes')
            view = view[written:]

    @classmethod
    def _publish_anonymous_bytes(cls, path, payload, mode=0o600,
                                 auth_key=None):
        """Publish complete bytes without exposing a partial destination."""
        # A prior portable-fallback crash may have left a fixed claim.  Only a
        # claim authenticated by a durable key may be reconciled.  Bootstrap
        # has no such key, so an exact residue is a preserved collision.
        if cls._recover_named_claim(path, auth_key=auth_key):
            return True
        if not _anonymous_publication_available():
            return False
        flags = os.O_RDWR | os.O_TMPFILE | getattr(os, 'O_BINARY', 0)
        try:
            fd = os.open(str(Path(path).parent), flags, mode)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EINVAL, errno.ENOSYS,
                             errno.ENOTSUP,
                             getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP)):
                return False
            raise
        linked = False
        try:
            try:
                os.fchmod(fd, mode)
            except (AttributeError, OSError):
                pass
            cls._write_fd_bytes(fd, payload)
            os.fsync(fd)
            try:
                cls._link_anonymous_fd(fd, path)
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP,
                                 getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP),
                                 errno.EPERM):
                    return False
                raise
            linked = True
        finally:
            os.close(fd)
        if linked:
            cls._fsync_directory(Path(path).parent)
        return linked

    @staticmethod
    def _named_publish_paths(path):
        path = Path(path)
        prefix = f'.{path.name}.create'
        return (path.with_name(prefix + '.tmp'),
                path.with_name(prefix + '.claim'),
                path.with_name(prefix + '.payload'))

    @classmethod
    def _named_claim_staging_path(cls, path):
        """Return the one fixed, private stage for a named claim record.

        The canonical ``.create.claim`` name is never written incrementally.
        A complete record is fsynced in this fixed stage and then published
        with a non-overwriting hard link.  The fixed name keeps crash residue
        bounded without recursively trying to claim the claim file itself.
        """
        _temporary, claim, _payload = cls._named_publish_paths(path)
        return claim.with_name(claim.name + '.tmp')

    @staticmethod
    def _named_claim_proof_path(path):
        """Return the fixed transaction-provenance inode for one target.

        This deliberately uses ``.create-proof`` rather than another
        ``.create.*`` stage.  The proof carries a complete record authenticated
        by the durable staging key, then is hard-linked to
        ``.create.claim.tmp``.  The link relationship is only an integrity
        check; it is never cleanup authority.  The direct inode protocol also
        avoids recursively trying to claim the claim-stage itself.
        """
        path = Path(path)
        return path.with_name(f'.{path.name}.create-proof')

    @classmethod
    def _named_claim_proof_from_stage(cls, stage):
        stage = Path(stage)
        marker = '.create.claim.tmp'
        if not stage.name.startswith('.') or not stage.name.endswith(marker):
            raise ValueError('invalid named create claim stage')
        target_name = stage.name[1:-len(marker)]
        return stage.with_name(f'.{target_name}.create-proof')

    @classmethod
    def _read_named_claim_proof(cls, proof, auth_key=None):
        """Read one fixed provenance inode without adopting its pathname.

        The proof is not merely an empty hard-link anchor.  It carries the
        same complete claim record as the stage, so a crash before the stage
        link (or after the stage link is removed) still leaves an
        identity-bound record that recovery can authenticate.  A foreign
        exact-name file with unrelated bytes therefore remains a collision.
        """
        proof = Path(proof)
        if _linklike(proof):
            raise FileExistsError(errno.EEXIST,
                                  'named create proof is link-like',
                                  os.fspath(proof))
        try:
            info = proof.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FileExistsError(errno.EEXIST,
                                  'cannot inspect named create proof',
                                  os.fspath(proof)) from exc
        if not stat.S_ISREG(info.st_mode):
            raise FileExistsError(errno.EEXIST,
                                  'named create proof has unsafe shape',
                                  os.fspath(proof))
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise FileExistsError(errno.EEXIST,
                                  'named create proof has foreign owner',
                                  os.fspath(proof))
        if _posix_mode_exposed(info.st_mode):
            raise FileExistsError(errno.EEXIST,
                                  'named create proof is too permissive',
                                  os.fspath(proof))
        if auth_key is None:
            raise FileExistsError(errno.EEXIST,
                                  'named create proof has no durable authenticator',
                                  os.fspath(proof))
        expected = (
            str(getattr(info, 'st_dev', None)),
            str(getattr(info, 'st_ino', None)),
        )
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        try:
            fd = os.open(str(proof), flags)
        except OSError as exc:
            raise FileExistsError(errno.EEXIST,
                                  'named create proof changed while opening',
                                  os.fspath(proof)) from exc
        try:
            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            opened_entry = _ConnectorOwnership._entry_identity(proof)
            if (opened_identity != expected or opened_entry != expected or
                    not stat.S_ISREG(opened.st_mode) or
                    _posix_mode_exposed(opened.st_mode)):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof changed while reading',
                    os.fspath(proof))
            link_count = getattr(opened, 'st_nlink', 1)
        finally:
            os.close(fd)
        if (_linklike(proof) or
                _ConnectorOwnership._entry_identity(proof) != expected):
            raise FileExistsError(errno.EEXIST,
                                  'named create proof changed after reading',
                                  os.fspath(proof))
        marker = '.create-proof'
        if (not proof.name.startswith('.') or
                not proof.name.endswith(marker)):
            raise FileExistsError(errno.EEXIST,
                                  'named create proof has an invalid name',
                                  os.fspath(proof))
        target = proof.with_name(proof.name[1:-len(marker)])
        try:
            record = cls._read_named_claim_record(
                proof, target, auth_key=auth_key)
        except _TornNamedClaimRecord:
            # Reported as itself so recovery can tell a half-written record
            # from a complete one that failed a check.  Every other caller
            # still sees a FileExistsError and fails closed as before.
            raise
        except (FileExistsError, ValueError) as exc:
            raise FileExistsError(
                errno.EEXIST,
                'named create proof has invalid provenance',
                os.fspath(proof)) from exc
        if record is None and link_count == 1:
            raise FileExistsError(errno.EEXIST,
                                  'named create proof has no claim record',
                                  os.fspath(proof))
        return {
            'identity': expected,
            'entry_identity': expected,
            'nlink': int(link_count),
            'size': record.get('size') if record is not None else None,
            'sha256': record.get('sha256') if record is not None else None,
            'mode': record.get('mode') if record is not None else None,
        }

    @classmethod
    def _create_named_claim_proof(cls, proof, record=None):
        """Exclusively create and durably identify a provenance inode.

        ``record`` is written before the inode is linked to the fixed claim
        stage.  Production records carry a durable MAC; a self-described
        record without that MAC is never recoverable.
        """
        proof = Path(proof)
        if _linklike(proof):
            raise FileExistsError(errno.EEXIST,
                                  'named create proof is link-like',
                                  os.fspath(proof))
        flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        fd = None
        identity = None
        entry_identity = None
        try:
            fd = os.open(str(proof), flags, 0o600)
            opened = os.fstat(fd)
            identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            entry_identity = _ConnectorOwnership._entry_identity(proof)
            if (not stat.S_ISREG(opened.st_mode) or
                    getattr(opened, 'st_nlink', 1) != 1 or
                    entry_identity != identity or
                    _posix_mode_exposed(opened.st_mode)):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof changed while creating',
                    os.fspath(proof))
            if record is not None:
                cls._write_fd_bytes(fd, record)
            os.fsync(fd)
        except FileExistsError:
            if identity is not None:
                cls._unlink_if_identity(proof, identity, entry_identity)
                cls._fsync_directory(proof.parent)
            raise
        except BaseException:
            if identity is not None:
                cls._unlink_if_identity(proof, identity, entry_identity)
                cls._fsync_directory(proof.parent)
            raise
        finally:
            if fd is not None:
                os.close(fd)
        cls._fsync_directory(proof.parent)
        return {'identity': identity, 'entry_identity': entry_identity}

    @classmethod
    def _named_claim_auth_message(cls, path, size, digest, mode):
        fields = (
            b'discord-mb-named-create-v2',
            os.fsencode(str(Path(os.path.abspath(os.fspath(path))))),
            str(int(size)).encode('ascii'),
            str(digest).encode('ascii'),
            str(int(mode)).encode('ascii'),
        )
        message = bytearray()
        for field in fields:
            message.extend(len(field).to_bytes(8, 'big'))
            message.extend(field)
        return bytes(message)

    @classmethod
    def _named_claim_record(cls, path, payload, mode, auth_key=None):
        payload = bytes(payload)
        size = len(payload)
        digest = hashlib.sha256(payload).hexdigest()
        record = {
            'magic': 'DISCORD-MB-NAMED-CREATE-1',
            'publication': 'fixed-stage-v1',
            'path': str(Path(os.path.abspath(os.fspath(path)))),
            'size': size,
            'sha256': digest,
            'mode': int(mode),
        }
        if auth_key is not None:
            record['auth'] = hmac.new(
                bytes(auth_key),
                cls._named_claim_auth_message(path, size, digest, mode),
                hashlib.sha256,
            ).hexdigest()
        return (json.dumps(record, sort_keys=True, separators=(',', ':'))
                .encode('ascii') + b'\n')

    @classmethod
    def _read_named_claim_record(cls, claim, path, auth_key=None):
        if _linklike(claim):
            raise FileExistsError(errno.EEXIST, 'named create claim is link-like',
                                  os.fspath(claim))
        try:
            info = claim.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FileExistsError(errno.EEXIST,
                                  'cannot inspect named create claim',
                                  os.fspath(claim)) from exc
        if not stat.S_ISREG(info.st_mode):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim has unsafe shape',
                                  os.fspath(claim))
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise FileExistsError(errno.EEXIST,
                                  'named create claim has foreign owner',
                                  os.fspath(claim))
        claim_identity = (
            str(getattr(info, 'st_dev', None)),
            str(getattr(info, 'st_ino', None)),
        )
        claim_entry_identity = _ConnectorOwnership._entry_identity(claim)
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        fd = os.open(str(claim), flags)
        try:
            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            opened_entry_identity = _ConnectorOwnership._entry_identity(claim)
            claim_stage = cls._named_claim_staging_path(path)
            stage_identity = _ConnectorOwnership._identity_for(claim_stage)
            proof_identity = _ConnectorOwnership._identity_for(
                cls._named_claim_proof_path(path))
            link_count = getattr(opened, 'st_nlink', 1)
            if (opened_identity != claim_identity or
                    opened_entry_identity != opened_identity or
                    (link_count != 1 and
                     not (link_count in (2, 3) and
                          stage_identity == opened_identity and
                          (proof_identity is None or
                           proof_identity == opened_identity))) or
                    _posix_mode_exposed(opened.st_mode)):
                raise FileExistsError(errno.EEXIST,
                                      'named create claim changed while reading',
                                      os.fspath(claim))
            raw = bytearray()
            while len(raw) <= 8192:
                chunk = os.read(fd, 8193 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > 8192:
                raise ValueError('named create claim is too large')
        finally:
            os.close(fd)
        if (_linklike(claim) or
                _ConnectorOwnership._entry_identity(claim) != opened_identity or
                claim_entry_identity != opened_identity):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim changed after reading',
                                  os.fspath(claim))
        try:
            record = json.loads(bytes(raw).decode('ascii'))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise _TornNamedClaimRecord(errno.EEXIST,
                                        'named create claim is torn',
                                        os.fspath(claim)) from exc
        expected_path = str(Path(os.path.abspath(os.fspath(path))))
        if (not isinstance(record, dict) or
                record.get('magic') != 'DISCORD-MB-NAMED-CREATE-1' or
                record.get('path') != expected_path):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim is not ours',
                                  os.fspath(claim))
        try:
            size = int(record['size'])
            digest = str(record['sha256'])
            mode = int(record['mode'])
        except (KeyError, TypeError, ValueError) as exc:
            raise FileExistsError(errno.EEXIST,
                                  'named create claim is incomplete',
                                  os.fspath(claim)) from exc
        if size < 0 or len(digest) != 64 or mode & ~0o7777:
            raise FileExistsError(errno.EEXIST,
                                  'named create claim is invalid',
                                  os.fspath(claim))
        publication = record.get('publication')
        if publication not in (None, 'fixed-stage-v1'):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim has unknown publication',
                                  os.fspath(claim))
        authenticated = False
        if auth_key is not None:
            supplied_auth = record.get('auth')
            expected_auth = hmac.new(
                bytes(auth_key),
                cls._named_claim_auth_message(path, size, digest, mode),
                hashlib.sha256,
            ).hexdigest()
            if (not isinstance(supplied_auth, str) or
                    not hmac.compare_digest(supplied_auth, expected_auth)):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create claim has invalid durable authenticator',
                    os.fspath(claim))
            authenticated = True
        return {
            'size': size,
            'sha256': digest,
            'mode': mode,
            'publication': publication,
            'authenticated': authenticated,
            'identity': claim_identity,
            # Bind the pathname contract to the inode that the fd actually
            # read.  A transient replacement between open(2) and the entry
            # capture above must not become cleanup authority.
            'entry_identity': opened_identity,
        }

    @classmethod
    def _read_named_payload(cls, payload_path, expected_size, expected_digest):
        if _linklike(payload_path):
            raise FileExistsError(errno.EEXIST,
                                  'named create payload is link-like',
                                  os.fspath(payload_path))
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        try:
            fd = os.open(str(payload_path), flags)
        except FileNotFoundError:
            return None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise FileExistsError(errno.EEXIST,
                                      'named create payload is not regular',
                                      os.fspath(payload_path))
            identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            entry_identity = _ConnectorOwnership._entry_identity(payload_path)
            data = bytearray()
            while len(data) <= expected_size:
                chunk = os.read(fd, expected_size + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
        finally:
            os.close(fd)
        current_identity = _ConnectorOwnership._identity_for(payload_path)
        if (_linklike(payload_path) or current_identity != identity or
                _ConnectorOwnership._entry_identity(payload_path) != entry_identity):
            raise FileExistsError(errno.EEXIST,
                                  'named create payload changed while reading',
                                  os.fspath(payload_path))
        if len(data) > expected_size:
            raise FileExistsError(errno.EEXIST,
                                  'named create payload has trailing data',
                                  os.fspath(payload_path))
        return bytes(data), identity, entry_identity

    @classmethod
    def _cleanup_named_claim(cls, claim, claim_record, payload_path,
                             payload_identity=None, payload_entry_identity=None,
                             temporary=None, temporary_identity=None,
                             temporary_entry_identity=None, proof=None,
                             proof_identity=None, proof_entry_identity=None):
        claim_stage = Path(claim).with_name(Path(claim).name + '.tmp')
        proof = (Path(proof) if proof is not None else
                 cls._named_claim_proof_from_stage(claim_stage))
        if proof_identity is None:
            proof_identity = claim_record.get('proof_identity',
                                              claim_record.get('identity'))
        if proof_entry_identity is None:
            proof_entry_identity = claim_record.get(
                'proof_entry_identity', proof_identity)
        # The stage and canonical claim briefly share one inode after the
        # atomic hard-link publication.  Remove the canonical marker first:
        # an exit between the two unlinks leaves only a fixed stage, which is
        # unambiguous recovery residue.  The proof is removed last, and only
        # when its identity is still the stage inode.  A rebound/foreign
        # entry is left intact by the identity-bound unlink primitive.
        cls._unlink_if_identity(
            claim, claim_record['identity'], claim_record['entry_identity'])
        cls._unlink_if_identity(
            claim_stage, claim_record['identity'], claim_record['entry_identity'])
        if temporary is not None and temporary_identity is not None:
            cls._unlink_if_identity(
                temporary, temporary_identity, temporary_entry_identity)
        if payload_identity is not None:
            cls._unlink_if_identity(
                payload_path, payload_identity, payload_entry_identity)
        if proof_identity is not None:
            cls._unlink_if_identity(
                proof, proof_identity, proof_entry_identity)
        cls._fsync_directory(Path(claim).parent)

    @classmethod
    def _named_claim_stage_identity(cls, stage):
        """Return a stage's fd-bound identity without adopting a rebound name."""
        stage = Path(stage)
        if _linklike(stage):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim stage is link-like',
                                  os.fspath(stage))
        try:
            info = stage.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(info.st_mode) or
                getattr(info, 'st_nlink', 1) < 1):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim stage has unsafe shape',
                                  os.fspath(stage))
        expected = (
            str(getattr(info, 'st_dev', None)),
            str(getattr(info, 'st_ino', None)),
        )
        before_entry = _ConnectorOwnership._entry_identity(stage)
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        fd = os.open(str(stage), flags)
        try:
            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            opened_entry = _ConnectorOwnership._entry_identity(stage)
            if (opened_identity != expected or opened_entry != opened_identity or
                    before_entry != opened_identity):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create claim stage changed while reading',
                    os.fspath(stage))
        finally:
            os.close(fd)
        if (_linklike(stage) or
                _ConnectorOwnership._entry_identity(stage) != opened_identity):
            raise FileExistsError(errno.EEXIST,
                                  'named create claim stage changed after reading',
                                  os.fspath(stage))
        return opened_identity, opened_identity

    @classmethod
    def _discard_torn_named_proof(cls, proof, claim_stage, claim):
        """Drop a half-written proof that never reached publication.

        A torn record can only exist on the portable path, where the fixed
        proof name exists from the moment the inode is created rather than
        from the moment its record is complete.  Linux publishes from an
        anonymous inode, so a crash there is invisible and the next start has
        nothing to reconcile; leaving the portable residue in place instead
        wedges every later start of that log for good.

        Discarding is safe exactly when the transaction never got past that
        first step: the proof is still the only link to its inode, and neither
        the claim stage nor the canonical claim exists.  Anything further
        along, any extra link, and any record that merely fails a check rather
        than failing to parse, is somebody's real state and is preserved.
        """
        if os.path.lexists(str(claim_stage)) or os.path.lexists(str(claim)):
            return False
        proof = Path(proof)
        if _linklike(proof):
            return False
        try:
            info = proof.lstat()
        except OSError:
            return False
        if (not stat.S_ISREG(info.st_mode) or
                getattr(info, 'st_nlink', 1) != 1):
            return False
        identity = _ConnectorOwnership._identity_for(proof)
        entry_identity = _ConnectorOwnership._entry_identity(proof)
        if identity is None:
            return False
        removed = cls._unlink_if_identity(proof, identity, entry_identity)
        cls._fsync_directory(proof.parent)
        return removed

    @classmethod
    def _recover_named_claim(cls, path, auth_key=None):
        temporary, claim, payload_path = cls._named_publish_paths(path)
        claim_stage = cls._named_claim_staging_path(path)
        proof = cls._named_claim_proof_path(path)

        # A fixed pathname is not an ownership token.  The only recovery
        # authority is a MAC made with a durable key; without that key every
        # exact proof/stage entry is preserved and startup fails closed.
        try:
            proof_info = cls._read_named_claim_proof(proof, auth_key=auth_key)
        except _TornNamedClaimRecord:
            if not cls._discard_torn_named_proof(proof, claim_stage, claim):
                raise
            proof_info = None
        stage_exists = os.path.lexists(str(claim_stage))
        if proof_info is None:
            if stage_exists:
                # A stage-only residue is recoverable only when its complete
                # record carries a genuine durable MAC.  Its name, mode, and
                # link count never authorize cleanup; they only constrain the
                # identity-bound unlink after authentication succeeds.
                if auth_key is None:
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create claim stage has no durable authenticator',
                        os.fspath(claim_stage))
                try:
                    stage_info = cls._read_named_claim_record(
                        claim_stage, path, auth_key=auth_key)
                    stage_stat = claim_stage.lstat()
                except (FileExistsError, OSError, ValueError) as exc:
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create claim stage has invalid authenticator',
                        os.fspath(claim_stage)) from exc
                if (stage_info is None or
                        not stage_info.get('authenticated') or
                        getattr(stage_stat, 'st_nlink', 1) != 1):
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create claim stage has unsafe ownership state',
                        os.fspath(claim_stage))
                removed = cls._unlink_if_identity(
                    claim_stage, stage_info['identity'],
                    stage_info['entry_identity'])
                if not removed and os.path.lexists(str(claim_stage)):
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create claim stage was rebound',
                        os.fspath(claim_stage))
                cls._fsync_directory(Path(path).parent)
                return False
            # A canonical claim without a matching proof is foreign residue.
            # Leave it untouched and fail closed.
            if os.path.lexists(str(claim)):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create claim has no authenticated proof',
                    os.fspath(claim))
            return False

        if not stage_exists:
            # A complete proof with no stage is the other half of an
            # interrupted fixed-pair cleanup (or a crash before the stage
            # link).  Only the exclusive single-link proof may be discarded;
            # a rebound/extra hard link is a collision and stays intact.
            if os.path.lexists(str(claim)) or proof_info['nlink'] != 1:
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof has no matching claim stage',
                    os.fspath(proof))

            # A cleanup interruption can leave the canonical destination and
            # its payload/temp hard links behind as well.  When the proof's
            # complete claim record authenticates those exact inodes, remove
            # the remaining transaction names in the same identity-bound
            # cleanup.  A missing/foreign/rebound payload is never guessed at.
            expected_size = proof_info.get('size')
            expected_digest = proof_info.get('sha256')
            payload_info = None
            canonical_info = None
            if expected_size is not None and expected_digest is not None:
                try:
                    payload_info = cls._read_named_payload(
                        payload_path, expected_size, expected_digest)
                    canonical_info = cls._read_named_payload(
                        Path(path), expected_size, expected_digest)
                except FileExistsError as exc:
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create proof payload collision',
                        os.fspath(payload_path)) from exc

            payload_identity = None
            payload_entry_identity = None
            if payload_info is not None:
                payload, payload_identity, payload_entry_identity = payload_info
                if (len(payload) > int(expected_size) or
                        (len(payload) == int(expected_size) and
                         hashlib.sha256(payload).hexdigest() !=
                         str(expected_digest))):
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create proof payload was rebound',
                        os.fspath(payload_path))

            canonical_identity = None
            if canonical_info is not None:
                canonical_identity = canonical_info[1]
                canonical_payload = canonical_info[0]
                if (len(canonical_payload) != int(expected_size) or
                        hashlib.sha256(canonical_payload).hexdigest() !=
                        str(expected_digest)):
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create proof destination was rebound',
                        os.fspath(path))
                if (payload_identity is not None and
                        canonical_identity != payload_identity):
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create proof payload identity changed',
                        os.fspath(path))

            temporary_identity = _ConnectorOwnership._identity_for(temporary)
            temporary_entry_identity = _ConnectorOwnership._entry_identity(temporary)
            if temporary_identity is not None:
                expected_temp_identity = payload_identity or canonical_identity
                if (expected_temp_identity is None or
                        temporary_identity != expected_temp_identity):
                    raise FileExistsError(
                        errno.EEXIST,
                        'named create proof temp collision',
                        os.fspath(temporary))

            if payload_identity is None and canonical_identity is None and \
                    temporary_identity is not None:
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof temp has no payload proof',
                    os.fspath(temporary))

            synthetic_record = {
                'identity': proof_info['identity'],
                'entry_identity': proof_info['entry_identity'],
                'proof_identity': proof_info['identity'],
                'proof_entry_identity': proof_info['entry_identity'],
            }
            cls._cleanup_named_claim(
                claim, synthetic_record, payload_path,
                payload_identity, payload_entry_identity,
                temporary if temporary_identity is not None else None,
                temporary_identity, temporary_entry_identity,
                proof=proof, proof_identity=proof_info['identity'],
                proof_entry_identity=proof_info['entry_identity'])
            return False

        stage_identity = cls._named_claim_stage_identity(claim_stage)
        if (stage_identity is None or
                stage_identity[0] != proof_info['identity']):
            raise FileExistsError(
                errno.EEXIST,
                'named create claim stage proof mismatch',
                os.fspath(claim_stage))
        expected_links = 3 if os.path.lexists(str(claim)) else 2
        if proof_info['nlink'] != expected_links:
            # A third-party hard link to the proof inode would let it mutate
            # an unrelated pathname while this transaction writes the stage.
            # Link-count drift is therefore a collision, not ownership.
            raise FileExistsError(
                errno.EEXIST,
                'named create proof has an unexpected link count',
                os.fspath(proof))

        # The proof/stage hard-link relationship is the transaction proof.  A
        # malformed record on that inode is therefore our torn write and may
        # be removed; the same malformed bytes without the proof above are
        # never cleanup-authoritative.
        record = None
        if os.path.lexists(str(claim)):
            claim_identity = cls._named_claim_stage_identity(claim)[0]
            if claim_identity != proof_info['identity']:
                raise FileExistsError(
                    errno.EEXIST,
                    'named create claim collision',
                    os.fspath(claim))
            try:
                record = cls._read_named_claim_record(
                    claim, path, auth_key=auth_key)
            except (FileExistsError, ValueError):
                record = None
        if record is None:
            try:
                staged_record = cls._read_named_claim_record(
                    claim_stage, path, auth_key=auth_key)
            except (FileExistsError, ValueError):
                cls._cleanup_named_claim(
                    claim, {
                        'identity': proof_info['identity'],
                        'entry_identity': proof_info['entry_identity'],
                        'proof_identity': proof_info['identity'],
                        'proof_entry_identity': proof_info['entry_identity'],
                    }, payload_path, proof=proof,
                    proof_identity=proof_info['identity'],
                    proof_entry_identity=proof_info['entry_identity'])
                return False
            if os.path.lexists(str(claim)):
                raise FileExistsError(errno.EEXIST,
                                      'named create claim collision',
                                      os.fspath(claim))
            try:
                os.link(str(claim_stage), str(claim))
                cls._fsync_directory(Path(path).parent)
            except FileExistsError:
                raise
            # There is no payload yet: this record was durable before the
            # payload transaction began.  Remove only this proven inode and
            # let the caller start a fresh bounded publication.
            staged_record['proof_identity'] = proof_info['identity']
            staged_record['proof_entry_identity'] = proof_info['entry_identity']
            cls._cleanup_named_claim(
                claim, staged_record, payload_path, proof=proof,
                proof_identity=proof_info['identity'],
                proof_entry_identity=proof_info['entry_identity'])
            return False

        record['proof_identity'] = proof_info['identity']
        record['proof_entry_identity'] = proof_info['entry_identity']
        payload_info = cls._read_named_payload(
            payload_path, record['size'], record['sha256'])
        if payload_info is None:
            canonical_info = cls._read_named_payload(
                Path(path), record['size'], record['sha256'])
            if (canonical_info is not None and
                    len(canonical_info[0]) == record['size'] and
                    hashlib.sha256(canonical_info[0]).hexdigest() ==
                    record['sha256']):
                cls._cleanup_named_claim(
                    claim, record, payload_path, proof=proof,
                    proof_identity=proof_info['identity'],
                    proof_entry_identity=proof_info['entry_identity'])
                return True
            # The claim was durable before its payload inode.  It is safe to
            # discard this own claim and start a fresh transaction.
            cls._cleanup_named_claim(
                claim, record, payload_path, proof=proof,
                proof_identity=proof_info['identity'],
                proof_entry_identity=proof_info['entry_identity'])
            return False
        data, payload_identity, payload_entry_identity = payload_info
        if (len(data) != record['size'] or
                hashlib.sha256(data).hexdigest() != record['sha256']):
            if len(data) < record['size']:
                cls._cleanup_named_claim(
                    claim, record, payload_path, payload_identity,
                    payload_entry_identity, proof=proof,
                    proof_identity=proof_info['identity'],
                    proof_entry_identity=proof_info['entry_identity'])
                return False
            raise FileExistsError(errno.EEXIST,
                                  'named create payload was rebound',
                                  os.fspath(payload_path))
        temporary_identity = _ConnectorOwnership._identity_for(temporary)
        temporary_entry_identity = _ConnectorOwnership._entry_identity(temporary)
        if temporary_identity is not None:
            if temporary_identity != payload_identity:
                raise FileExistsError(errno.EEXIST,
                                      'named create temp collision',
                                      os.fspath(temporary))
        else:
            try:
                os.link(str(payload_path), str(temporary))
                cls._fsync_directory(Path(path).parent)
            except FileExistsError:
                raise
            temporary_identity = payload_identity
            temporary_entry_identity = _ConnectorOwnership._entry_identity(temporary)
        canonical_identity = _ConnectorOwnership._identity_for(path)
        if canonical_identity is not None:
            if canonical_identity != payload_identity:
                # A complete foreign destination is never replaced or removed.
                cls._cleanup_named_claim(
                    claim, record, payload_path, payload_identity,
                    payload_entry_identity, temporary, temporary_identity,
                    temporary_entry_identity, proof=proof,
                    proof_identity=proof_info['identity'],
                    proof_entry_identity=proof_info['entry_identity'])
                raise FileExistsError(errno.EEXIST,
                                      'named create destination collision',
                                      os.fspath(path))
            cls._cleanup_named_claim(
                claim, record, payload_path, payload_identity,
                payload_entry_identity, temporary, temporary_identity,
                temporary_entry_identity, proof=proof,
                proof_identity=proof_info['identity'],
                proof_entry_identity=proof_info['entry_identity'])
            return True
        try:
            os.link(str(temporary), str(path))
            cls._fsync_directory(Path(path).parent)
        except FileExistsError:
            current_identity = _ConnectorOwnership._identity_for(path)
            if current_identity == payload_identity:
                cls._cleanup_named_claim(
                    claim, record, payload_path, payload_identity,
                    payload_entry_identity, temporary, temporary_identity,
                    temporary_entry_identity, proof=proof,
                    proof_identity=proof_info['identity'],
                    proof_entry_identity=proof_info['entry_identity'])
                return True
            cls._cleanup_named_claim(
                claim, record, payload_path, payload_identity,
                payload_entry_identity, temporary, temporary_identity,
                temporary_entry_identity, proof=proof,
                proof_identity=proof_info['identity'],
                proof_entry_identity=proof_info['entry_identity'])
            raise
        cls._cleanup_named_claim(
            claim, record, payload_path, payload_identity,
            payload_entry_identity, temporary, temporary_identity,
            temporary_entry_identity, proof=proof,
            proof_identity=proof_info['identity'],
            proof_entry_identity=proof_info['entry_identity'])
        return True

    @classmethod
    def _publish_named_bytes(cls, path, payload, mode=0o600, auth_key=None):
        """Portable fallback requiring a durable authenticator."""
        path = Path(path)
        payload = bytes(payload)
        if auth_key is None:
            raise RuntimeError(
                'named connector publication requires a durable authenticator')
        if cls._recover_named_claim(path, auth_key=auth_key):
            return True
        temporary, claim, payload_path = cls._named_publish_paths(path)
        claim_stage = cls._named_claim_staging_path(path)
        proof = cls._named_claim_proof_path(path)
        for candidate in (temporary, payload_path, claim_stage, proof):
            if os.path.lexists(str(candidate)):
                raise FileExistsError(errno.EEXIST,
                                      'named create collision',
                                      os.fspath(candidate))
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        # First create a durable exclusive inode outside the public
        # ``.create.*`` family.  The complete MAC-bearing record is written
        # before it is linked to the fixed claim stage.  A foreign exact-stage
        # collision makes the hard link fail and leaves that entry untouched.
        claim_bytes = cls._named_claim_record(
            path, payload, mode, auth_key=auth_key)
        claim_record = {
            'identity': None,
            'entry_identity': None,
            'proof_identity': None,
            'proof_entry_identity': None,
            'size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
            'mode': int(mode),
        }
        proof_info = cls._create_named_claim_proof(proof, claim_bytes)
        try:
            os.link(str(proof), str(claim_stage))
            proof_after_stage = cls._read_named_claim_proof(
                proof, auth_key=auth_key)
            stage_after_stage = cls._named_claim_stage_identity(claim_stage)
            if (proof_after_stage is None or
                    proof_after_stage['nlink'] != 2 or
                    stage_after_stage is None or
                    stage_after_stage[0] != proof_info['identity']):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof changed after stage link',
                    os.fspath(claim_stage))
            cls._fsync_directory(path.parent)
        except BaseException:
            cls._unlink_if_identity(
                proof, proof_info['identity'], proof_info['entry_identity'])
            cls._fsync_directory(path.parent)
            raise

        claim_record.update({
            'identity': proof_info['identity'],
            'entry_identity': proof_info['entry_identity'],
            'proof_identity': proof_info['identity'],
            'proof_entry_identity': proof_info['entry_identity'],
        })
        fd = None
        expected_identity = None
        expected_entry_identity = None
        temporary_owned = False
        claim_published = False
        try:
            # The hard-linked stage is immutable: its proof record was
            # completed and fsynced before the link.  Never truncate/write the
            # shared inode, because link shape is not authentication and a
            # torn self-described record cannot be recovered safely.
            proof_before_claim = cls._read_named_claim_proof(
                proof, auth_key=auth_key)
            if (proof_before_claim is None or
                    proof_before_claim['nlink'] != 2):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof changed before publication',
                    os.fspath(proof))
            if os.path.lexists(str(claim)):
                raise FileExistsError(errno.EEXIST,
                                      'named create claim collision',
                                      os.fspath(claim))
            try:
                # ``link`` is atomic and non-overwriting: a foreign claim at
                # the canonical name is never replaced or adopted.
                os.link(str(claim_stage), str(claim))
            except FileExistsError:
                raise
            claim_published = True
            if (_ConnectorOwnership._identity_for(claim) !=
                    claim_record['identity'] or
                    _ConnectorOwnership._entry_identity(claim) !=
                    claim_record['entry_identity']):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create claim changed after publication',
                    os.fspath(claim))
            proof_after_claim = cls._read_named_claim_proof(
                proof, auth_key=auth_key)
            if (proof_after_claim is None or
                    proof_after_claim['nlink'] != 3):
                raise FileExistsError(
                    errno.EEXIST,
                    'named create proof changed after claim publication',
                    os.fspath(claim))
            cls._fsync_directory(path.parent)
            # Keep the stage through the payload transaction and destination
            # publication.  The durable MAC authenticates the record; keeping
            # the hard link only preserves the bounded transaction shape.

            fd = os.open(str(payload_path), flags, mode)
            opened = os.fstat(fd)
            expected_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            expected_entry_identity = _ConnectorOwnership._entry_identity(payload_path)
            try:
                try:
                    os.fchmod(fd, mode)
                except (AttributeError, OSError):
                    os.chmod(str(payload_path), mode)
                cls._write_fd_bytes(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
                fd = None
            if (_ConnectorOwnership._identity_for(payload_path) !=
                    expected_identity or
                    _ConnectorOwnership._entry_identity(payload_path) !=
                    expected_entry_identity):
                raise FileExistsError(errno.EEXIST,
                                      'named create payload changed before link',
                                      os.fspath(payload_path))
            os.link(str(payload_path), str(temporary))
            temporary_owned = True
            cls._fsync_directory(path.parent)
            os.link(str(temporary), str(path))
            cls._fsync_directory(path.parent)
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if claim_published:
                # Only the inode created by this call may be removed.  A
                # collision at the public temp or destination remains intact.
                cls._cleanup_named_claim(
                    claim, claim_record, payload_path, expected_identity,
                    expected_entry_identity,
                    temporary if temporary_owned else None,
                    expected_identity if temporary_owned else None,
                    _ConnectorOwnership._entry_identity(temporary)
                    if temporary_owned else None,
                    proof=proof, proof_identity=proof_info['identity'],
                    proof_entry_identity=proof_info['entry_identity'])
            else:
                cls._unlink_if_identity(
                    claim_stage, claim_record['identity'],
                    claim_record['entry_identity'])
                cls._unlink_if_identity(
                    proof, proof_info['identity'], proof_info['entry_identity'])
                cls._fsync_directory(path.parent)
            raise
        cls._cleanup_named_claim(
            claim, claim_record, payload_path, expected_identity,
            expected_entry_identity, temporary, expected_identity,
            _ConnectorOwnership._entry_identity(temporary), proof=proof,
            proof_identity=proof_info['identity'],
            proof_entry_identity=proof_info['entry_identity'])
        return True

    @classmethod
    def _staging_key_authority_path(cls, key_path):
        """Return the durable bootstrap authority beside one staging key."""
        return Path(key_path).with_name(cls._STAGING_KEY_AUTHORITY_NAME)

    @classmethod
    def _open_staging_key_authority(cls, authority_path):
        """Create/adopt only a private, same-user regular authority file.

        The database is pre-created with an exclusive private descriptor so
        the engine never publishes it with a permissive mode, and it is
        validated on every use like the rest of the private lock root.
        """
        authority_path = Path(authority_path)
        if _linklike(authority_path):
            raise RuntimeError(
                f'connector staging key authority is link-like: {authority_path}')
        flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        try:
            os.close(os.open(str(authority_path), flags, 0o600))
        except FileExistsError:
            pass
        info = authority_path.lstat()
        if _linklike(authority_path) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(
                'connector staging key authority is not a regular file: '
                f'{authority_path}')
        if hasattr(os, 'geteuid'):
            if info.st_uid != os.geteuid():
                raise RuntimeError(
                    'connector staging key authority has foreign owner: '
                    f'{authority_path}')
            if _posix_mode_exposed(info.st_mode):
                raise RuntimeError(
                    'connector staging key authority is too permissive: '
                    f'{authority_path}')
        return authority_path

    @classmethod
    def _authority_staging_key(cls, authority_path, adopt=None):
        """Return the one durable bootstrap secret for this lock root.

        The authority is external to the publication it authenticates: the
        secret is committed atomically before any staging-key file exists, so
        residue from an interrupted first publication still carries a MAC the
        next start can verify.  Concurrent virgin writers converge because the
        first committed row wins for every one of them, and ``adopt`` seeds a
        virgin authority from a raw key file published before it existed.
        """
        import sqlite3
        if adopt is not None:
            adopt = bytes(adopt)
            if len(adopt) != cls._STAGING_KEY_BYTES:
                raise ValueError('a durable bootstrap key must be exactly '
                                 f'{cls._STAGING_KEY_BYTES} bytes')
        authority_path = cls._open_staging_key_authority(authority_path)
        connection = None
        try:
            try:
                connection = sqlite3.connect(
                    str(authority_path),
                    timeout=cls._STAGING_KEY_AUTHORITY_TIMEOUT,
                    isolation_level=None)
                # A rollback journal keeps the residue to one transient
                # sidecar name, and a full sync makes the committed secret
                # durable before the publication it will authenticate.
                connection.execute('PRAGMA journal_mode=DELETE')
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                connection.execute(
                    'CREATE TABLE IF NOT EXISTS staging_key ('
                    'id INTEGER PRIMARY KEY CHECK (id = 1), '
                    'key BLOB NOT NULL)')
                row = connection.execute(
                    'SELECT key FROM staging_key WHERE id = 1').fetchone()
                if row is None:
                    key = (adopt if adopt is not None
                           else os.urandom(cls._STAGING_KEY_BYTES))
                    connection.execute(
                        'INSERT INTO staging_key (id, key) VALUES (1, ?)',
                        (key,))
                else:
                    key = row[0]
                connection.execute('COMMIT')
            except BaseException:
                if connection is not None:
                    try:
                        connection.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                raise
            finally:
                if connection is not None:
                    connection.close()
        except sqlite3.Error as exc:
            raise RuntimeError('connector staging key authority is unusable: '
                               f'{authority_path}: {exc}') from exc
        if (not isinstance(key, (bytes, bytearray, memoryview)) or
                len(key) != cls._STAGING_KEY_BYTES):
            raise RuntimeError(
                'connector staging key authority holds a malformed key: '
                f'{authority_path}')
        cls._fsync_directory(authority_path.parent)
        return bytes(key)

    @classmethod
    def _create_staging_key(cls, path):
        """Publish the authority's durable secret as the raw key file."""
        path = Path(path)
        key = cls._authority_staging_key(cls._staging_key_authority_path(path))
        if cls._publish_anonymous_bytes(path, key, auth_key=key):
            return key
        cls._publish_named_bytes(path, key, auth_key=key)
        return key

    def _load_staging_key(self):
        """Return this lock root's staging key, publishing it when absent.

        The raw key file stays the canonical artifact; the durable authority
        only supplies the secret it is published from, so the first
        publication is recoverable and every writer converges on one key.
        """
        path = self._staging_key_path()
        guard_path = path.with_name(f'.{self._STAGING_KEY_NAME}.lock')
        if _linklike(guard_path):
            raise RuntimeError(f'connector staging key guard is link-like: {guard_path}')
        guard = None
        last_error = None
        for attempt in range(self._STAGING_KEY_GUARD_RETRIES):
            try:
                guard = self._owner._lock_sidecar(guard_path)
                break
            except _ConnectorOwnershipError as exc:
                message = str(exc)
                if ('appeared during creation' not in message and
                        'ownership lock is held' not in message):
                    raise
                last_error = exc
                if attempt + 1 < self._STAGING_KEY_GUARD_RETRIES:
                    time.sleep(self._STAGING_KEY_GUARD_RETRY_DELAY)
        if guard is None:
            raise last_error
        try:
            try:
                existing = self._read_secure_staging_key(path)
            except FileNotFoundError:
                existing = None
            authority = self._staging_key_authority_path(path)
            if existing is not None:
                # Once the canonical key exists it is the durable secret that
                # can authenticate and reconcile a named fallback residue.
                # Seeding a virgin authority with it keeps a later bootstrap
                # from minting a secret that disagrees with the journals this
                # key already authenticates.  The authority file is created
                # before its seeding transaction commits, so the file existing
                # is not evidence that it holds a row: only the authority can
                # answer that, and the seeding call is idempotent.  This
                # guarded section runs on every staged publication, so the
                # answer is remembered for the life of the writer instead.
                if not self._staging_key_authority_seeded:
                    self._authority_staging_key(authority, adopt=existing)
                    self._staging_key_authority_seeded = True
                self._recover_named_claim(path, auth_key=existing)
                return self._read_secure_staging_key(path)
            # The authority commits the bootstrap secret before the first
            # publication of it, so an interrupted publication leaves residue
            # this start can authenticate and reconcile.  Residue that does
            # not carry that MAC is still preserved and still fails closed.
            key = self._authority_staging_key(authority)
            self._staging_key_authority_seeded = True
            if not self._recover_named_claim(path, auth_key=key):
                try:
                    self._create_staging_key(path)
                except FileExistsError:
                    # The guard makes this impossible for cooperating writers;
                    # leave the retry for filesystems with unusual create
                    # semantics, then revalidate the complete file.
                    pass
            self._fsync_directory(path.parent)
            return self._read_secure_staging_key(path)
        finally:
            self._owner._release_handle(guard)

    def _current_staging_key(self):
        key = self._load_staging_key()
        if (getattr(self, '_staging_key_bytes', None) is not None and
                not hmac.compare_digest(key, self._staging_key_bytes)):
            raise RuntimeError('connector staging key changed during recovery')
        return key

    @staticmethod
    def _canonical_staging_destination(destination):
        return os.fsencode(str(Path(os.path.abspath(os.fspath(destination)))))

    @classmethod
    def _staging_auth_message(cls, token, nonce, kind, slot, destination,
                              size, digest, payload):
        fields = (
            b'discord-mb-staged-v2',
            str(token).encode('ascii'),
            str(nonce).encode('ascii'),
            str(kind).encode('ascii'),
            str(slot).encode('ascii'),
            cls._canonical_staging_destination(destination),
            str(int(size)).encode('ascii'),
            str(digest).encode('ascii'),
        )
        message = bytearray()
        for field in fields:
            message.extend(len(field).to_bytes(8, 'big'))
            message.extend(field)
        message.extend(len(payload).to_bytes(8, 'big'))
        message.extend(payload)
        return bytes(message)

    @classmethod
    def _staged_payload(cls, raw, *, key=None, expected_token=None,
                        expected_nonce=None, expected_kind=None,
                        expected_slot=None, expected_destination=None):
        """Decode and, when possible, authenticate a staged temp envelope."""
        raw = bytes(raw)
        if raw.startswith(cls._LEGACY_TEMP_MAGIC):
            try:
                header, payload = raw.split(b'\n', 1)
                fields = header.decode('ascii').split(':')
                if (len(fields) != 5 or
                        fields[0] != cls._LEGACY_TEMP_MAGIC.decode('ascii')):
                    raise ValueError
                _magic, token, nonce, size, digest = fields
                if (int(size) != len(payload) or
                        hashlib.sha256(payload).hexdigest() != digest):
                    raise ValueError
            except (UnicodeError, ValueError):
                raise ValueError('connector log staging temp has invalid provenance')
            return payload, {'token': token, 'nonce': nonce, 'size': int(size),
                             'sha256': digest, 'authenticated': False}
        if not raw.startswith(cls._TEMP_MAGIC):
            return raw, None
        try:
            header, payload = raw.split(b'\n', 1)
            fields = header.decode('ascii').split(':')
            if (len(fields) != 9 or
                    fields[0] != cls._TEMP_MAGIC.decode('ascii')):
                raise ValueError
            (_magic, token, nonce, kind, slot, encoded_destination, size,
             digest, mac) = fields
            destination = os.fsdecode(base64.urlsafe_b64decode(
                encoded_destination.encode('ascii')))
            size = int(size)
            if (size != len(payload) or hashlib.sha256(payload).hexdigest() != digest or
                    len(mac) != hashlib.sha256().digest_size * 2):
                raise ValueError
            if expected_token is not None and token != str(expected_token):
                raise ValueError
            if expected_nonce is not None and nonce != str(expected_nonce):
                raise ValueError
            if expected_kind is not None and kind != str(expected_kind):
                raise ValueError
            if expected_slot is not None and slot != str(expected_slot):
                raise ValueError
            if (expected_destination is not None and
                    destination != os.fsdecode(
                        cls._canonical_staging_destination(expected_destination))):
                raise ValueError
            authenticated = False
            if key is not None:
                expected_mac = hmac.new(
                    key,
                    cls._staging_auth_message(
                        token, nonce, kind, slot, destination, size, digest,
                        payload),
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(mac, expected_mac):
                    raise ValueError
                authenticated = True
        except (UnicodeError, ValueError, TypeError, IndexError,
                base64.binascii.Error):
            raise ValueError('connector log staging temp has invalid provenance')
        return payload, {
            'token': token, 'nonce': nonce, 'kind': kind, 'slot': slot,
            'destination': destination, 'size': size, 'sha256': digest,
            'mac': mac, 'authenticated': authenticated,
        }

    def _read_staged_payload(self, temporary, expected_size=None,
                             expected_digest=None, require_provenance=False,
                             expected_kind=None, expected_slot=None,
                             expected_destination=None, expected_nonce=None):
        raw = temporary.read_bytes()
        try:
            payload, provenance = self._staged_payload(
                raw, key=self._current_staging_key(),
                expected_token=self._identity_digest,
                expected_nonce=expected_nonce,
                expected_kind=expected_kind,
                expected_slot=expected_slot,
                expected_destination=expected_destination)
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f'connector log staging temp is not owned: {temporary}') from exc
        if require_provenance and (provenance is None or
                                   not provenance.get('authenticated', False)):
            raise RuntimeError(
                f'connector log staging temp has no ownership proof: {temporary}')
        if provenance is not None and provenance['token'] != self._identity_digest:
            raise RuntimeError(
                f'connector log staging temp identity mismatch: {temporary}')
        if (provenance is not None and
                f"-{provenance['nonce']}-" not in temporary.name):
            raise RuntimeError(
                f'connector log staging temp nonce mismatch: {temporary}')
        if expected_size is not None and len(payload) != int(expected_size):
            raise RuntimeError(
                f'connector log staging temp size mismatch: {temporary}')
        if (expected_digest is not None and
                self._sha256_bytes(payload) != str(expected_digest)):
            raise RuntimeError(
                f'connector log staging temp digest mismatch: {temporary}')
        return payload

    def _staged_temp_is_incomplete(self, temporary, context):
        """Recognize an owned write that stopped before its payload completed."""
        try:
            raw = temporary.read_bytes()
        except OSError:
            return False
        expected_size = int(context.get('expected_size', 0))
        if expected_size == 0 and len(raw) < len(self._TEMP_MAGIC):
            return True
        if len(raw) < expected_size:
            return True
        if raw.startswith(self._TEMP_MAGIC) and b'\n' not in raw:
            return True
        try:
            header, payload = raw.split(b'\n', 1)
            fields = header.decode('ascii').split(':')
            if len(fields) != 9 or fields[0] != self._TEMP_MAGIC.decode('ascii'):
                return False
            _magic, token, nonce, kind, slot, encoded_destination, size, _digest, _mac = fields
            destination = os.fsdecode(base64.urlsafe_b64decode(
                encoded_destination.encode('ascii')))
            return (
                token == self._identity_digest and
                nonce == str(context.get('expected_nonce')) and
                kind == str(context.get('expected_kind')) and
                slot == str(context.get('expected_slot')) and
                destination == os.fsdecode(self._canonical_staging_destination(
                    context['expected_destination'])) and
                int(size) == expected_size and len(payload) < int(size)
            )
        except (UnicodeError, ValueError, TypeError, IndexError,
                base64.binascii.Error):
            return False

    def _prepare_staged_for_replace(self, temporary, expected_size,
                                    expected_digest, **context):
        """Strip the envelope without trusting a mutable staged pathname.

        The validation result is about one directory entry, not about a name
        that may be rebound immediately afterwards.  Open the validated inode
        without following links, verify both inode and entry identities, and
        perform the envelope strip through that descriptor.  A rebound path is
        rejected before any write, while a swap after open can only mutate the
        already-open staged inode; the final entry check then prevents its
        pathname from being published.
        """
        temporary = Path(temporary)
        expected_identity = self._owner._identity_for(temporary)
        expected_entry_identity = self._owner._entry_identity(temporary)
        if expected_identity is None or expected_entry_identity is None:
            raise RuntimeError(
                f'connector log staging temp disappeared before preparation: '
                f'{temporary}')

        # Keep the existing validation seam, but never use its pathname reads
        # as the bytes that are subsequently written.  The descriptor below
        # is the only object allowed to supply or receive staged bytes.
        payload = self._read_staged_payload(
            temporary, expected_size=expected_size,
            expected_digest=expected_digest, **context)
        flags = (os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        try:
            fd = os.open(str(temporary), flags)
        except OSError as exc:
            raise RuntimeError(
                f'connector log staging temp changed before preparation: '
                f'{temporary}') from exc
        try:
            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            if opened_identity != tuple(str(value) for value in expected_identity):
                raise RuntimeError(
                    f'connector log staging temp inode changed before preparation: '
                    f'{temporary}')
            current_entry_identity = self._owner._entry_identity(temporary)
            if current_entry_identity != tuple(
                    str(value) for value in expected_entry_identity):
                raise RuntimeError(
                    f'connector log staging temp entry changed before preparation: '
                    f'{temporary}')

            chunks = []
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b''.join(chunks)
            try:
                actual_payload, _provenance = self._staged_payload(
                    raw, key=self._current_staging_key(),
                    expected_token=self._identity_digest,
                    expected_nonce=context.get('expected_nonce'),
                    expected_kind=context.get('expected_kind'),
                    expected_slot=context.get('expected_slot'),
                    expected_destination=context.get('expected_destination'))
            except (RuntimeError, ValueError) as exc:
                raise RuntimeError(
                    f'connector log staging temp changed during preparation: '
                    f'{temporary}') from exc
            if (actual_payload != payload or
                    len(actual_payload) != int(expected_size) or
                    self._sha256_bytes(actual_payload) != str(expected_digest)):
                raise RuntimeError(
                    f'connector log staging temp content changed during preparation: '
                    f'{temporary}')

            # Revalidate the directory entry immediately before mutating the
            # opened inode.  If an attacker rebound the name to a hard link,
            # only the original staged fd can be changed and the operation
            # fails closed before publication.
            current_identity = self._owner._identity_for(temporary)
            current_entry_identity = self._owner._entry_identity(temporary)
            if (current_identity != tuple(str(value) for value in expected_identity) or
                    current_entry_identity != tuple(
                        str(value) for value in expected_entry_identity)):
                raise RuntimeError(
                    f'connector log staging temp entry changed before write: '
                    f'{temporary}')
            if raw != payload:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError('short write while preparing connector temp')
                    view = view[written:]
                os.fsync(fd)

            # A swap during the write leaves the external inode untouched, but
            # must not let the rebound pathname reach the subsequent replace.
            opened_after = os.fstat(fd)
            after_identity = (
                str(getattr(opened_after, 'st_dev', None)),
                str(getattr(opened_after, 'st_ino', None)),
            )
            final_identity = self._owner._identity_for(temporary)
            final_entry_identity = self._owner._entry_identity(temporary)
            if (after_identity != tuple(str(value) for value in expected_identity) or
                    final_identity != tuple(str(value) for value in expected_identity) or
                    final_entry_identity != tuple(
                        str(value) for value in expected_entry_identity)):
                raise RuntimeError(
                    f'connector log staging temp entry changed after preparation: '
                    f'{temporary}')
            self._VERIFIED_STAGED_REPLACEMENTS[
                self._staged_path_key(temporary)] = (
                    tuple(str(value) for value in expected_identity),
                    tuple(str(value) for value in expected_entry_identity))
        finally:
            os.close(fd)
        return temporary

    def _revalidate_staged_for_replace(self, temporary):
        """Recheck the prepared source entry immediately before os.replace."""
        key = self._staged_path_key(temporary)
        expected = self._VERIFIED_STAGED_REPLACEMENTS.get(key)
        if expected is None:
            raise RuntimeError(
                f'connector log staging temp was not verified: {temporary}')
        expected_identity, expected_entry_identity = expected
        current_identity = self._owner._identity_for(temporary)
        current_entry_identity = self._owner._entry_identity(temporary)
        if (current_identity != expected_identity or
                current_entry_identity != expected_entry_identity):
            raise RuntimeError(
                f'connector log staging temp was rebound before replace: '
                f'{temporary}')

    def _forget_staged_for_replace(self, temporary):
        self._VERIFIED_STAGED_REPLACEMENTS.pop(
            self._staged_path_key(temporary), None)

    def _journal_temporary_paths(self, manifest, root, kind, log_name,
                                 field='created_temporaries'):
        """Return the bounded transaction paths recorded by a journal.

        A directory scan cannot prove that a similarly named file belongs to
        this writer.  Recovery never scans the log directory; it considers
        only the exact, root-confined names in the durable transaction intent
        created before staging.  ``O_EXCL`` prevents a live collision from
        being adopted by the current writer.
        """
        raw_paths = manifest.get(field, [])
        if not isinstance(raw_paths, list):
            raise ValueError('connector log journal has invalid temporary list')
        paths = []
        for raw_path in raw_paths:
            path = self._manifest_path(raw_path, root)
            self._validate_journal_temp(path, kind, log_name)
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise ValueError('connector log journal contains duplicate temporaries')
        return paths

    @staticmethod
    def _unlink_if_identity(path, expected_identity,
                            expected_entry_identity=None):
        """Unlink only the inode/entry that was validated by this writer.

        A pathname is not an ownership proof.  Re-check the opened inode and
        directory entry immediately before unlinking so a replacement after
        validation remains untouched.  O_NOFOLLOW also prevents a rebound
        symlink from being adopted as the validated temp.
        """
        if expected_identity is None:
            return False
        expected_identity = tuple(str(value) for value in expected_identity)
        if expected_entry_identity is not None:
            expected_entry_identity = tuple(
                str(value) for value in expected_entry_identity)
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        try:
            fd = os.open(str(path), flags)
        except OSError:
            return False
        try:
            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            if opened_identity != expected_identity:
                return False
            current_identity = _ConnectorOwnership._identity_for(path)
            current_entry_identity = _ConnectorOwnership._entry_identity(path)
            if current_identity != expected_identity:
                return False
            if (expected_entry_identity is not None and
                    current_entry_identity != expected_entry_identity):
                return False
            if os.name == 'nt':
                # CRT handles may deny pathname deletion while open.  Close
                # before the final identity check; the recheck still rejects
                # a replacement observed during that close window.
                try:
                    os.close(fd)
                except OSError:
                    return False
                fd = None
            # A second entry check is intentional: tests and hostile
            # processes can replace the name during validation.
            final_identity = _ConnectorOwnership._identity_for(path)
            final_entry_identity = _ConnectorOwnership._entry_identity(path)
            if (final_identity != expected_identity or
                    (expected_entry_identity is not None and
                     final_entry_identity != expected_entry_identity)):
                return False
            path = Path(path)
            quarantine = path.with_name(
                f'.{path.name}.unlink-{uuid.uuid4().hex}.tmp')
            try:
                # Move the validated directory entry out of the public name
                # atomically.  A rebound pathname is therefore moved to the
                # quarantine instead of being unlinked in place.
                os.rename(str(path), str(quarantine))
            except OSError:
                return False

            def restore_quarantine():
                try:
                    # Never overwrite a name that was recreated while the
                    # validated entry was quarantined.
                    if not os.path.lexists(str(path)):
                        os.rename(str(quarantine), str(path))
                except OSError:
                    pass

            quarantined_identity = _ConnectorOwnership._identity_for(quarantine)
            quarantined_entry_identity = _ConnectorOwnership._entry_identity(
                quarantine)
            if (quarantined_identity != expected_identity or
                    (expected_entry_identity is not None and
                     quarantined_entry_identity != expected_entry_identity)):
                restore_quarantine()
                return False
            try:
                Path(quarantine).unlink()
            except OSError:
                restore_quarantine()
                return False
            return True
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _cleanup_preparing_temps(self, manifest, root, kind, log_name):
        """Remove only temps with durable/enveloped creation proof.

        ``planned_temporaries`` is an intent list, not an ownership list.  A
        pre-existing exact collision is therefore left untouched.  A v3 temp
        that completed exclusive creation carries its own fsynced envelope,
        which also closes the crash window before the journal append.
        """
        planned = self._journal_temporary_paths(
            manifest, root, kind, log_name, field='planned_temporaries')
        created = set(self._journal_temporary_paths(
            manifest, root, kind, log_name, field='created_temporaries'))
        context_by_temp = {}
        identity_by_temp = {}
        entries = manifest.get('destinations', [])
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict) or not entry.get('temporary'):
                    continue
                try:
                    temporary = self._manifest_path(entry['temporary'], root)
                    destination = self._manifest_path(
                        entry['destination'], root)
                except (KeyError, TypeError, ValueError):
                    continue
                context_by_temp[temporary] = {
                    'expected_kind': kind,
                    'expected_slot': index,
                    'expected_destination': destination,
                    'expected_nonce': manifest.get('transaction_nonce'),
                    'expected_size': (int(entry['size'])
                                      if entry.get('present', True) else 0),
                    'expected_digest': str(entry['sha256'])
                    if entry.get('present', True) else self._sha256_bytes(b''),
                }
                identity_by_temp[temporary] = {
                    'identity': self._manifest_identity(
                        entry.get('temporary_identity')),
                    'entry_identity': self._manifest_identity(
                        entry.get('temporary_entry_identity')),
                }
        for temporary in planned:
            if not temporary.is_file():
                continue
            context = context_by_temp.get(temporary)
            if context is None:
                continue
            recorded = identity_by_temp.get(temporary, {})
            expected_identity = recorded.get('identity')
            expected_entry_identity = recorded.get('entry_identity')
            if temporary in created and expected_identity is not None:
                # A durable exclusive-create claim is sufficient to clean an
                # interrupted/invalid envelope.  Content validation is still
                # useful for diagnostics, but an invalid partial write must
                # not strand one temp per hard restart.
                try:
                    self._read_staged_payload(
                        temporary, require_provenance=True, **context)
                except RuntimeError:
                    if not self._staged_temp_is_incomplete(temporary, context):
                        continue
                self._unlink_if_identity(
                    temporary, expected_identity, expected_entry_identity)
                continue

            # A complete envelope created before the creation append remains
            # recoverable from older journals.  It may be deleted only when
            # authentication succeeds and the identity is captured before
            # validation; malformed/foreign exact-name collisions survive.
            current_identity = self._owner._identity_for(temporary)
            current_entry_identity = self._owner._entry_identity(temporary)
            try:
                self._read_staged_payload(
                    temporary, require_provenance=True, **context)
            except RuntimeError:
                # An exclusive create that died before its journal append is
                # only reachable on the portable path, where the name exists
                # from the moment of creation.  It leaves an empty entry at a
                # name this transaction alone planned.  Nothing distinguishes
                # that from an empty foreign collision, and nothing needs to:
                # an empty file has no content a deletion could lose, while
                # keeping it strands one hidden temp per hard restart.  Any
                # collision carrying bytes still survives untouched.
                if not self._planned_temp_is_empty(temporary):
                    continue
            self._unlink_if_identity(
                temporary, current_identity, current_entry_identity)

    @staticmethod
    def _planned_temp_is_empty(temporary):
        """True for a zero-length regular file at a planned temp name."""
        try:
            info = temporary.lstat()
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and info.st_size == 0

    def _cleanup_journal_temp(self, journal, kind):
        """Keep legacy journal temps: a pathname and JSON are not ownership.

        Current generations write the durable journal directly with an
        exclusive create, so this compatibility hook deliberately does
        nothing.  A process that finds a legacy ``*.json.tmp`` cannot prove
        that it created the file before a crash, and must neither adopt nor
        delete it merely because its contents look plausible.
        """
        return

    def _numeric_backups(self):
        found = {}
        prefix = f'{self._path.name}.'
        for candidate in self._path.parent.iterdir():
            name = candidate.name
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if suffix.isdigit():
                index = int(suffix)
                if index >= 1 and suffix == str(index):
                    found[index] = candidate
        return found

    @staticmethod
    def _decodes_as_utf8(path, block=64 * 1024):
        """Validate a file's UTF-8 in bounded blocks, never as one string.

        This runs on every start against files that may be a full ceiling
        (10 MiB by default) each, so the decoded text is discarded block by
        block instead of being materialized.  A sequence split across two
        blocks is carried by the incremental decoder, and ``final=True``
        rejects a file that ends mid-sequence.
        """
        decoder = codecs.getincrementaldecoder('utf-8')()
        try:
            with open(path, 'rb') as source:
                while True:
                    chunk = source.read(block)
                    if not chunk:
                        break
                    decoder.decode(chunk)
                decoder.decode(b'', True)
        except UnicodeDecodeError:
            return False
        return True

    def _window_is_bounded(self, backups):
        """Return true without materializing a window that is already safe."""
        if not self._path.is_file():
            return False
        paths = [self._path] + [backups[index]
                                for index in sorted(backups)
                                if 1 <= index <= self._backup_count]
        try:
            for path in paths:
                if path.stat().st_size > self._max_bytes:
                    return False
                # A bounded file can still contain malformed bytes from an
                # older writer.  It must go through the UTF-8-safe migration.
                if not self._decodes_as_utf8(path):
                    return False
            return True
        except OSError:
            return False

    @staticmethod
    def _fsync_directory(directory):
        """Persist directory entry updates where the platform supports it."""
        if os.name == 'nt':
            return
        fd = os.open(str(directory), os.O_RDONLY |
                     getattr(os, 'O_DIRECTORY', 0) |
                     getattr(os, 'O_BINARY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _staging_envelope(self, data):
        """Build the authenticated bytes before making a name visible."""
        data = bytes(data)
        digest = self._sha256_bytes(data)
        kind = getattr(self, '_staging_kind', None) or 'migrate'
        slot = getattr(self, '_staging_slot', None)
        if slot is None:
            slot = 0
        destination = getattr(self, '_staging_destination', None) or self._path
        nonce = getattr(self, '_transaction_nonce', '')
        encoded_destination = base64.urlsafe_b64encode(
            self._canonical_staging_destination(destination)).decode('ascii')
        mac = hmac.new(
            self._current_staging_key(),
            self._staging_auth_message(
                self._identity_digest, nonce, kind, slot, destination,
                len(data), digest, data),
            hashlib.sha256,
        ).hexdigest()
        header = (
            self._TEMP_MAGIC.decode('ascii') + ':' +
            self._identity_digest + ':' + str(nonce) + ':' +
            str(kind) + ':' + str(slot) + ':' + encoded_destination + ':' +
            str(len(data)) + ':' + digest + ':' + mac + '\n'
        ).encode('ascii')
        return header + data

    @staticmethod
    def _link_anonymous_fd(fd, name):
        """Link a durable anonymous inode without replacing a collision."""
        import ctypes
        if not sys.platform.startswith('linux'):
            raise OSError(errno.ENOTSUP, 'anonymous staged linking is unavailable')
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
        linkat.argtypes = [ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        linkat.restype = ctypes.c_int
        # Linux AT_EMPTY_PATH links the already-open O_TMPFILE inode.  The
        # destination operation is non-overwriting by definition: EEXIST is a
        # foreign exact-name collision, never a reason to adopt the entry.
        result = linkat(
            int(fd), ctypes.c_char_p(b''), -100,
            ctypes.c_char_p(os.fsencode(str(name))), 0x1000)
        if result == 0:
            return
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), os.fspath(name))
        raise OSError(code, os.strerror(code), os.fspath(name))

    def _write_anonymous_migration_temp(self, data, mode, name, directory):
        """Publish a complete staged envelope from an anonymous inode.

        POSIX/Linux can keep every crash before the non-overwriting link
        invisible.  Once the name exists it already contains authenticated
        bytes, so a crash before the journal identity callback is recoverable
        by envelope authentication rather than pathname adoption.
        """
        if not _anonymous_publication_available():
            return None
        flags = os.O_RDWR | os.O_TMPFILE | getattr(os, 'O_BINARY', 0)
        try:
            fd = os.open(str(directory), flags, 0o600)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP,
                             getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP),
                             errno.EACCES):
                return None
            raise

        linked = False
        expected_identity = None
        expected_entry_identity = None
        try:
            if mode is not None:
                try:
                    os.fchmod(fd, mode)
                except (AttributeError, OSError):
                    # The inode has no pathname yet; fchmod is the only safe
                    # operation.  Filesystems lacking it retain 0600.
                    pass
            envelope = self._staging_envelope(data)
            view = memoryview(envelope)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('short write while publishing connector log')
                view = view[written:]
            os.fsync(fd)
            try:
                self._link_anonymous_fd(fd, name)
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP,
                                 getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP),
                                 errno.EPERM):
                    return None
                raise
            linked = True
            opened = os.fstat(fd)
            expected_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            expected_entry_identity = _ConnectorOwnership._entry_identity(name)
            if expected_entry_identity is None:
                raise RuntimeError(
                    f'connector log staging temp disappeared after publish: {name}')
            claim_callback = getattr(self, '_staging_claim_callback', None)
            if claim_callback is not None:
                claim_callback(Path(name))
            self._fsync_directory(directory)
        except BaseException:
            try:
                os.close(fd)
            finally:
                if linked:
                    removed = self._unlink_if_identity(
                        name, expected_identity, expected_entry_identity)
                    if (not removed and os.path.lexists(str(name))):
                        self._staging_cleanup_failed = True
            raise
        else:
            os.close(fd)
        return Path(name)

    def _write_migration_temp(self, data, mode, name=None, directory=None):
        """Write and fsync one replacement in a target directory.

        Transaction temps have deterministic identity-bearing names.  The
        durable journal records planned names before staging and appends each
        exclusive creation separately; recovery deletes only paths with an
        ownership proof, while the fixed names keep the hidden set bounded
        independently of crash count.
        """
        directory = Path(directory or self._path.parent)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if name is None:
            name = self._owned_temp_path('migrate', 0, directory)
        else:
            name = Path(name)
            if not name.is_absolute():
                name = directory / name
        if getattr(self, '_use_anonymous_staging', False):
            anonymous = self._write_anonymous_migration_temp(
                data, mode, name, directory)
            if anonymous is not None:
                return anonymous
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_BINARY', 0))
        fd = os.open(str(name), flags, 0o600)
        opened = os.fstat(fd)
        expected_identity = (
            str(getattr(opened, 'st_dev', None)),
            str(getattr(opened, 'st_ino', None)),
        )
        expected_entry_identity = _ConnectorOwnership._entry_identity(name)
        try:
            if mode is not None:
                try:
                    os.fchmod(fd, mode)
                except (AttributeError, OSError):
                    os.chmod(str(name), mode)
            # Append the creation result before crossing the directory-fsync
            # boundary.  The durable transaction intent already names this
            # exact O_EXCL slot, while this record authenticates the inode that
            # this call actually created.  Recovery can therefore reclaim an
            # incomplete owned file without adopting an exact-name collision
            # for which O_EXCL never succeeded.
            claim_callback = getattr(self, '_staging_claim_callback', None)
            if claim_callback is not None:
                claim_callback(Path(name))
            # The directory entry and its inode identity must be durable
            # before any payload bytes are written.  A hard exit during the
            # first write therefore leaves a journaled ownership claim that
            # recovery can distinguish from a foreign exact-name collision.
            self._fsync_directory(directory)
            envelope = self._staging_envelope(data)
            view = memoryview(envelope)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('short write while publishing connector log')
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            finally:
                removed = self._unlink_if_identity(
                    name, expected_identity, expected_entry_identity)
                if (not removed and
                        os.path.lexists(str(name))):
                    self._staging_cleanup_failed = True
            raise
        else:
            os.close(fd)
        return Path(name)

    @staticmethod
    def _staged_path_key(temporary):
        return Path(os.path.abspath(os.fspath(temporary)))

    def _validate_and_remember_staged(self, temporary, *args, **kwargs):
        """Validate a temp while recording its exact object and payload."""
        identity = self._owner._identity_for(temporary)
        entry_identity = self._owner._entry_identity(temporary)
        payload = self._read_staged_payload(temporary, *args, **kwargs)
        self._VERIFIED_STAGED_PAYLOADS[self._staged_path_key(temporary)] = (
            payload, identity, entry_identity)
        return payload

    def _forget_verified_staged(self, temporary):
        self._VERIFIED_STAGED_PAYLOADS.pop(self._staged_path_key(temporary), None)

    @staticmethod
    def _publish_in_place(destination, temporary,
                          expected_destination_identity=None,
                          expected_destination_entry_identity=None):
        """Copy a staged slot into an existing hard-linked inode safely.

        The active slot always uses this path: the writer's ownership lock is
        on that inode, so replacing the pathname would strand the lock.
        """
        key = _ConnectorLogWriter._staged_path_key(temporary)
        verified = _ConnectorLogWriter._VERIFIED_STAGED_PAYLOADS.pop(
            key, None)
        if verified is None:
            # Decoding the envelope here would authenticate nothing and would
            # write the result straight into the live inode.  Callers stage
            # through _validate_and_remember_staged; anything else fails closed.
            raise RuntimeError(
                'connector log in-place publication requires a verified '
                f'staged payload: {temporary}')
        data, expected_identity, expected_entry_identity = verified
        if (expected_destination_identity is None and
                expected_destination_entry_identity is None):
            expected_destination_identity = _ConnectorOwnership._identity_for(
                destination)
            expected_destination_entry_identity = _ConnectorOwnership._entry_identity(
                destination)
        mode = 'r+b' if expected_destination_identity is not None else 'w+b'
        with open(destination, mode, buffering=0) as active:
            opened = os.fstat(active.fileno())
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            expected_destination_identity = (
                tuple(str(value) for value in expected_destination_identity)
                if expected_destination_identity is not None else None)
            expected_destination_entry_identity = (
                tuple(str(value) for value in expected_destination_entry_identity)
                if expected_destination_entry_identity is not None else None)
            current_entry_identity = _ConnectorOwnership._entry_identity(destination)
            if (opened_identity != expected_destination_identity or
                    current_entry_identity != expected_destination_entry_identity):
                raise RuntimeError(
                    f'connector log destination changed before in-place publish: '
                    f'{destination}')
            active.truncate(0)
            view = memoryview(data)
            while view:
                written = os.write(active.fileno(), view)
                if written <= 0:
                    raise OSError('short write while publishing connector log inode')
                view = view[written:]
            os.fsync(active.fileno())
        _ConnectorLogWriter._unlink_if_identity(
            temporary, expected_identity, expected_entry_identity)

    @staticmethod
    def _sha256_bytes(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _sha256_file(path):
        digest = hashlib.sha256()
        size = 0
        with open(path, 'rb') as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()

    @classmethod
    def _file_matches_state(cls, path, expected_size, expected_digest):
        """Compare a pathname's bounded metadata/hash without materializing it."""
        try:
            if not path.is_file() or path.stat().st_size != int(expected_size):
                return False
            size, digest = cls._sha256_file(path)
        except OSError:
            return False
        return size == int(expected_size) and digest == str(expected_digest)

    def _journal_file_state(self, path):
        """Capture the exact pre-crash identity/state of one pathname."""
        identity = self._owner._identity_for(path)
        entry_identity = self._owner._entry_identity(path)
        present = False
        size = 0
        digest = self._sha256_bytes(b'')
        try:
            present = path.is_file()
            if present:
                size, digest = self._sha256_file(path)
        except OSError:
            # The identity is still useful if a concurrent actor removed the
            # path; recovery will fail closed unless the journal can prove an
            # expected absence/completed destination.
            present = False
        return {
            'path': str(path),
            'identity': (list(identity) if identity is not None else None),
            'entry_identity': (list(entry_identity)
                               if entry_identity is not None else None),
            'present': bool(present),
            'size': int(size),
            'sha256': digest,
        }

    @staticmethod
    def _manifest_identity(value):
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError('connector log journal has invalid pathname identity')
        return tuple(str(item) for item in value)

    def _manifest_path_identity(self, manifest, path):
        for record in manifest.get('path_states', []):
            if isinstance(record, dict) and record.get('path') == str(path):
                return (
                    self._manifest_identity(record.get('identity')),
                    self._manifest_identity(record.get('entry_identity')),
                )
        return None, None

    def _write_migration_manifest(self, manifest, kind='migrate', *,
                                  create=False):
        """Durably create or update the fixed transaction journal.

        The first write is an ``O_EXCL`` create and happens before any data
        staging.  Subsequent state changes append another complete record to
        that same owned journal, so a torn update falls back to the last
        complete state; no fixed ``journal.json.tmp`` is ever created.  A
        stale file at that legacy pathname is therefore never adopted or
        removed based on JSON-shaped contents.
        """
        payload = (json.dumps(manifest, sort_keys=True, separators=(',', ':'))
                   .encode('utf-8') + b'\n')
        journal = (self._migration_manifest_path() if kind == 'migrate'
                   else self._rotation_manifest_path())
        self._owner._ensure_secure_lock_root(self._owner.lock_root)
        if create:
            # The first journal record must become visible only after its full
            # JSON bytes are durable.  A hard exit before publication leaves
            # no canonical partial record for restart to misparse or adopt.
            if self._publish_anonymous_bytes(
                    journal, payload, auth_key=self._staging_key_bytes):
                return
            self._publish_named_bytes(
                journal, payload, auth_key=self._staging_key_bytes)
            return
        flags = (os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
        expected_identity = None
        if not create:
            try:
                before = journal.lstat()
            except OSError as exc:
                raise RuntimeError(
                    f'connector log journal disappeared before append: {journal}') from exc
            if (_linklike(journal) or not stat.S_ISREG(before.st_mode) or
                    getattr(before, 'st_nlink', 1) != 1):
                raise RuntimeError(
                    f'connector log journal has an unsafe directory entry: {journal}')
            expected_identity = (
                str(getattr(before, 'st_dev', None)),
                str(getattr(before, 'st_ino', None)),
            )
            flags |= os.O_APPEND
        fd = os.open(str(journal), flags, 0o600)
        try:
            opened = os.fstat(fd)
            opened_identity = (
                str(getattr(opened, 'st_dev', None)),
                str(getattr(opened, 'st_ino', None)),
            )
            if (not stat.S_ISREG(opened.st_mode) or
                    getattr(opened, 'st_nlink', 1) != 1 or
                    (expected_identity is not None and
                     opened_identity != expected_identity) or
                    _linklike(journal) or
                    _ConnectorOwnership._entry_identity(journal) != opened_identity):
                raise RuntimeError(
                    f'connector log journal changed before append: {journal}')
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('short write while publishing connector journal')
                view = view[written:]
            os.fsync(fd)
            current = os.fstat(fd)
            current_identity = (
                str(getattr(current, 'st_dev', None)),
                str(getattr(current, 'st_ino', None)),
            )
            if (_linklike(journal) or current_identity != opened_identity or
                    _ConnectorOwnership._entry_identity(journal) != opened_identity):
                raise RuntimeError(
                    f'connector log journal changed after append: {journal}')
        finally:
            os.close(fd)
        self._fsync_directory(journal.parent)

    def _mark_destination_publishing(self, manifest, index, kind):
        """Persist the identity contract before mutating a destination inode."""
        entry = manifest['destinations'][index]
        entry['publish_state'] = 'publishing'
        entry['publish_identity'] = entry.get('before_identity')
        entry['publish_entry_identity'] = entry.get('before_entry_identity')
        self._write_migration_manifest(manifest, kind=kind)

    @staticmethod
    def _revalidate_destination(destination, expected_identity,
                                expected_entry_identity):
        """Refuse publication if a destination changed after observation."""
        expected = (None if expected_identity is None else
                    tuple(str(value) for value in expected_identity))
        expected_entry = (None if expected_entry_identity is None else
                          tuple(str(value) for value in expected_entry_identity))
        current = _ConnectorOwnership._identity_for(destination)
        current_entry = _ConnectorOwnership._entry_identity(destination)
        if current != expected or current_entry != expected_entry:
            raise RuntimeError(
                f'connector log destination changed before publish: {destination}')

    @staticmethod
    def _manifest_path(name, root):
        if not isinstance(name, str) or not name:
            raise ValueError('connector log journal contains an unsafe path')
        path = Path(name)
        if not path.is_absolute() or path.parent != root:
            raise ValueError('connector log journal contains a path outside its root')
        return path

    def _validate_journal_temp(self, path, kind, log_name):
        expected = (rf'\.{re.escape(log_name)}\.{re.escape(kind)}-'
                    rf'{re.escape(self._identity_digest)}-'
                    rf'(?:[0-9a-fA-F]{{32}}-)?\d+\.tmp')
        if (not path.is_absolute() or
                re.fullmatch(expected, path.name) is None):
            raise ValueError('connector log journal contains an unauthenticated temp')

    def _journal_manifest(self, journal, kind):
        try:
            raw = journal.read_text(encoding='utf-8')
            try:
                manifest = json.loads(raw)
            except json.JSONDecodeError:
                # Current journals are append-only state records.  If a hard
                # exit tears the newest append, retain the last complete JSON
                # line, which is the durable ``preparing`` intent.
                manifest = None
                for line in raw.splitlines():
                    try:
                        candidate = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict):
                        manifest = candidate
                if manifest is None:
                    raise ValueError('connector log journal has no complete state')
            if manifest.get('version') not in (2, 3) or manifest.get('kind') != kind:
                raise ValueError('unsupported connector log journal version')
            if manifest.get('token') != self._identity_digest:
                raise ValueError('connector log journal identity mismatch')
            state = manifest.get('state', 'prepared')
            if state not in ('preparing', 'prepared'):
                raise ValueError('connector log journal has invalid state')
            manifest['state'] = state
            root = Path(manifest['root'])
            if not root.is_absolute():
                raise ValueError('connector log journal root is not absolute')
            log_name = manifest['log_name']
            if not isinstance(log_name, str) or not log_name:
                raise ValueError('connector log journal has no log name')
            return manifest, root, log_name
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f'cannot recover connector log journal {journal}: {exc}') from exc

    def _journal_stable_paths(self, manifest, root, log_name):
        """Return authenticated path/inode records for journal recovery."""
        records = manifest.get('stable_paths')
        if records is not None:
            # v3 journals carry operation-specific pre/post identity contracts
            # below.  The namespace handoff only needs the names; validating an
            # old stable identity here would reject a destination that was
            # legitimately published before the crash.  Recovery validates
            # each destructive source/target against those contracts before
            # touching it.
            if int(manifest.get('version', 2)) >= 3:
                return [{'path': record.get('path'), 'identity': None}
                        for record in records if isinstance(record, dict)]
            return records
        # Version-2 journals predate stable-path records.  Keep their recovery
        # safe by requiring the original active identity at the original name;
        # a rebound path therefore fails closed instead of being overwritten.
        return [{'path': str(root / log_name),
                 'identity': (list(self._owner.identity)
                              if self._owner.identity else None)}]

    def _lock_journal_stable_paths(self, journal, kind):
        if not journal.exists():
            return None
        manifest, root, log_name = self._journal_manifest(journal, kind)
        return self._journal_stable_paths(manifest, root, log_name)

    def _validate_recovery_states(self, manifest, root, destinations,
                                  sources=(), kind=None):
        """Validate every destructive journal pathname before touching it.

        A journal stable-path lock prevents concurrent writers, but it cannot
        prove that an unlocked pathname still names the inode observed before
        the crash.  Each v3 destination therefore accepts only its durable
        pre-publish identity or the staged inode identity after publish; each
        source accepts its pre-publish identity or an already-absent path.
        Anything else is a rebound and recovery fails closed.
        """
        if int(manifest.get('version', 2)) < 3:
            return
        entry_contract = manifest.get('identity_contract') == 'entry-v1'
        raw_states = manifest.get('path_states')
        if not isinstance(raw_states, list):
            raise RuntimeError('connector log journal has no pathname states')
        states = {}
        for raw in raw_states:
            if not isinstance(raw, dict):
                raise RuntimeError('connector log journal has invalid pathname state')
            try:
                path = self._manifest_path(raw['path'], root)
                identity = self._manifest_identity(raw.get('identity'))
                entry_identity = self._manifest_identity(
                    raw.get('entry_identity'))
                present = bool(raw.get('present', False))
                size = int(raw.get('size', 0))
                digest = str(raw.get('sha256', self._sha256_bytes(b'')))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    'connector log journal has invalid pathname state') from exc
            states[path] = {
                'identity': identity, 'present': present,
                'entry_identity': entry_identity,
                'size': size, 'sha256': digest,
            }

        destination_paths = set()
        kind = kind or manifest.get('kind')
        transaction_nonce = manifest.get('transaction_nonce')
        for index, (destination, temporary, present, expected_size, expected_digest,
                    in_place, before_identity, before_entry_identity,
                    temporary_identity, temporary_entry_identity, before_size,
                    before_digest, before_present) in enumerate(destinations):
            destination_paths.add(destination)
            raw_entries = manifest.get('destinations', [])
            entry = (raw_entries[index] if isinstance(raw_entries, list) and
                     index < len(raw_entries) and
                     isinstance(raw_entries[index], dict) else {})
            state = states.get(destination)
            if state is None:
                raise RuntimeError(
                    f'connector log journal has no state for {destination}')
            if state['identity'] != before_identity:
                raise RuntimeError(
                    f'connector log journal pre-state mismatch for {destination}')
            current_identity = self._owner._identity_for(destination)
            current_entry_identity = self._owner._entry_identity(destination)
            publish_identity = self._manifest_identity(
                entry.get('publish_identity'))
            publish_entry_identity = self._manifest_identity(
                entry.get('publish_entry_identity'))
            allowed = {before_identity}
            if temporary_identity is not None:
                allowed.add(temporary_identity)
            allowed_entries = {before_entry_identity}
            if temporary_entry_identity is not None:
                allowed_entries.add(temporary_entry_identity)
            authenticated_post_delete = (
                not present and before_present and
                entry.get('publish_state') == 'publishing' and
                publish_identity == before_identity and
                publish_identity is not None and
                publish_entry_identity == before_entry_identity and
                current_identity is None and current_entry_identity is None)
            if authenticated_post_delete:
                allowed.add(None)
                allowed_entries.add(None)
            if (current_identity not in allowed or
                    (entry_contract and
                     current_entry_identity not in allowed_entries)):
                raise RuntimeError(
                    f'connector log journal destination was rebound: {destination}')
            if current_identity == temporary_identity and temporary_identity is not None:
                if not self._file_matches_state(
                        destination, expected_size, expected_digest):
                    raise RuntimeError(
                        f'connector log journal destination content changed: {destination}')
            elif current_identity == before_identity:
                if before_identity is None:
                    if destination.exists():
                        raise RuntimeError(
                            f'connector log journal absent destination was rebound: {destination}')
                elif destination.is_file():
                    pre_matches = self._file_matches_state(
                        destination, before_size, before_digest)
                    post_matches = self._file_matches_state(
                        destination, expected_size, expected_digest)
                    publish_identity = self._manifest_identity(
                        entry.get('publish_identity'))
                    publish_entry_identity = self._manifest_identity(
                        entry.get('publish_entry_identity'))
                    interrupted_in_place = (
                        in_place and entry.get('publish_state') == 'publishing' and
                        publish_identity == before_identity and
                        publish_identity is not None and
                        (not entry_contract or
                         publish_entry_identity == before_entry_identity) and
                        temporary is not None and temporary.is_file())
                    if not pre_matches and not (post_matches or interrupted_in_place):
                        raise RuntimeError(
                            f'connector log journal destination content changed: {destination}')
                elif before_present:
                    raise RuntimeError(
                        f'connector log journal destination disappeared: {destination}')

            if temporary is not None and temporary.exists():
                temp_identity = self._owner._identity_for(temporary)
                if (temporary_identity is not None and
                        temp_identity != temporary_identity):
                    raise RuntimeError(
                        f'connector log journal temp was rebound: {temporary}')
                temp_entry_identity = self._owner._entry_identity(temporary)
                if (entry_contract and temporary_entry_identity is not None and
                        temp_entry_identity != temporary_entry_identity):
                    raise RuntimeError(
                        f'connector log journal temp entry was rebound: {temporary}')
                self._read_staged_payload(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind=kind,
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=transaction_nonce)

        for source in sources:
            if source in destination_paths:
                continue
            state = states.get(source)
            if state is None:
                raise RuntimeError(
                    f'connector log journal has no state for {source}')
            current_identity = self._owner._identity_for(source)
            if current_identity not in (state['identity'], None):
                raise RuntimeError(
                    f'connector log journal source was rebound: {source}')
            current_entry_identity = self._owner._entry_identity(source)
            if (entry_contract and current_entry_identity not in
                    (state['entry_identity'], None)):
                raise RuntimeError(
                    f'connector log journal source entry was rebound: {source}')
            if current_identity == state['identity'] and current_identity is not None:
                if not self._file_matches_state(
                        source, state['size'], state['sha256']):
                    raise RuntimeError(
                        f'connector log journal source content changed: {source}')

    def _recover_migration(self):
        """Resume or roll back a prepared generation after an abrupt exit.

        The journal is fsynced before the first publish and contains a digest
        for every new slot.  A destination digest identifies a completed
        ``os.replace`` even when the process died before it could update any
        in-memory state; an uncompleted destination is filled from its staged
        temp.  No source is removed until every new slot is present, making the
        recovery idempotent and chronology-preserving.
        """
        journal = self._migration_manifest_path()
        if not journal.exists():
            # Without a durable journal there is no ownership proof for any
            # staged pathname. Never sweep similarly named user files from
            # the log directory.
            self._cleanup_journal_temp(journal, 'migrate')
            return

        manifest, root, log_name = self._journal_manifest(journal, 'migrate')
        if manifest.get('state') == 'preparing':
            # The intent journal was durable before any replacement.  A hard
            # exit in this phase leaves only the paths recorded in the
            # journal; discard them and leave every historical slot untouched.
            try:
                self._cleanup_preparing_temps(
                    manifest, root, 'migrate', log_name)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f'cannot recover connector log journal {journal}: {exc}') from exc
            journal.unlink()
            self._cleanup_journal_temp(journal, 'migrate')
            self._fsync_directory(root)
            self._fsync_directory(journal.parent)
            return
        try:
            raw_sources = manifest['sources']
            entries = manifest['destinations']
            if not isinstance(raw_sources, list) or not isinstance(entries, list):
                raise ValueError('connector log journal has invalid slot lists')
            slot_names = {log_name}
            slot_names.update(f'{log_name}.{index}'
                              for index in range(1, int(manifest['backup_count']) + 1))
            sources = []
            for raw_source in raw_sources:
                source = self._manifest_path(raw_source, root)
                if source.name not in slot_names:
                    raise ValueError('connector log journal contains an unsafe source')
                sources.append(source)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f'cannot recover connector log journal {journal}: {exc}') from exc

        destinations = []
        for entry in entries:
            try:
                destination = self._manifest_path(entry['destination'], root)
                temporary = self._manifest_path(entry['temporary'], root)
                if destination.name not in slot_names:
                    raise ValueError('unsafe migration destination')
                self._validate_journal_temp(temporary, 'migrate', log_name)
                expected_size = int(entry['size'])
                expected_digest = str(entry['sha256'])
                before_identity = self._manifest_identity(
                    entry.get('before_identity'))
                before_entry_identity = self._manifest_identity(
                    entry.get('before_entry_identity'))
                temporary_identity = self._manifest_identity(
                    entry.get('temporary_identity'))
                temporary_entry_identity = self._manifest_identity(
                    entry.get('temporary_entry_identity'))
                before_size = int(entry.get('before_size', 0))
                before_digest = str(entry.get(
                    'before_sha256', self._sha256_bytes(b'')))
                before_present = bool(entry.get('before_present',
                                                before_identity is not None))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f'cannot recover connector log journal {journal}: bad destination') from exc
            destinations.append((destination, temporary, True, expected_size,
                                 expected_digest, bool(entry.get('in_place', False)),
                                 before_identity, before_entry_identity,
                                 temporary_identity, temporary_entry_identity,
                                 before_size, before_digest, before_present))

        self._validate_recovery_states(manifest, root, destinations, sources)
        destination_paths = {destination for destination, *_ in destinations}
        for index, (destination, temporary, _present, expected_size, expected_digest,
                    in_place, _before_identity, _before_entry_identity,
                    temporary_identity, temporary_entry_identity, _before_size,
                    _before_digest, _before_present) in enumerate(destinations):
            complete = False
            if destination.is_file():
                complete = self._file_matches_state(
                    destination, expected_size, expected_digest)
            if complete:
                if temporary.exists():
                    self._read_staged_payload(
                        temporary, expected_size=expected_size,
                        expected_digest=expected_digest, expected_kind='migrate',
                        expected_slot=index, expected_destination=destination,
                        expected_nonce=manifest.get('transaction_nonce'))
                    self._unlink_if_identity(
                        temporary, temporary_identity, temporary_entry_identity)
                continue
            if not temporary.is_file():
                raise RuntimeError(
                    f'connector log migration generation is incomplete: '
                    f'{destination.name} has no staged replacement')
            if in_place:
                self._validate_and_remember_staged(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind='migrate',
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                try:
                    self._publish_in_place(
                        destination, temporary, _before_identity,
                        _before_entry_identity)
                finally:
                    self._forget_verified_staged(temporary)
            else:
                self._prepare_staged_for_replace(
                    temporary, expected_size, expected_digest,
                    expected_kind='migrate', expected_slot=index,
                    expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                self._revalidate_destination(
                    destination, _before_identity, _before_entry_identity)
                self._revalidate_staged_for_replace(temporary)
                try:
                    os.replace(temporary, destination)
                finally:
                    self._forget_staged_for_replace(temporary)
            self._fsync_directory(root)

        # New slots are complete. It is now safe to discard every source that
        # is not itself one of those slots. Every path is authenticated by the
        # journal and constrained to the original target directory.
        self._validate_recovery_states(manifest, root, destinations, sources)
        for source in sources:
            if source in destination_paths:
                continue
            source_identity, source_entry_identity = (
                self._manifest_path_identity(manifest, source))
            self._unlink_if_identity(
                source, source_identity, source_entry_identity)
        for index, (destination, temporary, _present, expected_size,
                    expected_digest, _in_place, _before_identity,
                    _before_entry_identity, _temporary_identity,
                    _temporary_entry_identity, _before_size, _before_digest,
                    _before_present) in enumerate(destinations):
            if temporary.exists():
                self._read_staged_payload(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind='migrate',
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                self._unlink_if_identity(
                    temporary, _temporary_identity,
                    _temporary_entry_identity)
        journal.unlink()
        self._cleanup_journal_temp(journal, 'migrate')
        self._fsync_directory(root)
        self._fsync_directory(journal.parent)

    def _recover_rotation(self):
        """Resume an interrupted ordinary rotation from its durable journal."""
        journal = self._rotation_manifest_path()
        if not journal.exists():
            # Without a durable journal there is no ownership proof for any
            # staged pathname. Never sweep similarly named user files from
            # the log directory.
            self._cleanup_journal_temp(journal, 'rotate')
            return

        manifest, root, log_name = self._journal_manifest(journal, 'rotate')
        if manifest.get('state') == 'preparing':
            try:
                self._cleanup_preparing_temps(
                    manifest, root, 'rotate', log_name)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f'cannot recover connector log journal {journal}: {exc}') from exc
            journal.unlink()
            self._cleanup_journal_temp(journal, 'rotate')
            self._fsync_directory(root)
            self._fsync_directory(journal.parent)
            return
        try:
            entries = manifest['destinations']
            backup_count = int(manifest['backup_count'])
            if not isinstance(entries, list) or backup_count < 0:
                raise ValueError('connector log journal has invalid rotation slots')
            slot_names = {log_name}
            slot_names.update(f'{log_name}.{index}'
                              for index in range(1, backup_count + 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f'cannot recover connector log journal {journal}: {exc}') from exc

        destinations = []
        for entry in entries:
            try:
                destination = self._manifest_path(entry['destination'], root)
                if destination.name not in slot_names:
                    raise ValueError('unsafe rotation destination')
                present = bool(entry['present'])
                temporary = None
                if present:
                    temporary = self._manifest_path(entry['temporary'], root)
                    self._validate_journal_temp(temporary, 'rotate', log_name)
                    expected_size = int(entry['size'])
                    expected_digest = str(entry['sha256'])
                else:
                    expected_size = 0
                    expected_digest = self._sha256_bytes(b'')
                before_identity = self._manifest_identity(
                    entry.get('before_identity'))
                before_entry_identity = self._manifest_identity(
                    entry.get('before_entry_identity'))
                temporary_identity = self._manifest_identity(
                    entry.get('temporary_identity'))
                temporary_entry_identity = self._manifest_identity(
                    entry.get('temporary_entry_identity'))
                before_size = int(entry.get('before_size', 0))
                before_digest = str(entry.get(
                    'before_sha256', self._sha256_bytes(b'')))
                before_present = bool(entry.get('before_present',
                                                before_identity is not None))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f'cannot recover connector log journal {journal}: bad rotation') from exc
            destinations.append((destination, temporary, present, expected_size,
                                 expected_digest, bool(entry.get('in_place', False)),
                                 before_identity, before_entry_identity,
                                 temporary_identity, temporary_entry_identity,
                                 before_size, before_digest, before_present))

        self._validate_recovery_states(manifest, root, destinations)
        for index, (destination, temporary, present, expected_size, expected_digest,
                    in_place, _before_identity, _before_entry_identity,
                    temporary_identity, temporary_entry_identity, _before_size,
                    _before_digest, _before_present) in enumerate(destinations):
            if not present:
                if temporary is not None and temporary.exists():
                    self._read_staged_payload(
                        temporary, expected_kind='rotate', expected_slot=index,
                        expected_destination=destination,
                        expected_nonce=manifest.get('transaction_nonce'))
                    self._unlink_if_identity(
                        temporary, temporary_identity, temporary_entry_identity)
                destination_identity, destination_entry_identity = (
                    self._manifest_path_identity(manifest, destination))
                self._unlink_if_identity(
                    destination, destination_identity, destination_entry_identity)
                self._fsync_directory(root)
                continue

            complete = False
            if destination.is_file():
                complete = self._file_matches_state(
                    destination, expected_size, expected_digest)
            if complete:
                if temporary.exists():
                    self._read_staged_payload(
                        temporary, expected_size=expected_size,
                        expected_digest=expected_digest,
                        expected_kind='rotate', expected_slot=index,
                        expected_destination=destination,
                        expected_nonce=manifest.get('transaction_nonce'))
                    self._unlink_if_identity(
                        temporary, temporary_identity, temporary_entry_identity)
                continue
            if not temporary.is_file():
                raise RuntimeError(
                    f'connector log rotation is incomplete: '
                    f'{destination.name} has no staged replacement')
            if in_place:
                self._validate_and_remember_staged(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind='rotate',
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                try:
                    self._publish_in_place(
                        destination, temporary, _before_identity,
                        _before_entry_identity)
                finally:
                    self._forget_verified_staged(temporary)
            else:
                self._prepare_staged_for_replace(
                    temporary, expected_size, expected_digest,
                    expected_kind='rotate', expected_slot=index,
                    expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                self._revalidate_destination(
                    destination, _before_identity, _before_entry_identity)
                self._revalidate_staged_for_replace(temporary)
                try:
                    os.replace(temporary, destination)
                finally:
                    self._forget_staged_for_replace(temporary)
            self._fsync_directory(root)

        self._validate_recovery_states(manifest, root, destinations)
        for index, (destination, temporary, _present, expected_size,
                    expected_digest, _in_place, _before_identity,
                    _before_entry_identity, _temporary_identity,
                    _temporary_entry_identity, _before_size, _before_digest,
                    _before_present) in enumerate(destinations):
            if temporary is not None and temporary.exists():
                self._read_staged_payload(
                    temporary, expected_size=expected_size,
                    expected_digest=expected_digest, expected_kind='rotate',
                    expected_slot=index, expected_destination=destination,
                    expected_nonce=manifest.get('transaction_nonce'))
                self._unlink_if_identity(
                    temporary, _temporary_identity,
                    _temporary_entry_identity)
        journal.unlink()
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(root)
        self._fsync_directory(journal.parent)

    @staticmethod
    def _bounded_byte_chunks(raw, limit):
        """Yield valid UTF-8 chunks, replacing malformed history if possible."""
        limit = int(limit)
        if limit < 1:
            raise ValueError('chunk limit must be positive')
        # Replacement is deliberate for historical bytes that predate this
        # writer.  New records never reach this helper.  If the configured cap
        # cannot hold U+FFFD (or any other one code point), fail cleanly rather
        # than emitting an invalid UTF-8 fragment.
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

    @staticmethod
    def _iter_historical_chunks(path, limit):
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

    def _migrate_existing(self):
        self._owner.revalidate()
        journal_paths = []
        journal_present = False
        for journal, kind in (
                (self._rotation_manifest_path(), 'rotate'),
                (self._migration_manifest_path(), 'migrate')):
            # The journal pathname may already be published when cleanup is
            # interrupted.  Sweep its authenticated fixed proof/stage pair
            # before deciding whether there is a journal to recover; otherwise
            # a bounded orphan survives every normal open that needs no new
            # rotation or migration.
            self._recover_named_claim(
                journal, auth_key=self._staging_key_bytes)
            records = self._lock_journal_stable_paths(journal, kind)
            if records is not None:
                journal_present = True
                journal_paths.extend(records)
        if journal_present:
            self._owner.reacquire_stable_paths(
                journal_paths, revalidate_identities=True)
        self._recover_rotation()
        self._recover_migration()
        backups = self._numeric_backups()
        obsolete_backup_states = {
            old: self._journal_file_state(old)
            for index, old in backups.items()
            if index > self._backup_count
        }
        if self._window_is_bounded(backups):
            # A count decrease should not rewrite already-bounded active or
            # retained files. Remove only the obsolete numeric names, and let
            # any deletion error fail startup visibly.
            for index, old in backups.items():
                if 1 <= index <= self._backup_count:
                    continue
                state = obsolete_backup_states[old]
                removed = self._unlink_if_identity(
                    old, state['identity'], state['entry_identity'])
                if (not removed and
                        os.path.lexists(str(old))):
                    raise RuntimeError(
                        f'connector log obsolete backup was rebound: {old}')
            return

        sources = [backups[index] for index in sorted(backups, reverse=True)]
        if self._path.exists():
            sources.append(self._path)
        elif not sources:
            return

        slots = [self._backup_path(i)
                 for i in range(self._backup_count, 0, -1)] + [self._path]
        # Select the newest complete lines/chunks that fit the *whole* window
        # before packing them into files.  Dropping already-packed oldest files
        # can discard a newer record that would have fit beside later records.
        capacity = len(slots) * self._max_bytes
        selected = deque()
        selected_bytes = 0
        for source in sources:
            if not source.is_file():
                continue
            for chunk in self._iter_historical_chunks(source, self._max_bytes):
                selected.append(chunk)
                selected_bytes += len(chunk)
                while selected and selected_bytes > capacity:
                    selected_bytes -= len(selected.popleft())

        packed = []
        current = bytearray()
        for chunk in selected:
            if current and len(current) + len(chunk) > self._max_bytes:
                packed.append(bytes(current))
                current = bytearray()
            current.extend(chunk)
        if current or not packed:
            packed.append(bytes(current))

        kept = packed[-len(slots):]
        destinations = slots[-len(kept):]

        mode = None
        for candidate in (self._path,) + tuple(sources):
            try:
                mode = os.stat(candidate).st_mode & 0o7777
                break
            except OSError:
                continue

        in_place_destinations = {}
        for destination in destinations:
            # Only the active inode is held by the ownership lock. A hard
            # link at a retained backup is an external alias and must receive
            # an atomic pathname replacement, never an in-place mutation.
            in_place_destinations[destination.name] = (
                destination == self._path and
                destination.is_file())

        path_state_map = {}
        for candidate in tuple(sources) + tuple(destinations):
            path_state_map.setdefault(candidate, self._journal_file_state(candidate))

        # Establish durable transaction ownership before creating any staged
        # file.  Same-directory temps make every pathname publish atomic, and
        # the fixed journal's O_EXCL create bounds repeated hard exits.
        self._transaction_nonce = uuid.uuid4().hex
        self._staging_cleanup_failed = False
        temporary = []
        staged_paths = [
            self._owned_temp_path('migrate', index)
            for index, _chunk in enumerate(kept)
        ]
        manifest = {
            'version': 3,
            'identity_contract': 'entry-v1',
            'kind': 'migrate',
            'token': self._identity_digest,
            'state': 'preparing',
            'root': str(self._path.parent),
            'log_name': self._path.name,
            'backup_count': self._backup_count,
            'generation': uuid.uuid4().hex,
            'transaction_nonce': self._transaction_nonce,
            'sources': [str(source) for source in sources],
            'temporaries': [str(path) for path in staged_paths],
            'planned_temporaries': [str(path) for path in staged_paths],
            'created_temporaries': [],
            'path_states': list(path_state_map.values()),
            'stable_paths': self._owner.manifest_stable_paths(),
            'destinations': [
                {
                    'destination': str(destination),
                    'temporary': str(staged_paths[index]),
                    'size': len(chunk),
                    'sha256': self._sha256_bytes(chunk),
                    'in_place': in_place_destinations.get(
                        destination.name, False),
                    'before_identity': path_state_map[destination]['identity'],
                    'before_entry_identity': path_state_map[destination][
                        'entry_identity'],
                    'before_present': path_state_map[destination]['present'],
                    'before_size': path_state_map[destination]['size'],
                    'before_sha256': path_state_map[destination]['sha256'],
                    'temporary_identity': None,
                    'temporary_entry_identity': None,
                }
                for index, (destination, chunk)
                in enumerate(zip(destinations, kept))
            ],
        }
        journal = self._migration_manifest_path()
        self._write_migration_manifest(manifest, create=True)
        published_count = 0
        try:
            # The intent is already durable.  A hard exit at any point in this
            # loop leaves a bounded, journal-owned set for recovery.
            for index, chunk in enumerate(kept):
                self._staging_kind = 'migrate'
                self._staging_slot = index
                self._staging_destination = destinations[index]

                def claim(staged):
                    entry = manifest['destinations'][index]
                    staged_identity = self._owner._identity_for(staged)
                    entry['temporary_identity'] = (
                        list(staged_identity) if staged_identity is not None
                        else None)
                    staged_entry_identity = self._owner._entry_identity(staged)
                    entry['temporary_entry_identity'] = (
                        list(staged_entry_identity) if staged_entry_identity
                        is not None else None)
                    manifest['created_temporaries'].append(str(staged))
                    self._write_migration_manifest(manifest, kind='migrate')
                self._staging_claim_callback = claim
                try:
                    staged = self._write_migration_temp(
                        chunk, mode, name=staged_paths[index])
                finally:
                    self._staging_claim_callback = None
                temporary.append(staged)
            self._fsync_directory(self._path.parent)
            manifest['state'] = 'prepared'
            self._write_migration_manifest(manifest)
            for index, (destination, source) in enumerate(
                    zip(destinations, temporary)):
                # Mark the generation before the filesystem operation: a
                # wrapper may report failure after the replace/truncate has
                # already committed, and recovery must retain the journal.
                self._mark_destination_publishing(manifest, index, 'migrate')
                published_count += 1
                entry = manifest['destinations'][index]
                if in_place_destinations.get(destination.name, False):
                    self._validate_and_remember_staged(
                        source, expected_size=len(kept[index]),
                        expected_digest=self._sha256_bytes(kept[index]),
                        expected_kind='migrate', expected_slot=index,
                        expected_destination=destination,
                        expected_nonce=self._transaction_nonce)
                    try:
                        self._publish_in_place(
                            destination, source,
                            self._manifest_identity(
                                entry.get('before_identity')),
                            self._manifest_identity(
                                entry.get('before_entry_identity')))
                    finally:
                        self._forget_verified_staged(source)
                else:
                    self._prepare_staged_for_replace(
                        source, len(kept[index]),
                        self._sha256_bytes(kept[index]),
                        expected_kind='migrate', expected_slot=index,
                        expected_destination=destination,
                        expected_nonce=self._transaction_nonce)
                    self._revalidate_destination(
                        destination,
                        self._manifest_identity(entry.get('before_identity')),
                        self._manifest_identity(
                            entry.get('before_entry_identity')))
                    self._revalidate_staged_for_replace(source)
                    try:
                        os.replace(source, destination)
                    finally:
                        self._forget_staged_for_replace(source)
                self._fsync_directory(self._path.parent)
            temporary.clear()
        except BaseException:
            # Before the first publish the current process has exact ownership
            # of every successfully-created temp and can remove those paths;
            # leave a durable journal only once a slot has changed.  A hard
            # exit has no finally block, so restart recovery handles that
            # journal-owned bounded set instead.
            cleanup_ok = not getattr(self, '_staging_cleanup_failed', False)
            if published_count == 0:
                for index, source in enumerate(temporary):
                    entry = manifest['destinations'][index]
                    removed = self._unlink_if_identity(
                        source,
                        self._manifest_identity(
                            entry.get('temporary_identity')),
                        self._manifest_identity(
                            entry.get('temporary_entry_identity')))
                    if (not removed and
                            os.path.lexists(str(source))):
                        cleanup_ok = False
                if cleanup_ok:
                    try:
                        journal.unlink()
                    except FileNotFoundError:
                        pass
                    self._fsync_directory(self._path.parent)
                    self._fsync_directory(journal.parent)
            raise

        # Remove only after all bounded destinations exist.  In particular, a
        # numeric backup from an older, larger count is an observable startup
        # error if it cannot be removed; silently retaining it defeats the
        # byte ceiling.
        keep_paths = set(destinations)
        for old in sources:
            if old in keep_paths:
                continue
            state = path_state_map[old]
            removed = self._unlink_if_identity(
                old, state['identity'], state['entry_identity'])
            if (not removed and
                    os.path.lexists(str(old))):
                raise RuntimeError(
                    f'connector log migration source was rebound: {old}')
        for index, leftover in enumerate(staged_paths):
            entry = next(
                (item for item in manifest['destinations']
                 if isinstance(item, dict) and
                 item.get('temporary') == str(leftover)),
                {})
            self._unlink_if_identity(
                leftover,
                self._manifest_identity(entry.get('temporary_identity')),
                self._manifest_identity(entry.get('temporary_entry_identity')))
        journal = self._migration_manifest_path()
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        self._cleanup_journal_temp(journal, 'migrate')
        self._fsync_directory(self._path.parent)
        self._fsync_directory(journal.parent)

    def _open_active(self, mode=None):
        if not self._path.exists():
            self._path.touch()
        self._fh = open(self._path, 'ab', buffering=0)
        if mode is not None:
            os.chmod(self._path, mode)
        self._fh.seek(0, os.SEEK_END)
        self._active_bytes = self._fh.tell()
        if self._active_bytes > self._max_bytes:
            raise AssertionError('active connector log exceeded byte ceiling after migration')

    def _rotate(self):
        self._use_anonymous_staging = True
        self._owner.revalidate()
        try:
            mode = os.stat(self._path).st_mode & 0o7777
        except OSError:
            mode = None
        destinations = []
        for index in range(self._backup_count, 0, -1):
            source = (self._backup_path(index - 1)
                      if index > 1 else self._path)
            present = source.is_file()
            data = source.read_bytes() if present else b''
            destination = self._backup_path(index)
            # A retained backup is not protected by the active inode lock.
            # Replace its pathname atomically even when it has external hard
            # links; publishing in place would mutate another owner's file.
            in_place = False
            destinations.append((destination, data, present, in_place))
        # The active inode is the final publish and is always updated in place.
        destinations.append((self._path, b'', True, True))

        self._transaction_nonce = uuid.uuid4().hex
        self._staging_cleanup_failed = False
        temporary = []
        staged_paths = [
            self._owned_temp_path('rotate', index)
            for index, (_destination, _data, present, _in_place)
            in enumerate(destinations) if present
        ]
        staged_iter = iter(staged_paths)
        planned_paths = [next(staged_iter) if present else None
                         for _destination, _data, present, _in_place
                         in destinations]
        path_state_map = {
            destination: self._journal_file_state(destination)
            for destination, _data, _present, _in_place in destinations
        }
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()
        manifest = {
            'version': 3,
            'identity_contract': 'entry-v1',
            'kind': 'rotate',
            'token': self._identity_digest,
            'state': 'preparing',
            'root': str(self._path.parent),
            'log_name': self._path.name,
            'backup_count': self._backup_count,
            'generation': uuid.uuid4().hex,
            'transaction_nonce': self._transaction_nonce,
            'temporaries': [str(path) for path in staged_paths],
            'planned_temporaries': [str(path) for path in staged_paths],
            'created_temporaries': [],
            'path_states': list(path_state_map.values()),
            'stable_paths': self._owner.manifest_stable_paths(),
            'destinations': [
                {
                    'destination': str(destination),
                    'temporary': (str(planned_paths[index])
                                  if planned_paths[index] is not None else None),
                    'present': present,
                    'size': len(data),
                    'sha256': self._sha256_bytes(data),
                    'in_place': in_place,
                    'before_identity': path_state_map[destination]['identity'],
                    'before_entry_identity': path_state_map[destination][
                        'entry_identity'],
                    'before_present': path_state_map[destination]['present'],
                    'before_size': path_state_map[destination]['size'],
                    'before_sha256': path_state_map[destination]['sha256'],
                    'temporary_identity': None,
                    'temporary_entry_identity': None,
                }
                for index, (destination, data, present, in_place)
                in enumerate(destinations)
            ],
        }
        journal = self._rotation_manifest_path()
        self._write_migration_manifest(manifest, kind='rotate', create=True)
        published_count = 0
        try:
            # Durable ownership exists before the first stage.  A hard exit
            # therefore leaves only this transaction's fixed, recoverable set.
            for index, (_destination, data, present, _in_place) in enumerate(
                    destinations):
                if not present:
                    temporary.append(None)
                    continue
                self._staging_kind = 'rotate'
                self._staging_slot = index
                self._staging_destination = destinations[index][0]

                def claim(staged):
                    entry = manifest['destinations'][index]
                    staged_identity = self._owner._identity_for(staged)
                    entry['temporary_identity'] = (
                        list(staged_identity) if staged_identity is not None
                        else None)
                    staged_entry_identity = self._owner._entry_identity(staged)
                    entry['temporary_entry_identity'] = (
                        list(staged_entry_identity) if staged_entry_identity
                        is not None else None)
                    manifest['created_temporaries'].append(str(staged))
                    self._write_migration_manifest(manifest, kind='rotate')
                self._staging_claim_callback = claim
                try:
                    staged = self._write_migration_temp(
                        data, mode, name=planned_paths[index])
                finally:
                    self._staging_claim_callback = None
                temporary.append(staged)
            self._fsync_directory(self._path.parent)
            manifest['state'] = 'prepared'
            self._write_migration_manifest(manifest, kind='rotate')
            for index, ((destination, _data, present, in_place), staged) in enumerate(
                    zip(destinations, temporary)):
                self._mark_destination_publishing(manifest, index, 'rotate')
                published_count += 1
                entry = manifest['destinations'][index]
                if present:
                    if in_place:
                        self._validate_and_remember_staged(
                            staged, expected_size=len(_data),
                            expected_digest=self._sha256_bytes(_data),
                            expected_kind='rotate', expected_slot=index,
                            expected_destination=destination,
                            expected_nonce=self._transaction_nonce)
                        try:
                            self._publish_in_place(
                                destination, staged,
                                self._manifest_identity(
                                    entry.get('before_identity')),
                                self._manifest_identity(
                                    entry.get('before_entry_identity')))
                        finally:
                            self._forget_verified_staged(staged)
                    else:
                        self._prepare_staged_for_replace(
                            staged, len(_data), self._sha256_bytes(_data),
                            expected_kind='rotate', expected_slot=index,
                            expected_destination=destination,
                            expected_nonce=self._transaction_nonce)
                        self._revalidate_destination(
                            destination,
                            self._manifest_identity(entry.get('before_identity')),
                            self._manifest_identity(
                                entry.get('before_entry_identity')))
                        self._revalidate_staged_for_replace(staged)
                        try:
                            os.replace(staged, destination)
                        finally:
                            self._forget_staged_for_replace(staged)
                else:
                    removed = self._unlink_if_identity(
                        destination,
                        self._manifest_identity(
                            entry.get('before_identity')),
                        self._manifest_identity(
                            entry.get('before_entry_identity')))
                    if (not removed and
                            os.path.lexists(str(destination))):
                        raise RuntimeError(
                            f'connector log sparse destination was rebound: '
                            f'{destination}')
                self._fsync_directory(self._path.parent)
            temporary.clear()
        except BaseException:
            cleanup_ok = not getattr(self, '_staging_cleanup_failed', False)
            if published_count == 0:
                for index, staged in enumerate(temporary):
                    if staged is not None:
                        entry = manifest['destinations'][index]
                        removed = self._unlink_if_identity(
                            staged,
                            self._manifest_identity(
                                entry.get('temporary_identity')),
                            self._manifest_identity(
                                entry.get('temporary_entry_identity')))
                        if (not removed and
                                os.path.lexists(str(staged))):
                            cleanup_ok = False
                if cleanup_ok:
                    try:
                        journal.unlink()
                    except FileNotFoundError:
                        pass
                    self._fsync_directory(self._path.parent)
                    self._fsync_directory(journal.parent)
            raise
        for index, leftover in enumerate(staged_paths):
            entry = next(
                (item for item in manifest['destinations']
                 if isinstance(item, dict) and
                 item.get('temporary') == str(leftover)),
                {})
            self._unlink_if_identity(
                leftover,
                self._manifest_identity(entry.get('temporary_identity')),
                self._manifest_identity(entry.get('temporary_entry_identity')))
        journal = self._rotation_manifest_path()
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        self._cleanup_journal_temp(journal, 'rotate')
        self._fsync_directory(self._path.parent)
        self._fsync_directory(journal.parent)
        self._owner.refresh_stable_identities()
        self._open_active(mode)
        self._use_anonymous_staging = False

    def _append_record(self, record):
        data = record.encode('utf-8')
        if len(data) > self._max_bytes:
            raise AssertionError('connector log record exceeded byte ceiling')
        if self._active_bytes and self._active_bytes + len(data) > self._max_bytes:
            self._rotate()
        view = memoryview(data)
        while view:
            written = self._fh.write(view)
            if written is None or written <= 0:
                raise OSError('short write while appending connector log record')
            view = view[written:]
        self._fh.flush()
        self._active_bytes += len(data)

    def _records_for_line(self, line):
        text = str(line)
        if text.endswith('\n'):
            text = text[:-1]
            if text.endswith('\r'):
                text = text[:-1]
        # Split embedded newlines before applying the byte budget.  Otherwise
        # a chunk that already ends in ``\n`` receives another terminator and
        # invents a blank physical line at an exact boundary.
        logical_lines = text.split('\n')
        budget = self._max_bytes - 1
        for logical in logical_lines:
            chars = []
            size = 0
            for char in logical:
                encoded = char.encode('utf-8')
                if len(encoded) > budget:
                    raise ValueError('max_bytes is too small for a UTF-8 log character')
                if chars and size + len(encoded) > budget:
                    yield ''.join(chars) + '\n'
                    chars = []
                    size = 0
                chars.append(char)
                size += len(encoded)
            yield ''.join(chars) + '\n'

    def write(self, line):
        with self._thread_lock:
            self._owner.revalidate()
            for record in self._records_for_line(line):
                self._append_record(record)

    def close(self):
        with self._thread_lock:
            fh, self._fh = self._fh, None
            error = None
            if fh is not None:
                try:
                    fh.close()
                except BaseException as exc:
                    error = exc
            try:
                self._owner.close()
            except BaseException as exc:
                if error is None:
                    error = exc
            if error is not None:
                raise error


_EVENT_SEGMENT_RE = re.compile(r'^events\.(\d+)\.jsonl$')
_LEGACY_EVENT_LOG_NAME = 'events.jsonl'


def _event_segment_name(generation):
    """Segment file name for one event-stream generation."""
    return f'events.{int(generation):012d}.jsonl'


def _event_segments(directory):
    """Existing event segments as a generation-ordered ``{gen: path}`` map.

    A pre-segmentation ``events.jsonl`` written by an older connector is
    reported as generation 0, so a current leech attached to an older master
    still sees its stream and still advances cleanly onto the segments a newer
    master starts.
    """
    found = {}
    directory = Path(directory)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        match = _EVENT_SEGMENT_RE.match(entry.name)
        if match is None:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        found[int(match.group(1))] = entry
    legacy = directory / _LEGACY_EVENT_LOG_NAME
    try:
        if legacy.is_file():
            found[0] = legacy
    except OSError:
        pass
    return dict(sorted(found.items()))


class _EventStreamWriter:
    """Append-only, generation-segmented JSON event stream for leeches.

    Retention deletes whole retired segments; no file is ever renamed or
    truncated in place.  That is what keeps a leech's ``(generation, byte
    offset)`` cursor meaningful: renaming ``events.jsonl`` to
    ``events.jsonl.1`` under a reader leaves its offset pointing into a
    different file's bytes, and an in-place truncation makes "the file got
    smaller" the only, ambiguous, reset signal.  With one immutable file per
    generation a reader drains a retired segment to EOF and continues at
    offset 0 of the next one, and a generation that retention removed before
    it was drained is an explicit, reportable gap.

    Bound: ``retain + 1`` segments of ``max_bytes`` each.  A single record
    larger than ``max_bytes`` is still written whole -- a partial JSON line
    must never reach a leech -- so the true per-segment ceiling is
    ``max(max_bytes, one record)``.  A segment a reader still holds open
    (Windows refuses to delete those) is retried on the next rotation, which
    bounds the overshoot at one extra segment instead of letting it grow.
    """

    def __init__(self, directory, max_bytes=EVENT_STREAM_SEGMENT_MAX_BYTES,
                 retain=EVENT_STREAM_RETAINED_SEGMENTS):
        max_bytes = int(max_bytes)
        retain = int(retain)
        if max_bytes < 1:
            raise ValueError('max_bytes must be positive')
        if retain < 0:
            raise ValueError('retain must be non-negative')
        self._dir = Path(os.path.abspath(os.fspath(directory)))
        self._max_bytes = max_bytes
        self._retain = retain
        self._dir.mkdir(parents=True, exist_ok=True)
        import threading
        self._thread_lock = threading.RLock()
        self._fh = None
        self._size = 0
        existing = _event_segments(self._dir)
        self._generation = (max(existing) + 1) if existing else 1
        self._open_segment()
        # One generation per master: a predecessor's segments are dead weight
        # because a leech whose master died exits instead of following the
        # replacement.  This also retires a legacy `events.jsonl`.
        self._prune(retain=0)

    @property
    def generation(self):
        return self._generation

    @property
    def path(self):
        return self._dir / _event_segment_name(self._generation)

    def _open_segment(self):
        """Create this generation's segment; never append to a foreign one."""
        while True:
            path = self._dir / _event_segment_name(self._generation)
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT |
                             os.O_EXCL | getattr(os, 'O_BINARY', 0),
                             0o600)
            except FileExistsError:
                self._generation += 1
                continue
            self._fh = os.fdopen(fd, 'wb', buffering=0)
            self._size = 0
            return

    def _close_segment(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.close()
        except OSError:
            pass

    def _rotate(self):
        self._close_segment()
        self._generation += 1
        self._open_segment()
        self._prune()

    def _prune(self, retain=None):
        retain = self._retain if retain is None else int(retain)
        segments = _event_segments(self._dir)
        retired = [gen for gen in segments if gen < self._generation]
        for generation in retired[:max(0, len(retired) - retain)]:
            try:
                segments[generation].unlink()
            except OSError:
                # A reader may still hold it open (Windows).  The next
                # rotation prunes it again, so the overshoot stays bounded.
                pass

    def write(self, record):
        """Append one complete JSON record, without its trailing newline."""
        data = record.encode('utf-8', 'replace')
        # One record is one line.  0x0A cannot occur inside a multi-byte UTF-8
        # sequence, so this substitution can never split a character (and
        # json.dumps never emits a raw newline in the first place).
        data = data.replace(b'\n', b' ') + b'\n'
        with self._thread_lock:
            if self._fh is None:
                self._open_segment()
            if self._size and self._size + len(data) > self._max_bytes:
                self._rotate()
            self._fh.write(data)
            self._size += len(data)

    def close(self):
        with self._thread_lock:
            self._close_segment()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _EventStreamReader:
    """Leech-side cursor over `_EventStreamWriter`'s segments.

    The cursor is ``(generation, byte offset)`` and only moves forward.  Every
    reset is driven by which generation files exist -- never by "the file got
    smaller": a retired segment is drained to EOF and the cursor then starts
    at offset 0 of the next generation, while a generation retention deleted
    before it was drained is reported once as an explicit gap.  Only bytes up
    to the last newline are published, so a record still being written is
    never exposed as a partial JSON line, and because 0x0A cannot appear
    inside a multi-byte UTF-8 sequence, splitting on it never splits a
    character.
    """

    def __init__(self, directory, generation=None, offset=0, on_gap=None):
        self._dir = Path(os.path.abspath(os.fspath(directory)))
        self._generation = generation
        self._offset = int(offset)
        self._on_gap = on_gap

    @classmethod
    def attach(cls, directory, on_gap=None):
        """Attach at the live end of the stream: follow, never replay."""
        directory = Path(os.path.abspath(os.fspath(directory)))
        segments = _event_segments(directory)
        if not segments:
            return cls(directory, on_gap=on_gap)
        newest = max(segments)
        try:
            offset = segments[newest].stat().st_size
        except OSError:
            offset = 0
        return cls(directory, generation=newest, offset=offset, on_gap=on_gap)

    @property
    def generation(self):
        return self._generation

    @property
    def offset(self):
        return self._offset

    def _gap(self, message):
        if self._on_gap is not None:
            self._on_gap(message)

    def read(self):
        """Every complete record that appeared since the previous call."""
        lines = []
        segments = _event_segments(self._dir)
        if not segments:
            return lines
        newest = max(segments)
        if self._generation is None:
            # Attached before the master created its first segment: read that
            # segment from its start rather than guessing an offset into it.
            self._generation = min(segments)
            self._offset = 0
        while True:
            if self._generation not in segments:
                ahead = [gen for gen in segments if gen > self._generation]
                if not ahead:
                    break
                target = min(ahead)
                self._gap(f'event generation {self._generation} was pruned '
                          f'before it drained — resuming at generation '
                          f'{target}')
                self._generation, self._offset = target, 0
            path = segments[self._generation]
            try:
                size = path.stat().st_size
            except OSError:
                break
            if size < self._offset:
                # Segments are append-only, so this is corruption rather than
                # a rotation.  Skip forward instead of replaying from zero.
                self._gap(f'event generation {self._generation} shrank to '
                          f'{size} below cursor {self._offset} — skipping '
                          f'to its end')
                self._offset = size
            if size > self._offset:
                try:
                    with open(path, 'rb') as fh:
                        fh.seek(self._offset)
                        chunk = fh.read(size - self._offset)
                except OSError:
                    break
                cut = chunk.rfind(b'\n')
                if cut >= 0:
                    for line in chunk[:cut].split(b'\n'):
                        line = line.strip()
                        if line:
                            lines.append(line)
                    self._offset += cut + 1
            if self._generation < newest:
                if self._offset < size:
                    self._gap(f'discarding a partial trailing record in event '
                              f'generation {self._generation}')
                    self._offset = size
                self._generation += 1
                self._offset = 0
                continue
            break
        return lines


class _SharedLockError(RuntimeError):
    """A cross-process advisory lock could not be taken."""


class _SharedFileLock:
    """Cross-process exclusive lock on a sidecar lock file.

    A bound that several processes enforce on ONE shared file cannot rest on
    an in-process thread lock: the leeches of an identity are separate
    processes.  Acquisition is non-blocking plus retry, so a dead holder's
    OS-released lock is picked up immediately on both POSIX (``flock``) and
    Windows (``msvcrt.locking``); process death releases either one.
    """

    def __init__(self, path, timeout=5.0, poll=0.02):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.timeout = float(timeout)
        self.poll = float(poll)
        self._fh = None
        self._windows = os.name == 'nt'

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fh = open(self.path, 'a+b')
            except OSError as exc:
                raise _SharedLockError(
                    f'cannot open lock file {self.path}: {exc}') from exc
            try:
                self._lock_handle(fh)
            except _SharedLockError:
                try:
                    fh.close()
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self.poll)
                continue
            except BaseException:
                try:
                    fh.close()
                except OSError:
                    pass
                raise
            self._fh = fh
            return

    def _lock_handle(self, fh):
        try:
            if self._windows:
                import msvcrt
                # msvcrt.locking() locks bytes from the current file pointer,
                # so the sidecar needs one byte to lock.
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b'\0')
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as exc:
            raise _SharedLockError(
                f'lock unavailable for {self.path}: {exc}') from exc

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if self._windows:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            # Closing the descriptor releases either OS lock anyway.
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class _LeechLogWriter:
    """Bounded human log shared by every leech process of one identity.

    The size check, the rotation and the append all happen under one
    cross-process lock, so concurrent leeches can neither interleave a partial
    line nor race two rotations of the same file.  No leech keeps the log open
    between lines: Windows refuses to rename a file another process holds
    open, so a persistent handle would make rotation platform-specific.

    Bound: ``backup_count + 1`` files of ``max_bytes`` each.  A single line
    larger than ``max_bytes`` is written whole into a fresh file rather than
    split.  A line is never dropped because the lock was unavailable: a
    platform without either primitive degrades to an unsynchronised append.
    """

    def __init__(self, path, max_bytes=LEECH_LOG_MAX_BYTES,
                 backup_count=LEECH_LOG_BACKUP_COUNT, timeout=5.0):
        max_bytes = int(max_bytes)
        backup_count = int(backup_count)
        if max_bytes < 1:
            raise ValueError('max_bytes must be positive')
        if backup_count < 0:
            raise ValueError('backup_count must be non-negative')
        self._path = Path(os.path.abspath(os.fspath(path)))
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _SharedFileLock(
            self._path.with_name(self._path.name + '.lock'), timeout=timeout)

    @property
    def path(self):
        return self._path

    def _backup_path(self, index):
        return self._path.with_name(f'{self._path.name}.{index}')

    def _current_size(self):
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def _rotate(self):
        """Shift the retained window down; drop whatever falls off the end."""
        if self._backup_count == 0:
            try:
                self._path.unlink()
            except OSError:
                pass
            return
        try:
            self._backup_path(self._backup_count).unlink()
        except OSError:
            pass
        for index in range(self._backup_count - 1, 0, -1):
            try:
                os.replace(self._backup_path(index),
                           self._backup_path(index + 1))
            except OSError:
                pass
        try:
            os.replace(self._path, self._backup_path(1))
        except OSError:
            pass

    def write(self, line):
        data = (line + '\n').encode('utf-8', 'replace')
        locked = True
        try:
            self._lock.acquire()
        except _SharedLockError:
            locked = False
        try:
            size = self._current_size()
            if size and size + len(data) > self._max_bytes:
                self._rotate()
            with open(self._path, 'ab') as fh:
                fh.write(data)
                fh.flush()
        finally:
            if locked:
                self._lock.release()

    def close(self):
        """No handle is held between lines; present for writer symmetry."""
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


__all__ = [name for name in globals() if not name.startswith('__')]
