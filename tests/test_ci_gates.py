#!/usr/bin/env python3
"""The decisions of the CI gate scripts, driven in-process on documents built here."""
import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

FLOOR = _util.load(os.path.join(_util.SCRIPTS, "scripts", "ci", "coverage_floor.py"))


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


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="cigates_")


if __name__ == "__main__":
    sys.exit(main())
