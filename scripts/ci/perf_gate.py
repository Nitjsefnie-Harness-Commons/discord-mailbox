"""Count each perf workload's instructions under callgrind and gate them.

measure: write {python, environment, workloads, failed}, where a workload's
count is Ir(--repeat K) - Ir(--repeat 0), its own start-up subtracted.
check: exit 1 when a workload is over budget * (1 + tolerance), missing,
failed or unbudgeted, or when a commit raises, adds or drops a budget
without touching only the budget file and naming it in its trailer; print
the table and the budget document re-recorded from the counts.
tighten: write the budget lowered to every count below budget *
(1 - tolerance) and exit 0, or write nothing and exit 3.
Exit 2 on a malformed document or argument.
"""
import argparse
import copy
import json
import os
import platform
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

BUDGET_FILE = '.github/perf-budget.json'
TRAILER = 'Perf-Budget-Raise'
REPEAT = 3
WORKLOADS = Path(__file__).with_name('perf_workloads.py')
ENV = {'PYTHONHASHSEED': '0', 'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C.UTF-8'}


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{value!r} is not a positive instruction count')
    return value


def budget(document):
    """The validated (workloads, tolerance, python) of a budget document."""
    try:
        if document['schema_version'] != 1 or document['counter'] != 'callgrind Ir':
            raise ValueError('expected schema_version 1 and counter "callgrind Ir"')
        tolerance = document['tolerance']
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance < 1:
            raise ValueError(f'tolerance {tolerance!r} is not in [0, 1)')
        if not re.fullmatch(r'\d+\.\d+\.\d+', document['python']):
            raise ValueError(f"python {document['python']!r} is not an exact X.Y.Z")
        return {k: _count(v) for k, v in document['workloads'].items()}, Decimal(str(tolerance)), document['python']
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f'malformed budget document: {error!r}') from error


def counts(document):
    """The validated (workloads, failed, python, environment) of a counts document."""
    try:
        return ({k: _count(v) for k, v in document['workloads'].items()}, set(document['failed']),
                document['python'], document['environment'])
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f'malformed counts document: {error!r}') from error


def compare(measured, failed, budgets, tolerance):
    """[(name, measured, budget, verdict)]; every verdict but 'ok' fails."""
    rows = []
    for name in sorted(set(measured) | set(budgets) | set(failed)):
        count, limit = measured.get(name), budgets.get(name)
        if name in failed:
            verdict = 'failed to measure'
        elif limit is None:
            verdict = 'unbudgeted'
        elif count is None:
            verdict = 'missing'
        else:
            verdict = 'over budget' if count > limit * (1 + tolerance) else 'ok'
        rows.append((name, count, limit, verdict))
    return rows


def raises(head, base):
    """What the head budget raises over the base: workloads raised, added or dropped; '*' for tolerance or python."""
    (workloads, tolerance, python), (base_workloads, base_tolerance, base_python) = head, base
    raised = {name for name, value in workloads.items() if value > base_workloads.get(name, 0)}
    raised |= set(base_workloads) - set(workloads)
    return raised | ({'*'} if tolerance > base_tolerance or python != base_python else set())


def spent(base, total):
    """Ir(--repeat K) - Ir(--repeat 0), or None when a run failed or the workload cost nothing."""
    return None if base is None or total is None or total <= base else total - base


def undeclared_raises(raised, commits):
    """The raises not made only by budget-only commits whose own trailer names them ('*' names all).

    Each commit carries the raises its own diff makes; a raise no commit made is undeclared.
    """
    made, undeclared = set(), set()
    for commit in commits:
        own = set(commit['raised']) & set(raised)
        made |= own
        if commit['files'] != [BUDGET_FILE]:
            undeclared |= own
        elif '*' not in commit['declares']:
            undeclared |= own - set(commit['declares'])
    return sorted(undeclared | (set(raised) - made))


def tighten(document, measured):
    """The budget lowered to each count below budget * (1 - tolerance), or None."""
    workloads, tolerance, _ = budget(document)
    lowered = {name: measured[name] for name, value in workloads.items()
               if name in measured and _count(measured[name]) < value * (1 - tolerance)}
    if not lowered:
        return None
    document = copy.deepcopy(document)
    document['workloads'].update(lowered)
    return document


def _git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout


def _budget_at(revision):
    shown = subprocess.run(['git', 'show', f'{revision}:{BUDGET_FILE}'], capture_output=True, text=True, check=False)
    try:
        return budget(json.loads(shown.stdout)) if shown.returncode == 0 else None
    except ValueError:
        return None


def _commits(revision_range):
    """Each commit's files, trailer names and own raises, all against its first parent."""
    for sha, *parents in (line.split() for line in _git('rev-list', '--parents', revision_range).splitlines()):
        diff = ['diff', '--name-only', '-z', parents[0], sha] if parents else ['diff-tree', '--root', '--no-commit-id', '--name-only', '-z', '-r', sha]
        files = [f for f in _git(*diff).split('\0') if f]
        values = _git('log', '-1', f'--format=%(trailers:key={TRAILER},valueonly,separator=%x2C)', sha)
        before = _budget_at(parents[0]) if parents and BUDGET_FILE in files else None
        raised = set()
        if before is not None:
            # A commit that deletes or breaks the budget drops every workload in it.
            after = _budget_at(sha)
            raised = raises(after, before) if after is not None else {'*', *before[0]}
        yield {'files': files, 'declares': [v.strip() for v in values.split(',') if v.strip()], 'raised': raised}


def _instructions(name, repeat, work, env):
    out = work / f'callgrind.{name}.{repeat}.out'
    run = subprocess.run(['valgrind', '--tool=callgrind', '--trace-children=no', f'--callgrind-out-file={out}',
                          sys.executable, str(WORKLOADS), name, '--repeat', str(repeat)],
                         env=env, capture_output=True, text=True, timeout=900, check=False)
    found = re.search(r'^summary: (\d+)$', out.read_text(encoding='utf-8'), re.M) if out.exists() else None
    if run.returncode or not found:
        print(f'{name} --repeat {repeat}: exit {run.returncode}' + ('' if found else ', no summary'),
              run.stderr[-3000:], sep='\n', file=sys.stderr)
        return None
    return int(found.group(1))


def measure(work):
    work.mkdir(parents=True, exist_ok=True)
    (work / 'tmp').mkdir(exist_ok=True)
    env = {**os.environ, **ENV, 'TMPDIR': str(work / 'tmp')}
    names = subprocess.run([sys.executable, str(WORKLOADS), '--list'], env=env, check=True,
                           capture_output=True, text=True).stdout.split()
    if not names:
        raise ValueError('perf_workloads.py lists no workload')
    result = {'python': platform.python_version(), 'workloads': {}, 'failed': [],
              'environment': {'valgrind': _valgrind_version(), 'runner': _runner()}}
    for name in names:
        count = spent(_instructions(name, 0, work, env), _instructions(name, REPEAT, work, env))
        if count is None:
            result['failed'].append(name)
        else:
            result['workloads'][name] = count
        print(f'{name}: {result["workloads"].get(name, "failed")}', file=sys.stderr)
    return result


def _valgrind_version():
    return subprocess.run(['valgrind', '--version'], check=True, capture_output=True,
                          text=True).stdout.strip().removeprefix('valgrind-')


def _runner():
    try:
        release = platform.freedesktop_os_release()
        return f"{release['ID']}-{release['VERSION_ID']}"
    except (OSError, KeyError):
        return platform.platform()


def _report(rows, tolerance, notes, document):
    lines = ['### Perf workloads (callgrind instructions)', '',
             f'Tolerance {tolerance}; a workload fails above budget x (1 + tolerance).', '',
             '| workload | measured | budget | verdict |', '| --- | ---: | ---: | --- |']
    lines += [f'| {n} | {c if c is not None else "-"} | {b if b is not None else "-"} | {v} |' for n, c, b, v in rows]
    lines += ['', *notes, '', f'{BUDGET_FILE} re-recorded from these counts:', '', '```json',
              json.dumps(document, indent=2), '```']
    text = '\n'.join(lines) + '\n'
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write(text)


def check(counts_doc, head_doc, base_doc=None, commits=()):
    measured, failed, python, environment = counts(counts_doc)
    head = budget(head_doc)
    notes, judged, tolerance = [], dict(head[0]), head[1]
    if python != head[2]:
        notes.append(f'FAIL: measured on Python {python}, the budget records {head[2]}.')
    if environment != head_doc.get('environment'):
        notes.append(f"Notice: measured on {environment}, the budget records {head_doc.get('environment')}.")
        print(f"::notice::perf environment {environment} differs from the budget's")
    if base_doc is not None:
        base = budget(base_doc)
        undeclared = undeclared_raises(raises(head, base), commits)
        if undeclared:
            notes.append(f'FAIL: raised, added or dropped outside a budget-only commit whose {TRAILER} trailer names it: {", ".join(undeclared)}.')
        for name in set(undeclared) - {'*'}:
            judged[name] = min(v for v in (judged.get(name), base[0].get(name)) if v is not None)
        if '*' in undeclared:
            tolerance = min(tolerance, base[1])
    rows = compare(measured, failed, judged, tolerance)
    document = {**copy.deepcopy(head_doc), 'python': python, 'environment': environment, 'workloads': measured}
    _report(rows, tolerance, notes, document)
    return 1 if any(v != 'ok' for *_, v in rows) or any(n.startswith('FAIL') for n in notes) else 0


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    gauge = commands.add_parser('measure')
    gauge.add_argument('--out', type=Path, required=True)
    gauge.add_argument('--work', type=Path, default=Path(os.environ.get('RUNNER_TEMP') or '.') / 'perf-work')
    gate = commands.add_parser('check')
    gate.add_argument('--counts', required=True)
    gate.add_argument('--budget', required=True)
    gate.add_argument('--base-budget')
    gate.add_argument('--range')
    ratchet = commands.add_parser('tighten')
    ratchet.add_argument('--counts', required=True)
    ratchet.add_argument('--budget', required=True)
    ratchet.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'measure':
            result = measure(args.work)
            args.out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            return 0
        if args.command == 'check':
            commits = list(_commits(args.range)) if args.range else []
            return check(_read(args.counts), _read(args.budget), args.base_budget and _read(args.base_budget), commits)
        measured, _, python, _ = counts(_read(args.counts))
        document = _read(args.budget)
        if python != budget(document)[2]:
            raise ValueError(f'counts measured on Python {python}, not the budget\'s')
        document = tighten(document, measured)
        if document is None:
            print('no workload got cheaper by more than the tolerance')
            return 3
        args.output.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        print(f"perf budgets lowered: {document['workloads']}")
    except (OSError, ValueError, ArithmeticError, subprocess.SubprocessError) as error:
        print(f'perf gate: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
