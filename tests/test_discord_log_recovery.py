#!/usr/bin/env python3
"""A connector log failure never disables logging for good or lifecycle control."""
import asyncio
import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from test_discord_log_rotation import _fixture_lock_root, _mod  # noqa: E402

LIMIT = 32


def _filled(m, path):
    """A writer whose startup migration filled the active log and both retained backups."""
    path.write_text("".join(f"line {i:02d}\n" for i in range(12)), encoding="utf-8")
    writer = m._ConnectorLogWriter(path, max_bytes=LIMIT, backup_count=2)
    assert [p.name for p in _window(path)] == [path.name, f"{path.name}.1", f"{path.name}.2"]
    return writer


def _window(path):
    return sorted(path.parent.glob(f"{path.name}*"))


def test_a_deleted_backup_is_absent_and_later_writes_succeed(tmp):
    """A temp cleaner removing connector.log.1 must not refuse every later write."""
    m = _mod()
    path = Path(tmp) / "connector.log"
    writer = _filled(m, path)
    try:
        path.with_name("connector.log.1").unlink()
        for i in range(12):  # appends into the active file, then rotates through the gap
            writer.write(f"after {i:02d}")
    finally:
        writer.close()
    try:
        writer.write("after close")
    except OSError as exc:
        assert "not open" in str(exc), exc
    else:
        raise AssertionError("a closed writer reopened its log without ownership")
    assert [p.name for p in _window(path)] == ["connector.log", "connector.log.1", "connector.log.2"]
    assert all(p.stat().st_size <= LIMIT for p in _window(path))
    assert "after 11" in path.read_text(encoding="utf-8")


def test_a_replaced_backup_or_a_vanished_owned_file_is_still_refused(tmp):
    """Only a backup with no directory entry left is absent; any other change refuses."""
    m = _mod()
    owned = Path(tmp) / "connector.pid"
    owned.write_text("1", encoding="utf-8")
    owner = m._ConnectorOwnership(owned)
    try:
        owned.unlink()
        try:
            owner.revalidate()
        except m._ConnectorOwnershipError as exc:
            assert "namespace path was replaced" in str(exc), exc
        else:
            raise AssertionError("the owned file vanishing was read as an absent backup")
    finally:
        owner.close()

    def foreign_file(backup):
        backup.with_name("foreign").write_bytes(b"FOREIGN\n")
        os.replace(backup.with_name("foreign"), backup)

    def dangling_link(backup):
        backup.unlink()
        try:
            os.symlink(backup.with_name("missing-target"), backup)
        except (OSError, NotImplementedError):
            _util.skip("this platform cannot create a symlink")

    real = {name: m._ConnectorLogWriter.__dict__[name] for name in
            ("_journal_file_state", "_write_migration_manifest", "_write_migration_temp", "_cleanup_journal_temp")}

    def hardlink(backup):
        backup.with_name("external").write_bytes(b"EXTERNAL\n")
        backup.unlink()
        os.link(backup.with_name("external"), backup)

    def hardlink_at(name, method, fault=True, when=lambda *args: True):  # swap .1 when the rotation reaches `method`
        def row(backup):
            def swap_then_fault(self, *args, **kwargs):
                if not when(*args):
                    return real[method](self, *args, **kwargs)
                setattr(m._ConnectorLogWriter, method, real[method])
                hardlink(backup)
                if fault:
                    raise OSError("injected fault")
                return real[method](self, *args, **kwargs)
            setattr(m._ConnectorLogWriter, method, swap_then_fault)
        row.__name__ = name
        return row

    staging_fault = hardlink_at("hardlink_then_staging_fault", "_write_migration_temp")

    def rotation_then_hardlink_staging_fault(backup):  # a completed rotation clears its own mark
        writer.write("x" * 25)
        staging_fault(backup)

    for replace in (foreign_file, hardlink_at("hardlink_mid_rotation", "_journal_file_state", fault=False),
                    hardlink_at("hardlink_then_journal_fault", "_write_migration_manifest"),
                    staging_fault, rotation_then_hardlink_staging_fault,
                    hardlink_at("hardlink_then_publish_mark_fault", "_write_migration_manifest",
                                when=lambda manifest, *_: any("publish_state" in d for d in manifest["destinations"])),
                    hardlink_at("hardlink_then_fault_after_journal_unlink", "_cleanup_journal_temp"), dangling_link):
        path = Path(tmp) / replace.__name__ / "connector.log"
        path.parent.mkdir()
        writer = _filled(m, path)
        try:
            replace(path.with_name("connector.log.1"))
            for attempt in range(2):  # the refusal holds on every later write
                try:
                    writer.write("refused")
                except (m._ConnectorOwnershipError, OSError) as exc:
                    assert "namespace path was replaced" in str(exc) or (attempt == 0 and str(exc) == "injected fault"), exc
                else:
                    raise AssertionError(f"{replace.__name__}: a changed backup was accepted")
        finally:
            for name, attribute in real.items():
                setattr(m._ConnectorLogWriter, name, attribute)
            writer.close()
        assert "refused" not in path.read_text(encoding="utf-8")


def test_a_rotation_that_failed_after_publishing_recovers_once_the_fault_clears(tmp):
    """An I/O fault after a rotated slot was published does not break every later write."""
    m = _mod()
    path = Path(tmp) / "connector.log"
    writer = _filled(m, path)
    journal = writer._rotation_manifest_path()
    real_replace, real_fsync = os.replace, m._ConnectorLogWriter._fsync_directory
    real_cleanup = m._ConnectorLogWriter._cleanup_journal_temp
    fault = {"published": False, "left": 2, "fired": 0}

    def replace(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        fault["published"] |= Path(dst).name.startswith("connector.log.")

    def fsync(directory):
        if fault["left"] and fault["published"]:
            fault["left"], fault["fired"] = fault["left"] - 1, fault["fired"] + 1
            raise OSError(5, "injected I/O fault after a rotated slot was published")
        real_fsync(directory)

    os.replace, m._ConnectorLogWriter._fsync_directory = replace, staticmethod(fsync)
    try:
        for attempt in (1, 2):  # the rotation itself, then a recovery while the fault holds
            try:
                writer.write("y" * 25)
            except OSError as exc:
                assert "injected I/O fault" in str(exc), exc
            else:
                raise AssertionError(f"write {attempt} succeeded under the injected fault")
            assert fault["fired"] == attempt and journal.exists(), (attempt, fault)

        def cleanup_fault(self, *args):  # a fault once the journal is gone, with nothing foreign moved
            m._ConnectorLogWriter._cleanup_journal_temp = real_cleanup
            raise OSError("injected fault after the journal unlink")
        raised = []
        for i in range(16):  # rotations keep running after the recovery
            try:
                writer.write(f"healed {i:02d}")
            except OSError as exc:
                raised.append(str(exc).split()[-1])
            if i == 2:
                kept = [ln for p in reversed(_window(path)) for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("line ")]
                assert kept and kept == sorted(set(kept)), kept  # pre-fault lines: each at most once, in order
            if i == 3:  # the next rotation faults after dropping its journal
                m._ConnectorLogWriter._cleanup_journal_temp = cleanup_fault
            if i == 8:  # the next rotation faults after a publish, then its recovery after dropping the journal
                fault.update(published=False, left=1)
                m._ConnectorLogWriter._cleanup_journal_temp = cleanup_fault
        assert raised == ["unlink", "published", "unlink"], raised
    finally:
        os.replace, m._ConnectorLogWriter._fsync_directory = real_replace, real_fsync
        m._ConnectorLogWriter._cleanup_journal_temp = real_cleanup
        writer.close()
    assert "healed 15" in path.read_text(encoding="utf-8")
    assert not journal.exists()
    assert all(p.stat().st_size <= LIMIT for p in _window(path))


def test_the_watchdog_closes_first_and_a_failed_log_write_falls_back_to_stderr(tmp):
    """A dead parent closes the client even when the connector log write raises."""
    if not _gateway.have_discord():
        _util.skip("discord.py is not installed")
    from discord_mb_lib import connector as c
    order, err = [], io.StringIO()
    real_write, real_alive = c._ConnectorLogWriter.write, c.pid_alive

    def failing_write(self, line):
        if "gone, shutting down" not in line:
            return real_write(self, line)
        order.append("log")
        raise c._ConnectorOwnershipError("injected connector log failure")

    async def script(client):
        async def close():
            order.append("close")
            client.closed = True
        client.close = close
        c._ConnectorLogWriter.write, c.pid_alive = failing_write, lambda pid: False
        with contextlib.redirect_stderr(err):
            for _ in range(100):  # the watchdog polls every 5 s
                if "log" in order:
                    break
                await asyncio.sleep(0.1)

    try:
        _gateway.run(tmp, script, keep=("parent_watchdog",))
    finally:
        c._ConnectorLogWriter.write, c.pid_alive = real_write, real_alive
    assert order == ["close", "log"], order
    lines = [line for line in err.getvalue().splitlines() if "gone, shutting down" in line]
    assert len(lines) == 1 and "injected connector log failure" in lines[0], err.getvalue()


def main():
    tests = [_fixture_lock_root(test) for test in _util.collect(globals())]
    return _util.runner(tests, "discordlogrecovery_")


if __name__ == "__main__":
    raise SystemExit(main())
