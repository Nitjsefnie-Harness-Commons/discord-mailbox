"""Count each perf workload's instructions under callgrind and gate them.

measure: write {python, environment, workloads, failed}, where a workload's
count is Ir(--repeat K) - Ir(--repeat 0), its own start-up subtracted.
check: exit 1 when a workload is over budget * (1 + tolerance), missing,
failed or unbudgeted. Against a base budget it also exits 1 when a commit
in the range changes the budget file beside another file, or when a raise
of the head over the base (a workload raised, added or dropped, the
tolerance raised, the python changed) is not named by a Perf-Budget-Raise
trailer of a budget-only commit in the range. It prints the table and the
budget document re-recorded from the counts.
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
    """The names of what the head budget raises over the base: workloads raised, added or dropped, 'tolerance', 'python'."""
    (workloads, tolerance, python), (base_workloads, base_tolerance, base_python) = head, base
    raised = {name for name, value in workloads.items() if value > base_workloads.get(name, 0)}
    raised |= set(base_workloads) - set(workloads)
    raised |= {'tolerance'} if tolerance > base_tolerance else set()
    return raised | ({'python'} if python != base_python else set())


def spent(base, total):
    """Ir(--repeat K) - Ir(--repeat 0), or None when a run failed or the workload cost nothing."""
    return None if base is None or total is None or total <= base else total - base


def mixed_budget_commits(commits):
    """The commits that change the budget file beside any other file."""
    return [commit['sha'] for commit in commits if BUDGET_FILE in commit['files'] and commit['files'] != [BUDGET_FILE]]


def undeclared_raises(raised, commits):
    """The raises no budget-only commit names in its trailer."""
    named = {name for commit in commits if commit['files'] == [BUDGET_FILE] for name in commit['declares']}
    return sorted(set(raised) - named)


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


def commits_in(revision_range, repo='.'):
    """Each commit's sha, trailer names and changed files; a merge lists only files it changed against every parent."""
    def git(*args):
        return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, text=True).stdout
    for sha in git('rev-list', revision_range).split():
        files = git('diff-tree', '-c', '-r', '--root', '--no-commit-id', '--name-only', '-z', sha).split('\0')
        values = git('log', '-1', f'--format=%(trailers:key={TRAILER},valueonly,separator=%x2C)', sha)
        yield {'sha': sha, 'files': [f for f in files if f], 'declares': [v.strip() for v in values.split(',') if v.strip()]}


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
        mixed, undeclared = mixed_budget_commits(commits), undeclared_raises(raises(head, base), commits)
        if mixed:
            notes.append(f'FAIL: these commits change {BUDGET_FILE} beside other files: {", ".join(mixed)}.')
        if undeclared:
            notes.append(f'FAIL: raised, added or dropped with no {TRAILER} trailer on a budget-only commit naming it: {", ".join(undeclared)}.')
        for name in set(undeclared) - {'tolerance', 'python'}:
            judged[name] = min(v for v in (judged.get(name), base[0].get(name)) if v is not None)
        if 'tolerance' in undeclared:
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
            commits = list(commits_in(args.range)) if args.range else []
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
