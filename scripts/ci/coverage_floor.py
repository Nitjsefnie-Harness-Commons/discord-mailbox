"""Read and raise the coverage floor kept in .github/ci-thresholds.json.

floor: print max(head floor, base floor); exit 1 when the head lowers the
floor or widens the gap.
raise: write the floor raised to round(measured - gap, 1) and exit 0, or
write nothing and exit 3 when that does not exceed the floor.
Exit 2 on a malformed document or argument.
"""
import argparse
import copy
import json
import sys
from decimal import Decimal
from pathlib import Path


class Lowered(Exception):
    pass


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f'{value!r} is not a number')
    number = Decimal(str(value))
    if not number.is_finite() or not 0 <= number <= 100:
        raise ValueError(f'{value} is not between 0 and 100')
    return number


def thresholds(document):
    try:
        record = document['coverage']['python']
        return _percent(record['floor']), _percent(record['gap'])
    except (KeyError, TypeError) as error:
        raise ValueError(f'coverage.python needs a floor and a gap: {error!r}') from error


def gate_floor(head, base=None):
    floor, gap = thresholds(head)
    base_floor, base_gap = (floor, gap) if base is None else thresholds(base)
    if floor < base_floor:
        raise Lowered(f'the head floor {floor} is below the base floor {base_floor}')
    if gap > base_gap:
        raise Lowered(f'the head gap {gap} is above the base gap {base_gap}')
    return floor


def raised(document, measured):
    floor, gap = thresholds(document)
    measured = _percent(Decimal(measured))
    new = round(measured - gap, 1)
    if new <= floor:
        return None
    document = copy.deepcopy(document)
    document['coverage']['python'].update(floor=float(new), measured=float(measured))
    return document


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    gate = commands.add_parser('floor')
    gate.add_argument('--thresholds', type=Path, required=True)
    gate.add_argument('--base', type=Path)
    ratchet = commands.add_parser('raise')
    ratchet.add_argument('--thresholds', type=Path, required=True)
    ratchet.add_argument('--measured', required=True)
    ratchet.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'floor':
            print(gate_floor(_read(args.thresholds), args.base and _read(args.base)))
            return 0
        document = raised(_read(args.thresholds), args.measured)
        if document is None:
            print(f'coverage {args.measured} raises no floor')
            return 3
        args.output.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        print(f"coverage floor raised to {document['coverage']['python']['floor']}")
    except Lowered as error:
        print(f'coverage floor: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, ArithmeticError) as error:
        print(f'coverage floor: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
