#!/usr/bin/env python3
"""The tag workflow and `__version__` must name the same file.

tag.yml is the only thing that turns a version bump into a release, and it is
invisible when wrong: it watches one path and reads one attribute, and if
either drifts from where `__version__` actually lives the workflow simply never
runs. Nothing goes red, the version looks shipped, and no release is cut
(issue #12). actionlint checks the workflow's syntax; only this checks that it
is pointed at the right file.

Stdlib only, no YAML parser (the suites are stdlib-only by design), OS-agnostic.
"""
import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

ROOT = Path(_util.SCRIPTS)
PACKAGE = ROOT / "discord_mb_lib"
TAG_WORKFLOW = ROOT / ".github" / "workflows" / "tag.yml"


def _modules_defining_version():
    """Every package module with a module-level `__version__`, as repo paths."""
    found = []
    for module in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), str(module))
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign)
                       else [])
            if any(isinstance(t, ast.Name) and t.id == "__version__"
                   for t in targets):
                found.append(module.relative_to(ROOT).as_posix())
                break
    return found


def test_exactly_one_module_declares_the_version(_tmp):
    """Two would make "the file that holds __version__" ambiguous, and the
    workflow can only watch one of them."""
    found = _modules_defining_version()
    assert found == ["discord_mb_lib/core.py"], (
        f"__version__ is declared in {found}, not in core.py alone; "
        "pyproject.toml resolves the distribution version from core")


def test_the_tag_workflow_watches_the_file_that_holds_the_version(_tmp):
    """The bump has to land on a watched path or the workflow never starts."""
    version_file = _modules_defining_version()[0]
    text = TAG_WORKFLOW.read_text(encoding="utf-8")
    trigger = text.split("paths:", 1)
    assert len(trigger) == 2, "tag.yml no longer filters its push trigger"
    trigger = trigger[1].split("workflow_dispatch:", 1)[0]
    assert version_file in trigger, (
        f"tag.yml does not watch {version_file}, so bumping __version__ "
        f"schedules no run at all:\n{trigger}")


def test_the_tag_workflow_does_not_read_the_version_off_the_bare_package(_tmp):
    """`discord_mb_lib` defines no `__version__` — reading it there is an
    AttributeError, and `__init__.py` cannot gain one without importing core,
    which drags discord.py and psutil into every consumer of the package."""
    import discord_mb_lib                             # noqa: PLC0415
    assert not hasattr(discord_mb_lib, "__version__"), (
        "the package now exports __version__; this test's premise moved and "
        "tag.yml's read should be revisited with it")
    text = TAG_WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "discord_mb_lib.__version__" not in code, (
        "tag.yml reads __version__ from the package root, which does not "
        "define it, so the version step fails with AttributeError")


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="versionsource_")


if __name__ == "__main__":
    sys.exit(main())
