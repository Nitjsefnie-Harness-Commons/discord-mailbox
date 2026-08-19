#!/usr/bin/env python3
"""Source-only tests for the bounded leech streams (issue #160).

Two append-only files outlived issue #157's connector-log bound: the JSON
event stream leeches follow, and the human `leech.log` several leech processes
share.  These tests drive the writers and the reader cursor directly -- no
Discord client, no connector process -- and cover the Windows lock path by
substituting the platform module, so one host exercises every branch.
"""
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")


def _mod():
    return _util.load(MB, "mb_event_stream")


def _segments(directory):
    return sorted(Path(directory).glob("events.*.jsonl"))


def _record(index):
    return json.dumps({"event": "message", "n": index}, ensure_ascii=False)


# --- writer: bounded retention -------------------------------------------

def test_event_stream_keeps_total_storage_within_the_documented_bound(tmp):
    """Continuous events keep one active segment plus `retain` retired ones."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=256, retain=2)
    try:
        for index in range(400):
            writer.write(_record(index))
        segments = _segments(tmp)
        assert len(segments) == 3, [p.name for p in segments]
        total = sum(p.stat().st_size for p in segments)
        # The bound is (retain + 1) segments of max_bytes, plus at most one
        # record of overshoot per segment (records are never split).
        assert total <= 3 * (256 + 128), total
        assert writer.generation > 3, writer.generation
    finally:
        writer.close()


def test_event_stream_never_splits_a_record(tmp):
    """Every surviving line is one complete JSON object, multi-byte included."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=200, retain=2)
    try:
        for index in range(200):
            writer.write(json.dumps({"n": index, "body": "žluťoučký kůň " * 3},
                                    ensure_ascii=False))
    finally:
        writer.close()
    seen = 0
    for segment in _segments(tmp):
        raw = segment.read_bytes()
        assert raw.endswith(b"\n"), segment.name
        for line in raw.splitlines():
            payload = json.loads(line.decode("utf-8"))   # raises on a split record
            assert payload["body"].startswith("žluťoučký")
            seen += 1
    assert seen > 0


def test_event_stream_writes_an_oversized_record_whole(tmp):
    """A record larger than a segment is never truncated to fit the bound."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=64, retain=2)
    big = json.dumps({"event": "message", "body": "x" * 500})
    try:
        writer.write(_record(0))
        writer.write(big)
        writer.write(_record(1))
    finally:
        writer.close()
    lines = []
    for segment in _segments(tmp):
        lines.extend(segment.read_bytes().splitlines())
    assert big.encode("utf-8") in lines


def test_event_stream_new_master_starts_a_fresh_generation(tmp):
    """A replacement master retires its predecessor's segments and legacy log."""
    m = _mod()
    first = m._EventStreamWriter(tmp, max_bytes=256, retain=2)
    try:
        for index in range(40):
            first.write(_record(index))
        first_generation = first.generation
    finally:
        first.close()
    (Path(tmp) / "events.jsonl").write_text("legacy\n", encoding="utf-8")

    second = m._EventStreamWriter(tmp, max_bytes=256, retain=2)
    try:
        assert second.generation > first_generation
        assert _segments(tmp) == [second.path]
        assert not (Path(tmp) / "events.jsonl").exists()
    finally:
        second.close()


def test_event_stream_prune_tolerates_an_undeletable_segment(tmp):
    """Windows refuses to delete a segment a reader holds open; bound recovers."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=128, retain=1)
    real_unlink = Path.unlink
    refused = {"n": 0}

    def unlink(self, *args, **kwargs):
        if refused["n"] == 0 and self.name.startswith("events."):
            refused["n"] = 1
            raise PermissionError("held open by another process")
        return real_unlink(self, *args, **kwargs)

    try:
        Path.unlink = unlink
        for index in range(200):
            writer.write(_record(index))
    finally:
        Path.unlink = real_unlink
        writer.close()
    assert refused["n"] == 1, "the refusal never fired"
    assert len(_segments(tmp)) == 2, [p.name for p in _segments(tmp)]


# --- reader: cursor semantics --------------------------------------------

def test_reader_attaches_at_the_live_end_without_replaying_history(tmp):
    """A leech joining mid-stream sees new events only."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=2)
    try:
        writer.write(_record(0))
        reader = m._EventStreamReader.attach(tmp)
        assert reader.read() == []
        writer.write(_record(1))
        assert [json.loads(line)["n"] for line in reader.read()] == [1]
    finally:
        writer.close()


def test_reader_receives_every_event_exactly_once_across_rotations(tmp):
    """A leech attached before retention keeps receiving each new event once."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=200, retain=3)
    reader = m._EventStreamReader.attach(tmp)
    seen = []
    try:
        for index in range(120):
            writer.write(_record(index))
            if index % 3 == 0:
                seen.extend(json.loads(line)["n"] for line in reader.read())
        seen.extend(json.loads(line)["n"] for line in reader.read())
    finally:
        writer.close()
    assert seen == list(range(120)), seen[:20]


def test_reader_drains_a_retired_segment_before_the_next_generation(tmp):
    """Events written just before a rotation are not lost to the new segment."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=3)
    reader = m._EventStreamReader.attach(tmp)
    try:
        writer.write(_record(0))
        writer._rotate()
        writer.write(_record(1))
        writer._rotate()
        writer.write(_record(2))
        assert [json.loads(line)["n"] for line in reader.read()] == [0, 1, 2]
        assert reader.generation == writer.generation
    finally:
        writer.close()


def test_reader_reports_a_pruned_generation_as_an_explicit_gap(tmp):
    """Retention that outruns a leech is a named gap, not a silent replay."""
    m = _mod()
    gaps = []
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=1)
    reader = m._EventStreamReader.attach(tmp, on_gap=gaps.append)
    try:
        writer.write(_record(0))
        for _ in range(3):
            writer._rotate()
        writer.write(_record(1))
        assert [json.loads(line)["n"] for line in reader.read()] == [1]
    finally:
        writer.close()
    assert len(gaps) == 1, gaps
    assert "pruned" in gaps[0]
    assert reader.generation == writer.generation


def test_reader_does_not_replay_when_a_generation_shrinks(tmp):
    """A size decrease inside one generation resyncs forward, never to zero."""
    m = _mod()
    gaps = []
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=2)
    try:
        writer.write(_record(0))
        writer.write(_record(1))
        reader = m._EventStreamReader.attach(tmp, on_gap=gaps.append)
        writer.close()
        with open(writer.path, "wb") as fh:
            fh.write(b"")
        assert reader.read() == []
        assert reader.offset == 0
        assert len(gaps) == 1 and "shrank" in gaps[0], gaps
    finally:
        writer.close()


def test_reader_never_publishes_a_partial_record(tmp):
    """A half-written line waits for its newline instead of being emitted."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=2)
    try:
        reader = m._EventStreamReader.attach(tmp)
        complete = _record(0).encode("utf-8")
        with open(writer.path, "ab") as fh:
            fh.write(complete + b"\n")
            fh.write(b'{"event": "message", "n": 1')      # still being written
        assert [json.loads(line)["n"] for line in reader.read()] == [0]
        with open(writer.path, "ab") as fh:
            fh.write(b"}\n")
        assert [json.loads(line)["n"] for line in reader.read()] == [1]
    finally:
        writer.close()


def test_reader_discards_a_partial_tail_left_in_a_retired_segment(tmp):
    """A record cut short by a crash cannot stall the cursor on a dead segment."""
    m = _mod()
    gaps = []
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=3)
    try:
        reader = m._EventStreamReader.attach(tmp, on_gap=gaps.append)
        with open(writer.path, "ab") as fh:
            fh.write(b'{"event": "message", "n": 0')
        writer._rotate()
        writer.write(_record(1))
        assert [json.loads(line)["n"] for line in reader.read()] == [1]
    finally:
        writer.close()
    assert len(gaps) == 1 and "partial" in gaps[0], gaps


def test_reader_follows_a_legacy_single_file_stream(tmp):
    """A current leech still sees an older master's unsegmented events.jsonl."""
    m = _mod()
    legacy = Path(tmp) / "events.jsonl"
    legacy.write_text(_record(0) + "\n", encoding="utf-8")
    reader = m._EventStreamReader.attach(tmp)
    assert reader.generation == 0
    assert reader.read() == []
    with open(legacy, "a", encoding="utf-8") as fh:
        fh.write(_record(1) + "\n")
    assert [json.loads(line)["n"] for line in reader.read()] == [1]


def test_reader_waits_for_the_first_segment_then_reads_it_whole(tmp):
    """Attaching before the master's first segment replays that segment only."""
    m = _mod()
    reader = m._EventStreamReader.attach(tmp)
    assert reader.generation is None
    assert reader.read() == []
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=2)
    try:
        writer.write(_record(0))
        writer.write(_record(1))
        assert [json.loads(line)["n"] for line in reader.read()] == [0, 1]
    finally:
        writer.close()


def test_reader_honors_facade_open_monkeypatch(tmp):
    """Direct class calls retain the monolith's facade monkeypatch seam."""
    m = _mod()
    writer = m._EventStreamWriter(tmp, max_bytes=4096, retain=2)
    reader = m._EventStreamReader.attach(tmp)
    try:
        writer.write(_record(0))

        def denied_open(*_args, **_kwargs):
            raise OSError("injected event stream read failure")

        m.open = denied_open
        assert reader.read() == []
        m.open = open
        assert [json.loads(line)["n"] for line in reader.read()] == [0]
    finally:
        m.open = open
        writer.close()


def test_class_override_sync_is_independent_across_threads(tmp):
    """One facade call cannot suppress another thread's override sync."""
    m = _mod()
    entered = threading.Event()
    release = threading.Event()

    def hold_gap(_message):
        entered.set()
        assert release.wait(timeout=10)

    reader = m._EventStreamReader(tmp, on_gap=hold_gap)
    thread = threading.Thread(target=reader._gap, args=("hold",))
    thread.start()
    assert entered.wait(timeout=10)

    original_os = m.os
    probe = Path(tmp) / "probe"
    probe.write_bytes(b"identity")
    fake_os = types.SimpleNamespace(
        stat=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected stat failure")))
    m.os = fake_os
    try:
        assert m._ConnectorOwnership._identity_for(probe) is None
    finally:
        m.os = original_os
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


# --- leech.log: bounded across processes ---------------------------------

def test_leech_log_rotates_within_a_bounded_window(tmp):
    """The shared human log keeps one active file plus `backup_count`."""
    m = _mod()
    path = Path(tmp) / "leech.log"
    writer = m._LeechLogWriter(path, max_bytes=256, backup_count=1)
    for index in range(200):
        writer.write(f"[leech 1234] tail error {index:03d}")
    writer.close()
    files = sorted(p for p in Path(tmp).glob("leech.log*")
                   if not p.name.endswith(".lock"))
    assert [p.name for p in files] == ["leech.log", "leech.log.1"], files
    assert sum(p.stat().st_size for p in files) <= 2 * (256 + 64)


def test_leech_log_writes_an_oversized_line_whole(tmp):
    """One line larger than the bound is kept intact rather than split."""
    m = _mod()
    path = Path(tmp) / "leech.log"
    writer = m._LeechLogWriter(path, max_bytes=64, backup_count=1)
    long_line = "[leech 1234] " + "y" * 300
    writer.write("first")
    writer.write(long_line)
    writer.close()
    assert path.read_text(encoding="utf-8").splitlines() == [long_line]


def test_leech_log_survives_concurrent_leech_processes(tmp):
    """Real concurrent writers keep every line intact and the file bounded."""
    m = _mod()  # noqa: F841 -- fail early if the module cannot load at all
    path = Path(tmp) / "leech.log"
    program = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "import _util\n"
        "m = _util.load(%r, 'mb_event_stream_child')\n"
        "w = m._LeechLogWriter(%r, max_bytes=4096, backup_count=1)\n"
        "for i in range(120):\n"
        "    w.write('[leech %%d] line %%03d %%s' %% (os.getpid(), i, 'z' * 40))\n"
        % (os.path.dirname(os.path.abspath(__file__)), MB, str(path)))
    children = [subprocess.Popen([sys.executable, "-c", program])
                for _ in range(4)]
    for child in children:
        assert child.wait(timeout=120) == 0, child.returncode

    files = sorted(p for p in Path(tmp).glob("leech.log*")
                   if not p.name.endswith(".lock"))
    assert [p.name for p in files] == ["leech.log", "leech.log.1"], files
    assert sum(p.stat().st_size for p in files) <= 2 * (4096 + 128)
    lines = []
    for candidate in files:
        lines.extend(candidate.read_text(encoding="utf-8").splitlines())
    assert lines, "no surviving lines"
    for line in lines:
        # An interleaved append would break this shape; a raced rotation would
        # leave a half line behind.
        assert line.startswith("[leech "), line
        assert line.endswith("z" * 40), line


# --- lock: both platform primitives, and neither -------------------------

def test_shared_lock_uses_flock_on_posix(tmp):
    """The POSIX branch takes a real exclusive flock and releases it."""
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return                                   # not applicable on this host
    m = _mod()
    lock = m._SharedFileLock(Path(tmp) / "leech.log.lock", timeout=0.2)
    other = m._SharedFileLock(Path(tmp) / "leech.log.lock", timeout=0.2)
    lock.acquire()
    try:
        try:
            other.acquire()
        except m._SharedLockError:
            pass
        else:
            other.release()
            raise AssertionError("a second holder acquired the same lock")
    finally:
        lock.release()
    other.acquire()                              # freed by the release above
    other.release()


def test_shared_lock_uses_msvcrt_on_windows(tmp):
    """The Windows branch locks/unlocks one byte through msvcrt.locking."""
    m = _mod()
    calls = []
    fake = types.ModuleType("msvcrt")
    fake.LK_NBLCK = 3
    fake.LK_UNLCK = 0
    fake.locking = lambda fd, mode, size: calls.append((mode, size))
    previous = sys.modules.get("msvcrt")
    sys.modules["msvcrt"] = fake
    try:
        path = Path(tmp) / "leech.log"
        writer = m._LeechLogWriter(path, max_bytes=4096, backup_count=1)
        writer._lock._windows = True
        writer.write("[leech 1234] windows path")
        writer.close()
    finally:
        if previous is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = previous
    assert calls == [(3, 1), (0, 1)], calls
    assert (Path(tmp) / "leech.log.lock").stat().st_size == 1
    assert path.read_text(encoding="utf-8") == "[leech 1234] windows path\n"


def test_leech_log_write_survives_a_platform_without_either_primitive(tmp):
    """A diagnostic is never dropped because the lock could not be taken."""
    m = _mod()
    path = Path(tmp) / "leech.log"
    writer = m._LeechLogWriter(path, max_bytes=4096, backup_count=1,
                               timeout=0.05)

    def refuse(self, fh):
        raise m._SharedLockError("no locking primitive")

    original = m._SharedFileLock._lock_handle
    try:
        m._SharedFileLock._lock_handle = refuse
        writer.write("[leech 1234] unlockable platform")
    finally:
        m._SharedFileLock._lock_handle = original
    assert path.read_text(encoding="utf-8") == "[leech 1234] unlockable platform\n"


# --- wiring ---------------------------------------------------------------

def test_leech_log_records_are_single_lines(tmp):
    """The leech's own log() must not double-terminate the writer's records."""
    m = _mod()
    log_path = Path(tmp) / "leech.log"
    original = m.state_dir
    try:
        m.state_dir = lambda identity: Path(tmp)
        try:
            m.leech_main("identity", log_path=log_path)
        except SystemExit as exit_code:
            assert exit_code.code == 1, exit_code.code
        else:
            raise AssertionError("leech_main did not exit without a connector")
    finally:
        m.state_dir = original
    raw = log_path.read_text(encoding="utf-8")
    assert raw.endswith("\n") and "\n\n" not in raw, repr(raw)
    assert len(raw.splitlines()) == 1, raw.splitlines()


def test_connector_and_leech_use_the_bounded_writers(tmp):
    """The connector and the leech are wired to the bounded implementations."""
    source = (Path(MB).with_name("discord_mb_lib") / "connector.py").read_text(
        encoding="utf-8")
    assert "_events['writer'] = _EventStreamWriter(sd)" in source
    assert "_events['writer'].write(line)" in source
    assert "log_fh = _LeechLogWriter(log_path)" in source
    assert "log_fh.write(line + '\\n')" not in source
    assert "_EventStreamReader.attach(sd, on_gap=log)" in source
    # The unbounded predecessors are gone.
    assert "open(sd / 'events.jsonl', 'w'" not in source
    assert "open(log_path, 'a', encoding='utf-8', buffering=1)" not in source


def main():
    return _util.runner(_util.collect(globals()), "discordevents_")


if __name__ == "__main__":
    raise SystemExit(main())
