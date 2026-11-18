#!/usr/bin/env python3
"""The decisions of the CI gate scripts, driven in-process on documents built here."""
import contextlib
import io
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

FLOOR, PERF, FRESH, GATE = (_util.load(_util.script(f"scripts/ci/{name}.py")) for name in ("coverage_floor", "perf_gate", "gate_base_freshness", "ci_gate"))
BUDGET = {"schema_version": 1, "counter": "callgrind Ir", "tolerance": 0.02, "python": "3.13.14", "workloads": {"a": 1000}}


def _thresholds(tmp, name, **record):
    return Path(tmp, name).write_text(json.dumps({"schema_version": 1, "coverage": {"python": record}}), encoding="utf-8") and os.path.join(tmp, name)  # write_text returns a nonzero length


def test_a_raise_moves_the_floor_to_measured_minus_the_gap_only_upward(tmp):
    for n, (measured, floor, gap, new) in enumerate(((55.0, 48.0, 1.5, 53.5), (49.5, 48.0, 1.5, None), (49.6, 48.0, 1.5, 48.1), (40.0, 48.0, 1.5, None))):
        assert ((code := FLOOR.main(["raise", "--thresholds", _thresholds(tmp, "t", floor=floor, gap=gap), "--measured", str(measured), "--output", str(out := Path(tmp, f"out{n}"))])), (written := out.exists() and json.loads(out.read_text(encoding="utf-8"))["coverage"]["python"])) == ((0, {"floor": new, "gap": gap, "measured": measured}) if new else (3, False)), (measured, code, written)


def test_the_gate_floor_is_the_higher_of_head_and_base_and_refuses_a_lower_floor_a_wider_gap_or_a_malformed_document(tmp):
    low, high, wide = (_thresholds(tmp, n, floor=f, gap=g) for n, f, g in (("low", 48.0, 1.5), ("high", 50.0, 1.5), ("wide", 50.0, 2.0)))
    with contextlib.redirect_stdout(io.StringIO()) as printed:  # raised, equal, lowered floor; widened gap
        assert ((codes := [FLOOR.main(["floor", "--thresholds", head, "--base", base]) for head, base in ((high, low), (low, low), (low, high), (wide, high))]), printed.getvalue(), (bad := [FLOOR.main(["floor", "--thresholds", path]) for path in (_thresholds(tmp, "nan", floor=float("nan"), gap=1.5), _thresholds(tmp, "gapless", floor=48.0), _thresholds(tmp, "text", floor="48.0", gap=1.5), _thresholds(tmp, "over", floor=100.5, gap=1.5), _thresholds(tmp, "negative", floor=48.0, gap=-1.5))])) == ([0, 0, 1, 1], "50.0\n48.0\n", [2] * 5), (codes, printed.getvalue(), bad)  # then five malformed documents


def test_a_perf_workload_fails_over_tolerance_missing_failed_or_unbudgeted_and_main_lowers_a_budget_only_past_the_tolerance(_tmp):
    assert (verdicts := [[v for *_, v in PERF.compare(measured, failed, {"a": 1000}, Decimal("0.02"))] for measured, failed in (({"a": 1020}, ()), ({"a": 1021}, ()), ({}, ()), ({}, {"a"}), ({"a": 1, "b": 5}, ()))]) == [["ok"], ["over budget"], ["missing"], ["failed to measure"], ["ok", "unbudgeted"]] and [(PERF.tighten(BUDGET, {"a": n}) or {}).get("workloads") for n in (970, 980, 990, 1100)] == [{"a": 970}, None, None, None] and BUDGET["workloads"] == {"a": 1000} and [PERF.spent(*runs) for runs in ((5, 5), (None, 9), (5, None), (5, 9))] == [None, None, None, 4], verdicts  # Ir(K) - Ir(0); no cost is a failed run
    TestCase().assertRaises(ValueError, PERF.counts, {"python": "3.13.14", "environment": None, "failed": [], "workloads": {"a": 0}})  # a zero instruction count is refused


def test_each_perf_budget_change_is_a_budget_only_commit_their_trailers_name_every_raise_and_a_head_behind_main_fails_freshness(tmp):
    alone, mixed, base = {"sha": "1", "files": [PERF.BUDGET_FILE], "declares": ["a", "c"]}, {"sha": "2", "files": [PERF.BUDGET_FILE, "x.py"], "declares": ["b"]}, ({"a": 1000, "c": 9}, Decimal("0.01"), "3.13.14")
    assert ([PERF.raises(head, base) for head in (({"a": 1001, "c": 9, "d": 1}, *base[1:]), ({"a": 1000}, Decimal("0.02"), base[2]), (*base[:2], "3.13.15"))], PERF.mixed_budget_commits([alone, mixed, {"sha": "3", "files": ["x.py"], "declares": []}]), PERF.undeclared_raises({"a", "b", "c"}, [alone, mixed])) == ([{"a", "d"}, {"tolerance", "c"}, {"python"}], ["2"], ["b"])  # a mixed commit's trailer names nothing
    for name, text in {"code.py": "{}", "budget.json": "{}", **(fold := {".github/workflows/gate.yml": "jobs:\n  fold:\n    steps:\n      - run: python3 scripts/ci/ci_gate.py\n", "scripts/ci/ci_gate.py": "COVERED = ('.github/workflows/gate.yml', '.github/workflows/leaf.yml')\n", ".github/workflows/leaf.yml": "jobs:\n  leaf:\n    steps:\n      - run: true\n"})}.items():  # the git reads: every file a commit changes and its trailer's names; then main gains a commit to code.py, which no job reads (branch healthy), and one to leaf.yml, which the required job reaches only by running ci_gate.py, whose COVERED folds it
        Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp, name).write_text(text, encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "mixed\n\nPerf-Budget-Raise: a, b\nCo-Authored-By: c <c@example.invalid>"], ["rm", "-q", "code.py"], ["commit", "-qm", "later"], ["branch", "healthy"], ["rm", "-q", ".github/workflows/leaf.yml"], ["commit", "-qm", "fold"], ["checkout", "-q", "--detach", "HEAD~2"], ["remote", "add", "origin", tmp]):
        subprocess.run(["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)
    with mock.patch.multiple(FRESH, REQUIRED_CHECKS=("fold",), IMPLICIT=()), contextlib.redirect_stdout(io.StringIO()) as printed:  # an unnamed raise, a mixed budget commit, a Python other than the budget's, a named raise, then a named raise measured past its new budget
        assert [PERF.check({"python": "3.13.14", "environment": None, "failed": [], "workloads": {"a": n}}, {**BUDGET, "python": py, "workloads": {"a": 1500}}, {**BUDGET, "python": py}, commits) for py, commits, n in (("3.13.14", [{**alone, "declares": []}], 1000), ("3.13.14", [alone, mixed], 1000), ("3.13.15", [alone], 1000), ("3.13.14", [alone], 1000), ("3.13.14", [alone], 1531))] == [1, 1, 1, 0, 1] and ([(c["files"], c["declares"]) for c in PERF.commits_in("HEAD", tmp)], [mock.patch.object(FRESH, "BASE_BRANCH", branch)(FRESH.check)(tmp, "pull_request") for branch in ("main", "healthy")], [line for line in printed.getvalue().splitlines() if line.startswith(("main holds", "    ."))]) == ([(sorted(["budget.json", "code.py", *fold]), ["a", "b"])], [1, 0], ["main holds 1 commit(s) this head does not, touching a file a required check reads:", "    .github/workflows/leaf.yml"]), printed.getvalue()  # main is stale, healthy fresh


def test_the_freshness_set_holds_the_files_the_required_checks_name_and_no_record_or_code_named_only_as_a_directory(_tmp):
    assert (paths := FRESH.gate_paths(_util.SCRIPTS)) == [*(f".github/workflows/{w}" for w in ("actionlint.yml", "audit.yml", "ci-gate.yml", "codeql.yml", "comment-share.yml", "lint.yml", "open-pull-request.yml", "perf.yml", "pr-gate.yml", "requirements-runtime.txt", "secrets.yml", "size-baseline.yml", "tests.yml", "types.yml")), ".gitleaks.toml", ".pylintrc", "pyproject.toml", "pyrightconfig.json", "requirements-build.txt", "requirements-dev.txt", "run_tests.py", "scripts/ci/ci_gate.py", "scripts/ci/coverage_floor.py", "scripts/ci/gate_base_freshness.py", "scripts/ci/perf_gate.py", "setup.cfg"], paths
    assert FRESH.covered_workflows("COVERED = ('a', 'b')", {"a", "b"}) == ("a", "b") and [TestCase().assertRaises(FRESH.GateError, FRESH.covered_workflows, text, {"a", "b"}) for text in ("COVERED = tuple(n for n in 'ab')", "COVERED: tuple = ('a',)", "COVERED = ('a', 1)", "COVERED = ('a', 'c')", "COVERED = ()", "COVERED = ['a', 'b']", "def f():\n    COVERED = ('a',)", "COVERED = (", "", *(f"COVERED = ('a',)\n{rebind}" for rebind in ("COVERED += ('b',)", "from m import COVERED", "import m as COVERED", "def COVERED(): pass", "class COVERED: pass", "del COVERED", "from m import *")))] == [None] * 16  # COVERED is one top-level literal tuple of tracked workflows that nothing else names; any other shape refuses


def test_the_ci_gate_is_final_once_every_covered_run_completed_and_green_only_when_each_job_succeeded_or_skipped_a_guard(_tmp):
    runs, lint, tests = {path: {"id": path, "status": "completed"} for path in GATE.COVERED}, ".github/workflows/lint.yml", ".github/workflows/tests.yml"  # each run's jobs are pylint succeeded unless a row says otherwise
    rows = (({}, {}, (True, [])), ({}, {lint: [("pylint", "success"), ("guard / lookup", "skipped")]}, (True, [])), ({}, {lint: [("pylint", "skipped"), ("guard", "skipped")]}, (True, [f"{lint}: pylint skipped", f"{lint}: guard skipped"])), ({}, {tests: [("suites (macos-latest, 3.13)", "cancelled"), ("guard / lookup", "cancelled")]}, (True, [f"{tests}: suites (macos-latest, 3.13) cancelled", f"{tests}: guard / lookup cancelled"])), ({".github/workflows/perf.yml": None}, {}, (False, [".github/workflows/perf.yml: no run"])), ({".github/workflows/types.yml": {"id": 0, "status": "in_progress"}}, {}, (False, [".github/workflows/types.yml: run in_progress"])), ({}, {".github/workflows/secrets.yml": []}, (True, [".github/workflows/secrets.yml: no jobs"])))
    with mock.patch.object(GATE, "api", lambda _endpoint: [{"workflow_runs": [{"path": p, "event": "pull_request", "id": n, "status": "completed", "run_attempt": 1} for n, p in enumerate(GATE.COVERED)], "jobs": [{"name": "pylint", "conclusion": "failure"}]}]), mock.patch.dict(os.environ, {"DISCORD_MAILBOX_REPO": "o/r", "DISCORD_MAILBOX_HEAD_SHA": "1", "GITHUB_STEP_SUMMARY": ""}), contextlib.redirect_stdout(io.StringIO()) as printed:  # main exits red on a finished run whose job failed
        assert (GATE.COVERED, verdicts := [GATE.verdict({**runs, **runs_over}, {path: [{"name": n, "conclusion": c} for n, c in jobs_over.get(path, [("pylint", "success")])] for path in GATE.COVERED}) for runs_over, jobs_over, _ in rows], GATE.main(), printed.getvalue().count(": pylint failure\n")) == ((".github/workflows/tests.yml", ".github/workflows/lint.yml", ".github/workflows/types.yml", ".github/workflows/audit.yml", ".github/workflows/codeql.yml", ".github/workflows/actionlint.yml", ".github/workflows/secrets.yml", ".github/workflows/perf.yml"), [want for *_, want in rows], 1, 8), (GATE.COVERED, verdicts, printed.getvalue())


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="cigates_")


if __name__ == "__main__":
    sys.exit(main())
