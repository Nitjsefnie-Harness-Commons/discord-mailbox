#!/usr/bin/env python3
"""Discord-backed mailbox for selected-harness agent communication.

The implementation lives in :mod:`discord_mb_lib`; this module remains the
canonical executable and compatibility import surface.
"""

import builtins as _compat_builtins

_facade_reload_snapshot = (
    _compat_builtins.dict(_compat_builtins.globals())
    if _compat_builtins.globals().get('_facade_build_complete') else None)
_facade_reload_active = _facade_reload_snapshot is not None
_facade_mutation_versions = _compat_builtins.globals().get(
    '_facade_mutation_versions')
if _facade_mutation_versions is None:
    _facade_mutation_versions = {}
_facade_mutation_depths = _compat_builtins.globals().get(
    '_facade_mutation_depths')
if _facade_mutation_depths is None:
    _facade_mutation_depths = {}
_facade_sync_depth = 0
_facade_reload_mutation_versions = None
_active_reload_lock = _compat_builtins.globals().get('_facade_mutation_lock')
_reload_lock_held = False
_FACADE_MISSING = _compat_builtins.object()


def _acquire_reload_lock():
    global _reload_lock_held
    if not _reload_lock_held and _active_reload_lock is not None:
        _active_reload_lock.acquire()
        _reload_lock_held = True


def _release_reload_lock():
    global _reload_lock_held
    if _reload_lock_held:
        _reload_lock_held = False
        _active_reload_lock.release()


def _restore_facade_snapshot():
    """Roll back a reload while retaining concurrent public writes."""
    snapshot = _facade_reload_snapshot
    if snapshot is None:
        return
    _acquire_reload_lock()
    rollback_lock = _active_reload_lock
    facade_globals = _compat_builtins.globals()
    public_names = _compat_builtins.set(snapshot.get('_facade_exports', ()))
    changed = {}
    deleted = _compat_builtins.set()
    candidate_names = public_names | {
        name for name in _compat_builtins.set(snapshot) |
        _compat_builtins.set(facade_globals)
        if not name.startswith('_')}
    for name in candidate_names:
        old_value = snapshot.get(name, _FACADE_MISSING)
        new_value = facade_globals.get(name, _FACADE_MISSING)
        if new_value is old_value:
            continue
        if new_value is _FACADE_MISSING:
            deleted.add(name)
        else:
            changed[name] = new_value
    facade_globals.update(snapshot)
    facade_globals.update(changed)
    modules = snapshot.get('_implementation_modules', ())
    implementation_values = snapshot.get('_implementation_values', {})
    propagation = snapshot.get('_compatibility_propagated', {})
    deletion_ledger = snapshot.get('_compatibility_deleted', {})
    builtin_names = _compat_builtins.set(
        _compat_builtins.vars(_compat_builtins))
    for name, value in changed.items():
        for module in modules:
            if (name not in module.__dict__ and name not in builtin_names
                    and (module, name) not in implementation_values
                    and name not in propagation.get(module, ())
                    and name not in deletion_ledger.get(module, ())):
                continue
            module.__dict__[name] = value
            propagation.setdefault(module, _compat_builtins.set()).add(name)
            deletion_ledger.setdefault(
                module, _compat_builtins.set()).discard(name)
    for name in deleted:
        facade_globals.pop(name, None)
        for module in modules:
            if (name not in module.__dict__
                    and (module, name) not in implementation_values
                    and name not in propagation.get(module, ())):
                continue
            module.__dict__.pop(name, None)
            propagation.setdefault(
                module, _compat_builtins.set()).discard(name)
            deletion_ledger.setdefault(module, _compat_builtins.set()).add(name)
    for name in _compat_builtins.tuple(facade_globals):
        if (name not in snapshot and name not in changed
                and name not in candidate_names):
            facade_globals.pop(name, None)
    _compat_builtins.globals()['_reload_lock_held'] = False
    _compat_builtins.globals()['_facade_reload_active'] = False
    _compat_builtins.globals()['_facade_mutation_versions'].clear()
    _compat_builtins.globals()['_facade_mutation_depths'].clear()
    if rollback_lock is not None:
        rollback_lock.release()

import functools as _compat_functools
import copy as _compat_copy
import importlib as _compat_importlib
import importlib.util as _compat_importlib_util
import inspect as _compat_inspect
import linecache as _compat_linecache
import os
import sys
import contextvars as _compat_contextvars
import threading as _compat_threading
import types as _compat_types
import uuid

_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)

# ``importlib.reload`` retains the facade's dictionary.  Keep the preceding
# propagation ledger long enough to undo facade-only overrides before the
# implementation modules become the baseline for this execution.
_previous_implementation_globals = _compat_builtins.globals().get(
    '_implementation_globals')
_previous_compatibility_propagated = _compat_builtins.globals().get(
    '_compatibility_propagated')
_previous_compatibility_deleted = _compat_builtins.globals().get(
    '_compatibility_deleted')
_previous_compatibility_sync_target = _compat_builtins.globals().get(
    '_compatibility_sync_target')
_previous_compatibility_class_globals = _compat_builtins.globals().get(
    '_compatibility_class_globals')
_previous_compatibility_class_specs = _compat_builtins.globals().get(
    '_compatibility_class_specs')
_facade_mutation_lock = _compat_builtins.globals().get(
    '_facade_mutation_lock')
if _facade_mutation_lock is None:
    _facade_mutation_lock = _compat_threading.RLock()

# A normal import reuses an already-imported local package, preserving type and
# import-lock semantics. Path imports and mismatched deployments use a private
# transaction that never deletes or replaces canonical sys.modules entries.
_canonical_package_name = 'discord_mb_lib'
_package_dir = os.path.join(_script_dir, 'discord_mb_lib')
_registered_facade = (
    __name__ == 'discord_mb'
    and _compat_builtins.getattr(
        sys.modules.get(__name__), '__dict__', None)
    is _compat_builtins.globals())
_facade_module = sys.modules.get(__name__) if _registered_facade else None
_canonical_package = sys.modules.get(_canonical_package_name)
_canonical_file = _compat_builtins.getattr(
    _canonical_package, '__file__', None)
_canonical_is_local = (
    _canonical_package is not None
    and _canonical_file is not None
    and os.path.realpath(os.path.dirname(_canonical_file)) ==
    os.path.realpath(_package_dir)
)
_canonical_owner = _compat_builtins.getattr(
    _canonical_package, '_discord_mb_facade_owner', None)
_canonical_is_bound = _canonical_owner is not None
_canonical_owned_by_facade = (
    _facade_module is not None and _canonical_owner is _facade_module)
_private_package = not (
    _registered_facade
    and (_canonical_package is None
         or (_canonical_is_local
             and (not _canonical_is_bound or _canonical_owned_by_facade))))
_reuse_instrumented_canonical = (
    not _private_package and _canonical_owned_by_facade)
_package_name = (_canonical_package_name if not _private_package else
                 f'_discord_mb_lib_{uuid.uuid4().hex}')

try:
    if _private_package:
        _package_spec = _compat_importlib_util.spec_from_file_location(
            _package_name,
            os.path.join(_package_dir, '__init__.py'),
            submodule_search_locations=[_package_dir],
        )
        if _package_spec is None or _package_spec.loader is None:
            raise _compat_builtins.ImportError(
                f'cannot load Discord mailbox package from {_package_dir}')
        _package_module = _compat_importlib_util.module_from_spec(
            _package_spec)
        sys.modules[_package_name] = _package_module
        _package_spec.loader.exec_module(_package_module)
    else:
        _package_module = _compat_importlib.import_module(_package_name)
    _core_module = _compat_importlib.import_module(f'{_package_name}.core')
    _storage_module = _compat_importlib.import_module(
        f'{_package_name}.storage')
    _connector_module = _compat_importlib.import_module(
        f'{_package_name}.connector')
    _cli_module = _compat_importlib.import_module(f'{_package_name}.cli')
except _compat_builtins.BaseException:
    if _private_package:
        for _loaded_name in _compat_builtins.tuple(sys.modules):
            if (_loaded_name == _package_name
                    or _loaded_name.startswith(f'{_package_name}.')):
                sys.modules.pop(_loaded_name, None)
    _restore_facade_snapshot()
    raise

_implementation_modules = (
    _core_module, _storage_module, _connector_module, _cli_module)
_prebuild_implementation_values = {}
_build_compatibility_class_globals = {}
_build_compatibility_class_specs = {}
if _compat_builtins.isinstance(
        _previous_compatibility_propagated, _compat_builtins.dict):
    for _module in _implementation_modules:
        _previous_touched_names = _compat_builtins.set(
            _previous_compatibility_propagated.get(_module, ()))
        if _compat_builtins.isinstance(
                _previous_compatibility_deleted, _compat_builtins.dict):
            _previous_touched_names.update(
                _previous_compatibility_deleted.get(_module, ()))
        for _name in _previous_touched_names:
            _prebuild_implementation_values[(_module, _name)] = (
                _name in _module.__dict__, _module.__dict__.get(_name))


def _cleanup_private_package():
    if not _private_package:
        return
    loaded_names = _compat_builtins.tuple(
        loaded_name for loaded_name in sys.modules
        if (loaded_name == _package_name
            or loaded_name.startswith(f'{_package_name}.')))
    package_snapshot = {
        loaded_name: sys.modules[loaded_name] for loaded_name in loaded_names}
    try:
        for loaded_name in loaded_names:
            sys.modules.pop(loaded_name, None)
    except _compat_builtins.BaseException:
        sys.modules.update(package_snapshot)
        raise


def _abort_facade_build(*, clean_private=True):
    """Roll back state touched by an unsuccessful facade build/reload."""
    for (module, name), (existed, value) in (
            _prebuild_implementation_values.items()):
        if existed:
            _compat_builtins.setattr(module, name, value)
        else:
            module.__dict__.pop(name, None)
    if clean_private:
        _cleanup_private_package()
    _restore_facade_snapshot()


_acquire_reload_lock()
_facade_reload_mutation_versions = _compat_builtins.dict(
    _facade_mutation_versions)
if (_compat_builtins.isinstance(
        _previous_implementation_globals, _compat_builtins.dict)
        and _compat_builtins.isinstance(
            _previous_compatibility_propagated, _compat_builtins.dict)):
    _restoration_snapshot = {}
    try:
        for _module in _implementation_modules:
            _previous_globals = _previous_implementation_globals.get(_module)
            _previous_names = _compat_builtins.set(
                _previous_compatibility_propagated.get(_module, ()))
            if _compat_builtins.isinstance(
                    _previous_compatibility_deleted, _compat_builtins.dict):
                _previous_names.update(
                    _previous_compatibility_deleted.get(_module, ()))
            if (not _compat_builtins.isinstance(
                    _previous_globals, _compat_builtins.dict)
                    or not _previous_names):
                continue
            for _name in _compat_builtins.tuple(_previous_names):
                _restoration_snapshot[(_module, _name)] = (
                    _name in _module.__dict__, _module.__dict__.get(_name))
                if _name in _previous_globals:
                    _compat_builtins.setattr(
                        _module, _name, _previous_globals[_name])
                else:
                    _module.__dict__.pop(_name, None)
    except _compat_builtins.BaseException:
        for (_module, _name), (_existed, _value) in (
                _restoration_snapshot.items()):
            if _existed:
                _module.__dict__[_name] = _value
            else:
                _module.__dict__.pop(_name, None)
        _restore_facade_snapshot()
        raise

# Re-executing the former monolith rebuilt its import-time configuration and
# lookup tables. Refresh that state without replacing the package's public
# functions and classes (whose identities are persistent).
_reload_state_names = _core_module.__RELOAD_STATE_NAMES
for _module in _implementation_modules:
    for _name in _reload_state_names:
        if _name in _module.__dict__:
            _prebuild_implementation_values.setdefault(
                (_module, _name),
                (True, _module.__dict__[_name]),
            )
try:
    _refreshed_dependencies = _core_module.__refresh_dependencies()
    _refreshed_state = _core_module.__refresh_reload_state(
        _refreshed_dependencies)
    for _module in _implementation_modules:
        for _name, _value in _refreshed_state.items():
            if _name in _module.__dict__:
                _module.__dict__[_name] = _value
except _compat_builtins.BaseException:
    _abort_facade_build()
    raise


def _legacy_code(code, module_name, inherited_delta=None,
                 qualname_override=None):
    """Rebuild a code object, optionally under a different qualified name."""
    del module_name, inherited_delta          # legacy projection retired
    if qualname_override is None:
        return code
    constants = _compat_builtins.tuple(
        _legacy_code(
            value, None, None,
            qualname_override + value.co_qualname[len(code.co_qualname):])
        if (_compat_builtins.isinstance(value, _compat_types.CodeType)
            and value.co_qualname.startswith(code.co_qualname + '.'))
        else value
        for value in code.co_consts)
    return code.replace(
        co_consts=constants, co_qualname=qualname_override,
        co_name=qualname_override.rsplit('.', 1)[-1])


def _clone_declaration_function(function):
    """Create the function object that a repeated class statement would."""
    cloned = _compat_types.FunctionType(
        _legacy_code(function.__code__, function.__module__),
        function.__globals__, function.__name__,
        _compat_copy.deepcopy(function.__defaults__), function.__closure__)
    cloned.__kwdefaults__ = _compat_copy.deepcopy(function.__kwdefaults__)
    cloned.__annotations__ = _compat_builtins.dict(function.__annotations__)
    cloned.__doc__ = function.__doc__
    cloned.__module__ = function.__module__
    cloned.__qualname__ = function.__qualname__
    for name, value in function.__dict__.items():
        if name == '__wrapped__' and _compat_inspect.isfunction(value):
            value = _clone_declaration_function(value)
        cloned.__dict__[name] = value
    return cloned


def _clone_declaration_value(value):
    """Freeze or instantiate one class-namespace declaration value."""
    if _compat_inspect.isfunction(value):
        return _clone_declaration_function(value)
    if _compat_builtins.isinstance(value, _compat_builtins.staticmethod):
        return _compat_builtins.staticmethod(
            _clone_declaration_function(value.__func__))
    if _compat_builtins.isinstance(value, _compat_builtins.classmethod):
        return _compat_builtins.classmethod(
            _clone_declaration_function(value.__func__))
    if _compat_builtins.isinstance(value, _compat_builtins.property):
        return _compat_builtins.property(
            _clone_declaration_function(value.fget)
            if value.fget is not None else None,
            _clone_declaration_function(value.fset)
            if value.fset is not None else None,
            _clone_declaration_function(value.fdel)
            if value.fdel is not None else None,
            value.__doc__)
    if _compat_builtins.isinstance(
            value, (_compat_builtins.dict, _compat_builtins.list,
                    _compat_builtins.set)):
        return _compat_copy.deepcopy(value)
    return value


# The monolith's class statements created a fresh generation on every reload.
# Recreate the saved declarations after the fallible import-time refresh, then
# atomically rebind implementation globals. Retained old classes stay untouched.
if _compat_builtins.isinstance(
        _previous_compatibility_class_globals, _compat_builtins.dict):
    try:
        for _old_class, _baseline in (
                _previous_compatibility_class_globals.items()):
            if not _compat_inspect.isclass(_old_class):
                continue
            _class_spec = (
                _previous_compatibility_class_specs.get(_old_class)
                if _compat_builtins.isinstance(
                    _previous_compatibility_class_specs,
                    _compat_builtins.dict)
                else None)
            if _class_spec is None:
                _class_spec = (
                    _old_class.__name__, _old_class.__qualname__, __name__,
                    _old_class.__bases__)
            _new_name, _new_qualname, _new_module, _new_bases = _class_spec
            _new_namespace = {}
            _slot_names = _compat_builtins.set(
                _baseline.get('__slots__', ()))
            if _compat_builtins.isinstance(
                    _baseline.get('__slots__'), _compat_builtins.str):
                _slot_names = {_baseline['__slots__']}
            for _namespace_name, _namespace_value in _baseline.items():
                if (_namespace_name in ('__dict__', '__weakref__')
                        or _namespace_name in _slot_names):
                    continue
                _new_namespace[_namespace_name] = (
                    _clone_declaration_value(_namespace_value))
            _new_namespace['__module__'] = _new_module
            _new_namespace['__qualname__'] = _new_qualname
            if _new_qualname == '_ConnectorOwnership':
                _new_namespace['_IDENTITY_LOCK_ROOT'] = (
                    _refreshed_dependencies['Path'](
                        os.environ.get(
                            _core_module._TEST_LOCK_ROOT_ENV,
                            _refreshed_state[
                                '_DEFAULT_CONNECTOR_LOCK_ROOT'])))
            _new_class = _compat_builtins.type(
                _new_name, _new_bases, _new_namespace)
            for _module in _implementation_modules:
                for _name, _value in _compat_builtins.tuple(
                        _module.__dict__.items()):
                    if _value is not _old_class:
                        continue
                    _prebuild_implementation_values.setdefault(
                        (_module, _name), (True, _old_class))
                    _compat_builtins.setattr(_module, _name, _new_class)
            _build_compatibility_class_globals[_new_class] = _baseline
            _build_compatibility_class_specs[_new_class] = _class_spec
    except _compat_builtins.BaseException:
        _abort_facade_build()
        raise

_build_implementation_values = {}
_build_implementation_globals = {
    module: _compat_builtins.dict(module.__dict__)
    for module in _implementation_modules}
_build_compatibility_propagated = {
    module: _compat_builtins.set() for module in _implementation_modules}
_build_compatibility_deleted = {
    module: _compat_builtins.set() for module in _implementation_modules}
_build_compatibility_wrappers = {}
_facade_exports = {}

for _module in _implementation_modules:
    for _name in _module.__all__:
        _build_implementation_values[(_module, _name)] = _compat_builtins.getattr(
            _module, _name)
        _facade_exports[_name] = _compat_builtins.getattr(_module, _name)
_release_reload_lock()


def _sync_compatibility_overrides_unlocked(
        _modules=_implementation_modules,
        _values=_build_implementation_values,
        _originals=_build_implementation_globals,
        _propagation=_build_compatibility_propagated,
        _deleted=_build_compatibility_deleted,
        _wrappers=_build_compatibility_wrappers,
        _facade=None):
    """Push facade monkeypatches into the implementation modules.

    `discord_mb.py` was importable long before it became a package.  Tests and
    local diagnostic callers patch its functions/classes to simulate failures;
    preserving that seam avoids a package split that looks correct while its
    failure-path coverage silently stops exercising production code.
    """
    facade = _compat_builtins.globals() if _facade is None else _facade
    wrappers = _wrappers
    versions = facade.get('_facade_mutation_versions', {})
    module_snapshots = {}
    superseded = _compat_builtins.set()
    active_name = None
    active_version = None

    def remember(module, name):
        key = (module, name)
        if key not in module_snapshots:
            module_snapshots[key] = (
                name in module.__dict__, module.__dict__.get(name),
                name in _propagation[module], name in _deleted[module])

    def reconcile_nested(name):
        exists = name in facade
        facade_value = facade.get(name)
        builtin_name = name in _compat_builtins.vars(_compat_builtins)
        for target in _modules:
            relevant = (
                name in target.__dict__ or builtin_name
                or (target, name) in _values
                or name in _propagation[target]
                or name in _deleted[target])
            if not relevant:
                continue
            if exists:
                value = facade_value
                if wrappers.get(name) is value:
                    value = _values.get(
                        (target, name), target.__dict__.get(name))
                target.__dict__[name] = value
                _propagation[target].add(name)
                _deleted[target].discard(name)
            else:
                target.__dict__.pop(name, None)
                _propagation[target].discard(name)
                _deleted[target].add(name)
        superseded.add(name)

    try:
        for module in _modules:
            propagated = _propagation[module]
            original_globals = _originals[module]
            for name in _compat_builtins.tuple(propagated):
                if name in superseded:
                    continue
                if name in facade:
                    continue
                remember(module, name)
                active_name = name
                active_version = versions.get(name, 0)
                if name in original_globals:
                    _compat_builtins.setattr(
                        module, name, original_globals[name])
                else:
                    module.__dict__.pop(name, None)
                if versions.get(name, 0) != active_version:
                    reconcile_nested(name)
                    continue
                propagated.discard(name)
            names = _compat_builtins.set(module.__dict__)
            names.update(_deleted[module])
            names.update(
                _compat_builtins.set(facade) &
                _compat_builtins.set(
                    _compat_builtins.vars(_compat_builtins)))
            names.update(
                name for name in facade
                if name in _values or _compat_builtins.any(
                    candidate_name == name
                    for candidate_module, candidate_name in _values
                    if candidate_module is module))
            for name in names:
                if (name in superseded
                        or (name.startswith('__') and name != '__version__')):
                    continue
                if name not in facade:
                    if (module, name) not in _values:
                        continue
                    if (name not in module.__dict__
                            and name in _deleted[module]):
                        continue
                    remember(module, name)
                    active_name = name
                    active_version = versions.get(name, 0)
                    module.__dict__.pop(name, None)
                    propagated.discard(name)
                    _deleted[module].add(name)
                    if versions.get(name, 0) != active_version:
                        reconcile_nested(name)
                    continue
                _deleted[module].discard(name)
                value = facade[name]
                if wrappers.get(name) is value:
                    value = _values.get((module, name),
                                        module.__dict__.get(name))
                baseline = original_globals.get(name, _COMPATIBILITY_MISSING)
                if value is baseline:
                    if (module.__dict__.get(name, _COMPATIBILITY_MISSING)
                            is not value):
                        remember(module, name)
                        active_name = name
                        active_version = versions.get(name, 0)
                        _compat_builtins.setattr(module, name, value)
                        if versions.get(name, 0) != active_version:
                            reconcile_nested(name)
                            continue
                    propagated.discard(name)
                else:
                    remember(module, name)
                    active_name = name
                    active_version = versions.get(name, 0)
                    _compat_builtins.setattr(module, name, value)
                    if versions.get(name, 0) != active_version:
                        reconcile_nested(name)
                        continue
                    propagated.add(name)
    except _compat_builtins.BaseException:
        nested_changed = (active_name is not None
                          and versions.get(active_name, 0) != active_version)
        for (module, name), (existed, value, was_propagated,
                             was_deleted) in module_snapshots.items():
            if existed:
                module.__dict__[name] = value
            else:
                module.__dict__.pop(name, None)
            if was_propagated:
                _propagation[module].add(name)
            else:
                _propagation[module].discard(name)
            if was_deleted:
                _deleted[module].add(name)
            else:
                _deleted[module].discard(name)
        if nested_changed:
            reconcile_nested(active_name)
        raise


def _sync_compatibility_overrides(*args, **kwargs):
    global _facade_sync_depth
    with _facade_mutation_lock:
        _facade_sync_depth += 1
        try:
            return _sync_compatibility_overrides_unlocked(*args, **kwargs)
        finally:
            _facade_sync_depth -= 1
            if _facade_sync_depth == 0 and not _facade_reload_active:
                for name in _compat_builtins.tuple(
                        _facade_mutation_versions):
                    if name not in _facade_mutation_depths:
                        _facade_mutation_versions.pop(name, None)


class _CompatibilitySyncTarget:
    def __init__(self, function):
        self.function = function

    def __call__(self):
        return self.function()


_build_compatibility_sync_target = (
    _previous_compatibility_sync_target
    if _previous_compatibility_sync_target is not None
    else _CompatibilitySyncTarget(_sync_compatibility_overrides))


_COMPATIBILITY_MISSING = _compat_builtins.object()
_generated_wrapper_count = 0
_generated_wrapper_namespace = uuid.uuid4().hex
_compatibility_depth = _compat_contextvars.ContextVar(
    f'discord_mb_compatibility_depth_{_generated_wrapper_namespace}', default=0)


def _forwarding_signature(function):
    """Return exact definition/call text plus globals for default values."""
    signature = _compat_inspect.signature(function)
    parameters = _compat_builtins.list(signature.parameters.values())
    definitions = []
    positional_only = [
        item for item in parameters
        if item.kind is _compat_inspect.Parameter.POSITIONAL_ONLY]
    positional_or_keyword = [
        item for item in parameters
        if item.kind is _compat_inspect.Parameter.POSITIONAL_OR_KEYWORD]
    var_positional = _compat_builtins.next((
        item for item in parameters
        if item.kind is _compat_inspect.Parameter.VAR_POSITIONAL), None)
    keyword_only = [
        item for item in parameters
        if item.kind is _compat_inspect.Parameter.KEYWORD_ONLY]
    var_keyword = _compat_builtins.next((
        item for item in parameters
        if item.kind is _compat_inspect.Parameter.VAR_KEYWORD), None)
    defaults = {}

    def definition(item):
        text = item.name
        if item.default is not _compat_inspect.Parameter.empty:
            default_name = (
                f'_compat_default_{_compat_builtins.len(defaults)}')
            defaults[default_name] = item.default
            text += f'={default_name}'
        return text

    definitions.extend(definition(item) for item in positional_only)
    if positional_only:
        definitions.append('/')
    definitions.extend(definition(item) for item in positional_or_keyword)
    if var_positional is not None:
        definitions.append(f'*{var_positional.name}')
    elif keyword_only:
        definitions.append('*')
    definitions.extend(definition(item) for item in keyword_only)
    if var_keyword is not None:
        definitions.append(f'**{var_keyword.name}')

    calls = [item.name for item in positional_only + positional_or_keyword]
    if var_positional is not None:
        calls.append(f'*{var_positional.name}')
    calls.extend(f'{item.name}={item.name}' for item in keyword_only)
    if var_keyword is not None:
        calls.append(f'**{var_keyword.name}')
    return ', '.join(definitions), ', '.join(calls), defaults


class _CompatibilitySyncIterator:
    """Delegate an iterator while synchronizing before every resumption."""

    def __init__(self, iterator, sync):
        self._iterator = iterator
        self._sync = sync

    def __iter__(self):
        return self

    def __next__(self):
        self._sync()
        return _compat_builtins.next(self._iterator)

    def send(self, value):
        self._sync()
        return self._iterator.send(value)

    def throw(self, *args):
        self._sync()
        return self._iterator.throw(*args)

    def close(self):
        self._sync()
        return self._iterator.close()


@_compat_types.coroutine
def _await_with_compatibility_sync(awaitable, sync):
    return (yield from _CompatibilitySyncIterator(awaitable.__await__(), sync))


def _iterate_with_compatibility_sync(iterator, sync):
    yield from _CompatibilitySyncIterator(_compat_builtins.iter(iterator), sync)


_ASYNC_GENERATOR_INITIAL = _compat_builtins.object()


def _exact_compatibility_wrapper(function, *, method=False):
    """Build a synchronization wrapper without changing its raw signature."""
    global _generated_wrapper_count
    _generated_wrapper_count += 1
    index = _generated_wrapper_count
    definition, call, defaults = _forwarding_signature(function)
    wrapper_name = (
        f'_compat_generated_wrapper_{_generated_wrapper_namespace}_{index}')
    namespace = _compat_builtins.dict(defaults)
    namespace.update({
        '_compat_implementation': function,
        '_compat_sync': _build_compatibility_sync_target,
        '_compat_depth_target': _compatibility_depth,
        '_await_with_compatibility_sync': _await_with_compatibility_sync,
        '_iterate_with_compatibility_sync': _iterate_with_compatibility_sync,
        '_ASYNC_GENERATOR_INITIAL': _ASYNC_GENERATOR_INITIAL,
        '__name__': __name__,
    })

    invocation = f'_compat_implementation({call})'
    if _compat_inspect.isasyncgenfunction(function):
        prefix = 'async '
        action = (
            f'    _compat_iterator = {invocation}.__aiter__()\n'
            '    _compat_send = _ASYNC_GENERATOR_INITIAL\n'
            '    _compat_throw = None\n'
            '    while True:\n'
            '        try:\n'
            '            if _compat_throw is not None:\n'
            '                _compat_awaitable = '
            '_compat_iterator.athrow(_compat_throw)\n'
            '                _compat_throw = None\n'
            '            elif _compat_send is _ASYNC_GENERATOR_INITIAL:\n'
            '                _compat_awaitable = _compat_iterator.__anext__()\n'
            '            else:\n'
            '                _compat_awaitable = '
            '_compat_iterator.asend(_compat_send)\n'
            '            _compat_item = await _await_with_compatibility_sync(\n'
            '                _compat_awaitable, _compat_sync)\n'
            '        except StopAsyncIteration:\n'
            '            return\n'
            '        try:\n'
            '            _compat_send = yield _compat_item\n'
            '        except BaseException as _compat_error:\n'
            '            _compat_throw = _compat_error\n'
            '            _compat_send = _ASYNC_GENERATOR_INITIAL')
    elif _compat_inspect.iscoroutinefunction(function):
        prefix = 'async '
        action = (
            '    return await _await_with_compatibility_sync(\n'
            f'        {invocation}, _compat_sync)')
    elif _compat_inspect.isgeneratorfunction(function):
        prefix = ''
        action = (
            '    yield from _iterate_with_compatibility_sync(\n'
            f'        {invocation}, _compat_sync)')
    else:
        prefix = ''
        action = f'    return {invocation}'

    if method and not (
            _compat_inspect.isgeneratorfunction(function)
            or _compat_inspect.isasyncgenfunction(function)):
        action_lines = action.splitlines()
        action = '\n'.join(f'    {line}' for line in action_lines)
        body = (
            '    _compat_depth = _compat_depth_target.get()\n'
            '    _compat_token = _compat_depth_target.set(_compat_depth + 1)\n'
            '    try:\n'
            '        if _compat_depth == 0:\n'
            '            _compat_sync()\n'
            f'{action}\n'
            '    finally:\n'
            '        _compat_depth_target.reset(_compat_token)')
    else:
        body = f'    _compat_sync()\n{action}'

    source = f'{prefix}def {wrapper_name}({definition}):\n{body}\n'
    _compat_builtins.exec(
        _compat_builtins.compile(source, __file__, 'exec'), namespace)
    wrapper = namespace[wrapper_name]
    _compat_functools.update_wrapper(wrapper, function)
    wrapper.__module__ = __name__
    return wrapper


def _compatibility_call(name, function):
    # Build once as a validation transaction: signature construction can fail
    # under hostile/introspective environments and must do so before exports
    # are committed. The exported callable itself uses the facade globals
    # directly, exactly like the former monolith, so it adds no call frame.
    _exact_compatibility_wrapper(function)
    rebound = _compat_types.FunctionType(
        _legacy_code(
            function.__code__, function.__module__,
            qualname_override=(
                'connector_main'
                if name == 'connector_main'
                and function.__name__ == '_run_connector'
                else None)),
        _compat_builtins.globals(), name,
        function.__defaults__, function.__closure__)
    rebound.__kwdefaults__ = function.__kwdefaults__
    rebound.__annotations__ = _compat_builtins.dict(function.__annotations__)
    rebound.__doc__ = function.__doc__
    rebound.__module__ = __name__
    rebound.__qualname__ = name
    rebound.__dict__.update(
        (key, value) for key, value in function.__dict__.items()
        if key != '__wrapped__')
    return rebound


try:
    # The monolith's extension directory table contains lambdas.  Rebind those
    # nested callables too: otherwise they retain ``core`` globals and miss
    # facade-level TOKEN_DIR monkeypatches even though ``extension_dir`` itself
    # correctly uses the facade namespace.
    _extension_dirs = _facade_exports['_EXTENSION_FLAVOR_DIRS']
    _extension_lines = {'claude': 2073, 'kimi': 2074, 'codex': 2075}
    _rebound_extension_dirs = {}
    for _extension_flavor, _extension_resolver in _extension_dirs.items():
        if (_extension_flavor in _extension_lines
                and _compat_inspect.isfunction(_extension_resolver)):
            _extension_code = _legacy_code(
                _extension_resolver.__code__,
                _extension_resolver.__module__,
                _extension_lines[_extension_flavor]
                - _extension_resolver.__code__.co_firstlineno,
                '<lambda>')
            _extension_resolver = _compat_types.FunctionType(
                _extension_code, _compat_builtins.globals(), '<lambda>',
                _extension_resolver.__defaults__,
                _extension_resolver.__closure__)
            _extension_resolver.__module__ = __name__
            _extension_resolver.__qualname__ = '<lambda>'
        _rebound_extension_dirs[_extension_flavor] = _extension_resolver
    for _module in _implementation_modules:
        if '_EXTENSION_FLAVOR_DIRS' not in _module.__dict__:
            continue
        _prebuild_implementation_values.setdefault(
            (_module, '_EXTENSION_FLAVOR_DIRS'),
            (True, _module.__dict__['_EXTENSION_FLAVOR_DIRS']))
        _module.__dict__['_EXTENSION_FLAVOR_DIRS'] = _rebound_extension_dirs
        _build_implementation_values[
            (_module, '_EXTENSION_FLAVOR_DIRS')] = _rebound_extension_dirs
        _build_implementation_globals[
            _module]['_EXTENSION_FLAVOR_DIRS'] = _rebound_extension_dirs
    _facade_exports['_EXTENSION_FLAVOR_DIRS'] = _rebound_extension_dirs
except _compat_builtins.BaseException:
    _abort_facade_build()
    raise


try:
    for _module in _implementation_modules:
        for _name in _module.__all__:
            _value = _build_implementation_values[(_module, _name)]
            if (_compat_inspect.isfunction(_value)
                    and _value.__module__ == _module.__name__):
                if (_module is _connector_module
                        and _name == 'connector_main'):
                    _value = _module._run_connector
                _wrapper = _compatibility_call(_name, _value)
                _build_compatibility_wrappers[_name] = _wrapper
                _facade_exports[_name] = _wrapper
except _compat_builtins.BaseException:
    _abort_facade_build()
    raise


def _compatibility_method(function):
    """Bind a method directly to the monolith-compatible facade globals."""
    rebound = _compat_types.FunctionType(
        _legacy_code(function.__code__, function.__module__),
        _compat_builtins.globals(), function.__name__,
        function.__defaults__, function.__closure__)
    rebound.__kwdefaults__ = function.__kwdefaults__
    rebound.__annotations__ = _compat_builtins.dict(function.__annotations__)
    rebound.__doc__ = function.__doc__
    rebound.__module__ = __name__
    rebound.__qualname__ = function.__qualname__
    rebound.__dict__.update(
        (key, value) for key, value in function.__dict__.items()
        if key != '__wrapped__')
    return rebound


def _prepare_compatibility_class(implementation):
    """Build descriptor replacements without mutating the implementation."""
    replacements = {}
    for name, descriptor in _compat_builtins.tuple(
            implementation.__dict__.items()):
        if _compat_builtins.isinstance(
                descriptor, _compat_builtins.staticmethod):
            replacements[name] = _compat_builtins.staticmethod(
                _compatibility_method(descriptor.__func__))
        elif _compat_builtins.isinstance(
                descriptor, _compat_builtins.classmethod):
            replacements[name] = _compat_builtins.classmethod(
                _compatibility_method(descriptor.__func__))
        elif _compat_inspect.isfunction(descriptor):
            replacements[name] = _compatibility_method(descriptor)
        elif _compat_builtins.isinstance(
                descriptor, _compat_builtins.property):
            replacements[name] = _compat_builtins.property(
                _compatibility_method(descriptor.fget)
                if descriptor.fget is not None else None,
                _compatibility_method(descriptor.fset)
                if descriptor.fset is not None else None,
                _compatibility_method(descriptor.fdel)
                if descriptor.fdel is not None else None,
                descriptor.__doc__,
            )
    return replacements


_compatibility_classes = {}
_compatibility_class_plans = []
try:
    for _module in _implementation_modules:
        for _name in _module.__all__:
            _value = _build_implementation_values[(_module, _name)]
            if (_compat_inspect.isclass(_value)
                    and _value.__module__ == _module.__name__):
                instrumentable = not _compat_builtins.issubclass(
                    _value, _compat_builtins.BaseException)
                replacements = (
                    _prepare_compatibility_class(_value)
                    if (instrumentable
                        and _name != 'ConnectorApp'
                        and not _reuse_instrumented_canonical)
                    else {})
                _compatibility_class_plans.append(
                    (_name, _value, replacements, instrumentable))
except _compat_builtins.BaseException:
    _abort_facade_build()
    raise

_reserved_canonical_owner = False
if not _private_package and not _reuse_instrumented_canonical:
    try:
        _compat_builtins.setattr(
            _package_module, '_discord_mb_facade_owner', _facade_module)
        _reserved_canonical_owner = True
    except _compat_builtins.BaseException:
        if (_compat_builtins.getattr(
                _package_module, '_discord_mb_facade_owner', None)
                is _facade_module):
            try:
                _compat_builtins.delattr(
                    _package_module, '_discord_mb_facade_owner')
            except _compat_builtins.BaseException:
                pass
        _abort_facade_build()
        raise

_applied_descriptors = []
try:
    for (_name, _value, replacements,
         _instrumentable) in _compatibility_class_plans:
        for _descriptor_name, _replacement in replacements.items():
            _original = _value.__dict__[_descriptor_name]
            _applied_descriptors.append(
                (_value, _descriptor_name, _original))
            _compat_builtins.setattr(
                _value, _descriptor_name, _replacement)
except _compat_builtins.BaseException:
    for _value, _descriptor_name, _original in _compat_builtins.reversed(
            _applied_descriptors):
        _compat_builtins.setattr(_value, _descriptor_name, _original)
    if _reserved_canonical_owner:
        try:
            _compat_builtins.delattr(
                _package_module, '_discord_mb_facade_owner')
        except _compat_builtins.BaseException:
            pass
    _abort_facade_build()
    raise

_changed_class_modules = []
try:
    for (_name, _value, replacements,
         _instrumentable) in _compatibility_class_plans:
        if _instrumentable:
            _compatibility_classes[_name] = _value
        if _name == 'ConnectorApp':
            _facade_exports[_name] = _value
            continue
        _facade_exports[_name] = _value
except _compat_builtins.BaseException:
    for (_value, _original_module,
         _original_firstlineno) in _compat_builtins.reversed(
            _changed_class_modules):
        _value.__module__ = _original_module
        if _original_firstlineno is _COMPATIBILITY_MISSING:
            _value.__dict__.pop('__firstlineno__', None)
        else:
            _value.__firstlineno__ = _original_firstlineno
    for _value, _descriptor_name, _original in _compat_builtins.reversed(
            _applied_descriptors):
        _compat_builtins.setattr(_value, _descriptor_name, _original)
    if _reserved_canonical_owner:
        try:
            _compat_builtins.delattr(
                _package_module, '_discord_mb_facade_owner')
        except _compat_builtins.BaseException:
            pass
    _abort_facade_build()
    raise

try:
    _cleanup_private_package()
except _compat_builtins.BaseException:
    for (_value, _original_module,
         _original_firstlineno) in _compat_builtins.reversed(
            _changed_class_modules):
        _value.__module__ = _original_module
        if _original_firstlineno is _COMPATIBILITY_MISSING:
            _value.__dict__.pop('__firstlineno__', None)
        else:
            _value.__firstlineno__ = _original_firstlineno
    for _value, _descriptor_name, _original in _compat_builtins.reversed(
            _applied_descriptors):
        _compat_builtins.setattr(_value, _descriptor_name, _original)
    if _reserved_canonical_owner:
        try:
            _compat_builtins.delattr(
                _package_module, '_discord_mb_facade_owner')
        except _compat_builtins.BaseException:
            pass
    _abort_facade_build(clean_private=False)
    raise

for _, _class_value, _, _ in _compatibility_class_plans:
    if _class_value not in _build_compatibility_class_globals:
        _class_namespace = {}
        for _namespace_name, _namespace_value in (
                _class_value.__dict__.items()):
            if _namespace_name in ('__dict__', '__weakref__'):
                continue
            _class_namespace[_namespace_name] = (
                _clone_declaration_value(_namespace_value))
        _build_compatibility_class_globals[_class_value] = _class_namespace
    _build_compatibility_class_specs.setdefault(
        _class_value,
        (_class_value.__name__, _class_value.__qualname__, __name__,
         _class_value.__bases__))

_acquire_reload_lock()
_facade_globals = _compat_builtins.globals()
_facade_removals = _compat_builtins.set()
_facade_declaration_values = {}

# Publishing here is the split module's declaration boundary.  Writes that
# precede it are replaced just as source declarations replace earlier globals;
# ordinary ModuleType writes after it remain visible through the rest of the
# reload, just as writes after a monolith declaration do.
for _name, _value in _facade_exports.items():
    _facade_globals[_name] = _value
    _facade_declaration_values[_name] = _value


def _facade_name_mutated(name):
    return (_facade_mutation_versions.get(name, 0)
            != _facade_reload_mutation_versions.get(name, 0))


if _facade_reload_snapshot is not None:
    for _module in _implementation_modules:
        _old_globals = _previous_implementation_globals.get(_module, {})
        for _name in _previous_compatibility_propagated.get(_module, ()):
            if _name in _facade_exports or _name not in _old_globals:
                continue
            _start_value = _facade_reload_snapshot.get(
                _name, _COMPATIBILITY_MISSING)
            if (_start_value is _COMPATIBILITY_MISSING
                    or _start_value is _old_globals[_name]):
                continue
            if (_facade_globals.get(_name, _COMPATIBILITY_MISSING)
                    is _start_value and not _facade_name_mutated(_name)):
                _facade_removals.add(_name)

_projected_facade = _compat_builtins.dict(_facade_globals)
for _name in _facade_removals:
    _projected_facade.pop(_name, None)
for _name, _value in _facade_exports.items():
    if (_facade_globals.get(_name, _COMPATIBILITY_MISSING)
            is not _facade_declaration_values[_name]):
        continue
    _projected_facade[_name] = _value

if _facade_reload_snapshot is not None:
    _old_deletion_ledger = _facade_reload_snapshot.get(
        '_compatibility_deleted', {})
    for _module in _implementation_modules:
        for _name in _old_deletion_ledger.get(_module, ()):
            if _name not in _projected_facade:
                _build_compatibility_deleted[_module].add(_name)

try:
    _sync_compatibility_overrides(_facade=_projected_facade)
    for _module in _implementation_modules:
        _original_globals = _build_implementation_globals[_module]
        _propagated = _build_compatibility_propagated[_module]
        for _name in _compat_builtins.tuple(_propagated):
            if _name not in _original_globals:
                _module.__dict__.pop(_name, None)
                _propagated.discard(_name)
except _compat_builtins.BaseException:
    for (_value, _original_module,
         _original_firstlineno) in _compat_builtins.reversed(
            _changed_class_modules):
        _value.__module__ = _original_module
        if _original_firstlineno is _COMPATIBILITY_MISSING:
            _value.__dict__.pop('__firstlineno__', None)
        else:
            _value.__firstlineno__ = _original_firstlineno
    for _value, _descriptor_name, _original in _compat_builtins.reversed(
            _applied_descriptors):
        _compat_builtins.setattr(_value, _descriptor_name, _original)
    if _reserved_canonical_owner:
        try:
            _compat_builtins.delattr(
                _package_module, '_discord_mb_facade_owner')
        except _compat_builtins.BaseException:
            pass
    _abort_facade_build(clean_private=False)
    raise

_compat_builtins.object.__setattr__(
    _build_compatibility_sync_target, 'function',
    _sync_compatibility_overrides)

_implementation_values = _build_implementation_values
_implementation_globals = _build_implementation_globals
_compatibility_propagated = _build_compatibility_propagated
_compatibility_deleted = _build_compatibility_deleted
_compatibility_wrappers = _build_compatibility_wrappers
_compatibility_sync_target = _build_compatibility_sync_target
_compatibility_class_globals = _build_compatibility_class_globals
_compatibility_class_specs = _build_compatibility_class_specs


for _name in _facade_removals:
    _facade_globals.pop(_name, None)
for _name, _value in _facade_exports.items():
    if (_facade_globals.get(_name, _COMPATIBILITY_MISSING)
            is not _facade_declaration_values[_name]):
        continue
    _facade_globals[_name] = _value

__doc__ = _core_module.__doc__
__version__ = _core_module.__version__
_facade_build_complete = True
_facade_reload_active = False
_facade_mutation_versions.clear()
_facade_mutation_depths.clear()
for _compat_helper_name in (
        '_baseline', '_class_namespace', '_class_spec', '_class_value',
        '_namespace_name', '_namespace_value', '_new_bases', '_new_class',
        '_new_module', '_new_name', '_new_namespace', '_new_qualname',
        '_old_class', '_slot_names', '_extension_code', '_extension_dirs',
        '_extension_flavor', '_extension_lines', '_extension_resolver',
        '_rebound_extension_dirs', '_facade_declaration_values',
        'instrumentable', 'replacements'):
    _facade_globals.pop(_compat_helper_name, None)
_facade_globals.pop('_compat_helper_name', None)
_release_reload_lock()


if __name__ == '__main__':
    _cli()
