#!/usr/bin/env python3
"""The decisions of the CI gate scripts, driven in-process on documents built here."""
import contextlib
import io
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

FLOOR = _util.load(os.path.join(_util.SCRIPTS, "scripts", "ci", "coverage_floor.py"))
PERF = _util.load(os.path.join(_util.SCRIPTS, "scripts", "ci", "perf_gate.py"))
BUDGET = {"schema_version": 1, "counter": "callgrind Ir", "tolerance": 0.02, "python": "3.13.14", "workloads": {"a": 1000}}


def _thresholds(tmp, name, **record):
    Path(tmp, name).write_text(json.dumps({"schema_version": 1, "coverage": {"python": record}}), encoding="utf-8")
    return os.path.join(tmp, name)


def test_a_raise_moves_the_floor_to_measured_minus_the_gap_only_upward(tmp):
    for n, (measured, floor, gap, new) in enumerate(((55.0, 48.0, 1.5, 53.5), (49.5, 48.0, 1.5, None), (49.6, 48.0, 1.5, 48.1), (40.0, 48.0, 1.5, None))):
        out = Path(tmp, f"out{n}")
        code = FLOOR.main(["raise", "--thresholds", _thresholds(tmp, "t", floor=floor, gap=gap), "--measured", str(measured), "--output", str(out)])
        written = out.exists() and json.loads(out.read_text(encoding="utf-8"))["coverage"]["python"]
        assert (code, written) == ((0, {"floor": new, "gap": gap, "measured": measured}) if new else (3, False)), (measured, code, written)


def test_the_gate_floor_is_the_higher_of_head_and_base_and_refuses_a_lower_floor_a_wider_gap_or_a_malformed_document(tmp):
    low, high, wide = (_thresholds(tmp, n, floor=f, gap=g) for n, f, g in (("low", 48.0, 1.5), ("high", 50.0, 1.5), ("wide", 50.0, 2.0)))
    with contextlib.redirect_stdout(io.StringIO()) as printed:  # raised, equal, lowered floor; widened gap
        codes = [FLOOR.main(["floor", "--thresholds", head, "--base", base]) for head, base in ((high, low), (low, low), (low, high), (wide, high))]
    assert (codes, printed.getvalue()) == ([0, 0, 1, 1], "50.0\n48.0\n"), (codes, printed.getvalue())
    for bad in (_thresholds(tmp, "nan", floor=float("nan"), gap=1.5), _thresholds(tmp, "gapless", floor=48.0), _thresholds(tmp, "text", floor="48.0", gap=1.5),
                _thresholds(tmp, "over", floor=100.5, gap=1.5), _thresholds(tmp, "negative", floor=48.0, gap=-1.5)):
        assert FLOOR.main(["floor", "--thresholds", bad]) == 2, bad


def test_a_perf_workload_fails_over_tolerance_missing_failed_or_unbudgeted_and_main_lowers_a_budget_only_past_the_tolerance(_tmp):
    rows = [PERF.compare(measured, failed, {"a": 1000}, Decimal("0.02")) for measured, failed in (({"a": 1020}, ()), ({"a": 1021}, ()), ({}, ()), ({}, {"a"}), ({"a": 1, "b": 5}, ()))]
    assert [[v for *_, v in r] for r in rows] == [["ok"], ["over budget"], ["missing"], ["failed to measure"], ["ok", "unbudgeted"]], rows
    doc = {**BUDGET, "workloads": {"a": 1000}}
    assert [(PERF.tighten(doc, {"a": n}) or {}).get("workloads") for n in (970, 980, 990, 1100)] == [{"a": 970}, None, None, None] and doc["workloads"] == {"a": 1000}
    assert [PERF.spent(*runs) for runs in ((5, 5), (None, 9), (5, None), (5, 9))] == [None, None, None, 4]  # Ir(K) - Ir(0); no cost is a failed run
    with contextlib.suppress(ValueError):
        PERF.counts({"python": "3.13.14", "environment": None, "failed": [], "workloads": {"a": 0}})
        raise AssertionError("a zero instruction count was accepted")


def test_a_perf_budget_raise_drop_or_addition_needs_its_own_commit_to_touch_only_the_budget_and_name_it(_tmp):
    alone, touching, base = [".github/perf-budget.json"], [".github/perf-budget.json", "run_tests.py"], ({"a": 1000, "c": 9}, Decimal("0.01"), "3.13.14")
    assert [PERF.raises(head, base) for head in (({"a": 1001, "c": 9, "d": 1}, *base[1:]), ({"a": 1000}, Decimal("0.02"), base[2]), (*base[:2], "3.13.15"))] == [{"a", "d"}, {"*", "c"}, {"*"}]
    laundered = [{"files": touching, "declares": [], "raised": {"a"}}, {"files": alone, "declares": ["a"], "raised": set()}]  # a later budget-only commit cannot declare it; nor can no commit
    for commits, undeclared in [([{"files": f, "declares": d, "raised": {"a"}}], u) for f, d, u in ((alone, ["a"], []), (touching, ["a"], ["a"]), (alone, ["b"], ["a"]), (alone, ["*"], []))] + [(laundered, ["a"]), ([], ["a"])]:
        assert PERF.undeclared_raises({"a"}, commits) == undeclared, commits
    with contextlib.redirect_stdout(io.StringIO()):  # an undeclared raise, a Python other than the budget's, then a declared raise
        codes = [PERF.check({"python": "3.13.14", "environment": None, "failed": [], "workloads": {"a": 1000}}, {**BUDGET, "python": python, "workloads": {"a": 1500}}, {**BUDGET, "python": python}, [{"files": files, "declares": ["a"], "raised": {"a"}}]) for python, files in (("3.13.14", touching), ("3.13.15", alone), ("3.13.14", alone))]
    assert codes == [1, 1, 0], codes


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="cigates_")


if __name__ == "__main__":
    sys.exit(main())
