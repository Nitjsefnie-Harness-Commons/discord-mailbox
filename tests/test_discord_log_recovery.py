#!/usr/bin/env python3
"""A connector log failure never disables logging for good or lifecycle control."""
import asyncio
import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
from test_discord_log_rotation import LIMIT, _filled, _fixture_lock_root, _hidden, _mod, _patched, _refuses, _restart, _window  # noqa: E402


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
    with _refuses("a closed writer reopened its log without ownership", OSError) as caught:
        writer.write("after close")
    assert "not open" in str(caught[0]), caught
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
        with _refuses("the owned file vanishing was read as an absent backup", m._ConnectorOwnershipError) as caught:
            owner.revalidate()
        assert "namespace path was replaced" in str(caught[0]), caught
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
            ("_journal_file_state", "_write_migration_manifest", "_write_migration_temp", "_cleanup_journal_temp", "_fsync_directory")}

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
                    hardlink_at("hardlink_then_journal_create_fsync_fault", "_fsync_directory"),
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
                with _refuses(f"{replace.__name__}: a changed backup was accepted", (m._ConnectorOwnershipError, OSError)) as caught:
                    writer.write("refused")
                assert "namespace path was replaced" in str(caught[0]) or (attempt == 0 and str(caught[0]) == "injected fault"), caught
        finally:
            for name, attribute in real.items():
                setattr(m._ConnectorLogWriter, name, attribute)
            writer.close()
        assert "refused" not in path.read_text(encoding="utf-8")


def test_a_slot_swapped_during_a_rotation_is_refused_and_never_written(tmp):
    """A backup or the active file swapped in before a rotation ends refuses every later write."""
    m = _mod()
    cls = m._ConnectorLogWriter
    # The swap lands after the last publish, then after the journal unlink and before the reopen.
    for seam, victim, refusal in (("_publish_in_place", "connector.log.1", "namespace path"),
                                  ("_cleanup_journal_temp", "connector.log", "identity")):
        path = Path(tmp) / seam / "connector.log"
        path.parent.mkdir()
        writer, real = _filled(m, path), cls.__dict__[seam]

        def swap(*args, call=getattr(writer, seam), seam=seam, real=real, target=path.with_name(victim)):
            setattr(cls, seam, real)  # one swap per row
            result = call(*args)
            target.with_name("foreign").write_bytes(b"FOREIGN\n")
            os.replace(target.with_name("foreign"), target)
            return result
        setattr(cls, seam, staticmethod(swap))
        try:
            for attempt in range(2):  # the refusal holds on the write after it
                with _refuses(f"{seam}: write {attempt} accepted a swapped {victim}", m._ConnectorOwnershipError) as caught:
                    writer.write("refused")
                assert str(caught[0]) == f"connector log {refusal} was replaced while owned: {path.with_name(victim)}", caught
        finally:
            setattr(cls, seam, real)
            writer.close()
        assert path.with_name(victim).read_bytes() == b"FOREIGN\n", seam  # swapped, and never written
    linked = Path(tmp) / "linked"  # a healthy writer reached through a symlinked directory rotates
    (Path(tmp) / "real").mkdir()
    try:
        os.symlink(Path(tmp) / "real", linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        _util.skip("this platform cannot create a symlink")
    writer = _filled(m, linked / "connector.log")
    try:
        for i in range(8):
            writer.write(f"healthy {i}")
    finally:
        writer.close()
    assert "healthy 7" in (Path(tmp) / "real" / "connector.log").read_text(encoding="utf-8")


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
            with _refuses(f"write {attempt} succeeded under the injected fault", OSError) as caught:
                writer.write("y" * 25)
            assert "injected I/O fault" in str(caught[0]), caught
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


def test_a_backup_deleted_while_a_rotation_journal_is_pending_is_absent(tmp):
    """A backup deleted after a rotation faulted mid-publish is absent to the recovery, restarts included."""
    m = _mod()
    real_replace, real_fsync = os.replace, m._ConnectorLogWriter.__dict__["_fsync_directory"]
    fault = {}

    def replace(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        fault["published"] += Path(dst).name.startswith("connector.log.")

    def fsync(directory):
        if fault["left"] and fault["published"] == fault["after"]:
            fault["left"] -= 1
            raise OSError("injected fault after a published slot")
        real_fsync.__func__(directory)
    # .1 moves to .2 first, then the stage takes .1: deleting .1 then leaves the active's bytes in the active alone.
    for victim, after in (("connector.log.2", 1), ("connector.log.2", 2), ("connector.log.1", 2)):
        path = Path(tmp) / f"{victim}-{after}" / "connector.log"
        path.parent.mkdir()
        writer = _filled(m, path)
        journal, active = writer._rotation_manifest_path(), path.read_text(encoding="utf-8").splitlines()
        fault.update(published=0, left=1, after=after)
        os.replace, m._ConnectorLogWriter._fsync_directory = replace, staticmethod(fsync)
        try:
            with _refuses(f"{victim}: the rotation succeeded under the injected fault", OSError) as caught:
                writer.write("y" * 25)
        finally:
            os.replace, m._ConnectorLogWriter._fsync_directory = real_replace, real_fsync
        assert "injected fault" in str(caught[0]), caught
        assert fault["left"] == 0 and journal.exists(), (victim, fault)
        path.with_name(victim).unlink()
        try:
            for i in range(2):
                writer.write(f"after {i}")
        finally:
            writer.close()
        assert not journal.exists() and _hidden(path.parent) == [], (victim, _hidden(path.parent))
        _restart(path, LIMIT, 2)
        assert all(p.stat().st_size <= LIMIT for p in _window(path)), victim
        kept = [ln for p in reversed(_window(path)) for ln in p.read_text(encoding="utf-8").splitlines()]
        assert kept[-2:] == ["after 0", "after 1"] and kept[:-2] == sorted(set(kept[:-2])), (victim, kept)
        assert set(active) <= set(kept), (victim, after, kept)


def test_a_finished_rotation_whose_journal_unlink_failed_is_discarded(tmp):
    """A rotation that published every slot but kept its journal recovers as complete."""
    m = _mod()
    path = Path(tmp) / "connector.log"
    writer = _filled(m, path)
    journal = writer._rotation_manifest_path()
    before = [p.read_bytes() for p in _window(path)]
    real_unlink, fired = Path.unlink, []

    def unlink(self, *args, **kwargs):
        if self == journal and not fired:
            fired.append(self)
            raise OSError("injected rotation journal unlink failure")
        return real_unlink(self, *args, **kwargs)
    Path.unlink = unlink
    try:
        with _refuses("the rotation succeeded under the injected unlink fault", OSError) as caught:
            writer.write("y" * 25)
        assert "journal unlink failure" in str(caught[0]), caught
        assert fired and journal.exists()
        writer.write("z" * 25)
    finally:
        Path.unlink = real_unlink
        writer.close()
    assert not journal.exists()
    assert [p.read_bytes() for p in _window(path)] == [b"z" * 25 + b"\n", before[0], before[1]]


def test_a_slot_swapped_while_a_rotation_journal_recovers_is_refused(tmp):
    """Recovery holds each slot to the identity it expects there, so a swap during it refuses every write."""
    m = _mod()
    cls, path = m._ConnectorLogWriter, Path(tmp) / "connector.log"
    writer, real, backup = _filled(m, path), cls.__dict__["_publish_in_place"], path.with_name("connector.log.2")

    def fault(*args):  # the rotation's active publish faults, leaving its journal pending
        cls._publish_in_place = staticmethod(swap)
        raise OSError("injected fault before the active publish")

    def swap(*args):  # the recovery's active publish lands, then a foreign file takes .2
        cls._publish_in_place = real
        real.__func__(*args)
        backup.with_name("foreign").write_bytes(b"FOREIGN\n")
        os.replace(backup.with_name("foreign"), backup)
    cls._publish_in_place = staticmethod(fault)
    try:
        for attempt in range(3):
            with _refuses(f"write {attempt} accepted a slot swapped during recovery", (OSError, m._ConnectorOwnershipError)) as caught:
                writer.write("y" * 25)
            assert str(caught[0]) == ("injected fault before the active publish" if attempt == 0 else
                                      f"connector log namespace path was replaced while owned: {backup}"), caught
    finally:
        cls._publish_in_place = real
        writer.close()
    assert backup.read_bytes() == b"FOREIGN\n"


def test_a_slot_swapped_after_the_rotation_journal_is_prepared_is_never_moved(tmp):
    """A rename rechecks its source and destination, the stage its temp; with the swap undone, recovery finishes."""
    m, held = _mod(), []
    cls = m._ConnectorLogWriter
    for seam, index, key in (("_mark_generation_publishing", 0, "source"), ("_mark_generation_publishing", 0, "destination"),
                             ("_mark_destination_publishing", 1, "temporary")):
        path = Path(tmp) / key / "connector.log"
        path.parent.mkdir()
        writer = _filled(m, path)
        before = [p.read_bytes() for p in _window(path)]
        held.clear()

        def swap(self, manifest, *args, real=getattr(cls, seam), index=index, key=key):
            real(self, manifest, *args)
            if not held:  # one swap, once the journal names every slot
                held.append(Path(manifest["destinations"][index][key]))
                os.replace(held[0], held[0].with_name("aside"))
                held[0].write_bytes(b"FOREIGN\n")
        with contextlib.closing(writer), _patched(cls, **{seam: swap}):
            with _refuses(f"{key}: a rotation moved a slot swapped after its journal was prepared") as caught:
                writer.write("y" * 25)
            assert held and held[0].read_bytes() == b"FOREIGN\n", (key, caught)  # refused before it moved
            os.replace(held[0].with_name("aside"), held[0])
            writer.write("z" * 25)
        assert [p.read_bytes() for p in _window(path)] == [b"z" * 25 + b"\n", before[0], before[1]], key


def test_a_version_3_journal_left_by_the_previous_release_recovers_or_refuses(tmp):
    """That release's journal, crashed at .1's replace, finishes; without the active's staged temp it refuses."""
    m = _mod()
    sha, nonce = m._ConnectorLogWriter._sha256_bytes, "7a" * 16
    active, backup = b"A" * 20 + b"\n", b"B" * 10 + b"\n"

    def ident(path):
        return list(m._ConnectorOwnership._identity_for(path))
    for drop in (False, True):
        path = Path(tmp) / f"v3-{drop}" / "c.log"
        path.parent.mkdir()
        path.write_bytes(active)
        path.with_name("c.log.1").write_bytes(backup)
        writer, slots = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1), (path.with_name("c.log.1"), path)
        temps = [path.with_name(f".c.log.rotate-{writer._identity_digest}-{nonce}-{i}.tmp") for i in (0, 1)]
        # As that release left them: .1's payload already stripped of its envelope, the active's empty one not.
        writer._transaction_nonce, writer._staging_kind, writer._staging_slot, writer._staging_destination = nonce, "rotate", 1, path
        temps[0].write_bytes(active)
        temps[1].write_bytes(writer._staging_envelope(b""))
        journal, manifest = writer._rotation_manifest_path(), {
            "version": 3, "kind": "rotate", "token": writer._identity_digest, "state": "prepared", "root": str(path.parent),
            "log_name": "c.log", "backup_count": 1, "transaction_nonce": nonce,
            "stable_paths": [{"path": str(p), "identity": ident(p)} for p in slots],
            "destinations": [{"destination": str(p), "in_place": p == path, "present": True, "temporary": str(t),
                              "size": len(data), "sha256": sha(data), "before_size": len(old), "before_sha256": sha(old),
                              "before_identity": ident(p), "before_entry_identity": ident(p),
                              "temporary_identity": ident(t), "temporary_entry_identity": ident(t)}
                             for p, t, data, old in zip(slots, temps, (active, b""), (backup, active))]}
        manifest["destinations"][0]["publish_state"] = "publishing"
        journal.write_bytes(json.dumps(manifest).encode("utf-8"))
        writer.close()
        if drop:
            temps[1].unlink()
        before = {p.name: p.read_bytes() for p in (journal, *path.parent.glob("*c.log*")) if p.is_file()}
        try:
            restarted = m._ConnectorLogWriter(path, max_bytes=32, backup_count=1)
        except RuntimeError as exc:
            assert drop and "c.log has no staged replacement" in str(exc), exc
            assert {p.name: p.read_bytes() for p in (journal, *path.parent.glob("*c.log*")) if p.is_file()} == before
        else:
            restarted.write("z")
            restarted.close()
            assert not drop and [path.read_bytes(), slots[0].read_bytes()] == [b"z\n", active], drop
            assert not journal.exists() and _hidden(path.parent) == [], _hidden(path.parent)


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
