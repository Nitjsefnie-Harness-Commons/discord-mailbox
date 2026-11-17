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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

FLOOR, PERF, FRESH = (_util.load(_util.script(f"scripts/ci/{name}.py")) for name in ("coverage_floor", "perf_gate", "gate_base_freshness"))
BUDGET = {"schema_version": 1, "counter": "callgrind Ir", "tolerance": 0.02, "python": "3.13.14", "workloads": {"a": 1000}}


def _thresholds(tmp, name, **record):
    Path(tmp, name).write_text(json.dumps({"schema_version": 1, "coverage": {"python": record}}), encoding="utf-8")
    return os.path.join(tmp, name)


def test_a_raise_moves_the_floor_to_measured_minus_the_gap_only_upward(tmp):
    for n, (measured, floor, gap, new) in enumerate(((55.0, 48.0, 1.5, 53.5), (49.5, 48.0, 1.5, None), (49.6, 48.0, 1.5, 48.1), (40.0, 48.0, 1.5, None))):
        code = FLOOR.main(["raise", "--thresholds", _thresholds(tmp, "t", floor=floor, gap=gap), "--measured", str(measured), "--output", str(out := Path(tmp, f"out{n}"))])
        assert (code, (written := out.exists() and json.loads(out.read_text(encoding="utf-8"))["coverage"]["python"])) == ((0, {"floor": new, "gap": gap, "measured": measured}) if new else (3, False)), (measured, code, written)


def test_the_gate_floor_is_the_higher_of_head_and_base_and_refuses_a_lower_floor_a_wider_gap_or_a_malformed_document(tmp):
    low, high, wide = (_thresholds(tmp, n, floor=f, gap=g) for n, f, g in (("low", 48.0, 1.5), ("high", 50.0, 1.5), ("wide", 50.0, 2.0)))
    with contextlib.redirect_stdout(io.StringIO()) as printed:  # raised, equal, lowered floor; widened gap
        codes = [FLOOR.main(["floor", "--thresholds", head, "--base", base]) for head, base in ((high, low), (low, low), (low, high), (wide, high))]
    assert (codes, printed.getvalue()) == ([0, 0, 1, 1], "50.0\n48.0\n"), (codes, printed.getvalue())
    for bad in (_thresholds(tmp, "nan", floor=float("nan"), gap=1.5), _thresholds(tmp, "gapless", floor=48.0), _thresholds(tmp, "text", floor="48.0", gap=1.5), _thresholds(tmp, "over", floor=100.5, gap=1.5), _thresholds(tmp, "negative", floor=48.0, gap=-1.5)):
        assert FLOOR.main(["floor", "--thresholds", bad]) == 2, bad


def test_a_perf_workload_fails_over_tolerance_missing_failed_or_unbudgeted_and_main_lowers_a_budget_only_past_the_tolerance(_tmp):
    assert (verdicts := [[v for *_, v in PERF.compare(measured, failed, {"a": 1000}, Decimal("0.02"))] for measured, failed in (({"a": 1020}, ()), ({"a": 1021}, ()), ({}, ()), ({}, {"a"}), ({"a": 1, "b": 5}, ()))]) == [["ok"], ["over budget"], ["missing"], ["failed to measure"], ["ok", "unbudgeted"]], verdicts
    assert [(PERF.tighten(BUDGET, {"a": n}) or {}).get("workloads") for n in (970, 980, 990, 1100)] == [{"a": 970}, None, None, None] and BUDGET["workloads"] == {"a": 1000} and [PERF.spent(*runs) for runs in ((5, 5), (None, 9), (5, None), (5, 9))] == [None, None, None, 4]  # Ir(K) - Ir(0); no cost is a failed run
    with contextlib.suppress(ValueError):  # a zero instruction count is refused, so a returned count fails the test
        raise AssertionError(PERF.counts({"python": "3.13.14", "environment": None, "failed": [], "workloads": {"a": 0}}))


def test_each_perf_budget_change_is_a_budget_only_commit_and_their_trailers_name_every_raise(tmp):
    alone, mixed, base = {"sha": "1", "files": [PERF.BUDGET_FILE], "declares": ["a", "c"]}, {"sha": "2", "files": [PERF.BUDGET_FILE, "x.py"], "declares": ["b"]}, ({"a": 1000, "c": 9}, Decimal("0.01"), "3.13.14")
    assert ([PERF.raises(head, base) for head in (({"a": 1001, "c": 9, "d": 1}, *base[1:]), ({"a": 1000}, Decimal("0.02"), base[2]), (*base[:2], "3.13.15"))], PERF.mixed_budget_commits([alone, mixed, {"sha": "3", "files": ["x.py"], "declares": []}]), PERF.undeclared_raises({"a", "b", "c"}, [alone, mixed])) == ([{"a", "d"}, {"tolerance", "c"}, {"python"}], ["2"], ["b"])  # a mixed commit's trailer names nothing
    with contextlib.redirect_stdout(io.StringIO()):  # an unnamed raise, a mixed budget commit, a Python other than the budget's, then a named raise
        assert [PERF.check({"python": "3.13.14", "environment": None, "failed": [], "workloads": {"a": 1000}}, {**BUDGET, "python": py, "workloads": {"a": 1500}}, {**BUDGET, "python": py}, commits) for py, commits in (("3.13.14", [{**alone, "declares": []}]), ("3.13.14", [alone, mixed]), ("3.13.15", [alone]), ("3.13.14", [alone]))] == [1, 1, 1, 0]
    for name in ("code.py", "budget.json"):  # the git read: every file a commit changes, and its trailer's names
        Path(tmp, name).write_text("{}", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "mixed\n\nPerf-Budget-Raise: a, b\nCo-Authored-By: c <c@example.invalid>"]):
        subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
    assert [(c["files"], c["declares"]) for c in PERF.commits_in("HEAD", tmp)] == [(["budget.json", "code.py"], ["a", "b"])]


def test_the_freshness_set_holds_the_files_the_required_checks_name_and_the_size_baseline_job_runs_the_check(_tmp):
    paths, steps = FRESH.gate_paths(_util.SCRIPTS), FRESH.workflow_jobs(Path(_util.script(".github/workflows/size-baseline.yml")).read_text(encoding="utf-8"), "size-baseline.yml")["size-baseline"]["steps"]
    assert {"run_tests.py", ".github/workflows/open-pull-request.yml", "pyproject.toml", "scripts/ci/gate_base_freshness.py"} <= set(paths) and not [p for p in paths if p.startswith(("discord_mb_lib/", "tests/", ".github/size-baseline.json", ".github/ci-thresholds.json"))] and "python3 scripts/ci/gate_base_freshness.py" in (runs := [s["run"] or "" for s in steps]) and [r for r in runs if "git grep -nI -E" in r], (paths, steps)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="cigates_")


if __name__ == "__main__":
    sys.exit(main())
