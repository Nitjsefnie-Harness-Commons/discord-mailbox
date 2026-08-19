#!/usr/bin/env python3
"""Source-only tests for bounded Discord connector human logs (issue #157).

These tests exercise the writer without importing Discord's network client or
starting a connector.  The connector's stdout event stream and the leech log
are intentionally separate concerns and are covered by their existing code.
"""
import os
import sys
import contextlib
import hashlib
import io
import json
import subprocess
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")
TEST_LOCK_ROOT_ENV = "DISCORD_MB_TEST_LOCK_ROOT"


def _mod():
    return _util.load(MB, "mb_connector_log_rotation")


def _fixture_lock_root(test):
    """Give this suite and its children a private connector journal root."""
    def run(tmp):
        old_lock_root = os.environ.get(TEST_LOCK_ROOT_ENV)
        private_root = Path(tmp) / ".connector-lock-root"
        os.environ[TEST_LOCK_ROOT_ENV] = str(private_root)
        try:
            return test(tmp)
        finally:
            if old_lock_root is None:
                os.environ.pop(TEST_LOCK_ROOT_ENV, None)
            else:
                os.environ[TEST_LOCK_ROOT_ENV] = old_lock_root

    run.__name__ = test.__name__
    run.__doc__ = test.__doc__
    return run


def test_connector_log_rotates_with_bounded_backups(tmp):
    """Continuous writes keep the active file plus exactly two backups."""
    m = _mod()
    path = Path(tmp) / "custom-connector.log"
    writer = m._ConnectorLogWriter(path, max_bytes=96, backup_count=2)
    try:
        for i in range(80):
            writer.write(f"scheduled status update {i:03d}")
    finally:
        writer.close()

    files = sorted(path.parent.glob("custom-connector.log*"))
    assert [p.name for p in files] == [
        "custom-connector.log", "custom-connector.log.1", "custom-connector.log.2"
    ], files
    assert all(p.stat().st_size <= 96 for p in files), [
        (p.name, p.stat().st_size) for p in files
    ]
    joined = "\n".join(p.read_text(encoding="utf-8") for p in files)
    assert "scheduled status update 079" in joined


def test_connector_log_is_line_buffered_and_reopens_after_rollover(tmp):
    """A write is visible immediately, including after a close/reopen cycle."""
    m = _mod()
    path = Path(tmp) / "connector.log"
    writer = m._ConnectorLogWriter(path, max_bytes=48, backup_count=2)
    writer.write("first human log line")
    assert path.read_text(encoding="utf-8") == "first human log line\n"
    writer.write("second human log line forces rollover")
    writer.write("third human log line after rollover")
    writer.close()

    contents = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(path.parent.glob("connector.log*"))
    )
    assert "third human log line after rollover" in contents
    assert "first human log line" in contents


def test_connector_log_byte_ceiling_is_utf8_bytes_and_chunks_long_lines(tmp):
    """UTF-8 records and oversized logical lines never exceed the byte cap."""
    m = _mod()
    path = Path(tmp) / "utf8.log"
    limit = 17
    writer = m._ConnectorLogWriter(path, max_bytes=limit, backup_count=3)
    try:
        writer.write("é" * 40)
        writer.write("🙂" * 20)
    finally:
        writer.close()

    files = sorted(path.parent.glob("utf8.log*"))
    assert [p.name for p in files] == [
        "utf8.log", "utf8.log.1", "utf8.log.2", "utf8.log.3"
    ], files
    for file in files:
        raw = file.read_bytes()
        assert len(raw) <= limit, (file.name, len(raw))
        assert raw.decode("utf-8")
        assert all(len(line.encode("utf-8")) <= limit
                   for line in file.read_text(encoding="utf-8").splitlines())


def test_connector_log_migrates_preexisting_oversized_active_file(tmp):
    """Opening a historical oversized active log bounds it before any write."""
    m = _mod()
    path = Path(tmp) / "historical.log"
    historical = ("pré-existing status update\n" * 20).encode("utf-8")
    path.write_bytes(historical)

    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
    try:
        writer.write("new status")
    finally:
        writer.close()

    files = sorted(path.parent.glob("historical.log*"))
    assert [p.name for p in files] == [
        "historical.log", "historical.log.1", "historical.log.2"
    ], files
    assert all(p.stat().st_size <= 32 for p in files), [
        (p.name, p.stat().st_size) for p in files
    ]
    assert "new status" in path.read_text(encoding="utf-8")
    assert any("pré-existing" in p.read_text(encoding="utf-8") for p in files)


def test_connector_log_migration_streams_history_without_whole_file_reads(tmp):
    """Migration keeps chronology while reading history in bounded chunks."""
    m = _mod()
    path = Path(tmp) / "streaming-history.log"
    backup = path.with_name("streaming-history.log.1")
    backup.write_bytes(b"OLD0000\nOLD0001\n")
    path.write_bytes(b"NEW0000\nNEW0001\nNEW0002\n")
    original_read_bytes = Path.read_bytes
    guarded = {path.resolve(), backup.resolve()}

    def reject_whole_history(candidate):
        if Path(candidate).resolve() in guarded:
            raise AssertionError("historical source was read as one byte string")
        return original_read_bytes(candidate)

    Path.read_bytes = reject_whole_history
    try:
        writer = m._ConnectorLogWriter(path, max_bytes=16, backup_count=1)
        writer.close()
    finally:
        Path.read_bytes = original_read_bytes

    assert backup.read_bytes() == b"OLD0001\nNEW0000\n"
    assert path.read_bytes() == b"NEW0001\nNEW0002\n"
    assert all(item.stat().st_size <= 16 for item in (backup, path))


def test_connector_log_bounded_window_check_streams_instead_of_reading_whole_files(tmp):
    """Startup validates an already-bounded window without materializing it.

    The bounded-window fast path runs on every start and only has to answer
    "is this valid UTF-8 within the ceiling", so it must not pull a whole
    ceiling-sized file (10 MiB in production) into memory to do it.
    """
    m = _mod()
    path = Path(tmp) / "bounded-window.log"
    backup = path.with_name("bounded-window.log.1")
    backup.write_bytes("héritage\n".encode("utf-8"))
    path.write_bytes("courant\n".encode("utf-8"))
    original_read_bytes = Path.read_bytes
    guarded = {path.resolve(), backup.resolve()}

    def reject_whole_window(candidate):
        if Path(candidate).resolve() in guarded:
            raise AssertionError("bounded window was read as one byte string")
        return original_read_bytes(candidate)

    Path.read_bytes = reject_whole_window
    try:
        writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        writer.close()
    finally:
        Path.read_bytes = original_read_bytes

    # An untouched window proves the bounded fast path really ran.
    assert path.read_bytes() == "courant\n".encode("utf-8")
    assert backup.read_bytes() == "héritage\n".encode("utf-8")


def test_connector_log_bounded_window_check_rejects_malformed_utf8(tmp):
    """A bounded but malformed file still goes through UTF-8-safe migration."""
    m = _mod()
    path = Path(tmp) / "malformed-window.log"
    path.write_bytes(b"good\n\xff\xfe bad\n")
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer.close()
    assert b"\xff\xfe" not in path.read_bytes()
    path.read_bytes().decode("utf-8")


def test_connector_log_bounded_window_check_rejects_truncated_utf8_tail(tmp):
    """A file ending mid-sequence is malformed even though each block decodes."""
    m = _mod()
    path = Path(tmp) / "truncated-window.log"
    path.write_bytes("ok\n".encode("utf-8") + "é".encode("utf-8")[:1])
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer.close()
    path.read_bytes().decode("utf-8")


def test_connector_log_in_place_publish_requires_a_verified_staged_payload(tmp):
    """The primitive that mutates the live inode never takes unverified bytes.

    Every current caller validates the staged envelope immediately beforehand,
    so this is the fail-open default that a future caller would inherit.  It
    has to fail closed like every other path in this design.
    """
    m = _mod()
    case = Path(tmp) / "in-place-unverified"
    case.mkdir()
    destination = case / "active.log"
    destination.write_bytes(b"LIVE")
    temporary = case / ".active.log.staged.tmp"
    temporary.write_bytes(b"UNVERIFIED")
    try:
        m._ConnectorLogWriter._publish_in_place(destination, temporary)
    except RuntimeError as exc:
        assert "verified staged payload" in str(exc), str(exc)
    else:
        raise AssertionError("an unverified staged payload was published")
    assert destination.read_bytes() == b"LIVE"
    assert temporary.read_bytes() == b"UNVERIFIED"


def test_connector_log_append_retries_short_writes(tmp):
    """A short file write is completed before accounting the record."""
    m = _mod()
    path = Path(tmp) / "short-write.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    real_handle = writer._fh
    written = bytearray()

    class ShortWriteHandle:
        def write(self, data):
            count = min(2, len(data))
            written.extend(bytes(data[:count]))
            return count

        def flush(self):
            return None

        def close(self):
            return None

    writer._fh = ShortWriteHandle()
    try:
        writer._append_record("abc")
        assert bytes(written) == b"abc"
        assert writer._active_bytes == 3
    finally:
        writer._fh = real_handle
        writer.close()


def test_connector_log_append_rejects_zero_progress_write(tmp):
    """A zero-byte file write fails without advancing the byte counter."""
    m = _mod()
    path = Path(tmp) / "zero-write.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    real_handle = writer._fh

    class ZeroWriteHandle:
        def write(self, data):
            return 0

        def flush(self):
            raise AssertionError("zero-progress write was flushed")

        def close(self):
            return None

    writer._fh = ZeroWriteHandle()
    try:
        try:
            writer._append_record("abc")
        except OSError as exc:
            assert "short write" in str(exc)
        else:
            raise AssertionError("zero-progress write was accepted")
        assert writer._active_bytes == 0
    finally:
        writer._fh = real_handle
        writer.close()


def test_connector_log_numeric_backups_require_canonical_positive_suffixes(tmp):
    """Only canonical positive backup suffixes participate in rotation."""
    m = _mod()
    path = Path(tmp) / "canonical-suffix.log"
    path.write_bytes(b"active\n")
    suffix_zero = path.with_name("canonical-suffix.log.0")
    suffix_padded = path.with_name("canonical-suffix.log.01")
    retained = path.with_name("canonical-suffix.log.1")
    oversized = path.with_name("canonical-suffix.log.3")
    suffix_zero.write_bytes(b"ZERO\n")
    suffix_padded.write_bytes(b"PADDED\n")
    retained.write_bytes(b"RETAINED\n")
    oversized.write_bytes(b"REMOVE\n")

    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer.close()

    assert suffix_zero.read_bytes() == b"ZERO\n"
    assert suffix_padded.read_bytes() == b"PADDED\n"
    assert retained.read_bytes() == b"RETAINED\n"
    assert not oversized.exists()


def test_connector_log_migration_keeps_backup_chronology(tmp):
    """Historical .N -> .1 ordering is retained, newest content stays active."""
    m = _mod()
    path = Path(tmp) / "chronological.log"
    path.write_text("active-newest\n", encoding="utf-8")
    path.with_name("chronological.log.1").write_text(
        "backup-newer\n", encoding="utf-8")
    path.with_name("chronological.log.2").write_text(
        "backup-oldest\n", encoding="utf-8")

    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
    writer.write("next-status-line-forces")
    writer.close()

    assert path.read_text(encoding="utf-8") == "next-status-line-forces\n"
    assert path.with_name("chronological.log.1").read_text(encoding="utf-8") == \
        "active-newest\n"
    assert path.with_name("chronological.log.2").read_text(encoding="utf-8") == \
        "backup-newer\n"


def test_connector_log_migration_packs_the_full_bounded_window(tmp):
    """Migration packs multiple historical records into every bounded slot."""
    m = _mod()
    path = Path(tmp) / "packed.log"
    lines = [f"{i:02d}\n" for i in range(12)]
    path.write_text("".join(lines), encoding="utf-8")

    writer = m._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
    writer.close()

    files = [path.with_name(f"packed.log.{i}") for i in (2, 1)] + [path]
    assert "".join(p.read_text(encoding="utf-8") for p in files) == "".join(lines)
    assert all(p.stat().st_size <= 16 for p in files)


def test_connector_log_migration_crash_keeps_recoverable_sources(tmp):
    """A crash at the first atomic publish leaves a recoverable history set."""
    m = _mod()
    del m
    path = Path(tmp) / "crash.log"
    lines = [f"{i:02d}\n" for i in range(12)]
    path.write_text("".join(lines), encoding="utf-8")
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
def crash_once(*args):
    os.replace = real_replace
    os._exit(73)
os.replace = crash_once
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    proc = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        capture_output=True,
    )
    assert proc.returncode == 73, proc.stderr.decode("utf-8", "replace")
    visible = []
    for candidate in path.parent.iterdir():
        if candidate.name == path.name or candidate.name.startswith(path.name + "."):
            if candidate.is_file():
                visible.append(candidate.read_bytes())
    assert visible, "the crash removed every source and replacement"
    assert b"00\n" in b"".join(visible)
    assert b"11\n" in b"".join(visible)


def test_connector_log_migration_recovers_after_every_real_publish(tmp):
    """Every post-replace crash recovers one generation without duplicates."""
    m = _mod()
    del m
    lines = [f"{i:02d}\n" for i in range(12)]
    original = "".join(lines).encode("utf-8")
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_crash_generation", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
mode = sys.argv[3]
threshold = int(sys.argv[4])
def publish_then_maybe_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if mode == "crash" and calls[0] == threshold:
        os._exit(100 + calls[0])
os.replace = publish_then_maybe_crash
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
writer.close()
if mode == "count":
    print(calls[0], flush=True)
'''

    probe = Path(tmp) / "publish-count"
    probe.write_bytes(original)
    counted = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(probe), "count", "999"],
        capture_output=True, text=True,
    )
    assert counted.returncode == 0, counted.stderr
    publish_count = int(counted.stdout.strip())
    assert publish_count >= 2, publish_count

    for crash_number in range(1, publish_count + 1):
        case = Path(tmp) / f"publish-crash-{crash_number}"
        path = case / "generation.log"
        case.mkdir()
        path.write_bytes(original)
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             "crash", str(crash_number)],
            capture_output=True,
        )
        assert crashed.returncode == 100 + crash_number, (
            crash_number, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )

        restarted = _mod()
        writer = restarted._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
        writer.close()
        slots = [path.with_name("generation.log.2"),
                 path.with_name("generation.log.1"), path]
        recovered = b"".join(slot.read_bytes() for slot in slots if slot.exists())
        assert recovered == original, (crash_number, recovered)
        assert all(len(record) == 3 for record in recovered.splitlines(True))
        hidden = [
            item.name for item in case.iterdir()
            if item.name.startswith(".")
            and not item.name.endswith(".lock")
        ]
        assert hidden == [], (crash_number, hidden)


def test_connector_log_migration_recovers_partial_in_place_publish(tmp):
    """A torn active-inode copy resumes only from its durable staged temp."""
    m = _mod()
    del m
    case = Path(tmp) / "partial-in-place"
    case.mkdir()
    path = case / "partial.log"
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    path.write_bytes(original)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_partial_in_place", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def partial_publish(destination, temporary, *args):
    raw = temporary.read_bytes()
    data, _ = mod._ConnectorLogWriter._staged_payload(raw)
    with open(destination, "r+b", buffering=0) as active:
        active.truncate(0)
        partial = data[:max(1, len(data) // 2)]
        os.write(active.fileno(), partial)
        os.fsync(active.fileno())
    os._exit(176)
mod._ConnectorLogWriter._publish_in_place = staticmethod(partial_publish)
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        capture_output=True,
    )
    assert crashed.returncode == 176, crashed.stderr.decode("utf-8", "replace")

    lock_root = Path(os.environ[TEST_LOCK_ROOT_ENV])
    journals = list(lock_root.glob("*.migrate.json"))
    assert len(journals) == 1, [item.name for item in journals]
    manifest = json.loads(journals[0].read_text(encoding="utf-8").splitlines()[-1])
    active = next(entry for entry in manifest["destinations"]
                  if entry["destination"] == str(path))
    assert active["publish_state"] == "publishing"
    staged = Path(active["temporary"])
    assert staged.is_file()
    staged_payload, _ = _mod()._ConnectorLogWriter._staged_payload(
        staged.read_bytes())
    assert path.read_bytes() != staged_payload, "partial-write crash was vacuous"

    restarted_mod = _mod()
    writer = restarted_mod._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
    writer.close()
    slots = [path.with_name("partial.log.2"),
             path.with_name("partial.log.1"), path]
    assert b"".join(slot.read_bytes() for slot in slots if slot.exists()) == original
    assert [
        item.name for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ] == []


def test_connector_log_rotation_recovers_after_every_publish_and_truncate(tmp):
    """Every durable rotation boundary can restart to one exact history."""
    m = _mod()
    del m
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_rotation_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mode = sys.argv[3]
threshold = int(sys.argv[4])
real_replace = os.replace
calls = [0]
def publish_then_maybe_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if mode == "replace" and calls[0] == threshold:
        os._exit(130 + calls[0])
os.replace = publish_then_maybe_crash
if mode == "truncate":
    real_publish = mod._ConnectorLogWriter._publish_in_place
    def publish_then_crash(destination, temporary, *args):
        real_publish(destination, temporary, *args)
        os._exit(180)
    mod._ConnectorLogWriter._publish_in_place = staticmethod(publish_then_crash)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=8, backup_count=2)
writer.write("ccccccc")
writer.close()
if mode == "count":
    print(calls[0], flush=True)
'''

    def seed(case):
        case.mkdir()
        path = case / "rotation.log"
        path.write_bytes(b"aaaaaaa\n")
        path.with_name("rotation.log.1").write_bytes(b"bbbbbbb\n")
        return path

    probe = Path(tmp) / "rotation-publish-count"
    path = seed(probe)
    counted = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path), "count", "999"],
        capture_output=True, text=True,
    )
    assert counted.returncode == 0, counted.stderr
    publish_count = int(counted.stdout.strip())
    assert publish_count == 2, publish_count

    old_active = b"aaaaaaa\n"
    old_backup = b"bbbbbbb\n"
    for crash_number in range(1, publish_count + 1):
        case = Path(tmp) / f"rotation-publish-crash-{crash_number}"
        path = seed(case)
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             "replace", str(crash_number)],
            capture_output=True,
        )
        assert crashed.returncode == 130 + crash_number, (
            crash_number, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )

        restarted = _mod()
        writer = restarted._ConnectorLogWriter(path, max_bytes=8, backup_count=2)
        writer.close()
        expected = {
            path.with_name("rotation.log.2"): old_backup,
            path.with_name("rotation.log.1"): old_active,
            path: b"",
        }
        assert {slot: slot.read_bytes() for slot in expected} == expected
        assert [
            item.name for item in case.iterdir()
            if item.name.startswith(".") and not item.name.endswith(".lock")
        ] == []

    case = Path(tmp) / "rotation-truncate-crash"
    path = seed(case)
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path),
         "truncate", "0"],
        capture_output=True,
    )
    assert crashed.returncode == 180, crashed.stderr.decode("utf-8", "replace")
    restarted = _mod()
    writer = restarted._ConnectorLogWriter(path, max_bytes=8, backup_count=2)
    writer.close()
    assert path.read_bytes() == b""
    assert path.with_name("rotation.log.1").read_bytes() == old_active
    assert path.with_name("rotation.log.2").read_bytes() == old_backup
    assert [
        item.name for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ] == []


def test_connector_log_migration_crash_loop_has_bounded_hidden_artifacts(tmp):
    """Repeated hard crashes do not accumulate migration temps unboundedly."""
    m = _mod()
    del m
    case = Path(tmp) / "crash-loop"
    case.mkdir()
    path = case / "loop.log"
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    path.write_bytes(original)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_crash_loop", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
def publish_then_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if calls[0] == 1:
        os._exit(119)
os.replace = publish_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    outcomes = []
    for _ in range(8):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path)],
            capture_output=True,
        )
        assert crashed.returncode in (0, 119), crashed.stderr.decode("utf-8", "replace")
        outcomes.append(crashed.returncode)
        if crashed.returncode == 0:
            break
    assert outcomes.count(119) >= 2, outcomes

    hidden_before_restart = [
        item for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ]
    # Three staged slot temps plus one durable journal are the fixed on-disk
    # budget; a crash loop must not grow this list.
    assert len(hidden_before_restart) <= (2 + 1) + 1, [
        item.name for item in hidden_before_restart
    ]

    restarted = _mod()
    writer = restarted._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
    writer.close()
    slots = [path.with_name("loop.log.2"), path.with_name("loop.log.1"), path]
    assert b"".join(slot.read_bytes() for slot in slots if slot.exists()) == original
    assert [
        item.name for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ] == []


def test_connector_log_migration_uses_validated_payload_after_temp_rebound(tmp):
    """A staged pathname swap cannot publish foreign bytes into the active inode."""
    m = _mod()
    path = Path(tmp) / "publish-rebound.log"
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    path.write_bytes(original)

    real_read = m._ConnectorLogWriter._read_staged_payload
    swapped = {"done": False}
    rebound = {"path": None}

    def validate_then_replace(self, temporary, *args, **kwargs):
        payload = real_read(self, temporary, *args, **kwargs)
        destination = kwargs.get("expected_destination")
        if (not swapped["done"] and destination is not None and
                Path(destination) == path):
            swapped["done"] = True
            replacement = Path(temporary).with_name(
                Path(temporary).name + ".foreign")
            replacement.write_bytes(b"FOREIGN-PUBLISH")
            os.replace(replacement, temporary)
            rebound["path"] = Path(temporary)
            assert temporary.read_bytes() == b"FOREIGN-PUBLISH"
        return payload
    m._ConnectorLogWriter._read_staged_payload = validate_then_replace
    try:
        writer = m._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
        writer.close()
    finally:
        m._ConnectorLogWriter._read_staged_payload = real_read

    assert swapped["done"]
    slots = [path.with_name("publish-rebound.log.2"),
             path.with_name("publish-rebound.log.1"), path]
    retained = b"".join(slot.read_bytes() for slot in slots if slot.exists())
    assert retained == original
    assert b"FOREIGN-PUBLISH" not in retained
    assert rebound["path"].read_bytes() == b"FOREIGN-PUBLISH"


def test_connector_log_migration_refuses_live_atomic_destination_swap(tmp):
    """Migration never replaces a backup rebound after staging validation."""
    m = _mod()
    path = Path(tmp) / "migration-live-atomic.log"
    path.write_bytes(b"AAAAAAA\nBBBBBBB\nCCCCCCC\n")
    destination = path.with_name("migration-live-atomic.log.1")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    original_prepare = m._ConnectorLogWriter._prepare_staged_for_replace
    swapped = {"done": False}

    def prepare_then_swap(self, temporary, *args, **kwargs):
        result = original_prepare(self, temporary, *args, **kwargs)
        if (not swapped["done"] and
                Path(kwargs["expected_destination"]) == destination):
            swapped["done"] = True
            replacement = destination.with_name(destination.name + ".foreign")
            replacement.write_bytes(b"LIVE-CONCURRENT\n")
            os.replace(replacement, destination)
        return result

    m._ConnectorLogWriter._prepare_staged_for_replace = prepare_then_swap
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=8, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("migration replaced a rebound atomic destination")
    finally:
        m._ConnectorLogWriter._prepare_staged_for_replace = original_prepare
    assert swapped["done"]
    assert destination.read_bytes() == b"LIVE-CONCURRENT\n"
    assert list(lock_root.glob("*.migrate.json")), "migration journal was discarded"


def test_connector_log_atomic_stage_swap_never_truncates_external_hardlink(tmp):
    """A staged atomic slot swap cannot mutate an unrelated hard-linked inode."""
    m = _mod()
    path = Path(tmp) / "migration-stage-hardlink.log"
    path.write_bytes(b"AAAAAAA\nBBBBBBB\nCCCCCCC\n")
    destination = path.with_name("migration-stage-hardlink.log.1")
    external = Path(tmp) / "external-stage-owner.log"
    external.write_bytes(b"EXTERNAL-STAGE-KEEP\n")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    real_read = m._ConnectorLogWriter._read_staged_payload
    swapped = {"done": False}

    def validate_then_hardlink(self, temporary, *args, **kwargs):
        payload = real_read(self, temporary, *args, **kwargs)
        if (not swapped["done"] and
                Path(kwargs["expected_destination"]) == destination):
            swapped["done"] = True
            replacement = Path(temporary).with_name(
                Path(temporary).name + ".foreign")
            os.link(external, replacement)
            os.replace(replacement, temporary)
        return payload

    m._ConnectorLogWriter._read_staged_payload = validate_then_hardlink
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=8, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("staged hard-link swap was silently accepted")
    finally:
        m._ConnectorLogWriter._read_staged_payload = real_read

    assert swapped["done"]
    assert external.read_bytes() == b"EXTERNAL-STAGE-KEEP\n"
    assert list(lock_root.glob("*.migrate.json")), "migration journal was discarded"


def test_connector_log_atomic_stage_swap_after_prepare_keeps_journal(tmp):
    """A staged swap after preparation is refused before atomic publication."""
    m = _mod()
    path = Path(tmp) / "migration-stage-after-prepare.log"
    path.write_bytes(b"AAAAAAA\nBBBBBBB\nCCCCCCC\n")
    destination = path.with_name("migration-stage-after-prepare.log.1")
    external = Path(tmp) / "external-after-prepare.log"
    external.write_bytes(b"EXTERNAL-AFTER-PREPARE\n")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    real_prepare = m._ConnectorLogWriter._prepare_staged_for_replace
    swapped = {"done": False}

    def prepare_then_hardlink(self, temporary, *args, **kwargs):
        result = real_prepare(self, temporary, *args, **kwargs)
        if (not swapped["done"] and
                Path(kwargs["expected_destination"]) == destination):
            swapped["done"] = True
            replacement = Path(temporary).with_name(
                Path(temporary).name + ".foreign")
            os.link(external, replacement)
            os.replace(replacement, temporary)
        return result

    m._ConnectorLogWriter._prepare_staged_for_replace = prepare_then_hardlink
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=8, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("post-prepare staged swap was published")
    finally:
        m._ConnectorLogWriter._prepare_staged_for_replace = real_prepare

    assert swapped["done"]
    assert external.read_bytes() == b"EXTERNAL-AFTER-PREPARE\n"
    assert not destination.exists()
    assert list(lock_root.glob("*.migrate.json")), "migration journal was discarded"


def test_connector_log_migration_refuses_live_in_place_destination_swap(tmp):
    """Migration checks the opened active inode before truncating it."""
    m = _mod()
    path = Path(tmp) / "migration-live-in-place.log"
    path.write_bytes(b"AAAAAAA\nBBBBBBB\nCCCCCCC\n")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    real_open = __import__("builtins").open
    swapped = {"done": False}

    def open_then_swap(file, mode="r", *args, **kwargs):
        if (not swapped["done"] and Path(file) == path and
                mode in ("r+b", "w+b")):
            swapped["done"] = True
            replacement = path.with_name(path.name + ".foreign")
            replacement.write_bytes(b"LIVE-CONCURRENT\n")
            os.replace(replacement, path)
        return real_open(file, mode, *args, **kwargs)

    m.open = open_then_swap
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=8, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("migration truncated a rebound in-place destination")
    finally:
        m.__dict__.pop("open", None)
    assert swapped["done"]
    assert path.read_bytes() == b"LIVE-CONCURRENT\n"
    assert list(lock_root.glob("*.migrate.json")), "migration journal was discarded"


def test_connector_log_rotation_refuses_live_atomic_destination_swap(tmp):
    """Rotation never replaces a backup rebound after staging validation."""
    m = _mod()
    path = Path(tmp) / "rotation-live-atomic.log"
    path.write_bytes(b"A" * 31)
    destination = path.with_name("rotation-live-atomic.log.1")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    original_prepare = m._ConnectorLogWriter._prepare_staged_for_replace
    swapped = {"done": False}

    def prepare_then_swap(self, temporary, *args, **kwargs):
        result = original_prepare(self, temporary, *args, **kwargs)
        if (not swapped["done"] and
                Path(kwargs["expected_destination"]) == destination):
            swapped["done"] = True
            replacement = destination.with_name(destination.name + ".foreign")
            replacement.write_bytes(b"LIVE-CONCURRENT\n")
            os.replace(replacement, destination)
        return result

    m._ConnectorLogWriter._prepare_staged_for_replace = prepare_then_swap
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
    try:
        try:
            writer.write("x")
        except RuntimeError:
            pass
        else:
            raise AssertionError("rotation replaced a rebound atomic destination")
    finally:
        m._ConnectorLogWriter._prepare_staged_for_replace = original_prepare
        writer.close()
    assert swapped["done"]
    assert destination.read_bytes() == b"LIVE-CONCURRENT\n"
    assert list(lock_root.glob("*.rotate.json")), "rotation journal was discarded"


def test_connector_log_rotation_stage_swap_after_prepare_keeps_journal(tmp):
    """Rotation refuses a staged hard-link swap after preparation too."""
    m = _mod()
    path = Path(tmp) / "rotation-stage-after-prepare.log"
    path.write_bytes(b"A" * 31)
    destination = path.with_name("rotation-stage-after-prepare.log.1")
    external = Path(tmp) / "rotation-external-after-prepare.log"
    external.write_bytes(b"ROTATION-EXTERNAL-KEEP\n")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    real_prepare = m._ConnectorLogWriter._prepare_staged_for_replace
    swapped = {"done": False}

    def prepare_then_hardlink(self, temporary, *args, **kwargs):
        result = real_prepare(self, temporary, *args, **kwargs)
        if (not swapped["done"] and
                Path(kwargs["expected_destination"]) == destination):
            swapped["done"] = True
            replacement = Path(temporary).with_name(
                Path(temporary).name + ".foreign")
            os.link(external, replacement)
            os.replace(replacement, temporary)
        return result

    m._ConnectorLogWriter._prepare_staged_for_replace = prepare_then_hardlink
    try:
        writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        try:
            try:
                writer.write("x")
            except RuntimeError:
                pass
            else:
                raise AssertionError("post-prepare rotation swap was published")
        finally:
            writer.close()
    finally:
        m._ConnectorLogWriter._prepare_staged_for_replace = real_prepare

    assert swapped["done"]
    assert external.read_bytes() == b"ROTATION-EXTERNAL-KEEP\n"
    assert not destination.exists()
    assert list(lock_root.glob("*.rotate.json")), "rotation journal was discarded"


def test_connector_log_rotation_refuses_live_in_place_destination_swap(tmp):
    """Rotation checks the opened active inode before truncating it."""
    m = _mod()
    path = Path(tmp) / "rotation-live-in-place.log"
    path.write_bytes(b"A" * 31)
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    real_open = __import__("builtins").open
    swapped = {"done": False}

    def open_then_swap(file, mode="r", *args, **kwargs):
        if (not swapped["done"] and Path(file) == path and
                mode in ("r+b", "w+b")):
            swapped["done"] = True
            replacement = path.with_name(path.name + ".foreign")
            replacement.write_bytes(b"LIVE-CONCURRENT\n")
            os.replace(replacement, path)
        return real_open(file, mode, *args, **kwargs)

    m.open = open_then_swap
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    try:
        try:
            writer.write("x")
        except RuntimeError:
            pass
        else:
            raise AssertionError("rotation truncated a rebound in-place destination")
    finally:
        m.__dict__.pop("open", None)
        writer.close()
    assert swapped["done"]
    assert path.read_bytes() == b"LIVE-CONCURRENT\n"
    assert list(lock_root.glob("*.rotate.json")), "rotation journal was discarded"


def test_connector_log_rotation_first_stage_crash_loop_has_bounded_temps(tmp):
    """Rotation also establishes ownership before its first staged temp."""
    m = _mod()
    del m
    case = Path(tmp) / "rotation-first-stage-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    path.write_bytes(b"a" * 31)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_rotation_first_stage", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = mod._ConnectorLogWriter._write_migration_temp
def write_then_crash(self, data, mode, name=None, directory=None):
    result = real_write(self, data, mode, name=name, directory=directory)
    if name is not None and ".rotate-" in str(name) and not str(name).endswith(".json.tmp"):
        os._exit(172)
    return result
mod._ConnectorLogWriter._write_migration_temp = write_then_crash
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1)
writer.write("x")
'''
    outcomes = []
    for _ in range(8):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path)],
            capture_output=True,
        )
        assert crashed.returncode == 172, crashed.stderr.decode("utf-8", "replace")
        outcomes.append(crashed.returncode)
    hidden = [
        item for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ]
    assert len(hidden) <= 3, [item.name for item in hidden]

    restarted = _mod()
    writer = restarted._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer.close()
    assert path.read_bytes() == b"a" * 31
    assert [
        item.name for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ] == []


def test_connector_log_migration_propagates_unremovable_old_backup(tmp):
    """A numeric backup beyond the configured count cannot be hidden."""
    m = _mod()
    path = Path(tmp) / "count-change.log"
    path.write_text("active\n", encoding="utf-8")
    extra = path.with_name("count-change.log.3")
    extra.write_text("old\n", encoding="utf-8")
    original_unlink = m._ConnectorLogWriter._unlink_if_identity

    def deny_extra(candidate, *args, **kwargs):
        if candidate == extra:
            raise PermissionError("injected backup deletion failure")
        return original_unlink(candidate, *args, **kwargs)

    m._ConnectorLogWriter._unlink_if_identity = staticmethod(deny_extra)
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        except PermissionError as exc:
            assert "injected backup deletion failure" in str(exc)
        else:
            raise AssertionError("unremovable numeric backup was silently retained")
    finally:
        m._ConnectorLogWriter._unlink_if_identity = original_unlink


def test_connector_log_rotation_propagates_backup_deletion_failure(tmp):
    """Rotation does not swallow an error deleting a stale backup."""
    m = _mod()
    path = Path(tmp) / "rotate-delete.log"
    stale = path.with_name("rotate-delete.log.2")
    stale.write_bytes(b"stale\n")
    writer = m._ConnectorLogWriter(path, max_bytes=8, backup_count=2)
    original_unlink = m._ConnectorLogWriter._unlink_if_identity

    def deny_stale(candidate, *args, **kwargs):
        if candidate == stale:
            raise PermissionError("injected rotation deletion failure")
        return original_unlink(candidate, *args, **kwargs)

    m._ConnectorLogWriter._unlink_if_identity = staticmethod(deny_stale)
    try:
        try:
            writer.write("1234567")
            writer.write("next")
        except PermissionError as exc:
            assert "injected rotation deletion failure" in str(exc)
        else:
            raise AssertionError("rotation swallowed a backup deletion failure")
    finally:
        m._ConnectorLogWriter._unlink_if_identity = original_unlink
        writer.close()


def test_connector_log_preserves_symlink_target_and_bounded_metadata(tmp):
    """A bounded symlink target is not replaced or rewritten on open."""
    m = _mod()
    target = Path(tmp) / "target.log"
    alias = Path(tmp) / "custom-alias.log"
    target.write_text("already bounded\n", encoding="utf-8")
    target.chmod(0o600)
    before = target.stat()
    alias.symlink_to(target)

    writer = m._ConnectorLogWriter(alias, max_bytes=128, backup_count=2)
    writer.close()

    after = target.stat()
    assert alias.is_symlink()
    assert alias.resolve() == target.resolve()
    assert after.st_ino == before.st_ino
    assert after.st_mode & 0o777 == 0o600
    assert after.st_mtime_ns == before.st_mtime_ns


def test_connector_log_rotation_preserves_target_mode(tmp):
    """Rotating a custom target keeps its restrictive mode on the new active."""
    m = _mod()
    target = Path(tmp) / "rotate-target.log"
    alias = Path(tmp) / "rotate-alias.log"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    alias.symlink_to(target)
    writer = m._ConnectorLogWriter(alias, max_bytes=8, backup_count=1)
    writer.write("1234567")
    writer.write("next")
    writer.close()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.with_name("rotate-target.log.1").stat().st_mode & 0o777 == 0o600


def test_connector_log_symlink_aliases_share_canonical_lock(tmp):
    """Aliases of one target cannot acquire independent ownership locks."""
    m = _mod()
    target = Path(tmp) / "canonical.log"
    alias = Path(tmp) / "alias.log"
    alias.symlink_to(target)
    first = m._ConnectorLogWriter(alias, max_bytes=32, backup_count=1)
    try:
        try:
            m._ConnectorLogWriter(target, max_bytes=32, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("symlink aliases acquired separate ownership locks")
    finally:
        first.close()


def test_connector_log_hardlink_aliases_share_ownership_across_processes(tmp):
    """Two writers using hard-link names contend on one inode ownership key."""
    m = _mod()
    target = Path(tmp) / "inode-target.log"
    alias = Path(tmp) / "inode-alias.log"
    target.write_bytes(b"")
    os.link(target, alias)
    assert target.stat().st_ino == alias.stat().st_ino
    child = r'''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("discord_mb_hardlink_child", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1)
writer.write("child-held")
print("READY", flush=True)
input()
writer.close()
'''
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(Path(MB)), str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "READY"
        try:
            m._ConnectorLogWriter(alias, max_bytes=32, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("hard-link aliases acquired independent ownership")
    finally:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""

    writer = m._ConnectorLogWriter(alias, max_bytes=32, backup_count=1)
    writer.write("after-release")
    writer.close()
    assert target.stat().st_ino == alias.stat().st_ino
    assert target.read_bytes() == alias.read_bytes()
    assert target.stat().st_size <= 32
    assert target.with_name("inode-target.log.1").exists() is False


def test_connector_log_hardlink_alias_stays_bounded_during_migration(tmp):
    """Migration updates a shared active inode instead of orphaning its alias."""
    m = _mod()
    target = Path(tmp) / "inode-migration-target.log"
    alias = Path(tmp) / "inode-migration-alias.log"
    target.write_bytes(("historical status\n" * 20).encode("utf-8"))
    os.link(target, alias)
    writer = m._ConnectorLogWriter(target, max_bytes=32, backup_count=1)
    writer.close()
    assert target.stat().st_ino == alias.stat().st_ino
    assert target.stat().st_size <= 32
    assert alias.stat().st_size <= 32
    assert alias.read_bytes() == target.read_bytes()
    assert target.with_name("inode-migration-target.log.1").stat().st_size <= 32


def test_connector_log_hardlink_alias_stays_bounded_after_rotation(tmp):
    """Rotation preserves the shared active inode and its byte ceiling."""
    m = _mod()
    target = Path(tmp) / "inode-rotation-target.log"
    alias = Path(tmp) / "inode-rotation-alias.log"
    target.write_bytes(b"")
    os.link(target, alias)
    writer = m._ConnectorLogWriter(target, max_bytes=8, backup_count=1)
    writer.write("1234567")
    writer.write("next")
    writer.close()
    assert target.stat().st_ino == alias.stat().st_ino
    assert target.stat().st_size <= 8
    assert alias.stat().st_size <= 8
    assert target.with_name("inode-rotation-target.log.1").stat().st_size <= 8


def test_connector_log_fork_child_closes_inherited_descriptor(tmp):
    """A forked child must not keep the parent's lock alive after parent close."""
    if not hasattr(os, "fork"):
        return
    path = Path(tmp) / "fork.log"
    holder = r'''
import importlib.util
import os
import sys
import time
spec = importlib.util.spec_from_file_location("discord_mb_holder", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
owner = mod._ConnectorOwnership(sys.argv[2])
ready_r, ready_w = os.pipe()
child = os.fork()
if child == 0:
    os.close(ready_r)
    time.sleep(3)
    os._exit(0)
os.close(ready_w)
os.write(ready_w, b"ready") if False else None
# The parent exits without unlocking; only the child can keep the inherited fd.
print("ready", flush=True)
os._exit(0)
'''
    proc = subprocess.Popen(
        [sys.executable, "-c", holder, str(Path(MB)), str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert proc.wait(timeout=2) == 0
        probe = subprocess.run(
            [sys.executable, "-c", (
                "import importlib.util, sys; "
                "s=importlib.util.spec_from_file_location('mb', sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "o=m._ConnectorOwnership(sys.argv[2]); o.close()"),
             str(Path(MB)), str(path)],
            capture_output=True,
        )
        assert probe.returncode == 0, probe.stderr.decode("utf-8", "replace")
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_connector_log_close_releases_owner_when_handle_close_fails(tmp):
    """An active stream error cannot strand the connector log ownership lock."""
    m = _mod()
    path = Path(tmp) / "close-error.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    real_handle = writer._fh

    class BrokenHandle:
        def close(self):
            raise RuntimeError("injected active close failure")

    writer._fh = BrokenHandle()
    try:
        try:
            writer.close()
        except RuntimeError as exc:
            assert "injected active close failure" in str(exc)
        else:
            raise AssertionError("active handle close failure was swallowed")
        owner = m._ConnectorOwnership(path)
        owner.close()
    finally:
        real_handle.close()


def test_connector_log_constructor_preserves_migration_error_if_owner_close_fails(tmp):
    """A startup migration error remains the primary exception."""
    m = _mod()
    path = Path(tmp) / "constructor-error.log"
    original_migrate = m._ConnectorLogWriter._migrate_existing
    original_close = m._ConnectorOwnership.close

    def fail_migration(self):
        raise ValueError("primary migration failure")

    def fail_close(self):
        raise RuntimeError("secondary ownership cleanup failure")

    m._ConnectorLogWriter._migrate_existing = fail_migration
    m._ConnectorOwnership.close = fail_close
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        except ValueError as exc:
            assert "primary migration failure" in str(exc)
        else:
            raise AssertionError("migration failure was not raised")
    finally:
        m._ConnectorLogWriter._migrate_existing = original_migrate
        m._ConnectorOwnership.close = original_close


def test_connector_main_preserves_writer_failure_when_process_owner_close_fails(tmp):
    """Writer startup is primary; process-owner cleanup is secondary and attached."""
    m = _mod()

    class FailingOwner:
        def close(self):
            raise RuntimeError("secondary process-owner cleanup failure")

    class FailingWriter:
        def __init__(self, *args, **kwargs):
            raise ValueError("primary writer startup failure")

    class FakeDiscord:
        pass

    old_owner = m._ConnectorOwnership
    old_writer = m._ConnectorLogWriter
    old_discord = sys.modules.get("discord")
    m._ConnectorOwnership = lambda *args, **kwargs: FailingOwner()
    m._ConnectorLogWriter = FailingWriter
    sys.modules["discord"] = FakeDiscord()
    try:
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                m.connector_main("identity", token="token", log_path=Path(tmp) / "writer.log",
                                 flavor="claude")
            except ValueError as exc:
                assert "primary writer startup failure" in str(exc)
                context = repr(exc.__context__)
                notes = " ".join(getattr(exc, "__notes__", ()))
                assert "secondary process-owner cleanup failure" in context + notes
            else:
                raise AssertionError("writer startup failure was not raised")
        assert "secondary process-owner cleanup failure" in stderr.getvalue()
    finally:
        m._ConnectorOwnership = old_owner
        m._ConnectorLogWriter = old_writer
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord


def test_connector_startup_cleanup_preserves_original_and_releases_process_owner(tmp):
    """Startup cleanup attempts all releases and preserves the primary error."""
    m = _mod()
    state = Path(tmp) / "state"
    state.mkdir()
    pidfile = state / "connector.pid"
    path = Path(tmp) / "cleanup-startup.log"

    class FailingWriter:
        def __init__(self, *args, **kwargs):
            self.inner = m._real_writer_for_test(*args, **kwargs)

        def write(self, line):
            return self.inner.write(line)

        def close(self):
            self.inner._owner.close()
            raise RuntimeError("injected writer cleanup failure")

    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise ValueError("primary startup failure")

    fake_discord = types.SimpleNamespace(
        Intents=types.SimpleNamespace(default=lambda: types.SimpleNamespace()),
        Client=ExplodingClient,
    )
    old_discord = sys.modules.get("discord")
    old_state_dir = m.state_dir
    old_sweep = m.sweep_status_plugin
    old_writer = m._ConnectorLogWriter
    m._real_writer_for_test = old_writer
    m.state_dir = lambda identity: state
    m.sweep_status_plugin = lambda identity: None
    m._ConnectorLogWriter = FailingWriter
    sys.modules["discord"] = fake_discord
    try:
        try:
            m.connector_main("identity", claude_pid=os.getpid(), token="token",
                             log_path=path, flavor="claude")
        except ValueError as exc:
            assert "primary startup failure" in str(exc)
        else:
            raise AssertionError("primary startup exception was not preserved")
    finally:
        m.state_dir = old_state_dir
        m.sweep_status_plugin = old_sweep
        m._ConnectorLogWriter = old_writer
        del m._real_writer_for_test
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord
    owner = m._ConnectorOwnership(pidfile)
    owner.close()


def test_connector_shutdown_sweep_baseexception_does_not_skip_cleanup(tmp):
    """Shutdown sweep SystemExit/KeyboardInterrupt still releases all locks."""
    m = _mod()
    state = Path(tmp) / "state"
    state.mkdir()
    pidfile = state / "connector.pid"
    path = Path(tmp) / "shutdown-sweep.log"

    class FakeClient:
        guilds = []
        user = None

        def __init__(self, *args, **kwargs):
            self.loop = types.SimpleNamespace(create_task=lambda task: task)

        def event(self, function):
            return function

        def is_closed(self):
            return True

        def run(self, *args, **kwargs):
            raise ValueError("primary connector failure")

    fake_discord = types.SimpleNamespace(
        Intents=types.SimpleNamespace(default=lambda: types.SimpleNamespace()),
        Client=FakeClient,
    )
    old_discord = sys.modules.get("discord")
    old_state_dir = m.state_dir
    old_sweep = m.sweep_status_plugin
    m.state_dir = lambda identity: state
    sweep_calls = []

    def sweep(identity):
        sweep_calls.append(identity)
        if len(sweep_calls) > 1:
            raise KeyboardInterrupt("injected shutdown sweep failure")

    m.sweep_status_plugin = sweep
    sys.modules["discord"] = fake_discord
    try:
        try:
            m.connector_main("identity", claude_pid=os.getpid(), token="token",
                             log_path=path, flavor="claude")
        except ValueError as exc:
            assert "primary connector failure" in str(exc)
        else:
            raise AssertionError("shutdown sweep replaced the primary exception")
    finally:
        m.state_dir = old_state_dir
        m.sweep_status_plugin = old_sweep
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord
    assert not pidfile.exists()
    assert len(sweep_calls) == 2
    owner = m._ConnectorOwnership(pidfile)
    owner.close()


def test_connector_stale_pid_startup_log_failure_closes_every_acquired_resource(tmp):
    """A stale-PID warning write failure still closes writer, lock, and pidfile."""
    m = _mod()
    state = Path(tmp) / "stale-state"
    state.mkdir()
    pidfile = state / "connector.pid"
    path = Path(tmp) / "stale-startup.log"
    marker = Path(tmp) / "stale-startup.marker"
    old_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    pidfile.write_text(str(old_process.pid), encoding="utf-8")
    child = r'''
import importlib.util
import pathlib
import sys
import types
spec = importlib.util.spec_from_file_location("discord_mb_stale_pid", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
state = pathlib.Path(sys.argv[3])
marker = pathlib.Path(sys.argv[4])
class FailingWriter:
    def __init__(self, *args, **kwargs):
        self.closed = False
    def write(self, line):
        raise RuntimeError("first startup log write failure")
    def close(self):
        self.closed = True
        marker.write_text("closed", encoding="utf-8")
class FakeDiscord:
    pass
mod.state_dir = lambda identity: state
mod.sweep_status_plugin = lambda identity: None
mod._ConnectorLogWriter = FailingWriter
sys.modules["discord"] = FakeDiscord()
try:
    mod.connector_main("identity", token="token", log_path=sys.argv[2], flavor="claude")
except RuntimeError as exc:
    previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
    marker.write_text(previous + "|" + str(exc), encoding="utf-8")
'''
    try:
        result = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             str(state), str(marker)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
    finally:
        old_process.terminate()
        try:
            old_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            old_process.kill()
            old_process.wait(timeout=5)
    assert marker.read_text(encoding="utf-8") == "closed|first startup log write failure"
    assert not pidfile.exists()
    owner = m._ConnectorOwnership(pidfile)
    owner.close()


def test_connector_live_legacy_pid_exit_closes_process_owner(tmp):
    """The live legacy-PID early exit releases its process-owner lease."""
    m = _mod()
    state = Path(tmp) / "live-legacy-state"
    state.mkdir()
    pidfile = state / "connector.pid"
    pidfile.write_text(str(os.getpid() + 1), encoding="utf-8")
    path = Path(tmp) / "live-legacy.log"
    closes = []

    class TrackingOwner:
        def __init__(self, *args, **kwargs):
            return None

        def close(self):
            closes.append("closed")

    old_owner = m._ConnectorOwnership
    old_state_dir = m.state_dir
    old_pid_alive = m.pid_alive
    old_is_connector = m.is_connector_process
    old_discord = sys.modules.get("discord")
    m._ConnectorOwnership = TrackingOwner
    m.state_dir = lambda identity: state
    m.pid_alive = lambda pid: True
    m.is_connector_process = lambda pid, identity: True
    sys.modules["discord"] = types.SimpleNamespace()
    try:
        try:
            m.connector_main("identity", token="token", log_path=path, flavor="claude")
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("live legacy PID did not exit")
    finally:
        m._ConnectorOwnership = old_owner
        m.state_dir = old_state_dir
        m.pid_alive = old_pid_alive
        m.is_connector_process = old_is_connector
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord
    assert closes == ["closed"]


def test_connector_ownership_error_exit_closes_process_owner(tmp):
    """An ownership error opening the log releases the process-owner lease."""
    m = _mod()
    state = Path(tmp) / "ownership-error-state"
    state.mkdir()
    path = Path(tmp) / "ownership-error.log"
    closes = []

    class TrackingOwner:
        def __init__(self, *args, **kwargs):
            return None

        def close(self):
            closes.append("closed")

    class FailingWriter:
        def __init__(self, *args, **kwargs):
            raise m._ConnectorOwnershipError("injected ownership conflict")

    old_owner = m._ConnectorOwnership
    old_writer = m._ConnectorLogWriter
    old_state_dir = m.state_dir
    old_discord = sys.modules.get("discord")
    m._ConnectorOwnership = TrackingOwner
    m._ConnectorLogWriter = FailingWriter
    m.state_dir = lambda identity: state
    sys.modules["discord"] = types.SimpleNamespace()
    try:
        try:
            m.connector_main("identity", token="token", log_path=path, flavor="claude")
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("ownership conflict did not exit")
    finally:
        m._ConnectorOwnership = old_owner
        m._ConnectorLogWriter = old_writer
        m.state_dir = old_state_dir
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord
    assert closes == ["closed"]


def test_connector_log_malformed_history_is_bounded_with_replacement(tmp):
    """Malformed historical bytes become valid UTF-8 replacement records."""
    m = _mod()
    path = Path(tmp) / "malformed.log"
    original = b"good\n\xff\xfe\xfd\n" * 5
    path.write_bytes(original)
    writer = m._ConnectorLogWriter(path, max_bytes=8, backup_count=2)
    writer.close()
    files = [path.with_name("malformed.log.2"),
             path.with_name("malformed.log.1"), path]
    assert all(p.stat().st_size <= 8 for p in files if p.exists())
    raw = b"".join(p.read_bytes() for p in files if p.exists())
    assert raw.decode("utf-8")
    assert "\ufffd" in raw.decode("utf-8")


def test_bounded_byte_chunks_fail_instead_of_emitting_invalid_utf8(tmp):
    """A byte cap smaller than one code point fails cleanly, never corrupts UTF-8."""
    m = _mod()
    raw = "🙂".encode("utf-8")
    try:
        chunks = list(m._ConnectorLogWriter._bounded_byte_chunks(raw, 2))
    except ValueError:
        return
    assert b"".join(chunks) == raw
    assert all(chunk.decode("utf-8") for chunk in chunks)


def test_connector_log_count_decrease_removes_old_backups(tmp):
    """A count decrease removes numeric history beyond the new count."""
    m = _mod()
    path = Path(tmp) / "decrease.log"
    path.write_text("active\n", encoding="utf-8")
    before = path.stat()
    path.with_name("decrease.log.1").write_text("one\n", encoding="utf-8")
    path.with_name("decrease.log.2").write_text("two\n", encoding="utf-8")
    path.with_name("decrease.log.3").write_text("three\n", encoding="utf-8")
    writer = m._ConnectorLogWriter(path, max_bytes=10, backup_count=1)
    writer.close()
    assert not path.with_name("decrease.log.2").exists()
    assert not path.with_name("decrease.log.3").exists()
    assert path.with_name("decrease.log.1").read_text(encoding="utf-8") == "one\n"
    assert path.read_text(encoding="utf-8") == "active\n"
    assert path.stat().st_ino == before.st_ino


def test_connector_log_count_decrease_preserves_rebound_obsolete_backup(tmp):
    """Count-decrease cleanup cannot unlink a replacement after observation."""
    m = _mod()
    path = Path(tmp) / "decrease-rebound.log"
    path.write_text("active\n", encoding="utf-8")
    obsolete = path.with_name("decrease-rebound.log.3")
    obsolete.write_text("old\n", encoding="utf-8")
    original_bounded = m._ConnectorLogWriter._window_is_bounded
    swapped = {"done": False}

    def bounded_then_replace(self, backups):
        result = original_bounded(self, backups)
        if not swapped["done"]:
            swapped["done"] = True
            replacement = obsolete.with_name(obsolete.name + ".foreign")
            replacement.write_bytes(b"FOREIGN-COUNT-DECREASE")
            os.replace(replacement, obsolete)
        return result

    m._ConnectorLogWriter._window_is_bounded = bounded_then_replace
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("rebound obsolete backup was silently removed")
    finally:
        m._ConnectorLogWriter._window_is_bounded = original_bounded
    assert swapped["done"]
    assert obsolete.read_bytes() == b"FOREIGN-COUNT-DECREASE"


def test_connector_log_migration_preserves_rebound_source_and_journal(tmp):
    """Migration source cleanup retains a pathname replaced after staging."""
    m = _mod()
    path = Path(tmp) / "migration-source-rebound.log"
    path.write_bytes(b"active\n" * 8)
    obsolete = path.with_name("migration-source-rebound.log.3")
    obsolete.write_bytes(b"old\n")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    original_replace = m.os.replace
    swapped = {"done": False}

    def replace_then_swap(source, destination):
        result = original_replace(source, destination)
        if not swapped["done"]:
            swapped["done"] = True
            replacement = obsolete.with_name(obsolete.name + ".foreign")
            replacement.write_bytes(b"FOREIGN-MIGRATION-SOURCE")
            original_replace(replacement, obsolete)
        return result

    m.os.replace = replace_then_swap
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=10, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("rebound migration source was silently removed")
    finally:
        m.os.replace = original_replace
    assert swapped["done"]
    assert obsolete.read_bytes() == b"FOREIGN-MIGRATION-SOURCE"
    assert list(lock_root.glob("*.migrate.json")), "migration journal was discarded"


def test_connector_log_journal_hardlink_swap_never_writes_external_inode(tmp):
    """A journal replacement cannot redirect manifest appends externally."""
    m = _mod()
    path = Path(tmp) / "journal-hardlink-swap.log"
    path.write_bytes(b"history\n" * 8)
    external = Path(tmp) / "external-journal-owner.log"
    external.write_bytes(b"EXTERNAL-JOURNAL-KEEP\n")
    real_write = m._ConnectorLogWriter._write_migration_manifest
    swapped = {"done": False}

    def write_then_swap(self, manifest, kind="migrate", **kwargs):
        result = real_write(self, manifest, kind=kind, **kwargs)
        if kind == "migrate" and not kwargs.get("create") and not swapped["done"]:
            swapped["done"] = True
            journal = self._migration_manifest_path()
            replacement = journal.with_name(journal.name + ".foreign")
            os.link(external, replacement)
            os.replace(replacement, journal)
        return result

    m._ConnectorLogWriter._write_migration_manifest = write_then_swap
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=8, backup_count=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("journal hard-link replacement was accepted")
    finally:
        m._ConnectorLogWriter._write_migration_manifest = real_write

    assert swapped["done"]
    assert external.read_bytes() == b"EXTERNAL-JOURNAL-KEEP\n"


def test_connector_log_rotation_preserves_rebound_sparse_destination(tmp):
    """Sparse rotation deletion retains a replacement at an absent slot."""
    m = _mod()
    path = Path(tmp) / "sparse-rebound.log"
    path.write_bytes(b"A" * 31)
    obsolete = path.with_name("sparse-rebound.log.2")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    original_manifest = m._ConnectorLogWriter._write_migration_manifest
    swapped = {"done": False}

    def manifest_then_swap(self, manifest, kind="migrate", **kwargs):
        result = original_manifest(self, manifest, kind=kind, **kwargs)
        if (kind == "rotate" and manifest.get("state") == "prepared" and
                not swapped["done"]):
            swapped["done"] = True
            replacement = obsolete.with_name(obsolete.name + ".foreign")
            replacement.write_bytes(b"FOREIGN-SPARSE-DESTINATION")
            os.replace(replacement, obsolete)
        return result

    m._ConnectorLogWriter._write_migration_manifest = manifest_then_swap
    try:
        writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
        try:
            writer.write("x")
        except RuntimeError:
            pass
        else:
            raise AssertionError("rebound sparse destination was silently removed")
        finally:
            writer.close()
    finally:
        m._ConnectorLogWriter._write_migration_manifest = original_manifest
    assert swapped["done"]
    assert obsolete.read_bytes() == b"FOREIGN-SPARSE-DESTINATION"
    assert list(lock_root.glob("*.rotate.json")), "rotation journal was discarded"


def test_connector_log_failed_staging_key_cleanup_preserves_rebound_path(tmp):
    """A failed key write cannot remove a replacement at the key pathname."""
    m = _mod()
    path = Path(tmp) / "failed-staging.key"
    original_write = m.os.write
    original_replace = m.os.replace
    swapped = {"done": False}

    def write_then_replace(fd, data):
        if not swapped["done"]:
            swapped["done"] = True
            replacement = path.with_name(path.name + ".foreign")
            replacement.write_bytes(b"FOREIGN-STAGING-KEY")
            original_replace(replacement, path)
            raise OSError("injected staging key write failure")
        return original_write(fd, data)

    m.os.write = write_then_replace
    try:
        try:
            m._ConnectorLogWriter._create_staging_key(path)
        except OSError as exc:
            assert "injected staging key write failure" in str(exc)
        else:
            raise AssertionError("staging key write failure was swallowed")
    finally:
        m.os.write = original_write
    assert swapped["done"]
    assert path.exists(), "failed key cleanup removed the rebound pathname"
    assert path.read_bytes() == b"FOREIGN-STAGING-KEY"


def test_connector_log_failed_staging_temp_cleanup_preserves_rebound_path(tmp):
    """A failed temp write cannot remove a replacement at the temp pathname."""
    m = _mod()
    path = Path(tmp) / "failed-staging-temp.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer._transaction_nonce = "test-staging-nonce"
    temporary = writer._owned_temp_path("rotate", 0)
    writer._staging_kind = "rotate"
    writer._staging_slot = 0
    writer._staging_destination = path
    swapped = {"done": False}

    def claim_then_replace(staged):
        swapped["done"] = True
        replacement = Path(staged).with_name(Path(staged).name + ".foreign")
        replacement.write_bytes(b"FOREIGN-STAGING-TEMP")
        os.replace(replacement, staged)

    writer._staging_claim_callback = claim_then_replace

    def fail_key():
        raise OSError("injected staging temp key failure")

    writer._current_staging_key = fail_key
    try:
        try:
            writer._write_migration_temp(b"payload", None, name=temporary)
        except OSError as exc:
            assert "injected staging temp key failure" in str(exc)
        else:
            raise AssertionError("staging temp failure was swallowed")
    finally:
        writer._staging_claim_callback = None
        writer.close()
    assert swapped["done"]
    assert temporary.exists(), "failed temp cleanup removed the rebound pathname"
    assert temporary.read_bytes() == b"FOREIGN-STAGING-TEMP"


def test_connector_log_exact_boundary_accepts_newline_record(tmp):
    """A record exactly equal to the byte limit is retained without rollover."""
    m = _mod()
    path = Path(tmp) / "boundary.log"
    writer = m._ConnectorLogWriter(path, max_bytes=4, backup_count=1)
    writer.write("abc")
    writer.close()
    assert path.read_bytes() == b"abc\n"
    assert not path.with_name("boundary.log.1").exists()


def test_connector_log_embedded_newlines_do_not_gain_blank_records(tmp):
    """Embedded newlines are emitted once, with one terminator per physical line."""
    m = _mod()
    path = Path(tmp) / "embedded.log"
    writer = m._ConnectorLogWriter(path, max_bytes=4, backup_count=2)
    writer.write("ab\nc")
    writer.close()
    history = path.with_name("embedded.log.1").read_bytes() + path.read_bytes()
    assert history == b"ab\nc\n"
    assert b"\n\n" not in history


def test_connector_log_ownership_uses_windows_byte_lock_api(tmp):
    """The Windows source path uses a CRT byte lock and releases it cleanly."""
    m = _mod()
    path = Path(tmp) / "windows-owned.log"
    locked = set()

    def locking(fd, mode, count):
        del count
        key = os.fstat(fd).st_ino
        if mode == fake_msvcrt.LK_NBLCK:
            if key in locked:
                raise OSError("already locked")
            locked.add(key)
        else:
            locked.discard(key)

    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    old_platform = m.sys.platform
    old_msvcrt = sys.modules.get("msvcrt")
    m.sys.platform = "win32"
    sys.modules["msvcrt"] = fake_msvcrt
    try:
        owner = m._ConnectorOwnership(path)
        try:
            try:
                m._ConnectorOwnership(path)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Windows byte lock admitted a second owner")
        finally:
            owner.close()
        released = m._ConnectorOwnership(path)
        released.close()
    finally:
        m.sys.platform = old_platform
        if old_msvcrt is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = old_msvcrt


def test_connector_log_windows_does_not_lock_active_inode_or_prefix_nul(tmp):
    """Windows ownership uses interlocks, never a byte lock on the active log."""
    m = _mod()
    path = Path(tmp) / "windows-active.log"
    locked = set()
    seen = []

    def locking(fd, mode, count):
        del count
        try:
            name = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
        except (FileNotFoundError, OSError):
            name = None
        seen.append(name)
        if name == path.resolve():
            raise AssertionError("active log must not use a CRT byte lock")
        key = os.fstat(fd).st_ino
        if mode == fake_msvcrt.LK_NBLCK:
            if key in locked:
                raise OSError("already locked")
            locked.add(key)
        else:
            locked.discard(key)

    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    old_platform = m.sys.platform
    old_msvcrt = sys.modules.get("msvcrt")
    m.sys.platform = "win32"
    sys.modules["msvcrt"] = fake_msvcrt
    try:
        writer = m._ConnectorLogWriter(path, max_bytes=8, backup_count=1)
        assert path.read_bytes() == b""
        writer.close()
    finally:
        m.sys.platform = old_platform
        if old_msvcrt is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = old_msvcrt
    assert path.resolve() not in seen


def test_connector_log_sidecar_rejects_windows_link_and_replacement_targets(tmp):
    """Sidecar locks never follow links/reparses or replacement identities."""
    m = _mod()
    locked = set()

    def locking(fd, mode, count):
        del count
        key = os.fstat(fd).st_ino
        if mode == fake_msvcrt.LK_NBLCK:
            if key in locked:
                raise OSError("already locked")
            locked.add(key)
        else:
            locked.discard(key)

    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    old_platform = m.sys.platform
    old_msvcrt = sys.modules.get("msvcrt")
    old_linklike = m._linklike
    m.sys.platform = "win32"
    sys.modules["msvcrt"] = fake_msvcrt
    owner = object.__new__(m._ConnectorOwnership)
    owner._windows = True
    try:
        external = Path(tmp) / "sidecar-external.bin"
        external.write_bytes(b"EXTERNAL-SIDECAR\n")

        symlink = Path(tmp) / "symlink.lock"
        symlink.symlink_to(external)
        try:
            owner._lock_sidecar(symlink)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Windows sidecar symlink was followed")
        assert external.read_bytes() == b"EXTERNAL-SIDECAR\n"

        reparse = Path(tmp) / "reparse.lock"
        reparse.write_bytes(b"REPARSE-KEEP")
        m._linklike = lambda candidate: (
            Path(candidate) == reparse or old_linklike(candidate))
        try:
            owner._lock_sidecar(reparse)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Windows reparse sidecar was adopted")
        assert reparse.read_bytes() == b"REPARSE-KEEP"
        m._linklike = old_linklike

        hardlink = Path(tmp) / "hardlink.lock"
        os.link(external, hardlink)
        try:
            owner._lock_sidecar(hardlink)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Windows hard-linked sidecar was adopted")
        assert external.read_bytes() == b"EXTERNAL-SIDECAR\n"

        replacement = Path(tmp) / "replacement.lock"
        replacement.write_bytes(b"")
        foreign = Path(tmp) / "replacement-foreign.bin"
        foreign.write_bytes(b"FOREIGN-REPLACEMENT\n")
        real_open = m.os.open
        swapped = {"done": False}

        def open_then_replace(name, flags, *args):
            fd = real_open(name, flags, *args)
            if Path(name) == replacement and not swapped["done"]:
                swapped["done"] = True
                os.replace(foreign, replacement)
            return fd

        m.os.open = open_then_replace
        try:
            try:
                owner._lock_sidecar(replacement)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Windows sidecar replacement was accepted")
        finally:
            m.os.open = real_open
        assert swapped["done"]
        assert replacement.read_bytes() == b"FOREIGN-REPLACEMENT\n"

        posix = Path(tmp) / "posix-existing.lock"
        posix.write_bytes(b"PRESERVE-ME")
        owner._windows = False
        handle = owner._lock_sidecar(posix)
        owner._release_handle(handle)
        assert posix.read_bytes() == b"PRESERVE-ME"
    finally:
        m._linklike = old_linklike
        m.sys.platform = old_platform
        if old_msvcrt is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = old_msvcrt


def test_connector_log_windows_hardlink_aliases_share_identity_lock(tmp):
    """Windows identity probing makes hard-link aliases contend safely."""
    m = _mod()
    target = Path(tmp) / "windows-inode-target.log"
    alias = Path(tmp) / "windows-inode-alias.log"
    target.write_bytes(b"")
    os.link(target, alias)
    assert target.stat().st_ino == alias.stat().st_ino
    locked = set()

    def locking(fd, mode, count):
        del count
        key = os.fstat(fd).st_ino
        if mode == fake_msvcrt.LK_NBLCK:
            if key in locked:
                raise OSError("already locked")
            locked.add(key)
        else:
            locked.discard(key)

    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    old_platform = m.sys.platform
    old_msvcrt = sys.modules.get("msvcrt")
    m.sys.platform = "win32"
    sys.modules["msvcrt"] = fake_msvcrt
    try:
        owner = m._ConnectorOwnership(target, lock_inode=True)
        try:
            try:
                m._ConnectorOwnership(alias, lock_inode=True)
            except RuntimeError:
                pass
            else:
                raise AssertionError(
                    "Windows hard-link aliases bypassed the identity interlock")
        finally:
            owner.close()
    finally:
        m.sys.platform = old_platform
        if old_msvcrt is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = old_msvcrt


def test_connector_log_hardlink_alias_recovers_identity_journal(tmp):
    """A restart through a hard-link alias resumes the original generation."""
    m = _mod()
    del m
    case = Path(tmp)
    target = case / "journal-target.log"
    alias = case / "journal-alias.log"
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    target.write_bytes(original)
    os.link(target, alias)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_alias_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
def publish_then_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if calls[0] == 2:
        os._exit(117)
os.replace = publish_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(target)],
        capture_output=True,
    )
    assert crashed.returncode == 117, crashed.stderr.decode("utf-8", "replace")

    restarted = _mod()
    writer = restarted._ConnectorLogWriter(alias, max_bytes=16, backup_count=2)
    writer.close()
    slots = [target.with_name("journal-target.log.2"),
             target.with_name("journal-target.log.1"), target]
    assert b"".join(slot.read_bytes() for slot in slots if slot.exists()) == original
    assert alias.read_bytes() == target.read_bytes()


def test_connector_log_hardlink_recovery_is_filename_order_independent(tmp):
    """Journal recovery locks aliases in either filename order."""
    m = _mod()
    del m
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_ordered_alias_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
def publish_then_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if calls[0] == 2:
        os._exit(119)
os.replace = publish_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''

    scenarios = (
        ("target-to-alias", "a-target.log", "z-alias.log"),
        ("alias-to-target", "z-alias.log", "a-target.log"),
        ("same-name-control", "a-target.log", "a-target.log"),
    )
    for name, crash_name, restart_name in scenarios:
        case = Path(tmp) / name
        case.mkdir()
        crash_path = case / crash_name
        restart_path = case / restart_name
        crash_path.write_bytes(original)
        if restart_path != crash_path:
            os.link(crash_path, restart_path)

        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(crash_path)],
            capture_output=True,
        )
        assert crashed.returncode == 119, (
            name, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )

        restarted = _mod()
        writer = restarted._ConnectorLogWriter(
            restart_path, max_bytes=16, backup_count=2)
        writer.close()
        slots = [crash_path.with_name(f"{crash_name}.2"),
                 crash_path.with_name(f"{crash_name}.1"), crash_path]
        assert b"".join(slot.read_bytes() for slot in slots if slot.exists()) == original
        assert restart_path.read_bytes() == crash_path.read_bytes()


def test_connector_log_exact_temp_collision_is_never_adopted_or_deleted(tmp):
    """An O_EXCL collision is not proof that a staged temp belongs to us."""
    m = _mod()

    for kind in ("migrate", "rotate"):
        case = Path(tmp) / f"collision-{kind}"
        case.mkdir()
        path = case / "collision.log"
        probe = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        digest = probe._identity_digest
        probe.close()

        if kind == "migrate":
            path.write_bytes(b"historical line\n" * 8)
        else:
            path.write_bytes(b"A" * 31)
        collision = path.with_name(
            f".{path.name}.{kind}-{digest}-0.tmp")
        collision.write_bytes(b"KEEP-ME-COLLISION")

        original_owned_temp_path = m._ConnectorLogWriter._owned_temp_path

        def collide(owner, requested_kind, index, directory=None):
            if requested_kind == kind:
                return collision
            return original_owned_temp_path(owner, requested_kind, index, directory)
        m._ConnectorLogWriter._owned_temp_path = collide
        writer = None
        try:
            writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
            if kind == "rotate":
                writer.write("x")
        except FileExistsError:
            pass
        else:
            raise AssertionError(f"{kind} collision was silently adopted")
        finally:
            m._ConnectorLogWriter._owned_temp_path = original_owned_temp_path
            if writer is not None:
                try:
                    writer.close()
                except (FileExistsError, RuntimeError):
                    pass

        restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        restarted.close()
        assert collision.read_bytes() == b"KEEP-ME-COLLISION"


def test_connector_named_fallback_collision_is_never_adopted_or_deleted(tmp):
    """A fixed fallback temp collision survives failed publication intact."""
    m = _mod()
    path = Path(tmp) / "named-collision.log"
    payload = b"new-payload"
    temporary, claim, private = m._ConnectorLogWriter._named_publish_paths(path)
    collision = b"KEEP-NAMED-FALLBACK-COLLISION"
    temporary.write_bytes(collision)
    try:
        m._ConnectorLogWriter._publish_named_bytes(
            path, payload, auth_key=b"A" * 32)
    except FileExistsError:
        pass
    else:
        raise AssertionError("named fallback collision was silently adopted")
    assert temporary.read_bytes() == collision
    assert not claim.exists()
    assert not private.exists()


def test_connector_named_fixed_claim_stage_collision_is_preserved(tmp):
    """A foreign fixed claim stage is never adopted or removed.

    The portable first-publication path uses one deterministic
    ``.create.claim.tmp`` entry.  Merely being same-user, private, and
    fd/path-stable is not ownership proof: each of the key, migration, and
    rotation transactions must fail closed when that exact entry is already
    occupied by unrelated bytes.
    """
    m = _mod()
    old_tmpfile = getattr(m.os, "O_TMPFILE", None)
    m.os.O_TMPFILE = None
    try:
        scenarios = (("key", 32), ("migrate", 16), ("rotate", 32))
        for kind, max_bytes in scenarios:
            case = Path(tmp) / f"fixed-stage-foreign-{kind}"
            case.mkdir()
            path = case / f"{kind}.log"
            lock_root = case / "lock-root"
            seed = m._ConnectorLogWriter(
                path, max_bytes=max_bytes, backup_count=1,
                lock_root=lock_root)
            if kind == "migrate":
                path.write_bytes(b"history\n" * 8)
                destination = seed._migration_manifest_path()
            elif kind == "rotate":
                path.write_bytes(b"A" * 31)
                destination = seed._rotation_manifest_path()
            else:
                destination = seed._staging_key_path()
            seed.close()

            stage = m._ConnectorLogWriter._named_claim_staging_path(destination)
            foreign = b"FOREIGN-EXACT-FIXED-STAGE"
            stage.write_bytes(foreign)
            os.chmod(stage, 0o600)
            writer = None
            try:
                if kind == "key":
                    m._ConnectorLogWriter._publish_named_bytes(
                        destination, b"K" * 32)
                else:
                    writer = m._ConnectorLogWriter(
                        path, max_bytes=max_bytes, backup_count=1,
                        lock_root=lock_root)
                    if kind == "rotate":
                        writer.write("x")
            except (FileExistsError, RuntimeError):
                pass
            else:
                raise AssertionError(f"foreign {kind} claim stage was adopted")
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except (FileExistsError, RuntimeError):
                        pass
            assert stage.read_bytes() == foreign, kind
    finally:
        if old_tmpfile is None:
            m.os.O_TMPFILE = None
        else:
            m.os.O_TMPFILE = old_tmpfile


def test_connector_named_fallback_preserves_foreign_canonical_claim(tmp):
    """A canonical claim without its matching stage is never adopted."""
    m = _mod()
    path = Path(tmp) / "foreign-claim.log"
    payload = b"FOREIGN-PAYLOAD"
    _temporary, claim, _payload = m._ConnectorLogWriter._named_publish_paths(path)
    record = (json.dumps({
        "magic": "DISCORD-MB-NAMED-CREATE-1",
        "path": str(path.absolute()),
        "publication": "fixed-stage-v1",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": 0o600,
    }, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
    claim.write_bytes(record)
    original = claim.read_bytes()
    try:
        m._ConnectorLogWriter._publish_named_bytes(
            path, b"LOCAL-PAYLOAD", auth_key=b"A" * 32)
    except FileExistsError:
        pass
    else:
        raise AssertionError("foreign canonical claim was adopted")
    assert claim.read_bytes() == original
    assert not path.exists()


def test_connector_named_fallback_partial_payload_is_recoverable(tmp):
    """A hard exit during fallback payload bytes is reclaimed on restart."""
    path = Path(tmp) / "named-partial.log"
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_named_partial", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = os.write
def partial_write(fd, data):
    if bytes(data) == b"PAYLOAD":
        real_write(fd, b"P")
        os._exit(189)
    return real_write(fd, data)
os.write = partial_write
mod._ConnectorLogWriter._publish_named_bytes(
    sys.argv[2], b"PAYLOAD", auth_key=b"P" * 32)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        capture_output=True,
    )
    assert crashed.returncode == 189, crashed.stderr.decode("utf-8", "replace")
    m = _mod()
    temporary, claim, private = m._ConnectorLogWriter._named_publish_paths(path)
    assert not temporary.exists()
    assert claim.exists() and private.read_bytes() == b"P"
    m._ConnectorLogWriter._publish_named_bytes(
        path, b"NEW-PAYLOAD", auth_key=b"P" * 32)
    assert path.read_bytes() == b"NEW-PAYLOAD"
    assert not list(path.parent.glob(f".{path.name}.create.*"))


def test_connector_named_fallback_reconciles_interrupted_fixed_claim_cleanup(tmp):
    """One-link proof/stage cleanup residues do not wedge the next publish.

    The fixed proof/stage pair is used for the staging key and both durable
    log journals.  A process can die after either hard-link pathname is
    removed, leaving one name behind.  Those states are writer-created and
    must be reconciled before the next ordinary open; an extra hard link or
    unrelated exact-name bytes remain a collision.
    """
    m = _mod()
    old_tmpfile = _force_portable(m)
    try:
        scenarios = (("key", 32), ("migrate", 16), ("rotate", 32))
        for kind, max_bytes in scenarios:
            case = Path(tmp) / f"interrupted-fixed-claim-{kind}"
            case.mkdir()
            path = case / f"{kind}.log"
            lock_root = case / "lock-root"
            seed = m._ConnectorLogWriter(
                path, max_bytes=max_bytes, backup_count=1, lock_root=lock_root)
            auth_key = seed._staging_key_bytes
            if kind == "key":
                destination = seed._staging_key_path()
                payload = b"K" * 32
            elif kind == "migrate":
                destination = seed._migration_manifest_path()
                payload = b"migration-journal"
            else:
                destination = seed._rotation_manifest_path()
                payload = b"rotation-journal"
            seed.close()

            _temporary, _claim, _payload = m._ConnectorLogWriter._named_publish_paths(
                destination)
            stage = m._ConnectorLogWriter._named_claim_staging_path(destination)
            proof = m._ConnectorLogWriter._named_claim_proof_path(destination)

            # A complete pair is what the writer has immediately before its
            # cleanup sequence.  Remove each half in turn to model an exit
            # between the two unlink operations.
            if destination.exists():
                destination.unlink()
            claim_bytes = m._ConnectorLogWriter._named_claim_record(
                destination, payload, 0o600, auth_key=auth_key)
            proof_info = m._ConnectorLogWriter._create_named_claim_proof(
                proof, claim_bytes)
            os.link(proof, stage)
            stage.unlink()
            assert proof.exists()
            m._ConnectorLogWriter._publish_named_bytes(
                destination, payload, auth_key=auth_key)
            assert destination.read_bytes() == payload
            assert not proof.exists() and not stage.exists()

            destination.unlink()
            proof_info = m._ConnectorLogWriter._create_named_claim_proof(
                proof, claim_bytes)
            os.link(proof, stage)
            proof.unlink()
            assert stage.exists()
            m._ConnectorLogWriter._publish_named_bytes(
                destination, payload, auth_key=auth_key)
            assert destination.read_bytes() == payload
            assert not proof.exists() and not stage.exists()

            # A third hard link invalidates the proof; recovery must leave all
            # exact names untouched instead of deleting an unrelated alias.
            destination.unlink()
            proof_info = m._ConnectorLogWriter._create_named_claim_proof(
                proof, claim_bytes)
            os.link(proof, stage)
            extra = proof.with_name(proof.name + ".extra")
            os.link(proof, extra)
            before_stage = stage.read_bytes()
            before_proof = proof.read_bytes()
            try:
                m._ConnectorLogWriter._publish_named_bytes(
                    destination, payload, auth_key=auth_key)
            except FileExistsError:
                pass
            else:
                raise AssertionError(f"unexpected {kind} proof link count accepted")
            assert stage.read_bytes() == before_stage
            assert proof.read_bytes() == before_proof
            assert extra.exists()
    finally:
        _restore_portable(m, old_tmpfile)


def _foreign_named_claim_residue(mod, case, kind, with_stage,
                                 canonical_present):
    """Create a same-user, mode-0600 but unauthenticated fixed claim."""
    case = Path(case)
    path = case / f"foreign-{kind}.log"
    lock_root = case / "lock-root"
    if kind == "key":
        lock_root.mkdir(mode=0o700)
        lock_root.chmod(0o700)
        destination = lock_root / "connector-staging.key"
        payload = b"F" * mod._ConnectorLogWriter._STAGING_KEY_BYTES
    else:
        seed = mod._ConnectorLogWriter(
            path, max_bytes=16 if kind == "migrate" else 32,
            backup_count=1, lock_root=lock_root)
        destination = (seed._migration_manifest_path()
                       if kind == "migrate"
                       else seed._rotation_manifest_path())
        seed.close()
        payload = (b"foreign-migration-journal"
                   if kind == "migrate" else b"foreign-rotation-journal")

    proof = mod._ConnectorLogWriter._named_claim_proof_path(destination)
    stage = mod._ConnectorLogWriter._named_claim_staging_path(destination)
    record = (json.dumps({
        "magic": "DISCORD-MB-NAMED-CREATE-1",
        "path": str(destination.absolute()),
        "publication": "fixed-stage-v1",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": 0o600,
    }, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
    # This is intentionally the exact shape accepted by the previous
    # implementation.  It has no durable authenticator and is therefore not
    # ownership evidence, even though its bytes are syntactically valid.
    proof.write_bytes(record)
    proof.chmod(0o600)
    if with_stage:
        os.link(proof, stage)
        assert stage.read_bytes() == record
    if canonical_present:
        destination.write_bytes(payload)
        destination.chmod(0o600)
    return path, lock_root, destination, proof, stage, payload


def test_connector_named_claim_direct_helper_rejects_forged_proof_shapes(tmp):
    """A self-described exact proof is never cleanup authority.

    Same-user mode-0600 files can reproduce the old JSON record and either
    the proof-only or proof/stage hard-link shape.  Recovery must fail closed
    and preserve every pathname, whether the canonical destination is absent
    or already occupied.
    """
    m = _mod()
    for kind in ("key", "migrate", "rotate"):
        for with_stage in (False, True):
            for canonical_present in (False, True):
                case = Path(tmp) / (
                    f"forged-proof-{kind}-{int(with_stage)}-"
                    f"{int(canonical_present)}")
                case.mkdir()
                _path, _root, destination, proof, stage, payload = \
                    _foreign_named_claim_residue(
                        m, case, kind, with_stage, canonical_present)
                proof_before = proof.read_bytes()
                stage_before = stage.read_bytes() if stage.exists() else None
                destination_before = (
                    destination.read_bytes()
                    if destination.exists() else None)
                try:
                    m._ConnectorLogWriter._recover_named_claim(destination)
                except FileExistsError:
                    pass
                else:
                    raise AssertionError(
                        f"forged {kind} claim was accepted: "
                        f"stage={with_stage} canonical={canonical_present}")
                assert proof.exists()
                assert proof.read_bytes() == proof_before
                if with_stage:
                    assert stage.exists()
                    assert stage.read_bytes() == stage_before
                else:
                    assert not stage.exists()
                if canonical_present:
                    assert destination.read_bytes() == destination_before
                else:
                    assert not destination.exists()


def test_connector_named_claim_operations_preserve_forged_proof_shapes(tmp):
    """Key, migration, and rotation callers cannot consume forged claims."""
    m = _mod()
    old_tmpfile = _force_portable(m)
    try:
        for kind in ("key", "migrate", "rotate"):
            for with_stage in (False, True):
                for canonical_present in (False, True):
                    case = Path(tmp) / (
                        f"forged-operation-{kind}-{int(with_stage)}-"
                        f"{int(canonical_present)}")
                    case.mkdir()
                    path, lock_root, destination, proof, stage, payload = \
                        _foreign_named_claim_residue(
                            m, case, kind, with_stage, canonical_present)
                    proof_before = proof.read_bytes()
                    stage_before = stage.read_bytes() if stage.exists() else None
                    destination_before = (
                        destination.read_bytes()
                        if destination.exists() else None)
                    writer = None
                    try:
                        if kind == "key":
                            m._ConnectorLogWriter._publish_named_bytes(
                                destination, payload)
                        else:
                            writer = m._ConnectorLogWriter(
                                path, max_bytes=16 if kind == "migrate" else 32,
                                backup_count=1, lock_root=lock_root)
                            if kind == "rotate":
                                writer.write("x")
                    except (FileExistsError, RuntimeError):
                        pass
                    finally:
                        if writer is not None:
                            try:
                                writer.close()
                            except (FileExistsError, RuntimeError):
                                pass
                    assert proof.exists(), (kind, with_stage,
                                            canonical_present)
                    assert proof.read_bytes() == proof_before
                    if with_stage:
                        assert stage.exists()
                        assert stage.read_bytes() == stage_before
                    else:
                        assert not stage.exists()
                    if canonical_present:
                        assert destination.read_bytes() == destination_before
                    else:
                        assert not destination.exists()
    finally:
        _restore_portable(m, old_tmpfile)


def _partial_first_named_claim_child(kind, code):
    """Return a child which exits during the first portable claim record."""
    return rf'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_partial_first_claim", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
real_write = os.write
armed = {{"value": True}}
def partial_claim(fd, data):
    if armed["value"] and b"DISCORD-MB-NAMED-CREATE-1" in bytes(data):
        armed["value"] = False
        real_write(fd, bytes(data)[:max(1, len(data) // 2)])
        os._exit({code})
    return real_write(fd, data)
os.write = partial_claim
kind = sys.argv[3]
path = sys.argv[2]
lock_root = sys.argv[4]
if kind == "key":
    mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                            lock_root=lock_root)
elif kind == "migrate":
    mod._ConnectorLogWriter(path, max_bytes=16, backup_count=1,
                            lock_root=lock_root)
else:
    writer = mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                     lock_root=lock_root)
    writer.write("x")
'''


def test_portable_publication_env_forces_the_named_path(tmp):
    """The CI seam that runs the macOS/Windows protocol on a Linux runner."""
    m = _mod()
    available = m._anonymous_publication_available
    # Not hasattr: _restore_portable ASSIGNS os.O_TMPFILE = None where there
    # was no attribute to restore, which is every macOS and Windows run, so
    # after any earlier test in this suite hasattr is True and the value is
    # None.  The predicate under test reads the value for that reason.

    def unforced():
        return getattr(os, "O_TMPFILE", None) is not None and os.name != "nt"

    old = os.environ.get("DISCORD_MB_PORTABLE_PUBLICATION")
    try:
        os.environ["DISCORD_MB_PORTABLE_PUBLICATION"] = "1"
        assert available() is False
        os.environ["DISCORD_MB_PORTABLE_PUBLICATION"] = "0"
        assert available() is unforced()
        os.environ.pop("DISCORD_MB_PORTABLE_PUBLICATION")
        assert available() is unforced()
    finally:
        if old is None:
            os.environ.pop("DISCORD_MB_PORTABLE_PUBLICATION", None)
        else:
            os.environ["DISCORD_MB_PORTABLE_PUBLICATION"] = old


def _force_portable(mod):
    old = getattr(mod.os, "O_TMPFILE", None)
    mod.os.O_TMPFILE = None
    return old


def _restore_portable(mod, old):
    if old is None:
        mod.os.O_TMPFILE = None
    else:
        mod.os.O_TMPFILE = old


def test_connector_named_fallback_partial_first_claim_is_bounded_and_reclaimed(tmp):
    """A torn bootstrap claim is bounded and does not wedge the next start.

    The torn record is never adopted -- its bytes cannot even be parsed, let
    alone authenticated -- but it must not survive as a permanent barrier
    either.  On Linux this residue cannot exist at all: publication happens
    from an anonymous inode, so a crash leaves no name behind.  The portable
    path names the proof from the moment it is created, and a proof that no
    start can get past would brick that log on macOS and Windows for good.

    The complete-but-unauthenticated case, which IS preserved and fails
    closed, is covered by the forged-proof tests below.
    """
    scenarios = (("key", 190), ("migrate", 191), ("rotate", 192))
    for kind, code in scenarios:
        case = Path(tmp) / f"partial-first-claim-{kind}"
        case.mkdir()
        path = case / f"{kind}.log"
        lock_root = case / "lock-root"
        if kind == "migrate":
            seed = _mod()._ConnectorLogWriter(
                path, max_bytes=16, backup_count=1, lock_root=lock_root)
            proof_destination = seed._migration_manifest_path()
            seed.close()
            path.write_bytes(b"history\n" * 8)
        elif kind == "rotate":
            seed = _mod()._ConnectorLogWriter(
                path, max_bytes=32, backup_count=1, lock_root=lock_root)
            proof_destination = seed._rotation_manifest_path()
            seed.close()
            path.write_bytes(b"A" * 31)
        else:
            proof_destination = lock_root / "connector-staging.key"

        crashed = subprocess.run(
            [sys.executable, "-c", _partial_first_named_claim_child(kind, code),
             str(Path(MB)), str(path), kind, str(lock_root)],
            capture_output=True,
        )
        assert crashed.returncode == code, crashed.stderr.decode("utf-8", "replace")

        # The first claim is a fixed-name transaction, never a UUID family.
        claim_residue = sorted(
            item.name for item in lock_root.glob("*.create.*")
        )
        assert len(claim_residue) <= 2, claim_residue
        proof = _mod()._ConnectorLogWriter._named_claim_proof_path(
            proof_destination)
        assert proof.exists()
        assert len(list(lock_root.glob("*.create-proof"))) == 1

        m = _mod()
        old = _force_portable(m)
        try:
            for attempt in range(2):
                writer = m._ConnectorLogWriter(
                    path, max_bytes=32 if kind == "rotate" else 16,
                    backup_count=1, lock_root=lock_root)
                writer.close()
                assert not proof.exists(), (
                    f"{kind} torn claim survived start {attempt}")
        finally:
            _restore_portable(m, old)

        assert not list(lock_root.glob("*.create-proof"))
        assert not list(lock_root.glob("*.create.*"))


def _claim_swap_restore(mod, claim):
    """Replace a claim entry while its creator still owns the open fd."""
    real_entry_identity = mod._ConnectorOwnership._entry_identity
    foreign = claim.with_name(claim.name + ".foreign")
    saved = claim.with_name(claim.name + ".saved")
    foreign.write_bytes(b"FOREIGN-CLAIM")
    armed = {"value": True}

    def swap(candidate):
        candidate = Path(candidate)
        if armed["value"] and candidate == claim:
            armed["value"] = False
            claim.rename(saved)
            foreign.rename(claim)
            try:
                return real_entry_identity(candidate)
            finally:
                claim.rename(foreign)
                saved.rename(claim)
        return real_entry_identity(candidate)

    mod._ConnectorOwnership._entry_identity = staticmethod(swap)
    return real_entry_identity, foreign


def test_connector_named_claim_fd_entry_swap_restore_is_rejected_for_key(tmp):
    """Key publication binds the claim fd identity to its directory entry."""
    m = _mod()
    path = Path(tmp) / "swap-key"
    old_tmpfile = _force_portable(m)
    _old_entry, foreign = _claim_swap_restore(
        m, m._ConnectorLogWriter._named_publish_paths(path)[1])
    try:
        try:
            m._ConnectorLogWriter._publish_named_bytes(
                path, b"K" * 32, auth_key=b"K" * 32)
        except FileExistsError:
            pass
        else:
            raise AssertionError("claim fd/path identity swap was accepted")
    finally:
        m._ConnectorOwnership._entry_identity = staticmethod(_old_entry)
        _restore_portable(m, old_tmpfile)
    assert foreign.read_bytes() == b"FOREIGN-CLAIM"
    assert not m._ConnectorLogWriter._named_publish_paths(path)[1].exists()


def test_connector_named_claim_fd_entry_swap_restore_is_rejected_for_migration(tmp):
    """Migration publication rejects a transient claim pathname rebound."""
    m = _mod()
    path = Path(tmp) / "swap-migrate.log"
    lock_root = Path(tmp) / "lock-root"
    seed = m._ConnectorLogWriter(path, max_bytes=16, backup_count=1,
                                 lock_root=lock_root)
    seed.close()
    path.write_bytes(b"history\n" * 8)
    old_tmpfile = _force_portable(m)
    journal = lock_root / f"{seed._identity_digest}.migrate.json"
    _old_entry, foreign = _claim_swap_restore(m, m._ConnectorLogWriter._named_publish_paths(journal)[1])
    try:
        try:
            m._ConnectorLogWriter(path, max_bytes=16, backup_count=1,
                                  lock_root=lock_root)
        except FileExistsError:
            pass
        else:
            raise AssertionError("migration claim fd/path swap was accepted")
    finally:
        m._ConnectorOwnership._entry_identity = staticmethod(_old_entry)
        _restore_portable(m, old_tmpfile)
    assert foreign.read_bytes() == b"FOREIGN-CLAIM"


def test_connector_named_claim_fd_entry_swap_restore_is_rejected_for_rotation(tmp):
    """Rotation publication rejects a transient claim pathname rebound."""
    m = _mod()
    path = Path(tmp) / "swap-rotate.log"
    lock_root = Path(tmp) / "lock-root"
    seed = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                 lock_root=lock_root)
    seed.close()
    path.write_bytes(b"A" * 31)
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                   lock_root=lock_root)
    old_tmpfile = _force_portable(m)
    journal = writer._rotation_manifest_path()
    _old_entry, foreign = _claim_swap_restore(
        m, m._ConnectorLogWriter._named_publish_paths(journal)[1])
    try:
        try:
            writer.write("x")
        except FileExistsError:
            pass
        else:
            raise AssertionError("rotation claim fd/path swap was accepted")
    finally:
        m._ConnectorOwnership._entry_identity = staticmethod(_old_entry)
        _restore_portable(m, old_tmpfile)
        writer.close()
    assert foreign.read_bytes() == b"FOREIGN-CLAIM"


def test_connector_log_preserves_forged_journal_staging_file(tmp):
    """A JSON-shaped journal temp is not proof of ownership."""
    m = _mod()
    path = Path(tmp) / "forged-journal.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    journal = writer._migration_manifest_path()
    token = writer._identity_digest
    writer.close()

    staging = journal.with_name(journal.name + ".tmp")
    staging.write_text(json.dumps({
        "version": 2,
        "kind": "migrate",
        "token": token,
        "root": str(path.parent),
        "log_name": path.name,
    }), encoding="utf-8")

    restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    restarted.close()
    assert staging.read_text(encoding="utf-8")


def test_connector_log_cleanup_preserves_rebound_temp_after_validation(tmp):
    """Cleanup must not unlink a foreign inode replacing a validated temp."""
    m = _mod()
    case = Path(tmp) / "cleanup-rebound-temp"
    case.mkdir()
    path = case / "rebound.log"
    path.write_bytes(b"history\n" * 8)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_cleanup_rebound", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_stage = mod._ConnectorLogWriter._write_migration_temp
def stage_then_crash(self, data, mode, name=None, directory=None):
    result = real_stage(self, data, mode, name=name, directory=directory)
    if name is not None and ".migrate-" in str(name):
        os._exit(173)
    return result
mod._ConnectorLogWriter._write_migration_temp = stage_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        capture_output=True,
    )
    assert crashed.returncode == 173, crashed.stderr.decode("utf-8", "replace")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    owner = m._ConnectorOwnership(path, lock_inode=True, lock_root=lock_root)
    token = hashlib.sha256(owner.identity_token.encode("utf-8")).hexdigest()[:32]
    owner.close()
    journal = lock_root / f"{token}.migrate.json"
    manifest = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    staged = Path(manifest["created_temporaries"][0])
    assert staged.is_file()
    original_staged = staged.read_bytes()
    assert original_staged != b"FOREIGN-CLEANUP"

    real_read = m._ConnectorLogWriter._read_staged_payload
    swapped = {"done": False}

    def validate_then_replace(self, temporary, *args, **kwargs):
        payload = real_read(self, temporary, *args, **kwargs)
        if not swapped["done"]:
            swapped["done"] = True
            replacement = Path(temporary).with_name(
                Path(temporary).name + ".foreign")
            replacement.write_bytes(b"FOREIGN-CLEANUP")
            os.replace(replacement, temporary)
            assert temporary.read_bytes() == b"FOREIGN-CLEANUP"
        return payload
    m._ConnectorLogWriter._read_staged_payload = validate_then_replace
    try:
        restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        restarted.close()
    finally:
        m._ConnectorLogWriter._read_staged_payload = real_read
    assert swapped["done"]
    assert staged.read_bytes() == b"FOREIGN-CLEANUP"


def test_connector_log_first_stage_crash_loop_has_bounded_temps(tmp):
    """Hard exits after the first data temp do not accumulate nonce files."""
    m = _mod()
    del m
    case = Path(tmp) / "first-stage-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    path.write_bytes(b"historic line\n" * 12)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_first_stage_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = mod._ConnectorLogWriter._write_migration_temp
def write_then_crash(self, data, mode, name=None, directory=None):
    result = real_write(self, data, mode, name=name, directory=directory)
    if name is not None and ".migrate-" in str(name) and not str(name).endswith(".json.tmp"):
        os._exit(171)
    return result
mod._ConnectorLogWriter._write_migration_temp = write_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    outcomes = []
    for _ in range(8):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path)],
            capture_output=True,
        )
        assert crashed.returncode == 171, crashed.stderr.decode("utf-8", "replace")
        outcomes.append(crashed.returncode)
    hidden = [
        item for item in case.iterdir()
        if item.name.startswith(".") and not item.name.endswith(".lock")
    ]
    assert len(hidden) <= 4, [item.name for item in hidden]


def test_connector_log_partial_first_stage_crash_loop_has_bounded_temps(tmp):
    """A hard exit during the first temp write cannot grow nonce artifacts."""
    m = _mod()
    del m
    case = Path(tmp) / "partial-first-stage-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    path.write_bytes(b"historic line\n" * 12)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_partial_first_stage", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# Exercise the named-file fallback's partial-write recovery explicitly; the
# normal Linux path now keeps its anonymous inode invisible until complete.
mod._ConnectorLogWriter._write_anonymous_migration_temp = lambda *args, **kwargs: None
real_write = os.write
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
active = {"armed": False}
def record_claim(self, manifest, kind="migrate", create=False):
    result = real_manifest(self, manifest, kind=kind, create=create)
    if not create and manifest.get("created_temporaries"):
        active["armed"] = True
    return result
mod._ConnectorLogWriter._write_migration_manifest = record_claim
def partial_write(fd, data):
    if active["armed"]:
        # A short write leaves an invalid envelope, then the process dies
        # before the caller can append the creation record to the journal.
        active["armed"] = False
        real_write(fd, data[:max(1, len(data) // 2)])
        os._exit(170)
    return real_write(fd, data)
os.write = partial_write
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    for _ in range(8):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path)],
            capture_output=True,
        )
        assert crashed.returncode == 170, crashed.stderr.decode("utf-8", "replace")

    partials = sorted(case.glob(".loop.log.migrate-*.tmp"))
    assert partials, "the crash did not leave a non-vacuous partial temp"
    assert any(not item.read_bytes().endswith(b"\n") for item in partials)
    assert len(partials) <= 1, [item.name for item in partials]

    restarted = _mod()
    writer = restarted._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
    writer.close()
    assert not list(case.glob(".loop.log.migrate-*.tmp"))


def test_connector_log_claim_boundary_crash_loop_reclaims_created_temp(tmp):
    """A crash after staged-dir fsync cannot strand one temp per restart."""
    m = _mod()
    del m
    case = Path(tmp) / "claim-boundary-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    original = b"historic line\n" * 12
    path.write_bytes(original)
    lock_root = case / "lock-root"
    child = r'''
import importlib.util
import os
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_claim_boundary", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
real_fsync = mod._ConnectorLogWriter._fsync_directory
armed = {"value": False}
def record_intent(self, manifest, kind="migrate", create=False):
    result = real_manifest(self, manifest, kind=kind, create=create)
    if create and kind == "migrate":
        armed["value"] = True
    return result
def crash_after_staged_directory_fsync(directory):
    result = real_fsync(directory)
    if armed["value"] and Path(directory) == Path(sys.argv[2]).parent:
        os._exit(179)
    return result
mod._ConnectorLogWriter._write_migration_manifest = record_intent
mod._ConnectorLogWriter._fsync_directory = staticmethod(
    crash_after_staged_directory_fsync)
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2,
                        lock_root=sys.argv[3])
'''

    for _ in range(5):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             str(lock_root)], capture_output=True,
        )
        assert crashed.returncode == 179, crashed.stderr.decode("utf-8", "replace")

        restarted = _mod()._ConnectorLogWriter(
            path, max_bytes=16, backup_count=2, lock_root=lock_root)
        restarted.close()
        path.write_bytes(original)

    leftovers = sorted(case.glob(".loop.log.migrate-*.tmp"))
    assert len(leftovers) <= 1, [item.name for item in leftovers]


def test_connector_log_claim_callback_crash_loop_reclaims_created_temp(tmp):
    """A crash before the identity callback cannot strand an O_EXCL temp."""
    m = _mod()
    del m
    case = Path(tmp) / "claim-callback-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    original = b"historic line\n" * 12
    path.write_bytes(original)
    lock_root = case / "lock-root"
    child = r'''
import importlib.util
import os
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_claim_callback", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
real_identity = mod._ConnectorOwnership._identity_for
armed = {"value": False}
def record_intent(self, manifest, kind="migrate", create=False):
    result = real_manifest(self, manifest, kind=kind, create=create)
    if create and kind == "migrate":
        armed["value"] = True
    return result
def crash_before_claim(pathname):
    if armed["value"] and ".migrate-" in Path(pathname).name:
        os._exit(180)
    return real_identity(pathname)
mod._ConnectorLogWriter._write_migration_manifest = record_intent
mod._ConnectorOwnership._identity_for = staticmethod(crash_before_claim)
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2,
                        lock_root=sys.argv[3])
'''

    for _ in range(5):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             str(lock_root)], capture_output=True,
        )
        assert crashed.returncode == 180, crashed.stderr.decode("utf-8", "replace")

        restarted = _mod()._ConnectorLogWriter(
            path, max_bytes=16, backup_count=2, lock_root=lock_root)
        restarted.close()
        path.write_bytes(original)

    leftovers = sorted(case.glob(".loop.log.migrate-*.tmp"))
    assert len(leftovers) <= 1, [item.name for item in leftovers]


def test_connector_log_rotation_claim_callback_crash_loop_reclaims_temp(tmp):
    """Rotation staging has the same no-orphan callback crash guarantee."""
    m = _mod()
    del m
    case = Path(tmp) / "rotation-claim-callback-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    lock_root = case / "lock-root"
    child = r'''
import importlib.util
import os
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_rotation_claim_callback", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
real_identity = mod._ConnectorOwnership._identity_for
armed = {"value": False}
def record_intent(self, manifest, kind="migrate", create=False):
    result = real_manifest(self, manifest, kind=kind, create=create)
    if create and kind == "rotate":
        armed["value"] = True
    return result
def crash_before_claim(pathname):
    if armed["value"] and ".rotate-" in Path(pathname).name:
        os._exit(181)
    return real_identity(pathname)
mod._ConnectorLogWriter._write_migration_manifest = record_intent
mod._ConnectorOwnership._identity_for = staticmethod(crash_before_claim)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                 lock_root=sys.argv[3])
writer.write("x")
'''

    for _ in range(5):
        path.write_bytes(b"A" * 31)
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             str(lock_root)], capture_output=True,
        )
        assert crashed.returncode == 181, crashed.stderr.decode("utf-8", "replace")

        restarted = _mod()._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        restarted.close()

    leftovers = sorted(case.glob(".loop.log.rotate-*.tmp"))
    assert len(leftovers) <= 1, [item.name for item in leftovers]


def test_connector_log_partial_empty_rotation_stage_crash_loop_has_bounded_temps(tmp):
    """A torn empty rotation envelope cannot accumulate nonce artifacts."""
    m = _mod()
    magic_length = len(m._ConnectorLogWriter._TEMP_MAGIC)
    del m
    case = Path(tmp) / "partial-empty-rotation-stage-crash-loop"
    case.mkdir()
    path = case / "loop.log"
    path.write_bytes(b"a" * 31)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_partial_empty_rotation", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# Keep this fallback-specific torn-envelope test meaningful after anonymous
# staging was added for the normal rotation path.
mod._ConnectorLogWriter._write_anonymous_migration_temp = lambda *args, **kwargs: None
real_write = os.write
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
active = {"armed": False}
def record_claim(self, manifest, kind="migrate", create=False):
    result = real_manifest(self, manifest, kind=kind, create=create)
    if (kind == "rotate" and not create and manifest.get("created_temporaries")
            and any(entry.get("size") == 0 and
                    entry.get("temporary") in manifest["created_temporaries"]
                    for entry in manifest.get("destinations", []))):
        active["armed"] = True
    return result
mod._ConnectorLogWriter._write_migration_manifest = record_claim
def partial_write(fd, data):
    if active["armed"]:
        active["armed"] = False
        real_write(fd, data[:len(mod._ConnectorLogWriter._TEMP_MAGIC) - 1])
        os._exit(178)
    return real_write(fd, data)
os.write = partial_write
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1)
writer.write("x")
'''
    for _ in range(8):
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path)],
            capture_output=True,
        )
        assert crashed.returncode == 178, crashed.stderr.decode("utf-8", "replace")

    # Slot 1 is the empty active-stage envelope; slot 0 is the real backup
    # and may have completed before the hard exit.
    partials = sorted(case.glob(".loop.log.rotate-*-1.tmp"))
    assert partials, "the crash did not leave a non-vacuous partial temp"
    assert all(len(item.read_bytes()) < magic_length for item in partials)
    assert len(partials) <= 1, [item.name for item in partials]

    restarted = _mod()
    writer = restarted._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer.close()
    assert not list(case.glob(".loop.log.rotate-*.tmp"))


def test_connector_log_rotation_recovers_completed_sparse_backup_deletion(tmp):
    """A journaled deletion that already completed is safe to resume."""
    m = _mod()
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    del m
    case = Path(tmp) / "sparse-backup-delete-recovery"
    case.mkdir()
    path = case / "sparse.log"
    path.write_bytes(b"A" * 31)
    stale = path.with_name("sparse.log.2")
    stale.write_bytes(b"STALE-BACKUP")
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_sparse_delete", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_unlink = mod._ConnectorLogWriter._unlink_if_identity
def unlink_then_crash(path, *args, **kwargs):
    result = real_unlink(path, *args, **kwargs)
    if result and os.path.basename(os.fspath(path)) == "sparse.log.2":
        os._exit(179)
    return result
mod._ConnectorLogWriter._unlink_if_identity = staticmethod(unlink_then_crash)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=2)
writer.write("x")
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        capture_output=True,
    )
    assert crashed.returncode == 179, crashed.stderr.decode("utf-8", "replace")
    journals = list(lock_root.glob("*.rotate.json"))
    assert len(journals) == 1, [item.name for item in journals]
    manifest = json.loads(journals[0].read_text(encoding="utf-8").splitlines()[-1])
    deleted = next(entry for entry in manifest["destinations"]
                   if entry["destination"].endswith("sparse.log.2"))
    assert deleted["present"] is False
    assert deleted["publish_state"] == "publishing"
    assert deleted["publish_identity"] is not None

    restarted = _mod()
    writer = restarted._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
    writer.close()
    assert not stale.exists()
    assert path.with_name("sparse.log.1").read_bytes() == b"A" * 31
    assert path.read_bytes() == b""


def test_connector_log_unlink_identity_rejects_swap_after_final_validation(tmp):
    """A rebound pathname is not deleted after identity validation completes."""
    m = _mod()
    path = Path(tmp) / "unlink-race.log"
    path.write_bytes(b"OWNED-TEMP")
    expected_identity = m._ConnectorOwnership._identity_for(path)
    expected_entry_identity = m._ConnectorOwnership._entry_identity(path)
    real_entry_identity = m._ConnectorOwnership._entry_identity
    entry_checks = {"count": 0, "swapped": False}

    def entry_identity_then_swap(candidate):
        result = real_entry_identity(candidate)
        if (candidate == path and entry_checks["count"] == 1 and
                not entry_checks["swapped"]):
            entry_checks["swapped"] = True
            replacement = path.with_name("unlink-race-foreign.log")
            replacement.write_bytes(b"FOREIGN-KEEP")
            os.replace(replacement, path)
        if candidate == path:
            entry_checks["count"] += 1
        return result

    m._ConnectorOwnership._entry_identity = staticmethod(entry_identity_then_swap)
    try:
        removed = m._ConnectorLogWriter._unlink_if_identity(
            path, expected_identity, expected_entry_identity)
    finally:
        m._ConnectorOwnership._entry_identity = staticmethod(real_entry_identity)

    assert entry_checks["swapped"]
    assert not removed
    assert path.read_bytes() == b"FOREIGN-KEEP"


def test_connector_log_alias_recovery_refuses_rebound_live_stable_path(tmp):
    """Alias recovery cannot overwrite a path rebound to another live inode."""
    m = _mod()
    case = Path(tmp)
    private_lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    private_lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_sentinel = (private_lock_root /
                        f"unrelated-connector-{os.getpid()}.rotate.json")
    private_sentinel.write_text("do not delete", encoding="utf-8")
    ambient_sentinel = (Path.home() / ".discord-mailbox-log-locks" /
                        f"unrelated-connector-{os.getpid()}.rotate.json")
    ambient_before = (ambient_sentinel.exists(),
                      ambient_sentinel.read_bytes()
                      if ambient_sentinel.is_file() else None)
    target = case / "rebound-target.log"
    alias = case / "rebound-alias.log"
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    target.write_bytes(original)
    os.link(target, alias)

    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_rebound_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
def publish_then_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if calls[0] == 2:
        os._exit(118)
os.replace = publish_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(target)],
        capture_output=True,
    )
    assert crashed.returncode == 118, crashed.stderr.decode("utf-8", "replace")

    replacement = case / "rebound-replacement.log"
    replacement.write_bytes(b"LIVE-WRITER\n")
    os.replace(replacement, target)
    live = m._ConnectorLogWriter(target, max_bytes=16, backup_count=2)
    try:
        before = target.read_bytes()
        try:
            m._ConnectorLogWriter(alias, max_bytes=16, backup_count=2)
        except RuntimeError:
            pass
        else:
            raise AssertionError("alias recovery acquired a rebound live path")
        assert target.read_bytes() == before == b"LIVE-WRITER\n"
    finally:
        live.close()

    try:
        # The recovery attempt above must stay within this fixture-owned root;
        # in particular it must not sweep the ambient production namespace.
        assert private_sentinel.exists(), "cleanup deleted the fixture sentinel"
        ambient_after = (ambient_sentinel.exists(),
                         ambient_sentinel.read_bytes()
                         if ambient_sentinel.is_file() else None)
        assert ambient_after == ambient_before, "recovery touched ambient lock state"
    finally:
        private_sentinel.unlink(missing_ok=True)


def test_connector_log_hardlinked_backup_replacement_preserves_external_inode(tmp):
    """A hard-linked backup slot is replaced, never published in place."""
    m = _mod()
    path = Path(tmp) / "backup-chronology.log"
    path.write_bytes(b"A" * 31)
    path.with_name("backup-chronology.log.1").write_bytes(b"BACKUP-ONE\n")
    external = Path(tmp) / "external-owner.log"
    external.write_bytes(b"EXTERNAL-KEEP\n")
    os.link(external, path.with_name("backup-chronology.log.2"))

    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
    try:
        writer.write("x")
    finally:
        writer.close()

    assert external.read_bytes() == b"EXTERNAL-KEEP\n"
    assert path.with_name("backup-chronology.log.2").read_bytes() == b"BACKUP-ONE\n"


def test_connector_log_preserves_unrelated_migration_notes(tmp):
    """Startup cleanup does not delete a user-owned migrate-notes file."""
    m = _mod()
    path = Path(tmp) / "custom.log"
    path.write_text("bounded\n", encoding="utf-8")
    note = path.with_name(".custom.log.migrate-notes")
    note.write_text("keep me", encoding="utf-8")
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    writer.close()
    assert note.read_text(encoding="utf-8") == "keep me"


def test_connector_log_preserves_user_temp_with_embedded_migration_token(tmp):
    """Cleanup cannot delete a user temp whose name merely contains our token."""
    m = _mod()
    path = Path(tmp) / "custom.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    digest = writer._identity_digest
    writer.close()

    note = path.with_name(
        f".{path.name}.user-notes.migrate-{digest}-42.tmp")
    note.write_text("KEEP-ME", encoding="utf-8")

    restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    restarted.close()
    assert note.read_text(encoding="utf-8") == "KEEP-ME"


def test_connector_log_preserves_user_temp_with_embedded_rotation_token(tmp):
    """Rotation recovery cannot delete a user temp with an embedded token."""
    m = _mod()
    path = Path(tmp) / "custom.log"
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    digest = writer._identity_digest
    writer.close()

    note = path.with_name(
        f".{path.name}.user-notes.rotate-{digest}-42.tmp")
    note.write_text("KEEP-ME", encoding="utf-8")

    restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    restarted.close()
    assert note.read_text(encoding="utf-8") == "KEEP-ME"


def test_connector_log_ownership_excludes_a_second_process(tmp):
    """A second process cannot open or rotate a path owned by the first."""
    m = _mod()
    path = Path(tmp) / "owned.log"
    child = r'''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("discord_mb_child", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=2)
print("READY", flush=True)
input()
writer.close()
'''
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "READY"
        try:
            writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=2)
        except RuntimeError:
            pass
        else:
            writer.close()
            raise AssertionError("a second process acquired the connector log")
    finally:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""


def test_connector_log_ownership_locks_complete_rotation_namespace(tmp):
    """A live active connector excludes another connector on its backup slot."""
    m = _mod()
    path = Path(tmp) / "shared.log"
    child = r'''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("discord_mb_namespace_child", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=2)
print("READY", flush=True)
input()
writer.close()
'''
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "READY"
        backup_writer = None
        try:
            backup_writer = m._ConnectorLogWriter(
                path.with_name("shared.log.1"), max_bytes=32, backup_count=2)
        except RuntimeError:
            pass
        else:
            backup_writer.close()
            raise AssertionError(
                "a live connector acquired a pathname in another rotation namespace")
    finally:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""


def test_connector_startup_exception_releases_ownership_and_pidfile(tmp):
    """An embedded startup failure must not strand the identity lock or PID."""
    m = _mod()
    state = Path(tmp) / "state"
    state.mkdir()
    pidfile = state / "connector.pid"
    path = Path(tmp) / "startup.log"
    real_writer = m._ConnectorLogWriter
    created = []

    class TrackingWriter:
        def __init__(self, *args, **kwargs):
            self.inner = real_writer(*args, **kwargs)
            self.closed = False
            created.append(self)

        def write(self, line):
            return self.inner.write(line)

        def close(self):
            self.closed = True
            return self.inner.close()

    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("injected connector startup failure")

    fake_discord = types.SimpleNamespace(
        Intents=types.SimpleNamespace(default=lambda: types.SimpleNamespace()),
        Client=ExplodingClient,
    )
    old_discord = sys.modules.get("discord")
    old_state_dir = m.state_dir
    old_sweep = m.sweep_status_plugin
    old_writer = m._ConnectorLogWriter
    m.state_dir = lambda identity: state
    m.sweep_status_plugin = lambda identity: None
    m._ConnectorLogWriter = TrackingWriter
    sys.modules["discord"] = fake_discord
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                m.connector_main("identity", claude_pid=os.getpid(), token="token",
                                 log_path=path, flavor="claude")
            except RuntimeError as exc:
                assert "injected connector startup failure" in str(exc)
            else:
                raise AssertionError("injected startup failure was swallowed")
    finally:
        m.state_dir = old_state_dir
        m.sweep_status_plugin = old_sweep
        m._ConnectorLogWriter = old_writer
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord

    assert created and created[0].closed, "log writer was not closed on startup failure"
    assert not pidfile.exists(), "startup failure left connector.pid behind"
    owner = m._ConnectorOwnership(pidfile)
    owner.close()


def test_connector_log_lock_namespace_is_tmpdir_independent_with_injection(tmp):
    """TMPDIR changes do not split hard-link locks or recovery journals."""
    m = _mod()
    default_child = r'''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("discord_mb_namespace", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(mod._ConnectorOwnership._IDENTITY_LOCK_ROOT, flush=True)
'''
    roots = []
    for name in ("tmp-a", "tmp-b"):
        env = os.environ.copy()
        env.pop(TEST_LOCK_ROOT_ENV, None)
        env["TMPDIR"] = str(Path(tmp) / name)
        Path(env["TMPDIR"]).mkdir()
        result = subprocess.run(
            [sys.executable, "-c", default_child, str(Path(MB))],
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        roots.append(Path(result.stdout.strip()))
    assert roots[0] == roots[1]

    lock_root = Path(tmp) / "explicit-lock-root"
    path = Path(tmp) / "tmpdir-contention.log"
    alias = Path(tmp) / "tmpdir-contention-alias.log"
    path.write_bytes(b"")
    os.link(path, alias)
    child = r'''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("discord_mb_tmpdir_owner", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                 lock_root=sys.argv[3])
print("READY", flush=True)
input()
writer.close()
'''
    env = os.environ.copy()
    env.pop(TEST_LOCK_ROOT_ENV, None)
    env["TMPDIR"] = str(Path(tmp) / "tmp-a")
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(Path(MB)), str(path), str(lock_root)],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "READY"
        old_tmpdir = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(Path(tmp) / "tmp-b")
        try:
            try:
                m._ConnectorLogWriter(alias, max_bytes=32, backup_count=1,
                                      lock_root=lock_root)
            finally:
                if old_tmpdir is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = old_tmpdir
        except RuntimeError:
            pass
        else:
            raise AssertionError("TMPDIR split an explicit identity lock root")
    finally:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
        proc.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""

    recovery_target = Path(tmp) / "tmpdir-recovery.log"
    recovery_alias = Path(tmp) / "tmpdir-recovery-alias.log"
    original = b"".join(f"{i:02d}\n".encode() for i in range(12))
    recovery_target.write_bytes(original)
    os.link(recovery_target, recovery_alias)
    crash = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_tmpdir_recovery", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
def replace_then_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if calls[0] == 2:
        os._exit(175)
os.replace = replace_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2,
                        lock_root=sys.argv[3])
'''
    recovery_env = os.environ.copy()
    recovery_env.pop(TEST_LOCK_ROOT_ENV, None)
    recovery_env["TMPDIR"] = str(Path(tmp) / "tmp-a")
    crashed = subprocess.run(
        [sys.executable, "-c", crash, str(Path(MB)), str(recovery_target),
         str(lock_root)], env=recovery_env, capture_output=True,
    )
    assert crashed.returncode == 175, crashed.stderr.decode("utf-8", "replace")
    old_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(Path(tmp) / "tmp-b")
    try:
        restarted = m._ConnectorLogWriter(
            recovery_alias, max_bytes=16, backup_count=2, lock_root=lock_root)
        restarted.close()
    finally:
        if old_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmpdir
    slots = [recovery_target.with_name("tmpdir-recovery.log.2"),
             recovery_target.with_name("tmpdir-recovery.log.1"), recovery_target]
    assert b"".join(slot.read_bytes() for slot in slots if slot.exists()) == original
    assert recovery_alias.read_bytes() == recovery_target.read_bytes()


def test_connector_lock_root_rejects_untrusted_shapes(tmp):
    """Predictable connector roots reject links, foreign owners, and modes."""
    m = _mod()
    path = Path(tmp) / "trusted-shape.log"
    real_root = Path(tmp) / "real-root"
    real_root.mkdir(mode=0o700)
    link_root = Path(tmp) / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    try:
        m._ConnectorOwnership(path, lock_inode=True, lock_root=link_root)
    except RuntimeError:
        pass
    else:
        raise AssertionError("symlink lock root was adopted")

    permissive = Path(tmp) / "permissive-root"
    permissive.mkdir(mode=0o777)
    os.chmod(permissive, 0o777)
    try:
        m._ConnectorOwnership(path, lock_inode=True, lock_root=permissive)
    except RuntimeError:
        pass
    else:
        raise AssertionError("world-writable lock root was adopted")

    regular_file = Path(tmp) / "regular-file-root"
    regular_file.write_text("not a directory", encoding="utf-8")
    try:
        m._ConnectorOwnership(path, lock_inode=True, lock_root=regular_file)
    except RuntimeError:
        pass
    else:
        raise AssertionError("regular-file lock root was adopted")

    foreign = Path(tmp) / "foreign-root"
    foreign.mkdir(mode=0o700)
    if hasattr(os, "chown") and hasattr(os, "geteuid"):
        foreign_uid = os.geteuid() + 1
        try:
            os.chown(foreign, foreign_uid, -1)
        except OSError:
            pass
        else:
            try:
                m._ConnectorOwnership(path, lock_inode=True, lock_root=foreign)
            except RuntimeError:
                pass
            else:
                raise AssertionError("foreign-owned lock root was adopted")


def test_connector_staging_key_lifecycle_rejects_tampering(tmp):
    """The persistent HMAC key is private, complete, and never adopted."""
    m = _mod()
    for kind in ("symlink", "permissive", "partial"):
        case = Path(tmp) / f"staging-key-{kind}"
        case.mkdir()
        root = case / "lock-root"
        path = case / "key.log"
        writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                       lock_root=root)
        key_path = writer._staging_key_path()
        key_bytes = key_path.read_bytes()
        writer.close()
        if kind == "symlink":
            key_path.unlink()
            target = case / "foreign-key"
            target.write_bytes(key_bytes)
            key_path.symlink_to(target)
        elif kind == "permissive":
            os.chmod(key_path, 0o644)
        else:
            key_path.write_bytes(key_bytes[:1])
            os.chmod(key_path, 0o600)
        try:
            m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                  lock_root=root)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"tampered {kind} staging key was adopted")


def test_connector_staging_key_rejects_transient_swap_restore_during_open(tmp):
    """A path restored after open cannot disguise a different opened inode."""
    m = _mod()
    path = Path(tmp) / "connector-staging.key"
    original = b"A" * m._ConnectorLogWriter._STAGING_KEY_BYTES
    rebound = b"B" * m._ConnectorLogWriter._STAGING_KEY_BYTES
    path.write_bytes(original)
    path.chmod(0o600)
    replacement = path.with_name("foreign-key")
    replacement.write_bytes(rebound)
    replacement.chmod(0o600)
    saved = path.with_name("saved-key")
    real_open = m.os.open
    armed = {"value": True}

    def swap_restore_open(candidate, flags, *args):
        if armed["value"] and Path(candidate) == path:
            armed["value"] = False
            path.rename(saved)
            replacement.rename(path)
            try:
                fd = real_open(candidate, flags, *args)
            finally:
                path.rename(replacement)
                saved.rename(path)
            return fd
        return real_open(candidate, flags, *args)

    m.os.open = swap_restore_open
    try:
        try:
            m._ConnectorLogWriter._read_secure_staging_key(path)
        except RuntimeError as exc:
            assert "changed while reading" in str(exc)
        else:
            raise AssertionError("transient swap/restore was accepted")
    finally:
        m.os.open = real_open
    assert path.read_bytes() == original
    assert replacement.read_bytes() == rebound


def test_connector_staging_key_first_create_partial_restart_is_recoverable(tmp):
    """A crash during virgin key publication cannot wedge later writers."""
    case = Path(tmp) / "first-staging-key-crash"
    case.mkdir()
    path = case / "connector.log"
    lock_root = case / "lock-root"
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_first_key_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = os.write
real_create = mod._ConnectorLogWriter._create_staging_key
armed = {"value": False}
def create(path):
    armed["value"] = True
    return real_create(path)
def partial_key_write(fd, data):
    if armed["value"] and len(data) == mod._ConnectorLogWriter._STAGING_KEY_BYTES:
        armed["value"] = False
        real_write(fd, data[:max(1, len(data) // 2)])
        os._exit(182)
    return real_write(fd, data)
mod._ConnectorLogWriter._create_staging_key = staticmethod(create)
os.write = partial_key_write
mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                        lock_root=sys.argv[3])
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path),
         str(lock_root)], capture_output=True,
    )
    assert crashed.returncode == 182, crashed.stderr.decode("utf-8", "replace")

    key_path = lock_root / "connector-staging.key"
    residue = key_path.read_bytes() if key_path.exists() else b""
    assert residue != b"" and len(residue) < 32 or not residue

    expected_key = None
    for _ in range(4):
        restarted = _mod()._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        current_key = key_path.read_bytes()
        assert len(current_key) == 32
        if expected_key is None:
            expected_key = current_key
        else:
            assert current_key == expected_key
        restarted.close()

    assert not list(lock_root.glob(".connector-staging.key-*.tmp"))
    assert not list(lock_root.glob(".connector-staging.key.create-*.tmp"))


def test_connector_staging_key_partial_restart_preserves_foreign_replacement(tmp):
    """Recovery of a torn first create never adopts a replacement key."""
    case = Path(tmp) / "first-staging-key-foreign"
    case.mkdir()
    path = case / "connector.log"
    lock_root = case / "lock-root"
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_first_key_foreign", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = os.write
real_create = mod._ConnectorLogWriter._create_staging_key
armed = {"value": False}
def create(path):
    armed["value"] = True
    return real_create(path)
def partial_key_write(fd, data):
    if armed["value"] and len(data) == mod._ConnectorLogWriter._STAGING_KEY_BYTES:
        armed["value"] = False
        real_write(fd, data[:1])
        os._exit(183)
    return real_write(fd, data)
mod._ConnectorLogWriter._create_staging_key = staticmethod(create)
os.write = partial_key_write
mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                        lock_root=sys.argv[3])
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path),
         str(lock_root)], capture_output=True,
    )
    assert crashed.returncode == 183, crashed.stderr.decode("utf-8", "replace")

    key_path = lock_root / "connector-staging.key"
    foreign = b"foreign-partial-key"
    key_path.write_bytes(foreign)
    try:
        _mod()._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
    except RuntimeError:
        pass
    else:
        raise AssertionError("foreign key residue was adopted")
    assert key_path.read_bytes() == foreign


def test_connector_staging_key_concurrent_virgin_creation_keeps_one_trusted_key(tmp):
    """Concurrent first users both succeed and share one complete key."""
    child = r'''
import importlib.util
import sys
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_concurrent_key", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
start = Path(sys.argv[4])
while not start.exists():
    time.sleep(0.001)
try:
    writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                     lock_root=sys.argv[3])
except Exception as exc:
    print("ERROR:" + type(exc).__name__, flush=True)
else:
    print("READY:" + writer._current_staging_key().hex(), flush=True)
    input()
    writer.close()
'''
    for trial in range(8):
        case = Path(tmp) / f"concurrent-staging-key-{trial}"
        case.mkdir()
        lock_root = case / "lock-root"
        start = case / "start"
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", child, str(Path(MB)),
                 str(case / f"connector-{index}.log"), str(lock_root), str(start)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            for index in (1, 2)
        ]
        try:
            start.touch()
            outputs = [process.stdout.readline().strip() for process in processes]
            assert len(outputs) == 2, outputs
            assert all(output.startswith("READY:") for output in outputs), outputs
            assert all(len(output.split(":", 1)[1]) == 64 for output in outputs)
            assert outputs[0] == outputs[1], outputs
        finally:
            for process in processes:
                if process.poll() is None and process.stdin is not None:
                    try:
                        process.stdin.write("\n")
                        process.stdin.flush()
                    except BrokenPipeError:
                        pass
                    finally:
                        process.stdin.close()
                process.wait(timeout=5)
        key_path = lock_root / "connector-staging.key"
        key = key_path.read_bytes()
        assert len(key) == 32
        assert key.hex() == outputs[0].split(":", 1)[1]
        assert key_path.stat().st_nlink == 1
        assert not list(lock_root.glob(".connector-staging.key-*.tmp"))


def test_connector_staging_key_authority_is_durable_and_matches_published_key(tmp):
    """The bootstrap secret is minted by a durable authority, not by the file.

    Every portable recovery needs a durable authenticator that exists before
    the first filesystem publication.  The authority therefore has to survive
    losing the published file and hand back the very same secret instead of
    minting a competing one.
    """
    case = Path(tmp) / "bootstrap-authority"
    case.mkdir()
    path = case / "connector.log"
    lock_root = case / "lock-root"
    m = _mod()
    writer = m._ConnectorLogWriter(
        path, max_bytes=32, backup_count=1, lock_root=lock_root)
    key = writer._current_staging_key()
    writer.close()

    key_path = lock_root / "connector-staging.key"
    authority = m._ConnectorLogWriter._staging_key_authority_path(key_path)
    assert authority.parent == lock_root, authority
    assert authority.exists(), sorted(item.name for item in lock_root.iterdir())
    assert not (authority.stat().st_mode & 0o077), oct(authority.stat().st_mode)
    assert len(key) == 32
    assert key_path.read_bytes() == key
    assert m._ConnectorLogWriter._authority_staging_key(authority) == key

    key_path.unlink()
    restarted = m._ConnectorLogWriter(
        path, max_bytes=32, backup_count=1, lock_root=lock_root)
    assert restarted._current_staging_key() == key
    restarted.close()
    assert key_path.read_bytes() == key


def test_connector_staging_key_authority_adopts_a_preexisting_raw_key_file(tmp):
    """An existing raw 32-byte key stays canonical and seeds the authority."""
    case = Path(tmp) / "legacy-raw-key"
    case.mkdir()
    path = case / "connector.log"
    lock_root = case / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    key_path = lock_root / "connector-staging.key"
    legacy = bytes(range(32))
    key_path.write_bytes(legacy)
    key_path.chmod(0o600)

    m = _mod()
    writer = m._ConnectorLogWriter(
        path, max_bytes=32, backup_count=1, lock_root=lock_root)
    assert writer._current_staging_key() == legacy
    writer.write("legacy key still signs staged payloads\n")
    writer.close()
    assert key_path.read_bytes() == legacy
    authority = m._ConnectorLogWriter._staging_key_authority_path(key_path)
    assert m._ConnectorLogWriter._authority_staging_key(authority) == legacy


def _legacy_key_lock_root(case, legacy):
    """Seed a private lock root holding only a raw 32-byte staging key."""
    case.mkdir()
    lock_root = case / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    key_path = lock_root / "connector-staging.key"
    key_path.write_bytes(legacy)
    key_path.chmod(0o600)
    return case / "connector.log", lock_root, key_path


def test_connector_staging_key_authority_seeds_from_raw_key_after_torn_creation(tmp):
    """A half-created authority still adopts the raw key it must agree with.

    ``_open_staging_key_authority`` creates the database file before the
    seeding transaction commits, so a crash in that window leaves an authority
    that exists but holds no row.  Existence is therefore not evidence of
    seeding: the next start has to ask the authority itself, adopt the raw key
    into it, and every later writer has to keep using that one key — including
    after the raw file is gone.
    """
    import sqlite3

    m = _mod()
    legacy = bytes(range(31, 63))
    for torn in ("zero-byte", "valid-empty-database"):
        path, lock_root, key_path = _legacy_key_lock_root(
            Path(tmp) / f"torn-authority-{torn}", legacy)
        authority = m._ConnectorLogWriter._staging_key_authority_path(key_path)
        if torn == "zero-byte":
            os.close(os.open(str(authority),
                             os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
        else:
            os.close(os.open(str(authority),
                             os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            connection = sqlite3.connect(str(authority))
            try:
                connection.execute(
                    'CREATE TABLE IF NOT EXISTS staging_key ('
                    'id INTEGER PRIMARY KEY CHECK (id = 1), '
                    'key BLOB NOT NULL)')
                connection.commit()
            finally:
                connection.close()
        assert authority.exists(), torn

        writer = m._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        assert writer._current_staging_key() == legacy, torn
        writer.close()
        assert key_path.read_bytes() == legacy, torn
        assert m._ConnectorLogWriter._authority_staging_key(
            authority) == legacy, torn

        # The raw file disappearing is the event that turns an unseeded
        # authority into a wedged deployment: the republished key must still
        # be the one every existing journal residue was signed with.
        key_path.unlink()
        restarted = m._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        assert restarted._current_staging_key() == legacy, torn
        restarted.close()
        assert key_path.read_bytes() == legacy, torn


def test_connector_staging_key_raw_file_wins_over_a_disagreeing_authority(tmp):
    """Adoption never overwrites a seeded authority, and the file still wins."""
    m = _mod()
    legacy = bytes(range(63, 95))
    path, lock_root, key_path = _legacy_key_lock_root(
        Path(tmp) / "authority-disagrees", legacy)
    authority = m._ConnectorLogWriter._staging_key_authority_path(key_path)
    seeded = m._ConnectorLogWriter._authority_staging_key(
        authority, adopt=b"S" * 32)
    assert seeded == b"S" * 32

    writer = m._ConnectorLogWriter(
        path, max_bytes=32, backup_count=1, lock_root=lock_root)
    assert writer._current_staging_key() == legacy
    writer.close()
    assert key_path.read_bytes() == legacy
    assert m._ConnectorLogWriter._authority_staging_key(authority) == b"S" * 32


def _bootstrap_destination_link_child():
    """Return a child which dies before the virgin key is ever published."""
    return r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_bootstrap_link", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
real_link = os.link
def interrupt_before_destination(source, destination, *args, **kwargs):
    if os.path.basename(str(destination)) == "connector-staging.key":
        os._exit(int(sys.argv[4]))
    return real_link(source, destination, *args, **kwargs)
os.link = interrupt_before_destination
mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                        lock_root=sys.argv[3])
'''


def _bootstrap_partial_payload_child():
    """Return a child which tears the virgin key payload mid-write."""
    return r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_bootstrap_payload", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
armed = {"value": False}
real_publish = mod._ConnectorLogWriter._publish_named_bytes
def publish(path, payload, mode=0o600, auth_key=None):
    if os.path.basename(str(path)) == "connector-staging.key":
        armed["value"] = True
    return real_publish(path, payload, mode=mode, auth_key=auth_key)
mod._ConnectorLogWriter._publish_named_bytes = staticmethod(publish)
real_write = os.write
def partial_key_payload(fd, data):
    if armed["value"] and len(data) == mod._ConnectorLogWriter._STAGING_KEY_BYTES:
        armed["value"] = False
        real_write(fd, bytes(data)[:len(data) // 2])
        os._exit(int(sys.argv[4]))
    return real_write(fd, data)
os.write = partial_key_payload
mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                        lock_root=sys.argv[3])
'''


def _restart_bootstrap_twice(m, path, lock_root, key_path):
    """Restart twice, returning the agreed key and each lock-root listing."""
    keys = set()
    listings = []
    for _ in range(2):
        writer = m._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        keys.add(writer._current_staging_key())
        writer.close()
        assert len(key_path.read_bytes()) == 32
        listings.append(sorted(item.name for item in lock_root.iterdir()))
    assert len(keys) == 1, "restarts disagreed about the durable bootstrap key"
    return keys.pop(), listings


def test_connector_staging_key_bootstrap_transaction_residue_recovers_twice(tmp):
    """A virgin portable bootstrap killed before publication restarts itself."""
    case = Path(tmp) / "bootstrap-transaction-residue"
    case.mkdir()
    path = case / "connector.log"
    lock_root = case / "lock-root"
    crashed = subprocess.run(
        [sys.executable, "-c", _bootstrap_destination_link_child(),
         str(Path(MB)), str(path), str(lock_root), "202"],
        capture_output=True,
    )
    assert crashed.returncode == 202, crashed.stderr.decode("utf-8", "replace")

    key_path = lock_root / "connector-staging.key"
    assert not key_path.exists(), "the destination was published before the exit"
    proof = _mod()._ConnectorLogWriter._named_claim_proof_path(key_path)
    stage = _mod()._ConnectorLogWriter._named_claim_staging_path(key_path)
    assert proof.exists(), "no writer-created proof residue to recover"
    assert stage.exists(), "no writer-created stage residue to recover"

    m = _mod()
    old = _force_portable(m)
    try:
        _key, listings = _restart_bootstrap_twice(m, path, lock_root, key_path)
    finally:
        _restore_portable(m, old)
    assert not proof.exists() and not stage.exists()
    assert not list(lock_root.glob("*.create.*")), sorted(
        item.name for item in lock_root.glob("*.create.*"))
    assert not list(lock_root.glob("*.create-proof"))
    assert listings[0] == listings[1], listings


def test_connector_staging_key_bootstrap_partial_payload_recovers_twice(tmp):
    """A torn virgin key payload is reclaimed instead of wedging every start."""
    case = Path(tmp) / "bootstrap-partial-payload"
    case.mkdir()
    path = case / "connector.log"
    lock_root = case / "lock-root"
    crashed = subprocess.run(
        [sys.executable, "-c", _bootstrap_partial_payload_child(),
         str(Path(MB)), str(path), str(lock_root), "203"],
        capture_output=True,
    )
    assert crashed.returncode == 203, crashed.stderr.decode("utf-8", "replace")

    key_path = lock_root / "connector-staging.key"
    assert not key_path.exists()
    _temporary, _claim, payload_path = \
        _mod()._ConnectorLogWriter._named_publish_paths(key_path)
    torn = payload_path.read_bytes()
    assert 0 < len(torn) < 32, torn

    m = _mod()
    old = _force_portable(m)
    try:
        key, listings = _restart_bootstrap_twice(m, path, lock_root, key_path)
    finally:
        _restore_portable(m, old)
    assert not payload_path.exists()
    assert not list(lock_root.glob("*.create.*")), sorted(
        item.name for item in lock_root.glob("*.create.*"))
    assert not list(lock_root.glob("*.create-proof"))
    assert key_path.read_bytes() == key
    assert listings[0] == listings[1], listings


def test_connector_staging_key_bootstrap_preserves_unauthenticated_residue(tmp):
    """A durable authority never turns a foreign fixed entry into ownership."""
    m = _mod()
    old = _force_portable(m)
    try:
        for case_name in ("foreign-proof", "foreign-pair", "rebound-stage"):
            case = Path(tmp) / f"authority-{case_name}"
            case.mkdir()
            path = case / "connector.log"
            lock_root = case / "lock-root"
            lock_root.mkdir(mode=0o700)
            lock_root.chmod(0o700)
            key_path = lock_root / "connector-staging.key"
            authority = m._ConnectorLogWriter._staging_key_authority_path(
                key_path)
            key = m._ConnectorLogWriter._authority_staging_key(authority)
            assert len(key) == 32
            proof = m._ConnectorLogWriter._named_claim_proof_path(key_path)
            stage = m._ConnectorLogWriter._named_claim_staging_path(key_path)
            if case_name == "rebound-stage":
                # A genuine record, but the stage names a different inode:
                # this pair is not the one a transaction of ours created.
                record = m._ConnectorLogWriter._named_claim_record(
                    key_path, b"R" * 32, 0o600, auth_key=key)
                proof.write_bytes(record)
                proof.chmod(0o600)
                stage.write_bytes(record)
                stage.chmod(0o600)
            else:
                # The exact bytes an old build accepted, MAC'd with a secret
                # that is not this lock root's durable bootstrap key.
                record = m._ConnectorLogWriter._named_claim_record(
                    key_path, b"F" * 32, 0o600, auth_key=b"F" * 32)
                proof.write_bytes(record)
                proof.chmod(0o600)
                if case_name == "foreign-pair":
                    os.link(proof, stage)
            stage_before = stage.read_bytes() if stage.exists() else None
            # Assert the reason, not merely that construction failed: an
            # unrelated future breakage must not keep this green.
            expected_reason = ('named create claim stage proof mismatch'
                               if case_name == "rebound-stage"
                               else 'named create proof has invalid provenance')
            for _ in range(2):
                try:
                    m._ConnectorLogWriter(
                        path, max_bytes=32, backup_count=1,
                        lock_root=lock_root)
                except (OSError, RuntimeError) as exc:
                    assert expected_reason in str(exc), (case_name, str(exc))
                else:
                    raise AssertionError(
                        f"{case_name} bootstrap residue was adopted")
                assert proof.read_bytes() == record, case_name
                if stage_before is None:
                    assert not stage.exists(), case_name
                else:
                    assert stage.read_bytes() == stage_before, case_name
                assert not key_path.exists(), case_name
    finally:
        _restore_portable(m, old)


def test_connector_staging_key_portable_concurrent_virgin_creation_converges(tmp):
    """Portable virgin writers race for one key and all agree on the winner."""
    child = r'''
import importlib.util
import sys
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_portable_key_race", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
start = Path(sys.argv[4])
while not start.exists():
    time.sleep(0.001)
try:
    writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                     lock_root=sys.argv[3])
except Exception as exc:
    print("ERROR:" + type(exc).__name__ + ":" + str(exc), flush=True)
else:
    print("READY:" + writer._current_staging_key().hex(), flush=True)
    input()
    writer.close()
'''
    for trial in range(4):
        case = Path(tmp) / f"portable-key-race-{trial}"
        case.mkdir()
        lock_root = case / "lock-root"
        start = case / "start"
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", child, str(Path(MB)),
                 str(case / f"connector-{index}.log"), str(lock_root),
                 str(start)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            for index in (1, 2, 3)
        ]
        try:
            start.touch()
            outputs = [process.stdout.readline().strip()
                       for process in processes]
            assert all(output.startswith("READY:") for output in outputs), outputs
            assert len(set(outputs)) == 1, outputs
        finally:
            for process in processes:
                if process.poll() is None and process.stdin is not None:
                    try:
                        process.stdin.write("\n")
                        process.stdin.flush()
                    except BrokenPipeError:
                        pass
                    finally:
                        process.stdin.close()
                process.wait(timeout=10)
        key_path = lock_root / "connector-staging.key"
        key = key_path.read_bytes()
        assert len(key) == 32
        assert key.hex() == outputs[0].split(":", 1)[1]
        assert key_path.stat().st_nlink == 1
        assert not list(lock_root.glob("*.create.*")), sorted(
            item.name for item in lock_root.glob("*.create.*"))
        assert not list(lock_root.glob("*.create-proof"))


def test_connector_named_fallback_crash_loops_do_not_accumulate_uuid_temps(tmp):
    """Portable first-publication crashes keep a bounded residue per target."""
    scenarios = (
        ("key", 186, ".connector-staging.key.create-*.tmp"),
        ("migrate", 187, ".*.migrate.json.create-*.tmp"),
        ("rotate", 188, ".*.rotate.json.create-*.tmp"),
    )
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_named_fallback_crash", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
real_link = os.link
def crash_payload_link(source, destination, *args, **kwargs):
    if str(destination).endswith(".create.tmp"):
        os._exit(int(sys.argv[4]))
    return real_link(source, destination, *args, **kwargs)
mod.os.link = crash_payload_link
kind = sys.argv[3]
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32 if kind == "rotate" else 16,
                                 backup_count=1, lock_root=sys.argv[5])
if kind == "rotate":
    writer.write("x")
'''
    for kind, code, pattern in scenarios:
        case = Path(tmp) / f"named-fallback-{kind}"
        case.mkdir()
        path = case / f"{kind}.log"
        lock_root = case / "lock-root"
        journal_name = "connector-staging.key"
        if kind == "key":
            pass
        elif kind == "migrate":
            seed = _mod()._ConnectorLogWriter(
                path, max_bytes=16, backup_count=1, lock_root=lock_root)
            journal_name = seed._migration_manifest_path().name
            seed.close()
            path.write_bytes(b"history\n" * 8)
        else:
            seed = _mod()._ConnectorLogWriter(
                path, max_bytes=32, backup_count=1, lock_root=lock_root)
            journal_name = seed._rotation_manifest_path().name
            seed.close()
            path.write_bytes(b"A" * 31)
        attempts = 5
        for _ in range(attempts):
            crashed = subprocess.run(
                [sys.executable, "-c", child, str(Path(MB)), str(path),
                 kind, str(code), str(lock_root)], capture_output=True,
            )
            assert crashed.returncode == code, (
                kind, crashed.returncode,
                crashed.stderr.decode("utf-8", "replace"),
            )
        residue = sorted(lock_root.glob(pattern))
        assert not residue, [item.name for item in residue]
        claim_residue = sorted(
            item.name for item in lock_root.glob(f".{journal_name}.create.*"))
        assert claim_residue == [
            f".{journal_name}.create.claim",
            f".{journal_name}.create.claim.tmp",
            f".{journal_name}.create.payload",
        ], claim_residue
        assert len(list(lock_root.glob("*.create-proof"))) <= 1

        if kind == "key":
            for _ in range(2):
                restarted = _mod()._ConnectorLogWriter(
                    path, max_bytes=32, backup_count=1, lock_root=lock_root)
                assert len(restarted._current_staging_key()) == 32
                restarted.close()
            assert len((lock_root / journal_name).read_bytes()) == 32
        elif kind == "migrate":
            restarted = _mod()._ConnectorLogWriter(
                path, max_bytes=16, backup_count=1, lock_root=lock_root)
            restarted.close()
        else:
            restarted = _mod()._ConnectorLogWriter(
                path, max_bytes=32, backup_count=1, lock_root=lock_root)
            restarted.write("x")
            restarted.close()
        assert not list(lock_root.glob(f".{journal_name}.create.*"))
        assert not list(lock_root.glob("*.create-proof"))


def _named_claim_scenario(tmp, kind):
    """Seed one named-publication scenario and return its fixed destination."""
    case = Path(tmp) / f"named-claim-half-state-{kind}"
    case.mkdir()
    path = case / f"{kind}.log"
    lock_root = case / "lock-root"
    if kind == "key":
        lock_root.mkdir(mode=0o700)
        lock_root.chmod(0o700)
        destination = lock_root / "connector-staging.key"
    elif kind == "migrate":
        seed = _mod()._ConnectorLogWriter(
            path, max_bytes=16, backup_count=1, lock_root=lock_root)
        destination = seed._migration_manifest_path()
        seed.close()
        path.write_bytes(b"history\n" * 8)
    else:
        seed = _mod()._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        destination = seed._rotation_manifest_path()
        seed.close()
        path.write_bytes(b"A" * 31)
    return case, path, lock_root, destination


def _proof_only_claim_child():
    return r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_proof_only_claim", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
real_link = os.link
def interrupt_before_stage(source, destination, *args, **kwargs):
    if str(destination).endswith(".create.claim.tmp"):
        os._exit(int(sys.argv[4]))
    return real_link(source, destination, *args, **kwargs)
os.link = interrupt_before_stage
kind = sys.argv[3]
path = sys.argv[2]
lock_root = sys.argv[5]
if kind == "key":
    mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                            lock_root=lock_root)
elif kind == "migrate":
    mod._ConnectorLogWriter(path, max_bytes=16, backup_count=1,
                            lock_root=lock_root)
else:
    writer = mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                     lock_root=lock_root)
    writer.write("x")
'''


def test_connector_named_claim_proof_only_restart_is_authenticated_or_fails_closed(tmp):
    """Writer-created proof-only residue is authenticated and recovered.

    The bootstrap key destination is no different from the journals: its
    durable authenticator is minted before the first publication, so a
    proof-only residue this writer created is reconciled by the next two
    starts instead of wedging every one of them.
    """
    scenarios = (("key", 193), ("migrate", 194), ("rotate", 195))
    sizes = {"key": 32, "migrate": 16, "rotate": 32}
    for kind, code in scenarios:
        case, path, lock_root, destination = _named_claim_scenario(tmp, kind)
        proof = _mod()._ConnectorLogWriter._named_claim_proof_path(destination)
        stage = _mod()._ConnectorLogWriter._named_claim_staging_path(destination)
        crashed = subprocess.run(
            [sys.executable, "-c", _proof_only_claim_child(), str(Path(MB)),
             str(path), kind, str(code), str(lock_root)],
            capture_output=True,
        )
        assert crashed.returncode == code, (
            kind, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )
        assert proof.exists(), (kind, "proof-only residue was not created")
        assert not stage.exists(), (kind, "stage link happened unexpectedly")

        for _ in range(2):
            try:
                restarted = _mod()._ConnectorLogWriter(
                    path, max_bytes=sizes[kind], backup_count=1,
                    lock_root=lock_root)
            except (OSError, RuntimeError) as exc:
                raise AssertionError(
                    f"{kind} restart was blocked: {exc}") from exc
            if kind == "rotate":
                restarted.write("x")
            restarted.close()
            assert not proof.exists(), (kind, proof)
            assert not stage.exists(), (kind, stage)
            assert not list(lock_root.glob("*.create.*")), sorted(
                item.name for item in lock_root.glob("*.create.*"))
            assert not list(lock_root.glob("*.create-proof")), case


def _stage_validation_exception_child():
    return r'''
import importlib.util
import os
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_stage_validation", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
real_link = os.link
linked = {"value": False}
def mark_stage_link(source, destination, *args, **kwargs):
    result = real_link(source, destination, *args, **kwargs)
    if str(destination).endswith(".create.claim.tmp"):
        linked["value"] = True
    return result
os.link = mark_stage_link
real_read = mod._ConnectorLogWriter._read_named_claim_proof
def fail_after_stage_link(cls, proof, *args, **kwargs):
    if linked["value"]:
        raise RuntimeError("injected post-link validation failure")
    return real_read(proof, *args, **kwargs)
mod._ConnectorLogWriter._read_named_claim_proof = classmethod(
    fail_after_stage_link)
kind = sys.argv[3]
path = sys.argv[2]
lock_root = sys.argv[5]
try:
    if kind == "key":
        mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                lock_root=lock_root)
    elif kind == "migrate":
        mod._ConnectorLogWriter(path, max_bytes=16, backup_count=1,
                                lock_root=lock_root)
    else:
        writer = mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                         lock_root=lock_root)
        writer.write("x")
except RuntimeError as exc:
    Path(sys.argv[6]).write_text(str(exc), encoding="utf-8")
else:
    raise AssertionError("post-link validation exception was not injected")
'''


def test_connector_named_claim_stage_validation_restart_is_authenticated_or_fails_closed(tmp):
    """Writer-created stage-only residue is authenticated and recovered.

    A post-link validation failure unlinks its own proof and leaves the fixed
    stage behind.  For the bootstrap key that residue is signed with the
    durable authority secret, so the next two starts reconcile it exactly as
    they reconcile a journal's.
    """
    scenarios = (("key", 196), ("migrate", 197), ("rotate", 198))
    sizes = {"key": 32, "migrate": 16, "rotate": 32}
    for kind, code in scenarios:
        case, path, lock_root, destination = _named_claim_scenario(tmp, kind)
        marker = case / "validation-error.txt"
        proof = _mod()._ConnectorLogWriter._named_claim_proof_path(destination)
        stage = _mod()._ConnectorLogWriter._named_claim_staging_path(destination)
        crashed = subprocess.run(
            [sys.executable, "-c", _stage_validation_exception_child(),
             str(Path(MB)), str(path), kind, str(code), str(lock_root),
             str(marker)], capture_output=True,
        )
        assert crashed.returncode == 0, (
            kind, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )
        assert marker.read_text(encoding="utf-8") == \
            "injected post-link validation failure"
        assert not proof.exists(), (kind, proof)
        assert stage.exists(), (kind, "stage-only residue was not created")

        for _ in range(2):
            try:
                restarted = _mod()._ConnectorLogWriter(
                    path, max_bytes=sizes[kind], backup_count=1,
                    lock_root=lock_root)
            except (OSError, RuntimeError) as exc:
                raise AssertionError(
                    f"{kind} restart was blocked: {exc}") from exc
            if kind == "rotate":
                restarted.write("x")
            restarted.close()
            assert not proof.exists(), (kind, proof)
            assert not stage.exists(), (kind, stage)
            assert not list(lock_root.glob("*.create.*")), sorted(
                item.name for item in lock_root.glob("*.create.*"))
            assert not list(lock_root.glob("*.create-proof")), case


def _cleanup_interrupt_child():
    return r'''
import importlib.util
import os
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("discord_mb_cleanup_interrupt", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.os.O_TMPFILE = None
claim_stage = Path(sys.argv[6])
real_unlink = mod._ConnectorLogWriter._unlink_if_identity
def unlink_then_interrupt(candidate, *args, **kwargs):
    result = real_unlink(candidate, *args, **kwargs)
    if result and Path(candidate) == claim_stage:
        os._exit(int(sys.argv[4]))
    return result
mod._ConnectorLogWriter._unlink_if_identity = staticmethod(
    unlink_then_interrupt)
kind = sys.argv[3]
path = sys.argv[2]
lock_root = sys.argv[5]
if kind == "key":
    mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                            lock_root=lock_root)
elif kind == "migrate":
    mod._ConnectorLogWriter(path, max_bytes=16, backup_count=1,
                            lock_root=lock_root)
else:
    writer = mod._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                     lock_root=lock_root)
    writer.write("x")
'''


def test_connector_named_claim_cleanup_half_state_restart_is_recoverable(tmp):
    """An exit after stage cleanup cannot leave proof-only residue blocking open."""
    scenarios = (("key", 199), ("migrate", 200), ("rotate", 201))
    sizes = {"key": 32, "migrate": 16, "rotate": 32}
    for kind, code in scenarios:
        case, path, lock_root, destination = _named_claim_scenario(tmp, kind)
        proof = _mod()._ConnectorLogWriter._named_claim_proof_path(destination)
        stage = _mod()._ConnectorLogWriter._named_claim_staging_path(destination)
        crashed = subprocess.run(
            [sys.executable, "-c", _cleanup_interrupt_child(), str(Path(MB)),
             str(path), kind, str(code), str(lock_root), str(stage)],
            capture_output=True,
        )
        assert crashed.returncode == code, (
            kind, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )
        assert proof.exists(), (kind, "cleanup did not leave proof residue")
        assert not stage.exists(), (kind, "stage cleanup did not run")

        for _ in range(2):
            try:
                restarted = _mod()._ConnectorLogWriter(
                    path, max_bytes=sizes[kind], backup_count=1,
                    lock_root=lock_root)
            except (OSError, RuntimeError) as exc:
                raise AssertionError(
                    f"{kind} restart was blocked: {exc}") from exc
            if kind == "rotate":
                restarted.write("x")
            restarted.close()
            assert not proof.exists(), (kind, proof)
            assert not list(lock_root.glob("*.create.*")), sorted(
                item.name for item in lock_root.glob("*.create.*"))
            assert not list(lock_root.glob("*.create-proof")), case


def test_connector_log_first_migration_journal_create_partial_restart_preserves_history(tmp):
    """A torn initial migration record is discarded and migration retries safely."""
    case = Path(tmp) / "first-migration-journal-crash"
    case.mkdir()
    path = case / "migration.log"
    lock_root = case / "lock-root"
    original = b"".join(f"{index:02d}\n".encode() for index in range(12))
    seed = _mod()._ConnectorLogWriter(
        path, max_bytes=16, backup_count=2, lock_root=lock_root)
    seed.close()
    path.write_bytes(original)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_first_migration_journal", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = os.write
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
armed = {"value": False}
def publish(self, manifest, kind="migrate", create=False):
    if create and kind == "migrate":
        armed["value"] = True
    return real_manifest(self, manifest, kind=kind, create=create)
def partial_journal(fd, data):
    if armed["value"]:
        armed["value"] = False
        real_write(fd, data[:max(1, len(data) // 2)])
        os._exit(184)
    return real_write(fd, data)
mod._ConnectorLogWriter._write_migration_manifest = publish
os.write = partial_journal
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2,
                        lock_root=sys.argv[3])
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path),
         str(lock_root)], capture_output=True,
    )
    assert crashed.returncode == 184, crashed.stderr.decode("utf-8", "replace")

    for _ in range(4):
        restarted = _mod()._ConnectorLogWriter(
            path, max_bytes=16, backup_count=2, lock_root=lock_root)
        restarted.close()
        slots = [path.with_name("migration.log.2"),
                 path.with_name("migration.log.1"), path]
        assert b"".join(slot.read_bytes() for slot in slots if slot.exists()) == original

    assert not list(lock_root.glob("*.migrate.json"))
    assert not list(case.glob(".migration.log.migrate-*.tmp"))
    assert not list(lock_root.glob("*.json.tmp"))


def test_connector_log_first_rotation_journal_create_partial_restart_is_bounded(tmp):
    """A torn initial rotation record cannot wedge future rotations."""
    case = Path(tmp) / "first-rotation-journal-crash"
    case.mkdir()
    path = case / "rotation.log"
    lock_root = case / "lock-root"
    seed = _mod()._ConnectorLogWriter(
        path, max_bytes=32, backup_count=1, lock_root=lock_root)
    seed.close()
    path.write_bytes(b"A" * 31)
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_first_rotation_journal", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = os.write
real_manifest = mod._ConnectorLogWriter._write_migration_manifest
armed = {"value": False}
def publish(self, manifest, kind="migrate", create=False):
    if create and kind == "rotate":
        armed["value"] = True
    return real_manifest(self, manifest, kind=kind, create=create)
def partial_journal(fd, data):
    if armed["value"]:
        armed["value"] = False
        real_write(fd, data[:max(1, len(data) // 2)])
        os._exit(185)
    return real_write(fd, data)
mod._ConnectorLogWriter._write_migration_manifest = publish
os.write = partial_journal
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                 lock_root=sys.argv[3])
writer.write("x")
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path),
         str(lock_root)], capture_output=True,
    )
    assert crashed.returncode == 185, crashed.stderr.decode("utf-8", "replace")

    for _ in range(3):
        restarted = _mod()._ConnectorLogWriter(
            path, max_bytes=32, backup_count=1, lock_root=lock_root)
        restarted.write("x")
        restarted.close()
        assert path.stat().st_size <= 32
        assert path.with_name("rotation.log.1").stat().st_size <= 32

    assert not list(lock_root.glob("*.rotate.json"))
    assert not list(case.glob(".rotation.log.rotate-*.tmp"))
    assert not list(lock_root.glob("*.json.tmp"))


def test_connector_log_first_journal_create_rejects_and_preserves_tampered_record(tmp):
    """Malformed first-create journal bytes are never repaired in place."""
    for kind in ("migrate", "rotate"):
        case = Path(tmp) / f"tampered-first-{kind}"
        case.mkdir()
        path = case / "tampered.log"
        lock_root = case / "lock-root"
        max_bytes = 16 if kind == "migrate" else 32
        seed = _mod()._ConnectorLogWriter(
            path, max_bytes=max_bytes, backup_count=1, lock_root=lock_root)
        journal = (seed._migration_manifest_path() if kind == "migrate"
                   else seed._rotation_manifest_path())
        seed.close()
        tampered = b"{\"version\":3,\"state\":\n"
        journal.write_bytes(tampered)
        try:
            _mod()._ConnectorLogWriter(
                path, max_bytes=max_bytes, backup_count=1, lock_root=lock_root)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"tampered {kind} journal was adopted")
        assert journal.read_bytes() == tampered


def _crash_after_journal_publish_script(kind):
    return rf'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_{kind}_collision", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = mod._ConnectorLogWriter._write_migration_manifest
def publish_then_crash(self, manifest, kind="migrate", create=False):
    result = real_write(self, manifest, kind=kind, create=create)
    if create and kind == "{kind}":
        os._exit(173)
    return result
mod._ConnectorLogWriter._write_migration_manifest = publish_then_crash
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                 lock_root=sys.argv[3])
if "{kind}" == "rotate":
    writer.write("x")
'''


def test_connector_log_collision_after_migration_journal_publish_is_preserved(tmp):
    """Recovery never deletes a planned migration temp it did not create."""
    m = _mod()
    for kind in ("migrate", "rotate"):
        path = Path(tmp) / f"{kind}-collision.log"
        if kind == "migrate":
            path.write_bytes(b"history\n" * 8)
        else:
            path.write_bytes(b"A" * 31)
        writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        token = writer._identity_digest
        lock_root = writer._owner.lock_root
        writer.close()
        if kind == "migrate":
            # Probe the stable inode/token first, then make the next startup
            # perform migration so the crash occurs at journal publication.
            path.write_bytes(b"history\n" * 8)
        child = _crash_after_journal_publish_script(kind)
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(Path(MB)), str(path),
             str(lock_root)], capture_output=True,
        )
        assert crashed.returncode == 173, crashed.stderr.decode("utf-8", "replace")
        journal = lock_root / f"{token}.{kind}.json"
        manifest = json.loads(journal.read_text(encoding="utf-8"))
        planned = manifest["planned_temporaries"]
        assert planned and not manifest["created_temporaries"]
        collision = Path(planned[0])
        collision.write_bytes(b"KEEP-PLANNED-COLLISION")

        restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                          lock_root=lock_root)
        restarted.close()
        assert collision.read_bytes() == b"KEEP-PLANNED-COLLISION"


def _crash_after_first_stage_script(kind):
    return rf'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_{kind}_forged_temp", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_write = mod._ConnectorLogWriter._write_migration_temp
def write_then_crash(self, data, mode, name=None, directory=None):
    result = real_write(self, data, mode, name=name, directory=directory)
    if name is not None and ".{kind}-" in str(name):
        os._exit(177)
    return result
mod._ConnectorLogWriter._write_migration_temp = write_then_crash
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes=32, backup_count=1,
                                 lock_root=sys.argv[3])
if "{kind}" == "rotate":
    writer.write("x")
'''


def test_connector_log_preparing_cleanup_rejects_forged_envelope(tmp):
    """A public-looking temp without the private MAC survives cleanup."""
    m = _mod()
    for kind in ("migrate", "rotate"):
        case = Path(tmp) / f"forged-envelope-{kind}"
        case.mkdir()
        path = case / "forged.log"
        if kind == "migrate":
            path.write_bytes(b"history\n" * 8)
        else:
            path.write_bytes(b"A" * 31)
        writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        lock_root = writer._owner.lock_root
        writer.close()
        if kind == "migrate":
            path.write_bytes(b"history\n" * 8)

        crashed = subprocess.run(
            [sys.executable, "-c", _crash_after_first_stage_script(kind),
             str(Path(MB)), str(path), str(lock_root)],
            capture_output=True,
        )
        assert crashed.returncode == 177, (
            kind, crashed.returncode,
            crashed.stderr.decode("utf-8", "replace"),
        )
        token = writer._identity_digest
        journal = lock_root / f"{token}.{kind}.json"
        manifest = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
        staged = Path(manifest["planned_temporaries"][0])
        raw = staged.read_bytes()
        header, payload = raw.split(b"\n", 1)
        fields = header.split(b":")
        # The data, size and digest remain valid; only the unforgeable field is
        # changed.  A v1/public-only parser would incorrectly delete this.
        assert len(fields) == 9, fields
        fields[-1] = (b"0" if fields[-1] != b"0" else b"1") * len(fields[-1])
        forged = b":".join(fields) + b"\n" + payload
        staged.write_bytes(forged)

        restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                                          lock_root=lock_root)
        restarted.close()
        assert staged.read_bytes() == forged


def _crash_after_first_replace_script(kind):
    max_bytes = 16 if kind == "migrate" else 32
    backup_count = 2 if kind == "migrate" else 1
    return rf'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_{kind}_rebound", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_replace = os.replace
calls = [0]
def replace_then_crash(*args):
    real_replace(*args)
    calls[0] += 1
    if calls[0] == 1:
        os._exit(174)
os.replace = replace_then_crash
writer = mod._ConnectorLogWriter(sys.argv[2], max_bytes={max_bytes}, backup_count={backup_count},
                                 lock_root=sys.argv[3])
if "{kind}" == "rotate":
    writer.write("x")
'''


def test_connector_log_prepared_migration_recovery_streams_legacy_paths(tmp):
    """Prepared recovery hashes old active/source paths incrementally."""
    m = _mod()
    path = Path(tmp) / "prepared-streaming.log"
    source = path.with_name("prepared-streaming.log.2")
    source.write_bytes(b"SRC0000\n")
    path.write_bytes(b"ACT0000\nACT0001\nACT0002\n")
    child = r'''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("discord_mb_prepared_stream", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def mark_then_crash(self, manifest, index, kind):
    if kind == "migrate":
        os._exit(176)
    return original_mark(self, manifest, index, kind)
original_mark = mod._ConnectorLogWriter._mark_destination_publishing
mod._ConnectorLogWriter._mark_destination_publishing = mark_then_crash
mod._ConnectorLogWriter(sys.argv[2], max_bytes=16, backup_count=2)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(Path(MB)), str(path)],
        capture_output=True,
    )
    assert crashed.returncode == 176, crashed.stderr.decode("utf-8", "replace")
    lock_root = m._ConnectorOwnership._IDENTITY_LOCK_ROOT
    journals = list(lock_root.glob("*.migrate.json"))
    assert len(journals) == 1, [item.name for item in journals]

    original_read_bytes = Path.read_bytes
    guarded = {path.resolve(), source.resolve()}
    armed = {"value": True}

    def reject_legacy_whole_read(candidate):
        if armed["value"] and Path(candidate).resolve() in guarded:
            raise AssertionError("prepared recovery read a legacy path wholesale")
        return original_read_bytes(candidate)

    original_recover = m._ConnectorLogWriter._recover_migration

    def recover_then_disarm(self):
        try:
            return original_recover(self)
        finally:
            armed["value"] = False

    Path.read_bytes = reject_legacy_whole_read
    m._ConnectorLogWriter._recover_migration = recover_then_disarm
    try:
        restarted = m._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
        restarted.close()
    finally:
        Path.read_bytes = original_read_bytes
        m._ConnectorLogWriter._recover_migration = original_recover
    assert not list(lock_root.glob("*.migrate.json"))
    assert not source.exists()
    assert all(item.stat().st_size <= 16
               for item in path.parent.glob("prepared-streaming.log*"))


def test_connector_log_migration_rejects_rebound_journal_destination(tmp):
    """Migration recovery fails closed before replacing a rebound slot."""
    m = _mod()
    path = Path(tmp) / "migration-rebound.log"
    path.write_bytes(b"".join(f"{i:02d}\n".encode() for i in range(12)))
    writer = m._ConnectorLogWriter(path, max_bytes=16, backup_count=2)
    lock_root = writer._owner.lock_root
    writer.close()
    path.write_bytes(b"".join(f"{i:02d}\n".encode() for i in range(12)))
    crashed = subprocess.run(
        [sys.executable, "-c", _crash_after_first_replace_script("migrate"),
         str(Path(MB)), str(path), str(lock_root)], capture_output=True,
    )
    assert crashed.returncode == 174, crashed.stderr.decode("utf-8", "replace")
    rebound = path.with_name("migration-rebound.log.1")
    rebound.write_bytes(b"LIVE-MIGRATION-WRITER\n")
    before = rebound.read_bytes()
    try:
        m._ConnectorLogWriter(path, max_bytes=16, backup_count=2,
                              lock_root=lock_root)
    except RuntimeError:
        pass
    else:
        raise AssertionError("migration recovery overwrote a rebound destination")
    assert rebound.read_bytes() == before


def test_connector_log_rotation_rejects_rebound_journal_destination(tmp):
    """Rotation recovery fails closed before replacing a rebound slot."""
    m = _mod()
    path = Path(tmp) / "rotation-rebound.log"
    path.write_bytes(b"A" * 31)
    path.with_name("rotation-rebound.log.1").write_bytes(b"B" * 10)
    writer = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
    lock_root = writer._owner.lock_root
    writer.close()
    crashed = subprocess.run(
        [sys.executable, "-c", _crash_after_first_replace_script("rotate"),
         str(Path(MB)), str(path), str(lock_root)], capture_output=True,
    )
    assert crashed.returncode == 174, crashed.stderr.decode("utf-8", "replace")
    rebound = path.with_name("rotation-rebound.log.1")
    rebound.write_bytes(b"LIVE-ROTATION-WRITER\n")
    before = rebound.read_bytes()
    try:
        m._ConnectorLogWriter(path, max_bytes=32, backup_count=1,
                              lock_root=lock_root)
    except RuntimeError:
        pass
    else:
        raise AssertionError("rotation recovery overwrote a rebound destination")
    assert rebound.read_bytes() == before


def test_connector_cli_preserves_custom_log_path(tmp):
    """`connector --log PATH` reaches the existing log_path API unchanged."""
    m = _mod()
    path = str(Path(tmp) / "explicit.log")
    seen = {}
    original = m.ConnectorApp
    old_argv = sys.argv

    class FakeConnectorApp:
        def __init__(self, *args, **kwargs):
            seen.update(args=args, kwargs=kwargs)

        def run(self):
            return None

    m.ConnectorApp = FakeConnectorApp
    try:
        sys.argv = ["discord_mb.py", "connector", "identity", "--token", "token",
                    "--log", path]
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                m._cli()
            except SystemExit as exc:
                assert exc.code == 0, f"connector parser rejected --log (exit {exc.code})"
    finally:
        sys.argv = old_argv
        m.ConnectorApp = original

    assert seen["args"] == ("identity",)
    assert seen["kwargs"]["log_path"] == path


def test_connector_ownership_never_blocks_on_its_own_sidecar(tmp):
    """One acquisition must not report its own lock as another owner's.

    Two candidate pathnames can reach a single sidecar file -- on Windows the
    8.3 short name of %TEMP% beside the long name realpath returns, and on
    POSIX a symlinked parent directory.  Byte-range locks are per file, so the
    second name contended with the lock the same call had just taken and the
    connector refused to start at all, permanently (issue #217).
    """
    m = _mod()
    real = Path(tmp) / "real"
    real.mkdir()
    (Path(tmp) / "link").symlink_to(real, target_is_directory=True)
    aliased = Path(tmp) / "link" / "self-block.log"
    aliased.write_bytes(b"")

    owner = m._ConnectorOwnership(aliased)
    try:
        assert owner._path_handles, "acquisition held no sidecar at all"
        assert len(owner._path_handles) == len(owner._path_lock_paths)
        held = {m.os.path.realpath(str(path))
                for path in owner._path_lock_paths}
        assert len(held) == len(owner._path_lock_paths), (
            "one sidecar file was locked twice through aliased names")

        # The alias collapse must not disable exclusion: a genuine second
        # owner of the same log still has to be refused.
        try:
            second = m._ConnectorOwnership(real / "self-block.log")
        except m._ConnectorOwnershipError:
            second = None
        else:
            second.close()
        assert second is None, "a second owner acquired a held connector log"
    finally:
        owner.close()

    reacquired = m._ConnectorOwnership(real / "self-block.log")
    try:
        assert reacquired._path_handles, "lock was not released on close"
    finally:
        reacquired.close()


def main():
    tests = [_fixture_lock_root(test) for test in _util.collect(globals())]
    return _util.runner(tests, "discordlog_")


if __name__ == "__main__":
    raise SystemExit(main())
