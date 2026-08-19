#!/usr/bin/env python3
"""Behavioral contract for the Discord mailbox executable facade."""
# pylint: disable=subprocess-run-check
# Every subprocess.run below is a probe whose exit status is the
# assertion; check=True would raise before the test could read it.

import os
import asyncio
import gc
import importlib
import inspect
import shutil
import subprocess
import sys
import traceback
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

MB = os.path.join(_util.SCRIPTS, "discord_mb.py")

EXPECTED_COMMANDS = {
    "send", "attachments", "creds", "conversation", "register",
    "list-agents", "topic", "pins", "context", "forum", "move",
    "message", "thread", "emoji", "servers", "channels",
    "status-plugin", "extension", "setup", "connector", "leech",
}


def _assert_child_ok(result):
    """Fail with the exit status too, not just whatever reached stderr.

    A child that dies during interpreter finalization, or is killed by a
    signal, exits non-zero with nothing on stderr at all -- which used to
    surface as an assertion carrying an empty message.
    """
    assert result.returncode == 0, (
        f"child exited {result.returncode}\n"
        f"stderr: {result.stderr!r}\n"
        f"stdout: {result.stdout!r}")


def _help(*args):
    result = subprocess.run(
        [sys.executable, MB, *args], capture_output=True, text=True, timeout=30)
    _assert_child_ok(result)
    return result.stdout


def test_top_level_help_keeps_every_command(tmp):
    """Dropping a parser branch is a functionality regression."""
    output = _help("--help")
    missing = {name for name in EXPECTED_COMMANDS if name not in output}
    assert not missing, missing


def test_nested_help_remains_executable(tmp):
    """The facade must dispatch nested parser help without a connector."""
    assert "upload" in _help("attachments", "--help")
    assert "rename" in _help("emoji", "identity", "--help")
    assert "call" in _help("extension", "identity", "--help")


def test_package_modules_are_importable_from_the_script_directory(tmp):
    """The package seam is real, not an installer-only path assumption."""
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "import discord_mb_lib.core, discord_mb_lib.storage, "
        "discord_mb_lib.connector, discord_mb_lib.cli"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    _assert_child_ok(result)


def test_canonical_facade_remains_an_exact_module_type(tmp):
    """The compatibility layer must not replace the monolith's module type."""
    script = (
        "import sys, types\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "assert type(discord_mb) is types.ModuleType\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facades_loaded_from_distinct_deployments_are_isolated(tmp):
    """One process may inspect two installed copies without cross-binding."""
    roots = []
    for name in ("first", "second"):
        scripts = Path(tmp) / name / "scripts"
        shutil.copytree(
            _util.SCRIPTS, scripts,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        roots.append(scripts)

    first = _util.load(roots[0] / "discord_mb.py", "discord_copy_first")
    second = _util.load(roots[1] / "discord_mb.py", "discord_copy_second")

    assert Path(first._core_module.__file__).is_relative_to(roots[0])
    assert Path(second._core_module.__file__).is_relative_to(roots[1])


def test_async_facade_preserves_coroutine_semantics_and_patch_timing(tmp):
    """Async wrappers synchronize when execution starts, as the monolith did."""
    m = _util.load(MB, "discord_async_contract")
    assert inspect.iscoroutinefunction(m.status_plugin_gateway_call)

    async def transport_failure():
        raise ConnectionError("gateway reset")

    coroutine = m.status_plugin_gateway_call(transport_failure())
    original = m.status_plugin_failure_is_transport
    m.status_plugin_failure_is_transport = lambda _exc: False
    try:
        try:
            asyncio.run(coroutine)
        except ConnectionError:
            pass
        else:
            raise AssertionError("late facade patch did not reach coroutine body")
    finally:
        m.status_plugin_failure_is_transport = original


def test_facade_functions_are_pickleable_when_imported_normally(tmp):
    """Windows spawn can resolve public functions through ``discord_mb``."""
    script = (
        "import pickle, sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "import discord_mb; "
        "[pickle.dumps(value) for value in "
        "(discord_mb.send, discord_mb.status_plugin_gateway_call, "
        "discord_mb.connector_main)]"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facade_and_package_share_public_type_identity(tmp):
    """The facade and its internal package expose one exception/type universe."""
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "import discord_mb; "
        "from discord_mb_lib import core, storage, connector; "
        "assert discord_mb.SendError is core.SendError; "
        "assert discord_mb.StatusPluginGatewayTransportError is "
        "core.StatusPluginGatewayTransportError; "
        "assert discord_mb._ConnectorOwnership is storage._ConnectorOwnership; "
        "assert discord_mb.ConnectorApp is connector.ConnectorApp"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_preimported_local_package_keeps_identity_and_pickleability(tmp):
    """Importing the facade cannot orphan already imported local package types."""
    script = (
        "import pickle, sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "from discord_mb_lib import connector; "
        "old_module = connector; old_class = connector.ConnectorApp; "
        "import discord_mb; "
        "from discord_mb_lib import connector as current; "
        "assert old_module is current; "
        "assert old_class is current.ConnectorApp; "
        "assert old_class is discord_mb.ConnectorApp; "
        "pickle.dumps(old_class)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reimported_facade_keeps_new_package_api_deployments_isolated(tmp):
    """New package APIs retain real source globals per deployment."""
    script = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "first = importlib.import_module('discord_mb'); "
        "connector = importlib.import_module('discord_mb_lib.connector'); "
        "connector._run_connector = lambda *a, **k: 11; "
        "assert first.ConnectorApp('agent').run() == 11; "
        "del sys.modules['discord_mb']; "
        "second = importlib.import_module('discord_mb'); "
        "second.ConnectorApp.run.__globals__['_run_connector'] = "
        "lambda *a, **k: 23; "
        "assert second.ConnectorApp('agent').run() == 23; "
        "assert first.ConnectorApp is connector.ConnectorApp; "
        "assert second.ConnectorApp is not first.ConnectorApp"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facade_reload_does_not_recurse_through_old_wrappers(tmp):
    """Reload cannot retarget an old generated method back onto itself."""
    script = (
        "import importlib, pathlib, sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "import discord_mb; "
        "discord_mb._ConnectorOwnership._identity_for(pathlib.Path('/tmp/x')); "
        "importlib.reload(discord_mb); "
        "discord_mb._ConnectorOwnership._identity_for(pathlib.Path('/tmp/x'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_preserves_canonical_package_type_identity(tmp):
    """Reloading the same facade keeps its canonical exception/type universe."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import core, connector\n"
        "importlib.reload(discord_mb)\n"
        "assert discord_mb.SendError is core.SendError\n"
        "assert discord_mb.ConnectorApp is connector.ConnectorApp\n"
        "try:\n"
        "    raise core.SendError('package failure')\n"
        "except discord_mb.SendError:\n"
        "    pass\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_private_facade_methods_remain_pickleable_after_reload(tmp):
    """Legacy projected methods remain pickleable across facade generations."""
    script = (
        "import importlib, pickle, sys; "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "import discord_mb; importlib.reload(discord_mb); "
        "pickle.dumps(discord_mb._ConnectorOwnership._identity_for); "
        "del sys.modules['discord_mb']; "
        "discord_mb = importlib.import_module('discord_mb'); "
        "pickle.dumps(discord_mb._ConnectorOwnership._identity_for)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_exported_method_pickle_uses_legacy_facade_module(tmp):
    """Generated method pickles remain loadable without discord_mb_lib."""
    script = (
        "import importlib, pickle, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "method = discord_mb._ConnectorOwnership._identity_for\n"
        "assert method.__module__ == 'discord_mb'\n"
        "assert pickle.loads(pickle.dumps(method)) is method\n"
        "importlib.reload(discord_mb)\n"
        "method = discord_mb._ConnectorOwnership._identity_for\n"
        "assert method.__module__ == 'discord_mb'\n"
        "assert pickle.loads(pickle.dumps(method)) is method\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_registered_alias_cannot_claim_the_canonical_package(tmp):
    """Only the real executable module may bind the public package universe."""
    script = (
        "import importlib, importlib.util, pathlib, sys; "
        f"path = pathlib.Path({str(MB)!r}); "
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r}); "
        "spec = importlib.util.spec_from_file_location('diagnostic_alias', path); "
        "alias = importlib.util.module_from_spec(spec); "
        "sys.modules[spec.name] = alias; spec.loader.exec_module(alias); "
        "normal = importlib.import_module('discord_mb'); "
        "from discord_mb_lib import core, connector, storage; "
        "assert alias._private_package is True; "
        "assert normal._private_package is False; "
        "assert normal.SendError is core.SendError; "
        "assert normal.ConnectorApp is connector.ConnectorApp; "
        "assert normal._ConnectorOwnership is storage._ConnectorOwnership"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_class_methods_do_not_add_compatibility_traceback_frames(tmp):
    """Class methods use facade globals without a generated call frame."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "del discord_mb.__dict__['_event_segments']\n"
        "try:\n"
        "    discord_mb._EventStreamReader('/tmp').read()\n"
        "except NameError as exc:\n"
        "    names = []\n"
        "    tb = exc.__traceback__\n"
        "    while tb is not None:\n"
        "        names.append(tb.tb_frame.f_code.co_name); tb = tb.tb_next\n"
        "    assert names == ['<module>', 'read'], names\n"
        "else: raise AssertionError('deleted global stayed defined')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_descriptor_rollback_handles_set_then_raise(tmp):
    """Rollback includes a descriptor even if setattr raises after writing it."""
    script = (
        "import builtins, importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "from discord_mb_lib import core\n"
        "target = core._ExtensionContext\n"
        "original = target.__dict__['__init__']\n"
        "real_setattr = builtins.setattr\n"
        "fired = False\n"
        "def set_then_raise(obj, name, value):\n"
        "    global fired\n"
        "    real_setattr(obj, name, value)\n"
        "    if obj is target and name == '__init__' and not fired:\n"
        "        fired = True\n"
        "        raise RuntimeError('injected post-write setattr failure')\n"
        "builtins.setattr = set_then_raise\n"
        "try:\n"
        "    try: importlib.import_module('discord_mb')\n"
        "    except RuntimeError: pass\n"
        "    else: raise AssertionError('injected setattr failure was swallowed')\n"
        "finally: builtins.setattr = real_setattr\n"
        "assert fired\n"
        "assert target.__dict__['__init__'] is original\n"
        "assert not hasattr(original, '__wrapped__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_binding_marker_failure_precedes_descriptor_mutation(tmp):
    """A rejected ownership marker cannot leave canonical classes wrapped."""
    script = (
        "import importlib, sys, types\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "package = importlib.import_module('discord_mb_lib')\n"
        "from discord_mb_lib import core\n"
        "target = core._ExtensionContext\n"
        "original = target.__dict__['__init__']\n"
        "class RejectMarker(types.ModuleType):\n"
        "    def __setattr__(self, name, value):\n"
        "        if name == '_discord_mb_facade_owner':\n"
        "            raise RuntimeError('injected binding marker failure')\n"
        "        return super().__setattr__(name, value)\n"
        "package.__class__ = RejectMarker\n"
        "try:\n"
        "    try: importlib.import_module('discord_mb')\n"
        "    except RuntimeError: pass\n"
        "    else: raise AssertionError('marker failure was swallowed')\n"
        "finally: package.__class__ = types.ModuleType\n"
        "assert target.__dict__['__init__'] is original\n"
        "assert not hasattr(original, '__wrapped__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_deleted_facade_monkeypatch_restores_implementation_global(tmp):
    """Deleting a temporary override restores builtins/module defaults."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "def fake_open(*args, **kwargs):\n"
        "    raise RuntimeError('stale fake open')\n"
        "discord_mb.open = fake_open\n"
        "try:\n"
        # pid_cmdline reads /proc through the builtin on POSIX; on Windows it
        # goes through psutil and never calls open, so there the override has
        # nothing to reach and only the restore half below is meaningful.
        "    if sys.platform != 'win32':\n"
        "        try: discord_mb.pid_cmdline(999999999)\n"
        "        except RuntimeError: pass\n"
        "        else: raise AssertionError("
        "'override did not reach implementation')\n"
        "finally:\n"
        "    del discord_mb.open\n"
        "assert discord_mb.pid_cmdline(999999999) is None\n"
        "assert 'open' not in discord_mb._core_module.__dict__\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_suspended_coroutine_observes_late_facade_monkeypatch(tmp):
    """Implementation globals stay facade-compatible at each await resume."""
    script = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "async def scenario():\n"
        "    started = asyncio.Event()\n"
        "    release = asyncio.Event()\n"
        "    async def delayed_failure():\n"
        "        started.set()\n"
        "        await release.wait()\n"
        "        raise ConnectionError('late failure')\n"
        "    task = asyncio.create_task(\n"
        "        discord_mb.status_plugin_gateway_call(delayed_failure()))\n"
        "    await started.wait()\n"
        "    discord_mb.status_plugin_failure_is_transport = lambda exc: False\n"
        "    release.set()\n"
        "    try:\n"
        "        await task\n"
        "    except ConnectionError:\n"
        "        return\n"
        "    raise AssertionError('late override was not observed')\n"
        "asyncio.run(scenario())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_failed_private_wrapper_build_removes_package_tree(tmp):
    """Every private import failure cleans its temporary package modules."""
    script = (
        "import importlib.util, inspect, pathlib, sys\n"
        f"mailbox = pathlib.Path({MB!r})\n"
        "before = {n for n in sys.modules if n.startswith('_discord_mb_lib_')}\n"
        "original_signature = inspect.signature\n"
        "calls = 0\n"
        "def failing_signature(value):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 2:\n"
        "        raise RuntimeError('injected wrapper failure')\n"
        "    return original_signature(value)\n"
        "inspect.signature = failing_signature\n"
        "try:\n"
        "    spec = importlib.util.spec_from_file_location('discord_alias', mailbox)\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    try: spec.loader.exec_module(module)\n"
        "    except RuntimeError: pass\n"
        "    else: raise AssertionError('wrapper failure was swallowed')\n"
        "finally:\n"
        "    inspect.signature = original_signature\n"
        "after = {n for n in sys.modules if n.startswith('_discord_mb_lib_')}\n"
        "assert after == before, sorted(after - before)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_failed_reload_preserves_override_assigned_while_staged(tmp):
    """Rollback merges a concurrent public write instead of erasing it."""
    script = (
        "import importlib, inspect, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import core, storage, connector, cli\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "original_signature = inspect.signature\n"
        "first = True\n"
        "def paused_then_failing(value):\n"
        "    global first\n"
        "    if first:\n"
        "        first = False\n"
        "        entered.set()\n"
        "        assert release.wait(10)\n"
        "    if getattr(value, '__module__', '').endswith('.cli'):\n"
        "        raise RuntimeError('injected reload failure')\n"
        "    return original_signature(value)\n"
        "inspect.signature = paused_then_failing\n"
        "failure = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: failure.append(exc)\n"
        "thread = threading.Thread(target=reload_it)\n"
        "thread.start()\n"
        "assert entered.wait(10)\n"
        "fake = lambda pid: True\n"
        "discord_mb.pid_alive = fake\n"
        "release.set()\n"
        "thread.join(10)\n"
        "inspect.signature = original_signature\n"
        "assert not thread.is_alive()\n"
        "assert failure and isinstance(failure[0], RuntimeError), failure\n"
        "assert discord_mb.pid_alive is fake\n"
        "assert all(module.pid_alive is fake for module in "
        "(core, storage, connector, cli))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_failed_reload_preserves_concurrent_override_deletion(tmp):
    """Rollback cannot resurrect a facade override deleted while staged."""
    script = (
        "import importlib, inspect, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import core\n"
        "discord_mb.coarse_duration = lambda value: 'STALE'\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "original_signature = inspect.signature\n"
        "first = True\n"
        "def paused_then_failing(value):\n"
        "    global first\n"
        "    if first:\n"
        "        first = False\n"
        "        entered.set()\n"
        "        assert release.wait(10)\n"
        "    if getattr(value, '__module__', '').endswith('.cli'):\n"
        "        raise RuntimeError('injected reload failure')\n"
        "    return original_signature(value)\n"
        "inspect.signature = paused_then_failing\n"
        "failure = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: failure.append(exc)\n"
        "thread = threading.Thread(target=reload_it)\n"
        "thread.start()\n"
        "assert entered.wait(10)\n"
        "del discord_mb.coarse_duration\n"
        "release.set()\n"
        "thread.join(10)\n"
        "inspect.signature = original_signature\n"
        "assert failure and isinstance(failure[0], RuntimeError), failure\n"
        "assert not hasattr(discord_mb, 'coarse_duration')\n"
        "assert not hasattr(core, 'coarse_duration')\n"
        "try: discord_mb.recovery_label({'recover_in': 3600})\n"
        "except NameError: pass\n"
        "else: raise AssertionError('deleted override was resurrected')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_public_call_during_reload_cannot_publish_raw_implementations(tmp):
    """Old wrappers retain their own sync state while reload is staged."""
    script = (
        "import importlib, inspect, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "original_signature = inspect.signature\n"
        "first = True\n"
        "def paused_signature(value):\n"
        "    global first\n"
        "    if first:\n"
        "        first = False\n"
        "        entered.set()\n"
        "        assert release.wait(10)\n"
        "    return original_signature(value)\n"
        "inspect.signature = paused_signature\n"
        "failure = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: failure.append(exc)\n"
        "thread = threading.Thread(target=reload_it)\n"
        "thread.start()\n"
        "assert entered.wait(10)\n"
        "discord_mb.pid_alive(999999999)\n"
        "release.set()\n"
        "thread.join(10)\n"
        "inspect.signature = original_signature\n"
        "assert not failure, failure\n"
        "assert not hasattr(discord_mb.pid_cmdline, '__wrapped__')\n"
        "assert discord_mb.pid_cmdline.__code__.co_name == 'pid_cmdline'\n"
        "def fake_open(*args, **kwargs):\n"
        "    raise RuntimeError('patch reached implementation')\n"
        "discord_mb.open = fake_open\n"
        "if sys.platform != 'win32':\n"
        "    try: discord_mb.pid_cmdline(999999999)\n"
        "    except RuntimeError: pass\n"
        "    else: raise AssertionError('raw implementation was published')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_old_call_cannot_repropagate_patch_into_staged_reload(tmp):
    """Frozen discovery ignores old-generation writes made during reload."""
    script = (
        "import importlib, inspect, pickle, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import core\n"
        "old_call = discord_mb.is_connector_process\n"
        "fake = lambda pid: True\n"
        "discord_mb.pid_alive = fake\n"
        "old_call(999999999, 'nobody')\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "original_signature = inspect.signature\n"
        "first = True\n"
        "def paused_signature(value):\n"
        "    global first\n"
        "    if first:\n"
        "        first = False\n"
        "        entered.set()\n"
        "        assert release.wait(10)\n"
        "    return original_signature(value)\n"
        "inspect.signature = paused_signature\n"
        "failure = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: failure.append(exc)\n"
        "thread = threading.Thread(target=reload_it)\n"
        "thread.start()\n"
        "assert entered.wait(10)\n"
        "old_call(999999999, 'nobody')\n"
        "release.set()\n"
        "thread.join(10)\n"
        "inspect.signature = original_signature\n"
        "assert not failure, failure\n"
        "assert not hasattr(discord_mb.pid_alive, '__wrapped__')\n"
        "assert core.pid_alive.__code__.co_code == "
        "discord_mb.pid_alive.__code__.co_code\n"
        "pickle.dumps(discord_mb.pid_alive)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_async_generator_wrapper_delegates_full_protocol(tmp):
    """asend, athrow, and aclose reach the underlying async generator."""
    script = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "closed = []\n"
        "async def probe():\n"
        "    try:\n"
        "        try:\n"
        "            received = yield 'ready'\n"
        "            yield ('received', received)\n"
        "        except ValueError:\n"
        "            yield 'caught'\n"
        "    finally:\n"
        "        closed.append(True)\n"
        "wrapped = discord_mb._exact_compatibility_wrapper(probe)\n"
        "async def scenario():\n"
        "    stream = wrapped()\n"
        "    assert await anext(stream) == 'ready'\n"
        "    assert await stream.asend(42) == ('received', 42)\n"
        "    await stream.aclose()\n"
        "    stream = wrapped()\n"
        "    assert await anext(stream) == 'ready'\n"
        "    assert await stream.athrow(ValueError('probe')) == 'caught'\n"
        "    await stream.aclose()\n"
        "    stream = wrapped()\n"
        "    assert await anext(stream) == 'ready'\n"
        "    before = len(closed)\n"
        "    await stream.aclose()\n"
        "    assert len(closed) == before + 1\n"
        "    async def catches_generator_exit():\n"
        "        try:\n"
        "            yield 'ready'\n"
        "        except GeneratorExit:\n"
        "            yield 'caught-generator-exit'\n"
        "    wrapped_exit = discord_mb._exact_compatibility_wrapper(\n"
        "        catches_generator_exit)\n"
        "    stream = wrapped_exit()\n"
        "    assert await anext(stream) == 'ready'\n"
        "    assert await stream.athrow(GeneratorExit) == "
        "'caught-generator-exit'\n"
        "    await stream.aclose()\n"
        "asyncio.run(scenario())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_absent_builtin_facade_override_reaches_implementation(tmp):
    """Patched builtins retain the monolith's shared-global behavior."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "seen = []\n"
        "discord_mb.print = lambda *args, **kwargs: seen.append(args)\n"
        "try:\n"
        "    discord_mb.send('me', 'you', 'subject', "
        "'x' * (discord_mb.MAX_BODY_TOTAL + 1))\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 2\n"
        "else:\n"
        "    raise AssertionError('oversized body was accepted')\n"
        "assert len(seen) == 1, seen\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_builtin_setattr_override_is_not_used_by_facade_machinery(tmp):
    """Compatibility internals keep builtin patches confined to app code."""
    script = (
        "import builtins, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "state = {'armed': False, 'calls': 0}\n"
        "def replacement(obj, name, value):\n"
        "    state['calls'] += 1\n"
        "    if state['armed']:\n"
        "        raise RuntimeError('compatibility machinery used patch')\n"
        "    return builtins.setattr(obj, name, value)\n"
        "discord_mb.setattr = replacement\n"
        "setup_calls = state['calls']\n"
        "state['armed'] = True\n"
        "assert discord_mb.is_connector_process(999999999, 'nobody') is None\n"
        "assert state['calls'] == setup_calls\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_builtin_set_override_is_not_used_by_facade_machinery(tmp):
    """Installing a ``set`` patch must not execute it inside the facade."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "calls = []\n"
        "def replacement(*args, **kwargs):\n"
        "    calls.append((args, kwargs))\n"
        "    raise RuntimeError('compatibility machinery used set patch')\n"
        "discord_mb.set = replacement\n"
        "assert calls == []\n"
        "assert discord_mb.is_connector_process(999999999, 'nobody') is None\n"
        "assert calls == []\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_refreshes_environment_backed_configuration(tmp):
    """Reload re-evaluates settings and recreates declared public types."""
    script = (
        "import importlib, os, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "os.environ['DISCORD_MB_BRIDGE_CHANNEL'] = 'first'\n"
        "import discord_mb\n"
        "from discord_mb_lib import core, storage, connector, cli\n"
        "modules = (core, storage, connector, cli)\n"
        "assert discord_mb.BRIDGE_CHANNEL_NAME == 'first'\n"
        "assert all(module.BRIDGE_CHANNEL_NAME == 'first' "
        "for module in modules)\n"
        "connector_type = discord_mb.ConnectorApp\n"
        "os.environ['DISCORD_MB_BRIDGE_CHANNEL'] = 'second'\n"
        "importlib.reload(discord_mb)\n"
        "assert discord_mb.BRIDGE_CHANNEL_NAME == 'second'\n"
        "assert all(module.BRIDGE_CHANNEL_NAME == 'second' "
        "for module in modules)\n"
        "assert discord_mb.ConnectorApp is not connector_type\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_refreshes_home_derived_paths(tmp):
    """Reload follows a changed home for token and built-in plugin paths."""
    script = (
        "import importlib, os, pathlib, sys, tempfile\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "first = pathlib.Path(tempfile.mkdtemp())\n"
        "second = pathlib.Path(tempfile.mkdtemp())\n"
        "os.environ['HOME'] = str(first)\n"
        "os.environ['USERPROFILE'] = str(first)\n"
        "import discord_mb\n"
        "from discord_mb_lib import core, storage, connector, cli\n"
        "modules = (core, storage, connector, cli)\n"
        "assert discord_mb.TOKEN_DIR == first / '.agent-bundle' / 'discord'\n"
        "os.environ['HOME'] = str(second)\n"
        "os.environ['USERPROFILE'] = str(second)\n"
        "importlib.reload(discord_mb)\n"
        "expected = {\n"
        "  'TOKEN_DIR': second / '.agent-bundle' / 'discord',\n"
        "  'KIMI_TOKEN_DIR': second / '.agent-bundle' / 'discord',\n"
        "  'CODEX_TOKEN_DIR': second / '.agent-bundle' / 'discord',\n"
        "  'DEFAULT_STATUS_PLUGIN': second / '.claude' / 'skills' / "
        "'discord' / 'discord_status_default.py',\n"
        "  'KIMI_STATUS_PLUGIN': second / '.kimi-code' / 'skills' / "
        "'discord' / 'discord_status_kimi.py',\n"
        "  'CODEX_STATUS_PLUGIN': second / '.codex' / 'skills' / "
        "'discord' / 'discord_status_codex.py',\n"
        "  '_DEFAULT_CONNECTOR_LOCK_ROOT': second / "
        "'.discord-mailbox-log-locks',\n"
        "}\n"
        "for name, value in expected.items():\n"
        "    assert getattr(discord_mb, name) == value, name\n"
        "    assert all(getattr(module, name) == value "
        "for module in modules), name\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_rebuilds_mutated_import_time_lookup_tables(tmp):
    """In-place mutations do not survive monolith-compatible reload."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import core, storage, connector, cli\n"
        "names = ('_PARENT_CMD_PATTERNS', '_DUR_UNITS', "
        "'_FLAVOR_STATUS_PLUGINS', '_EXTENSION_FLAVOR_DIRS')\n"
        "for name in names: getattr(discord_mb, name)['adversary'] = object()\n"
        "importlib.reload(discord_mb)\n"
        "for name in names:\n"
        "    assert 'adversary' not in getattr(discord_mb, name), name\n"
        "    assert all('adversary' not in getattr(module, name) "
        "for module in (core, storage, connector, cli)), name\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_rebinds_from_import_dependencies(tmp):
    """Reload repeats the monolith's dependency attribute imports."""
    script = (
        "import collections, importlib, pathlib, sys, typing\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "from discord_mb_lib import _settings, _temp_provenance; import discord_mb\n"
        "from discord_mb_lib import core, storage, connector, cli\n"
        "modules = (core, storage, connector, cli)\n"
        "real_path = pathlib.Path\n"
        # Bind to the CONCRETE class (PosixPath/WindowsPath), captured before
        # pathlib.Path is reassigned below. On 3.11 Path.__new__ substitutes the
        # concrete class only when `cls is Path`, and that compares against the
        # module global this test has just replaced -- so routing through the
        # abstract base leaves it unsubstituted and it has no _flavour. Later
        # versions do not care, and the identity assertions are unaffected.
        "real_concrete = type(real_path())\n"
        "class PathReplacement:\n"
        "    def __new__(cls, *args, **kwargs): "
        "return real_concrete(*args, **kwargs)\n"
        "    @staticmethod\n"
        "    def home(): return real_concrete.home()\n"
        "markers = {\n"
        "  'deque': object(), 'Path': PathReplacement, 'Any': object(),\n"
        "  'setting': lambda name, default=None: 'PATCHED-' + name,\n"
        "  '_linklike': object(), 'ensure_owned_temp_dir': object(),\n"
        "}\n"
        "collections.deque = markers['deque']\n"
        "pathlib.Path = markers['Path']\n"
        "typing.Any = markers['Any']\n"
        "_settings.setting = markers['setting']\n"
        "_temp_provenance._linklike = markers['_linklike']\n"
        "_temp_provenance.ensure_owned_temp_dir = "
        "markers['ensure_owned_temp_dir']\n"
        "importlib.reload(discord_mb)\n"
        "for name, marker in markers.items():\n"
        "    assert getattr(discord_mb, name) is marker, name\n"
        "    assert all(getattr(module, name) is marker "
        "for module in modules), name\n"
        "assert discord_mb.BRIDGE_CHANNEL_NAME == "
        "'PATCHED-DISCORD_MB_BRIDGE_CHANNEL'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_preserves_user_attributes_named_like_facade_internals(tmp):
    """Compatibility-only imports do not claim new public module names."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "names = ('builtins', 'functools', 'importlib', 'inspect', "
        "'contextvars', 'threading', 'types')\n"
        "markers = {name: object() for name in names}\n"
        "for name, marker in markers.items(): setattr(discord_mb, name, marker)\n"
        "importlib.reload(discord_mb)\n"
        "assert all(getattr(discord_mb, name) is marker "
        "for name, marker in markers.items())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_declaration_overwrites_an_earlier_concurrent_assignment(tmp):
    """Normal re-execution lets declarations win over preceding writes."""
    script = (
        "import importlib, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "from discord_mb_lib import _settings; import discord_mb\n"
        "entered = threading.Event(); release = threading.Event()\n"
        "original = _settings.setting; calls = 0; errors = []\n"
        "def blocking_setting(*args, **kwargs):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 1:\n"
        "        entered.set(); assert release.wait(10)\n"
        "    return original(*args, **kwargs)\n"
        "_settings.setting = blocking_setting\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: errors.append(exc)\n"
        "thread = threading.Thread(target=reload_it); thread.start()\n"
        "assert entered.wait(10)\n"
        "discord_mb.MAX_BODY = 777\n"
        "release.set(); thread.join(15); _settings.setting = original\n"
        "assert not errors, errors\n"
        "assert discord_mb.MAX_BODY == 1900\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_preserves_an_assignment_after_declaration_staging(tmp):
    """An ordinary write after declaration remains the final module value."""
    script = (
        "import importlib, pathlib, sys, threading\n"
        f"path = pathlib.Path({str(MB)!r})\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "source = path.read_text(encoding='utf-8').splitlines()\n"
        "pause_line = next(i for i, text in enumerate(source, 1) "
        "if text.strip() == "
        "'_old_globals = _previous_globals_by_module.get(_module, {})')\n"
        "entered = threading.Event(); release = threading.Event()\n"
        "paused = False; errors = []\n"
        "def trace(frame, event, arg):\n"
        "    global paused\n"
        "    if (not paused and event == 'line' "
        "and frame.f_code.co_name == '<module>' "
        "and frame.f_lineno == pause_line):\n"
        "        paused = True; entered.set(); assert release.wait(10)\n"
        "    return trace\n"
        "threading.settrace(trace)\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: errors.append(exc)\n"
        "thread = threading.Thread(target=reload_it); thread.start()\n"
        "assert entered.wait(10)\n"
        "sentinel = lambda value: 'late-assignment'\n"
        "discord_mb.coarse_duration = sentinel\n"
        "release.set(); thread.join(15); threading.settrace(None)\n"
        "assert not errors, errors\n"
        "assert discord_mb.coarse_duration is sentinel\n"
        "assert discord_mb.coarse_duration(60) == 'late-assignment'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_transient_attribute_versions_remain_bounded(tmp):
    """ABA counters exist only while a reload transaction is active."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "for round_no in range(3):\n"
        "    for index in range(1000):\n"
        "        name = f'transient_{round_no}_{index}'\n"
        "        setattr(discord_mb, name, None)\n"
        "        delattr(discord_mb, name)\n"
        "    assert len(discord_mb._facade_mutation_versions) == 0\n"
        "    importlib.reload(discord_mb)\n"
        "    assert len(discord_mb._facade_mutation_versions) == 0\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_recreates_classes_without_mutating_retained_generation(tmp):
    """Reload matches class statements: old classes stay, new bindings reset."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "target = discord_mb._ConnectorLogWriter\n"
        "original_write = target.write\n"
        "original_backup_path = target._backup_path\n"
        "replacement = lambda self, line: 'patched'\n"
        "target.write = replacement\n"
        "del target._backup_path\n"
        "target.adversary_added = object()\n"
        "importlib.reload(discord_mb)\n"
        "current = discord_mb._ConnectorLogWriter\n"
        "assert current is not target\n"
        "assert target.write is replacement\n"
        "assert not hasattr(target, '_backup_path')\n"
        "assert hasattr(target, 'adversary_added')\n"
        "assert current.write is not replacement\n"
        "assert current._backup_path.__name__ == original_backup_path.__name__\n"
        "assert not hasattr(current, 'adversary_added')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_concurrent_class_assignment_stays_on_retained_generation(tmp):
    """A class mutation during staging cannot poison the new generation."""
    script = (
        "import importlib, inspect, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "target = discord_mb._ConnectorLogWriter\n"
        "original = target.write\n"
        "entered = threading.Event(); release = threading.Event()\n"
        "real_signature = inspect.signature\n"
        "first = True\n"
        "def paused_signature(value):\n"
        "    global first\n"
        "    if first:\n"
        "        first = False; entered.set(); assert release.wait(10)\n"
        "    return real_signature(value)\n"
        "inspect.signature = paused_signature\n"
        "errors = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: errors.append(exc)\n"
        "thread = threading.Thread(target=reload_it); thread.start()\n"
        "assert entered.wait(10)\n"
        "concurrent = lambda self, line: 'concurrent'\n"
        "target.write = concurrent\n"
        "release.set(); thread.join(10); inspect.signature = real_signature\n"
        "assert not errors, errors\n"
        "assert target.write is concurrent\n"
        "current = discord_mb._ConnectorLogWriter\n"
        "assert current is not target\n"
        "assert current.write is not concurrent\n"
        "importlib.reload(discord_mb)\n"
        "assert target.write is concurrent\n"
        "assert discord_mb._ConnectorLogWriter is not current\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_failed_reload_preserves_concurrent_class_assignment(tmp):
    """Class rollback preserves explicit ABA mutation after restoration."""
    script = (
        "import importlib, inspect, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "target = discord_mb._ConnectorLogWriter\n"
        "baseline = target.write\n"
        "prior = lambda self, line: 'prior'\n"
        "target.write = prior\n"
        "entered = threading.Event(); release = threading.Event()\n"
        "real_signature = inspect.signature\n"
        "calls = 0\n"
        "def failing_signature(value):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 1:\n"
        "        entered.set(); assert release.wait(10)\n"
        "    if calls > 3: raise RuntimeError('injected staging failure')\n"
        "    return real_signature(value)\n"
        "inspect.signature = failing_signature\n"
        "errors = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: errors.append(exc)\n"
        "thread = threading.Thread(target=reload_it); thread.start()\n"
        "assert entered.wait(10)\n"
        "concurrent = lambda self, line: 'concurrent'\n"
        "target.write = concurrent\n"
        "target.write = baseline\n"
        "release.set(); thread.join(10); inspect.signature = real_signature\n"
        "assert len(errors) == 1 and isinstance(errors[0], RuntimeError), errors\n"
        "assert target.write is baseline\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_failed_reload_preserves_exception_class_delete_aba(tmp):
    """Exception classes participate in atomic class rollback ownership."""
    script = (
        "import importlib, sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "from discord_mb_lib import _settings; import discord_mb\n"
        "target = discord_mb.StatusPluginGatewayTransportError\n"
        "prior = object(); target.adversary_probe = prior\n"
        "entered = threading.Event(); release = threading.Event()\n"
        "real_setting = _settings.setting\n"
        "calls = 0\n"
        "def failing_setting(name, default=None):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 1:\n"
        "        entered.set(); assert release.wait(10)\n"
        "    if calls > 3: raise RuntimeError('injected refresh failure')\n"
        "    return real_setting(name, default)\n"
        "_settings.setting = failing_setting\n"
        "errors = []\n"
        "def reload_it():\n"
        "    try: importlib.reload(discord_mb)\n"
        "    except BaseException as exc: errors.append(exc)\n"
        "thread = threading.Thread(target=reload_it); thread.start()\n"
        "assert entered.wait(10)\n"
        "target.adversary_probe = object(); del target.adversary_probe\n"
        "release.set(); thread.join(10); _settings.setting = real_setting\n"
        "assert len(errors) == 1 and isinstance(errors[0], RuntimeError), errors\n"
        "assert not hasattr(target, 'adversary_probe')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_public_exceptions_accept_independent_subclass_metaclass(tmp):
    """The package split does not constrain callers' exception metaclasses."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "class IndependentMeta(type): pass\n"
        "class Derived(discord_mb.SendError, metaclass=IndependentMeta): pass\n"
        "assert type(Derived) is IndependentMeta\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_exception_reload_uses_immutable_declaration_metadata(tmp):
    """Caller mutations cannot rename or rebase the next exception class."""
    script = (
        "import importlib, pickle, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "old = discord_mb.SendError\n"
        "old.__name__ = 'RenamedByCaller'\n"
        "old.__qualname__ = 'RenamedByCaller'\n"
        "old.__bases__ = (RuntimeError,)\n"
        "importlib.reload(discord_mb)\n"
        "current = discord_mb.SendError\n"
        "assert current is not old\n"
        "assert current.__name__ == 'SendError'\n"
        "assert current.__qualname__ == 'SendError'\n"
        "assert current.__bases__ == (Exception,)\n"
        "assert issubclass(current, Exception)\n"
        "assert not issubclass(current, RuntimeError)\n"
        "assert pickle.loads(pickle.dumps(current)) is current\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_recreates_internal_classes_and_preserves_old_generation(tmp):
    """Every declared class follows monolith reload identity semantics."""
    script = (
        "import importlib, pickle, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "names = ('_ConnectorOwnershipError', '_SharedLockError', "
        "'_ConnectorOwnership', '_ExtensionContext', 'ConnectorApp')\n"
        "old = {name: getattr(discord_mb, name) for name in names}\n"
        "old['_ConnectorOwnership'].adversary_probe = object()\n"
        "importlib.reload(discord_mb)\n"
        "assert all(getattr(discord_mb, name) is not value "
        "for name, value in old.items())\n"
        "assert hasattr(old['_ConnectorOwnership'], 'adversary_probe')\n"
        "assert not hasattr(discord_mb._ConnectorOwnership, 'adversary_probe')\n"
        "assert all(getattr(discord_mb, name).__module__ == 'discord_mb' "
        "for name in names)\n"
        "assert pickle.loads(pickle.dumps(discord_mb.ConnectorApp)) "
        "is discord_mb.ConnectorApp\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_reexecutes_dynamic_class_declarations(tmp):
    """Environment-derived class attributes are evaluated for each generation."""
    script = (
        "import importlib, os, pathlib, sys, tempfile\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "os.environ.pop('DISCORD_MB_TEST_LOCK_ROOT', None)\n"
        "import discord_mb\n"
        "new_root = pathlib.Path(tempfile.mkdtemp()) / 'identity-locks'\n"
        "os.environ['DISCORD_MB_TEST_LOCK_ROOT'] = str(new_root)\n"
        "importlib.reload(discord_mb)\n"
        "assert discord_mb._ConnectorOwnership._IDENTITY_LOCK_ROOT == new_root\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_reload_recreates_method_descriptors_from_frozen_declaration(tmp):
    """Method identities and attributes reset like repeated class statements."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "old_class = discord_mb._ConnectorLogWriter\n"
        "old_method = old_class.write\n"
        "old_method.adversary_probe = object()\n"
        "importlib.reload(discord_mb)\n"
        "new_method = discord_mb._ConnectorLogWriter.write\n"
        "assert new_method is not old_method\n"
        "assert hasattr(old_method, 'adversary_probe')\n"
        "assert not hasattr(new_method, 'adversary_probe')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_direct_facade_dictionary_version_reaches_cli(tmp):
    """Raw facade version mutation remains visible to the legacy CLI."""
    script = (
        "import contextlib, io, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "discord_mb.__dict__['__version__'] = 'adversary-direct-dict'\n"
        "sys.argv = ['discord_mb.py', '--version']\n"
        "output = io.StringIO()\n"
        "with contextlib.redirect_stdout(output):\n"
        "    try: discord_mb._cli()\n"
        "    except SystemExit as exc: assert exc.code == 0\n"
        "assert output.getvalue().strip() == "
        "'discord_mb.py adversary-direct-dict'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_direct_facade_dictionary_version_deletion_reaches_cli(tmp):
    """Deleting the facade version leaves the legacy CLI global undefined."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "discord_mb.__dict__.pop('__version__')\n"
        "sys.argv = ['discord_mb.py', '--version']\n"
        "try: discord_mb._cli()\n"
        "except NameError: pass\n"
        "else: raise AssertionError('deleted version stayed defined')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_direct_facade_dictionary_deletion_reaches_function_globals(tmp):
    """Legacy functions observe deletion from their facade globals mapping."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "del discord_mb.__dict__['coarse_duration']\n"
        "try: discord_mb.recovery_label({'recover_in': 3600})\n"
        "except NameError: pass\n"
        "else: raise AssertionError('deleted global remained in implementation')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_successive_reloads_prune_obsolete_function_helpers(tmp):
    """Generated top-level helper globals remain bounded across reloads."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "baseline = len(vars(discord_mb))\n"
        "for _ in range(4):\n"
        "    importlib.reload(discord_mb)\n"
        "    assert len(vars(discord_mb)) <= baseline + 10, "
        "(baseline, len(vars(discord_mb)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_retained_top_level_wrapper_survives_reload(tmp):
    """Old public callable references stay executable after a reload."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "old = discord_mb.pid_alive\n"
        "assert old(999999999) is False\n"
        "importlib.reload(discord_mb)\n"
        "assert old(999999999) is False\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_missing_nonbuiltin_global_can_be_restored_from_facade(tmp):
    """Facade assignments repair missing implementation globals on demand."""
    script = (
        "import pathlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import storage\n"
        "del storage.Path\n"
        "discord_mb.Path = pathlib.Path\n"
        "import tempfile\n"
        "probe = pathlib.Path(tempfile.gettempdir()) / 'probe'\n"
        "ownership = discord_mb._ConnectorOwnership(probe)\n"
        "ownership.close()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_running_sync_call_observes_concurrent_facade_assignment(tmp):
    """Facade global writes are visible at the next implementation lookup."""
    script = (
        "import sys, tempfile, threading\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "root = Path(tempfile.mkdtemp())\n"
        "(root / 'in').mkdir()\n"
        "(root / 'out').mkdir()\n"
        "discord_mb.meta_in_dir = lambda _: root / 'in'\n"
        "discord_mb.meta_out_dir = lambda _: root / 'out'\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n"
        "seen = []\n"
        "class A:\n"
        "    def time(self): return 0\n"
        "    def sleep(self, delay):\n"
        "        seen.append('A')\n"
        "        if len(seen) == 1:\n"
        "            entered.set()\n"
        "            assert release.wait(5)\n"
        "        else:\n"
        "            raise RuntimeError('A-used-again')\n"
        "class B:\n"
        "    def time(self): return 0\n"
        "    def sleep(self, delay):\n"
        "        seen.append('B')\n"
        "        raise RuntimeError('B-used')\n"
        "discord_mb.time = A()\n"
        "failure = []\n"
        "def run():\n"
        "    try: discord_mb._meta_request('x', {}, 1)\n"
        "    except BaseException as exc: failure.append(exc)\n"
        "thread = threading.Thread(target=run)\n"
        "thread.start()\n"
        "assert entered.wait(5)\n"
        "discord_mb.time = B()\n"
        "release.set()\n"
        "thread.join(5)\n"
        "assert seen == ['A', 'B'], seen\n"
        "assert str(failure[0]) == 'B-used'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_failed_sync_target_rotation_cannot_poison_old_wrappers(tmp):
    """Commit rotates the retained sync target without invoking overrides."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "from discord_mb_lib import core\n"
        "raw = core.pid_alive\n"
        "target = discord_mb._compatibility_sync_target\n"
        "Target = type(target)\n"
        "class SetThenRaise(Target):\n"
        "    armed = True\n"
        "    def __setattr__(self, name, value):\n"
        "        object.__setattr__(self, name, value)\n"
        "        if name == 'function' and self.armed:\n"
        "            object.__setattr__(self, 'armed', False)\n"
        "            raise RuntimeError('rotated then failed')\n"
        "target.__class__ = SetThenRaise\n"
        "importlib.reload(discord_mb)\n"
        "discord_mb.is_connector_process(999999999, 'nobody')\n"
        "assert core.pid_alive is raw\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_inflight_call_observes_late_raw_dictionary_mutation(tmp):
    """Later global lookups share the live facade dictionary."""
    script = (
        "import sys, threading\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "entered = threading.Event(); release = threading.Event()\n"
        "real_time = discord_mb.time\n"
        "class PausingTime:\n"
        "    @staticmethod\n"
        "    def strftime(fmt):\n"
        "        entered.set(); assert release.wait(10); return 'stamp'\n"
        "discord_mb.time = PausingTime\n"
        "used = []\n"
        "class FakePath:\n"
        "    def __truediv__(self, name): return self\n"
        "    def with_suffix(self, suffix): return self\n"
        "    def write_text(self, *a, **k): pass\n"
        "    def replace(self, other): pass\n"
        "    def __str__(self): return used[-1]\n"
        "def old(identity): used.append('OLD'); return FakePath()\n"
        "def new(identity): used.append('NEW'); return FakePath()\n"
        "discord_mb.outbox_dir = old\n"
        "thread = threading.Thread(target=lambda: discord_mb.send("
        "'me', 'you', 'subject', 'body'))\n"
        "thread.start(); assert entered.wait(10)\n"
        "discord_mb.__dict__['outbox_dir'] = new\n"
        "release.set(); thread.join(10)\n"
        "discord_mb.time = real_time\n"
        "assert used == ['NEW'], used\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_top_level_functions_do_not_add_compatibility_traceback_frames(tmp):
    """Top-level calls execute original code directly against facade globals."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "del discord_mb.__dict__['MAX_BODY_TOTAL']\n"
        "try: discord_mb.send('me', 'you', 'subject', 'body')\n"
        "except NameError as exc:\n"
        "    names = []; tb = exc.__traceback__\n"
        "    while tb is not None:\n"
        "        names.append(tb.tb_frame.f_code.co_name); tb = tb.tb_next\n"
        "    assert names == ['<module>', 'send'], names\n"
        "else: raise AssertionError('deleted global stayed defined')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facade_code_objects_name_the_installed_module(tmp):
    """A traceback must point at source the consumer actually has.

    Code objects used to be projected onto the pre-package monolith's file and
    line numbers, which required shipping a compressed copy of that deleted
    file so linecache could agree. Installed as a wheel that names a location
    the consumer cannot open, so provenance stays on the real module now.
    """
    script = (
        "import inspect, pathlib, sys, traceback\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "code = discord_mb.moved_body.__code__\n"
        "origin = pathlib.Path(code.co_filename)\n"
        "assert origin.name == 'core.py', origin\n"
        "assert origin.is_file(), origin\n"
        "assert inspect.getsource(discord_mb.moved_body).startswith("
        "'def moved_body(')\n"
        "try: discord_mb.moved_body('a', 'b', 'c', extra_urls=1)\n"
        "except TypeError as exc:\n"
        "    frame = traceback.extract_tb(exc.__traceback__)[-1]\n"
        "    assert frame.name == 'moved_body'\n"
        "    assert pathlib.Path(frame.filename).is_file(), frame.filename\n"
        "    assert frame.line, 'traceback showed no source text'\n"
        "else: raise AssertionError('invalid extra_urls was accepted')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facade_baseline_deletion_reaches_implementation_until_reload(tmp):
    """Deleting an original facade global removes the split global too."""
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "del discord_mb.coarse_duration\n"
        "try: discord_mb.recovery_label({'recover_in': '1h'})\n"
        "except NameError: pass\n"
        "else: raise AssertionError('deleted global remained in implementation')\n"
        "importlib.reload(discord_mb)\n"
        "assert discord_mb.recovery_label({'recover_in': '1h'}) == '1h'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facade_version_assignment_reaches_cli_module_reference(tmp):
    """Data dunder assignments preserve the monolith's global behavior."""
    script = (
        "import contextlib, io, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "discord_mb.__version__ = '9.8.7-test'\n"
        "sys.argv = ['discord_mb.py', '--version']\n"
        "output = io.StringIO()\n"
        "try:\n"
        "    with contextlib.redirect_stdout(output): discord_mb._cli()\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "assert '9.8.7-test' in output.getvalue(), output.getvalue()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_private_cleanup_failure_rolls_back_removed_modules(tmp):
    """A failing pop cannot leave a partial private package tree behind.

    Injecting that failure means giving sys.modules a dict subclass, and on
    macOS 3.12 and 3.13 CPython segfaults under it. faulthandler put the crash
    inside its own import machinery, during a collection, with no frame from
    this package on the stack at all:

        Garbage-collecting
        File "<frozen importlib._bootstrap>", line 488 in
            _call_with_frames_removed
        File "<frozen importlib._bootstrap_external>", line 1087 in
            source_to_code

    A plain import of an unimported stdlib module survives the same swap, so
    it is not the substitution by itself -- it takes enough allocation to
    trigger a collection while the import lock is held, which compiling this
    package's four modules does and importing http.client does not. macOS 3.11
    and every Linux and Windows lane run the test normally.
    """
    if sys.platform == "darwin" and sys.version_info >= (3, 12):
        _util.skip("CPython segfaults in its own import machinery here; see "
                   "this test's docstring for the faulthandler stack")
    script = (
        "import contextvars, faulthandler, functools, importlib, "
        "importlib.util, inspect, pathlib, sys, types, uuid\n"
        # A segfault here leaves no Python traceback of its own; faulthandler
        # is what turns it into a named frame on stderr.
        "faulthandler.enable()\n"
        f"mailbox = pathlib.Path({MB!r})\n"
        "original_modules = sys.modules\n"
        "class FailingModules(dict):\n"
        "    failed = False\n"
        "    def pop(self, name, *default):\n"
        "        value = super().pop(name, *default)\n"
        "        if name.startswith('_discord_mb_lib_') and not self.failed:\n"
        "            self.failed = True\n"
        "            raise RuntimeError('injected cleanup failure')\n"
        "        return value\n"
        # Breadcrumbs, because this child has died by signal on some macOS
        # runners: a bare exit status says nothing about how far it got, and
        # the interpreter cannot report its own segfault.
        "def mark(step): print(step, file=sys.stderr, flush=True)\n"
        "replacement = FailingModules(original_modules)\n"
        "mark('built-replacement')\n"
        "sys.modules = replacement\n"
        "mark('swapped-sys-modules')\n"
        "try:\n"
        # Is a plain import survivable with sys.modules replaced at all? If
        # this marker never prints, the crash is CPython's import machinery
        # against a substituted modules mapping, not anything in the facade.
        "    importlib.import_module('http.client')\n"
        "    mark('plain-import-ok')\n"
        "    before = {n for n in replacement if n.startswith('_discord_mb_lib_')}\n"
        "    spec = importlib.util.spec_from_file_location('discord_alias', mailbox)\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    mark('about-to-exec')\n"
        "    try: spec.loader.exec_module(module)\n"
        "    except RuntimeError: mark('raised-as-expected')\n"
        "    else: raise AssertionError('cleanup failure was swallowed')\n"
        "    after = {n for n in replacement if n.startswith('_discord_mb_lib_')}\n"
        "    assert after == before, sorted(after - before)\n"
        "    mark('assertions-passed')\n"
        "finally:\n"
        "    sys.modules = original_modules\n"
        "    mark('restored-sys-modules')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_facade_wrappers_preserve_raw_signatures_and_defaults(tmp):
    """Facade-bound callables expose their original signatures directly."""
    m = _util.load(MB, "discord_raw_signature")
    callables = (
        (m.send, m._core_module.send),
        (m.status_plugin_gateway_call,
         m._core_module.status_plugin_gateway_call),
        (m.connector_main, m._connector_module.connector_main),
    )
    for value, implementation in callables:
        assert not hasattr(value, '__wrapped__')
        assert value.__defaults__ == implementation.__defaults__
        assert value.__kwdefaults__ == implementation.__kwdefaults__
        assert inspect.signature(value, follow_wrapped=False) == \
            inspect.signature(implementation, follow_wrapped=False)
    method = m.ConnectorApp.__init__
    assert not hasattr(method, '__wrapped__')
    assert method.__code__.co_name == '__init__'
    assert str(inspect.signature(method)) == \
        '(self, identity, claude_pid=None, token=None, log_path=None, flavor=None)'


def test_repeated_path_imports_do_not_retain_private_packages(tmp):
    """Ephemeral facade loads must not root a new package tree forever."""
    before = {name for name in sys.modules
              if name.startswith("_discord_mb_lib_")}
    for index in range(4):
        module = _util.load(MB, f"discord_ephemeral_{index}")
        del module
    gc.collect()
    after = {name for name in sys.modules
             if name.startswith("_discord_mb_lib_")}
    assert after == before


def test_failed_path_import_preserves_canonical_package_transactionally(tmp):
    """A broken private deployment cannot damage an imported good package."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        importlib.import_module("discord_mb_lib.connector")
    finally:
        sys.path.pop(0)
    canonical = {
        name: module for name, module in sys.modules.items()
        if name == "discord_mb_lib" or name.startswith("discord_mb_lib.")
    }
    private_before = {name for name in sys.modules
                      if name.startswith("_discord_mb_lib_")}
    real_import = importlib.import_module

    def fail_connector(name, package=None):
        if name.startswith("_discord_mb_lib_") and name.endswith(".connector"):
            raise RuntimeError("injected connector import failure")
        return real_import(name, package)

    importlib.import_module = fail_connector
    try:
        try:
            _util.load(MB, "discord_failed_private_import")
        except RuntimeError as exc:
            assert "injected connector import failure" in str(exc)
        else:
            raise AssertionError("injected package failure was swallowed")
    finally:
        importlib.import_module = real_import

    assert all(sys.modules.get(name) is module
               for name, module in canonical.items())
    private_after = {name for name in sys.modules
                     if name.startswith("_discord_mb_lib_")}
    assert private_after == private_before


def test_connector_app_owns_lifecycle_configuration(tmp):
    """The connector package exposes an object boundary for one lifecycle."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        connector = importlib.import_module("discord_mb_lib.connector")
        connector = importlib.reload(connector)
    finally:
        sys.path.pop(0)

    seen = {}
    original = connector._run_connector
    connector._run_connector = lambda *args, **kwargs: seen.update(
        args=args, kwargs=kwargs) or 17
    try:
        app = connector.ConnectorApp(
            "agent", claude_pid=123, token="secret", log_path="agent.log",
            flavor="codex")
        assert app.run() == 17
    finally:
        connector._run_connector = original

    assert seen == {
        "args": ("agent",),
        "kwargs": {
            "claude_pid": 123,
            "token": "secret",
            "log_path": "agent.log",
            "flavor": "codex",
        },
    }


def test_connector_main_constructs_the_lifecycle_object(tmp):
    """The procedural compatibility API delegates to ``ConnectorApp``."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        connector = importlib.import_module("discord_mb_lib.connector")
        connector = importlib.reload(connector)
    finally:
        sys.path.pop(0)

    seen = {}

    class FakeConnectorApp:
        def __init__(self, *args, **kwargs):
            seen["init"] = (args, kwargs)

        def run(self):
            seen["run"] = True
            return 23

    original = connector.ConnectorApp
    connector.ConnectorApp = FakeConnectorApp
    try:
        result = connector.connector_main(
            "agent", claude_pid=456, token="token", log_path="custom.log",
            flavor="kimi")
    finally:
        connector.ConnectorApp = original

    assert result == 23
    assert seen == {
        "init": (("agent",), {
            "claude_pid": 456,
            "token": "token",
            "log_path": "custom.log",
            "flavor": "kimi",
        }),
        "run": True,
    }


def test_cli_connector_dispatches_through_connector_app(tmp):
    """The production CLI enters the object-oriented lifecycle boundary."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        cli = importlib.import_module("discord_mb_lib.cli")
    finally:
        sys.path.pop(0)
    seen = {}

    class FakeConnectorApp:
        def __init__(self, *args, **kwargs):
            seen["init"] = (args, kwargs)

        def run(self):
            seen["run"] = True

    original = cli.ConnectorApp
    original_argv = sys.argv
    cli.ConnectorApp = FakeConnectorApp
    sys.argv = ["discord_mb.py", "connector", "agent", "--flavor", "codex"]
    try:
        cli._cli()
    finally:
        cli.ConnectorApp = original
        sys.argv = original_argv

    assert seen == {
        "init": (("agent",), {
            "claude_pid": None,
            "token": None,
            "log_path": None,
            "flavor": "codex",
        }),
        "run": True,
    }


def test_connector_app_uses_its_real_package_source(tmp):
    """New APIs introspect against their actual source, not legacy projection."""
    sys.path.insert(0, _util.SCRIPTS)
    try:
        connector = importlib.import_module("discord_mb_lib.connector")
    finally:
        sys.path.pop(0)

    assert inspect.getsource(connector.ConnectorApp).startswith(
        "class ConnectorApp:")
    assert inspect.getsource(connector.ConnectorApp.run).lstrip().startswith(
        "def run(self):")


def test_facade_connector_main_keeps_the_legacy_call_shape(tmp):
    """The facade exposes the original lifecycle body, not its OOP adapter."""
    m = _util.load(MB, "discord_connector_call_shape")

    assert m.connector_main.__code__.co_name == "connector_main"
    assert m.connector_main.__code__.co_qualname == "connector_main"
    nested = {
        value.co_qualname
        for value in m.connector_main.__code__.co_consts
        if isinstance(value, types.CodeType)
    }
    assert "connector_main.<locals>.cleanup_startup" in nested

    try:
        m.connector_main("probe", flavor="bogus")
    except ValueError as exc:
        frames = traceback.extract_tb(exc.__traceback__)
    else:
        raise AssertionError("invalid connector flavor was accepted")
    assert [frame.name for frame in frames] == [
        "test_facade_connector_main_keeps_the_legacy_call_shape",
        "connector_main",
    ]


def test_facade_connector_main_source_is_the_real_implementation(tmp):
    """Inspection returns the code that runs, not a copy of a deleted file."""
    m = _util.load(MB, "discord_connector_source_shape")
    source = inspect.getsource(m.connector_main)

    # connector_main is the exported alias of _run_connector, so the real
    # source carries the implementation's own name -- which is the point: it
    # is the code that runs, not a copy of a file the consumer does not have.
    assert source.startswith("def _run_connector("), source.splitlines()[0]
    origin = Path(inspect.getsourcefile(m.connector_main))
    assert origin.is_file() and origin.name == "connector.py", origin


def test_fetch_usage_resolves_helper_from_current_facade_file(tmp):
    """The public facade's current location selects its usage helper."""
    m = _util.load(MB, "discord_usage_helper_location")
    relocated = Path(tmp) / "scripts"
    relocated.mkdir()
    helper = relocated / "usage_query.py"
    helper.write_text("# probe\n", encoding="utf-8")
    seen = {}

    class Result:
        stdout = '{"usage": {"probe": {"pct": 1}}}'

    class FakeSubprocess:
        SubprocessError = subprocess.SubprocessError

        @staticmethod
        def run(args, **kwargs):
            seen["args"] = args
            return Result()

    original_file = m.__file__
    original_subprocess = m.subprocess
    m.__file__ = str(relocated / "discord_mb.py")
    m.subprocess = FakeSubprocess
    try:
        assert m.fetch_usage() == {"probe": {"pct": 1}}
    finally:
        m.__file__ = original_file
        m.subprocess = original_subprocess

    assert Path(seen["args"][1]) == helper


def test_package_fetch_usage_resolves_the_helper_from_the_environment(tmp):
    """A wheel install has no sibling helper, so the path must be injectable.

    site-packages is not the caller's scripts directory, so the historical
    sibling probe finds nothing once this ships as a wheel. The installer
    points DISCORD_MB_USAGE_QUERY at the real helper; without it the board is
    simply not published.
    """
    core = importlib.import_module("discord_mb_lib.core")
    helper = Path(tmp) / "usage_query.py"
    helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    seen = {}

    class _Result:
        stdout = '{"usage": {"probe": {"pct": 1}}}'

    class _Sub:
        SubprocessError = Exception

        @staticmethod
        def run(args, **kwargs):
            seen["args"] = args
            return _Result()

    original_subprocess = core.subprocess
    original_env = os.environ.get("DISCORD_MB_USAGE_QUERY")
    core.subprocess = _Sub
    os.environ["DISCORD_MB_USAGE_QUERY"] = str(helper)
    try:
        assert core.fetch_usage() == {"probe": {"pct": 1}}
        assert Path(seen["args"][1]) == helper
    finally:
        core.subprocess = original_subprocess
        if original_env is None:
            os.environ.pop("DISCORD_MB_USAGE_QUERY", None)
        else:
            os.environ["DISCORD_MB_USAGE_QUERY"] = original_env


def test_package_fetch_usage_is_quiet_without_any_helper(tmp):
    """No helper anywhere means no board, not an exception."""
    core = importlib.import_module("discord_mb_lib.core")
    original_env = os.environ.get("DISCORD_MB_USAGE_QUERY")
    os.environ["DISCORD_MB_USAGE_QUERY"] = str(Path(tmp) / "absent.py")
    original_file = core.__file__
    core.__file__ = str(Path(tmp) / "pkg" / "core.py")
    try:
        assert core.fetch_usage() == {}
    finally:
        core.__file__ = original_file
        if original_env is None:
            os.environ.pop("DISCORD_MB_USAGE_QUERY", None)
        else:
            os.environ["DISCORD_MB_USAGE_QUERY"] = original_env


def test_star_import_does_not_leak_facade_build_temporaries(tmp):
    """Refactor-only loop state must not overwrite caller globals."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "instrumentable = replacements = SCRIPT_ROOT = 'caller-sentinel'\n"
        "from discord_mb import *\n"
        "assert instrumentable == 'caller-sentinel'\n"
        "assert replacements == 'caller-sentinel'\n"
        "assert SCRIPT_ROOT == 'caller-sentinel'\n"
        "assert ConnectorApp.__name__ == 'ConnectorApp'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_projected_classes_keep_working_source_locations(tmp):
    """Class introspection must resolve against the module that defines them.

    The facade used to reassign __module__ onto itself, and inspect resolves a
    class's source THROUGH __module__ -- which is why a copy of the monolith
    had to ship. Classes now keep their real module, so getsourcelines works
    against the installed file with no blob at all.
    """
    script = (
        "import inspect, pathlib, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "cases = ((discord_mb.SendRetry, 'class SendRetry'), "
        "(discord_mb._ConnectorLogWriter, 'class _ConnectorLogWriter'))\n"
        "for cls, declaration in cases:\n"
        "    assert cls.__module__.startswith('discord_mb_lib.'), cls.__module__\n"
        "    lines, first_line = inspect.getsourcelines(cls)\n"
        "    assert first_line > 0\n"
        "    assert lines[0].startswith(declaration), lines[0]\n"
        "    origin = pathlib.Path(inspect.getsourcefile(cls))\n"
        "    assert origin.is_file(), origin\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_cli_reads_the_live_facade_docstring(tmp):
    """The importable CLI retains the monolith's dynamic ``__doc__`` lookup."""
    script = (
        "import contextlib, io, sys\n"
        f"sys.path.insert(0, {str(_util.SCRIPTS)!r})\n"
        "import discord_mb\n"
        "discord_mb.__doc__ = 'ADVERSARY-DOC-SENTINEL'\n"
        "sys.argv = ['discord_mb.py']\n"
        "output = io.StringIO()\n"
        "with contextlib.redirect_stdout(output):\n"
        "    discord_mb._cli()\n"
        "assert 'ADVERSARY-DOC-SENTINEL' in output.getvalue()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=30)
    _assert_child_ok(result)


def test_core_exports_match_the_module_namespace(tmp):
    """core.__all__ is now a literal, so nothing recomputes it on drift.

    It was a comprehension over globals(), which no static analyser can
    evaluate -- `from .core import *` therefore resolved to nothing in the
    three modules that do it. Spelling the list out fixed that and introduced
    the usual hazard of a hand-maintained export list: a new public name that
    silently never reaches the facade. This recomputes the old expression and
    requires the two to agree.
    """
    # Through the facade, never `from discord_mb_lib import core`: this
    # package is also installed as a wheel, so a bare import resolves to
    # site-packages and the test would pass while describing somebody else's
    # copy of core.py.
    core = _util.load(MB, "mb_core_exports")._core_module

    expected = sorted(
        name for name in vars(core)
        if (not name.startswith('__')
            and not name.startswith('_discord_mb_class_')
            and name != 'SCRIPT_ROOT'))
    declared = sorted(core.__all__)
    assert declared == expected, (
        "core.__all__ has drifted from the module namespace.\n"
        f"missing from __all__: {sorted(set(expected) - set(declared))}\n"
        f"stale in __all__:     {sorted(set(declared) - set(expected))}")


def test_module_exports_cover_the_package(tmp):
    """storage, connector and cli list only their own names now.

    Each used to re-list everything its wildcard imports had pulled in. The
    facade unions the four __all__ lists, so trimming them to what each module
    defines leaves the exported set identical -- but only while every public
    name is actually reachable from one of the four. This recomputes the old
    per-module expression and requires the union to match.
    """
    facade = _util.load(MB, "mb_module_exports")
    modules = (facade._core_module, facade._storage_module,
               facade._connector_module, facade._cli_module)
    declared = set()
    for module in modules:
        declared.update(module.__all__)
    reachable = set()
    for module in modules:
        reachable.update(
            name for name in vars(module)
            if (not name.startswith('__')
                and not name.startswith('_discord_mb_class_')
                and name != 'SCRIPT_ROOT'))
    assert declared == reachable, (
        "the package's exported set has drifted.\n"
        f"reachable but unexported: {sorted(reachable - declared)}\n"
        f"exported but unreachable: {sorted(declared - reachable)}")


def _functions_calling_os_open(path):
    """(line, name, source) for every function that calls os.open with flags.

    The flags argument is almost always a `flags` local built a few lines
    earlier, so the whole enclosing function is the unit that has to mention
    O_BINARY, not the call's second argument.
    """
    import ast

    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [
            child for child in ast.walk(node)
            if (isinstance(child, ast.Call) and
                isinstance(child.func, ast.Attribute) and
                child.func.attr == "open" and
                isinstance(child.func.value, ast.Name) and
                child.func.value.id == "os" and
                len(child.args) >= 2)
        ]
        if calls:
            found.append((node.lineno, node.name, ast.get_source_segment(
                source, node) or ""))
    return found


def test_every_os_open_selects_binary_mode(tmp):
    """os.open defaults to TEXT mode on Windows, which corrupts every payload.

    Without O_BINARY the CRT rewrites \\n to \\r\\n on the way out and strips
    \\r on the way back in, and stops reading at a 0x1a byte. Everything this
    package writes through a raw descriptor is bytes -- staged envelopes with
    a recorded length and digest, a 32-byte random staging key, hard-linked
    payloads -- so a translated round trip changes the file's size, breaks its
    digest, and truncates its reads. A POSIX runner cannot notice: O_BINARY
    does not exist there, and getattr(os, 'O_BINARY', 0) is 0.

    This reads source text rather than calling anything, so it holds on every
    platform.
    """
    package = Path(_util.SCRIPTS) / "discord_mb_lib"
    sources = sorted(package.glob("*.py")) + [Path(MB)]
    missing = []
    checked = 0
    for source in sources:
        for lineno, name, body in _functions_calling_os_open(source):
            checked += 1
            if "O_BINARY" not in body:
                missing.append(f"{source.name}:{lineno}: {name}")
    assert checked >= 15, f"only found {checked} such functions; parser drifted"
    assert not missing, "os.open without O_BINARY in:\n" + "\n".join(missing)


def main():
    return _util.runner(_util.collect(globals()), "discord_package_contract_")


if __name__ == "__main__":
    raise SystemExit(main())
