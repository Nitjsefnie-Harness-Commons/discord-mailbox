#!/usr/bin/env python3
"""An extension module another local user can rewrite is refused by `extension set`
and by the startup reload; a group-writable one loads with a warning. The registry
is written through a private temporary file into a private directory."""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway  # noqa: E402
import _util  # noqa: E402
sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib import _ext_trust, core  # noqa: E402

ME, OTHER = 1000, 1001


def _loaded(path):
    return any(getattr(mod, "__file__", None) == str(path) for mod in list(sys.modules.values()))


def _stats(table):
    """os.stat answering each path's (mode, uid) from `table`, and nothing else."""
    return lambda target: types.SimpleNamespace(st_mode=table[Path(target)][0], st_uid=table[Path(target)][1])


def test_the_check_refuses_what_another_user_can_rewrite_and_warns_on_group_write(tmp):
    module, link_dir = Path(tmp) / "ext.py", Path(tmp) / "links"
    module.write_text("", encoding="utf-8")
    link_dir.mkdir()
    link = link_dir / "ext.py"
    try:
        link.symlink_to(module)
    except OSError:         # Windows without the symlink privilege
        link = None
    owned, world, group = (0o644, ME), (0o646, ME), (0o664, ME)
    rows = [  # (path, file stat, directory stat, link-directory stat, posix, outcome)
        (module, owned, (0o755, ME), None, True, None),
        (module, world, (0o755, ME), None, True, (PermissionError, f"{module} is writable by other users")),
        (module, owned, (0o757, ME), None, True, (PermissionError, f"{tmp} is writable by other users")),
        (module, (0o644, OTHER), (0o755, ME), None, True,
         (PermissionError, f"{module} is owned by uid {OTHER}, not by this user or root")),
        (module, owned, (0o755, OTHER), None, True,
         (PermissionError, f"{tmp} is owned by uid {OTHER}, not by this user or root")),
        (module, (0o644, 0), (0o755, 0), None, True, None),
        (module, group, (0o755, ME), None, True, f"{module} is group-writable"),
        (module, owned, (0o775, ME), None, True, f"{tmp} is group-writable"),
        (module, (0o666, OTHER), (0o777, OTHER), None, False, None),
        (Path(tmp) / "missing.py", owned, owned, None, True, None),
    ]
    if link is not None:
        rows += [(link, owned, (0o755, ME), (0o777, ME), True, (PermissionError, f"{link_dir} is writable by other users")),
                 (link, owned, (0o757, ME), (0o755, ME), True, (PermissionError, f"{tmp} is writable by other users"))]
    for path, file_stat, dir_stat, link_stat, posix, expected in rows:
        stat_of = _stats({module: file_stat, Path(tmp): dir_stat, link_dir: link_stat})
        try:
            outcome = _ext_trust.check_extension(path, posix=posix, euid=ME, stat_of=stat_of)
        except PermissionError as exc:
            outcome = (type(exc), str(exc))
        assert outcome == expected, (path, file_stat, dir_stat, link_stat, posix, outcome)
    if link is not None and _util.POSIX_MODES:      # the real stat, through a symlinked directory
        for owned_path, mode in ((module, 0o644), (Path(tmp), 0o755)):
            owned_path.chmod(mode)
        (Path(tmp) / "alias").symlink_to(tmp, target_is_directory=True)
        assert _ext_trust.check_extension(Path(tmp) / "alias" / "ext.py") is None


def test_the_registry_goes_through_a_private_temp_into_a_private_directory(tmp):
    seen, real, decoy = [], os.replace, Path(tmp) / "decoy"
    decoy.write_text("legacy", encoding="utf-8")

    def spy(src, dst):
        seen.append(os.stat(src).st_mode & 0o777)
        real(src, dst)
    saved, umask = (core.TOKEN_DIR, os.replace), os.umask(0o022)
    core.TOKEN_DIR, os.replace = Path(tmp) / "tokens", spy
    try:
        core.write_extension_registry("bob", {"path": "/x.py"}, "claude")
        directory = core.extension_dir("claude")
        os.link(decoy, directory / "bob.json.tmp")      # an older writer's temp; a write through it shows in decoy
        core.write_extension_registry("bob", {"path": "/y.py"}, "claude")
    finally:
        (core.TOKEN_DIR, os.replace), _ = saved, os.umask(umask)
    assert (len(seen), os.listdir(directory), decoy.read_text(encoding="utf-8")) == (2, ["bob.json"], "legacy"), seen
    if _util.POSIX_MODES:
        assert seen == [0o600, 0o600] and directory.stat().st_mode & 0o777 == 0o700, (seen, oct(directory.stat().st_mode))


def test_the_connector_refuses_an_exposed_module_at_start_and_on_set(tmp):
    if not _util.POSIX_MODES:
        _util.skip("Windows reports 0o666 for every file, so the check is skipped there")
    relay, shared, exposed = Path(tmp) / "relay.py", Path(tmp) / "shared.py", Path(tmp) / "exposed.py"
    for path, mode in ((relay, 0o666), (shared, 0o664), (exposed, 0o666)):
        path.write_text("async def setup(ctx):\n    pass\n", encoding="utf-8")
        path.chmod(mode)

    async def script(client):
        await client.ask({"start": {"op": "extension-list"}})
        await client.ask({"refused": {"op": "extension-set", "path": str(exposed)}})
        await client.ask({"set": {"op": "extension-set", "path": str(shared)}})

    client = _gateway.run(tmp, script, extension=True)
    start, refused, warned = (client.responses[k] for k in ("start", "refused", "set"))
    reason = f"PermissionError: {relay} is writable by other users"
    assert (start["loaded"], start["last_error"]) == (False, reason), start
    assert f"extension load failed: {reason}" in client.log, client.log
    assert refused["error"] == f"PermissionError: {exposed} is writable by other users", refused
    warning = f"{shared} is group-writable"
    assert (warned["ok"], warned["path"], warned.get("warning")) == (True, str(shared), warning), warned
    assert f"extension loaded: {shared} (warning: {warning})" in client.log, client.log
    assert (_loaded(relay), _loaded(exposed), _loaded(shared)) == (False, False, True)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="mbtrust_")


if __name__ == "__main__":
    raise SystemExit(main())
