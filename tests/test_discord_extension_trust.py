#!/usr/bin/env python3
"""An extension module another local user can rewrite is refused by `extension set`
and by the startup reload; a group-writable one loads with a warning. The registry
is written through a private temporary file into a private directory."""
import os
import sys
import types
from importlib.util import cache_from_source
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
    """os.stat answering each path's (mode, uid) from `table`; every other path is missing."""
    def stat_of(target):
        if table.get(Path(target)) is None:
            raise FileNotFoundError(target)
        return types.SimpleNamespace(st_mode=table[Path(target)][0], st_uid=table[Path(target)][1])
    return stat_of


def test_the_check_refuses_what_another_user_can_rewrite_and_warns_on_group_write(tmp):
    module, link, chain, through, loop, safe, hops = (Path(tmp) / name for name in ("ext.py", "links/ext.py", "safe/a.py", "hops/up/ext.py", "safe/loop.py", "safe", "hops"))
    cache = (pyc := Path(cache_from_source(str(module)))).parent     # where the import keeps its bytecode
    module.write_text("", encoding="utf-8")
    owned, world, group, owned_dir = (0o644, ME), (0o646, ME), (0o664, ME), (0o755, ME)
    rows = [  # (path, module stat, its directory's stat, every other path's stat, posix, outcome)
        (module, world, (0o755, ME), {}, True, (PermissionError, f"{module} is writable by other users")),
        (module, owned, (0o757, ME), {}, True, (PermissionError, f"{tmp} is writable by other users")),
        (module, (0o644, OTHER), (0o755, ME), {}, True, (PermissionError, f"{module} is owned by uid {OTHER}, not by this user or root")),
        (module, owned, (0o755, OTHER), {}, True, (PermissionError, f"{tmp} is owned by uid {OTHER}, not by this user or root")),
        (module, (0o644, 0), (0o755, 0), {}, True, None),
        (module, group, (0o755, ME), {}, True, f"{module} is group-writable"),
        (module, owned, (0o775, ME), {}, True, f"{tmp} is group-writable"),
        (module, (0o666, OTHER), (0o777, OTHER), {}, False, None),
        (Path(tmp) / "missing.py", owned, owned, {}, True, None),
        (module, owned, (0o755, ME), {cache: (0o777, ME)}, True, (PermissionError, f"{cache} is writable by other users")),
        (module, owned, (0o755, ME), {cache: (0o755, OTHER)}, True, (PermissionError, f"{cache} is owned by uid {OTHER}, not by this user or root")),
        (module, owned, (0o755, ME), {cache: owned_dir, pyc: (0o644, OTHER)}, True, (PermissionError, f"{pyc} is owned by uid {OTHER}, not by this user or root")),
        (module, owned, (0o755, ME), {cache: owned_dir, pyc: world}, True, (PermissionError, f"{pyc} is writable by other users")),
    ]
    if _util.POSIX_MODES:   # the symlink walk is POSIX-only; chain: safe/a.py -> safe/dir/b.py -> hops/b.py -> links/ext.py
        for source, target in ((link, module), (chain, safe / "dir" / "b.py"), (safe / "dir", "../hops"), (hops / "b.py", "../links/ext.py"),
                               (hops / "up", ".."), (loop, "loop.py"), (Path(tmp) / "alias", tmp), (link.parent / "__pycache__", "../hops/up/__pycache__")):
            source.parent.mkdir(exist_ok=True)
            source.symlink_to(target)
        rows += [(link, owned, (0o755, ME), {link.parent: (0o777, ME)}, True, (PermissionError, f"{link.parent} is writable by other users")),
                 (link, owned, (0o757, ME), {link.parent: (0o755, ME)}, True, (PermissionError, f"{tmp} is writable by other users")),
                 (chain, owned, owned_dir, {hops: (0o777, ME)}, True, (PermissionError, f"{hops} is writable by other users")),
                 (through, owned, owned_dir, {hops: (0o757, ME)}, True, (PermissionError, f"{hops} is writable by other users")),
                 (chain, owned, owned_dir, {hops: (0o755, OTHER)}, True, (PermissionError, f"{hops} is owned by uid {OTHER}, not by this user or root")),
                 (chain, owned, owned_dir, {hops: (0o775, ME)}, True, f"{hops} is group-writable"),
                 (link, owned, owned_dir, {hops: (0o775, ME), cache: owned_dir, pyc: group}, True, f"{hops} is group-writable; {pyc} is group-writable"),
                 (loop, owned, owned_dir, {}, True, (OSError, f"{loop}: too many levels of symbolic links"))]
        for owned_path, mode in ((module, 0o644), (Path(tmp), 0o755), (safe, 0o755), (hops, 0o755), (link.parent, 0o755)):
            owned_path.chmod(mode)      # the real stat accepts a trusted chain and a symlinked directory
        assert [_ext_trust.check_extension(p) for p in (Path(tmp) / "alias" / "ext.py", chain, through)] == [None] * 3
    for path, file_stat, dir_stat, others, posix, expected in rows:
        try:
            outcome = _ext_trust.check_extension(path, posix=posix, euid=ME, stat_of=_stats({module: file_stat, Path(tmp): dir_stat, **others}))
        except OSError as exc:
            outcome = (type(exc), str(exc))
        assert outcome == expected, (path, file_stat, dir_stat, others, posix, outcome)


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
