#!/usr/bin/env python3
"""Require this head to carry every commit on main touching a file the required checks read.

Main's ruleset does not require a branch to be up to date, so a pull request
can merge on a green run that never saw a later main commit changing a gate's
own inputs. The set is derived from the workflows: each required job's
workflow file, every local reusable workflow or action reached through the job
or its `needs:` ancestors, every tracked file their `run:` blocks name by
exact path, and IMPLICIT, minus the EXCLUDED records the ratchets commit.

Limits, each narrowing the set: a directory, glob or `.` names no file (the
conflict-marker step reads every tracked file); what a named script reaches
at run time (run_tests.py discovers tests/); a path built in `${{ }}`; a
tool's own config file outside IMPLICIT; records, read at the base revision;
a local composite action's own steps (its action.yml is in the set).

A push run passes a head main already holds. The reader models the block YAML
these workflows use and refuses any other shape rather than shrink the set.

    scripts/ci/gate_base_freshness.py [--root DIR] [--print-paths]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REQUIRED_CHECKS = ('gate', 'comment-share', 'size-baseline', 'pycodestyle', 'pylint', 'pyright',
                   'coverage', 'wheel', 'portable', 'pip-audit', 'analyze (python)')
IMPLICIT = ('setup.cfg', 'pyrightconfig.json', 'pyproject.toml')
EXCLUDED = ('.github/ci-thresholds.json', '.github/perf-budget.json',
            '.github/comment-share.json', '.github/size-baseline.json')
BASE_BRANCH = 'main'
WORKFLOW_DIR = '.github/workflows'
PATHSPECS_MAX_BYTES = 65536


class GateError(Exception):
    """This run could not establish the answer."""


class WorkflowError(GateError):
    """A workflow shape the reader does not model."""


def git(root, *arguments, what):
    command = ('git', '--literal-pathspecs', '-C', str(root)) + arguments
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise GateError(f"cannot {what}: `{' '.join(command)}` exited {done.returncode}: "
                        f"{done.stderr.strip() or 'no output'}")
    return done.stdout


MAPPING = re.compile(r'^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$')
SEQUENCE = re.compile(r'^- (?P<rest>.+)$')
BLOCK_SCALAR = re.compile(r'^[|>][0-9+-]*$')
FLOW_LIST = re.compile(r'^\[(?P<items>[^\[\]{}]*)\]$')
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]*$')


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def skippable(line):
    stripped = line.strip()
    return not stripped or stripped.startswith('#')


def block_end(lines, start, indent):
    index = start + 1
    while index < len(lines) and (skippable(lines[index]) or indent_of(lines[index]) > indent):
        index += 1
    return index


def block_scalar(lines, start, key_indent, limit):
    """The text of the block scalar opened at `start`, comment lines kept, and the index after it."""
    body = []
    index = start + 1
    while index < limit and not (lines[index].strip() and indent_of(lines[index]) <= key_indent):
        body.append(lines[index])
        index += 1
    lead = min((indent_of(line) for line in body if line.strip()), default=key_indent + 2)
    return '\n'.join(line[lead:] for line in body), index


def uses_of(value):
    return value.split()[0].strip('\'"') if value else None


def key_of(line, workflow, expected):
    match = MAPPING.match(line.strip())
    if not match:
        raise WorkflowError(f'{workflow}: expected {expected}, found {line!r}')
    return match['key'], (match['value'] or '').strip()


def workflow_jobs(text, workflow):
    """{job id: {'needs': [...], 'uses': str or None, 'steps': [{'run', 'uses'}]}}."""
    lines = text.splitlines()
    tops = [i for i, line in enumerate(lines) if not skippable(line) and indent_of(line) == 0
            and re.match(r'jobs:(\s|$)', line)]
    if len(tops) != 1:
        raise WorkflowError(f'{workflow} has {len(tops)} top-level `jobs:` keys; this reader models one')
    if key_of(lines[tops[0]], workflow, '`jobs:`')[1]:
        raise WorkflowError(f'{workflow} writes `jobs:` in flow form; this reader models the block form')
    jobs = {}
    index = tops[0] + 1
    while index < len(lines):
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) < 2:
            break
        name, value = key_of(line, workflow, 'a job entry')
        if indent_of(line) != 2 or value:
            raise WorkflowError(f'{workflow}: expected a job entry, found {line!r}')
        if name in jobs:
            raise WorkflowError(f'{workflow}: duplicate job {name!r}')
        end = block_end(lines, index, 2)
        jobs[name] = job_fields(lines, name, index + 1, end, workflow)
        index = end
    return jobs


def job_fields(lines, name, start, end, workflow):
    job = {'needs': [], 'uses': None, 'steps': []}
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 4:
            raise WorkflowError(f'{workflow}: expected a key of job `{name}`, found {line!r}')
        key, value = key_of(line, workflow, f'a key of job `{name}`')
        stop = block_end(lines, index, 4)
        if key == 'steps':
            if value:
                raise WorkflowError(f'{workflow}: `steps:` of job `{name}` is not a block sequence')
            job['steps'] = step_entries(lines, name, index + 1, stop, workflow)
        elif key == 'needs':
            job['needs'] = needs_of(lines, name, value, index + 1, stop, workflow)
        elif key == 'uses':
            job['uses'] = uses_of(value)
        index = stop
    return job


def needs_of(lines, name, value, start, end, workflow):
    """A job's `needs:` as a scalar, a flow list or a block list of job ids."""
    flow = FLOW_LIST.match(value)
    if flow:
        items = [item.strip() for item in flow['items'].split(',') if item.strip()]
    elif value:
        items = [value]
    else:
        items = []
        for line in (line for line in lines[start:end] if not skippable(line)):
            entry = SEQUENCE.match(line.strip())
            if indent_of(line) != 6 or not entry:
                raise WorkflowError(f'{workflow}: expected a `needs:` entry in job `{name}`, found {line!r}')
            items.append(entry['rest'].strip())
    for item in items:
        if not NAME.match(item):
            raise WorkflowError(f'{workflow}: `needs:` of job `{name}` names {item!r}, not a job id')
    return items


def step_entries(lines, name, start, end, workflow):
    steps = []
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        entry = SEQUENCE.match(line.strip()) if indent_of(line) == 6 else None
        if entry is None:
            raise WorkflowError(f'{workflow}: expected a step entry in job `{name}`, found {line!r}')
        stop = block_end(lines, index, 6)
        steps.append(step_fields(lines, name, index, stop, workflow, entry['rest']))
        index = stop
    return steps


def apply_field(lines, end, key, value, at, step):
    if key == 'run':
        if not value or BLOCK_SCALAR.match(value):
            step['run'], after = block_scalar(lines, at, 8, end)
            return after
        step['run'] = value
    elif key == 'uses':
        step['uses'] = uses_of(value)
    elif not value or BLOCK_SCALAR.match(value):
        # A `with:` or `env:` block is input to a step, not a file it reads.
        return block_end(lines, at, 8)
    return at + 1


def step_fields(lines, name, at, end, workflow, dash):
    step = {'run': None, 'uses': None}
    key, value = key_of(dash, workflow, f'a key on a step of job `{name}`')
    index = apply_field(lines, end, key, value, at, step)
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 8:
            raise WorkflowError(f'{workflow}: expected a key in a step of job `{name}`, found {line!r}')
        key, value = key_of(line, workflow, f'a key in a step of job `{name}`')
        index = apply_field(lines, end, key, value, index, step)
    return step


EXPRESSION = re.compile(r'\$\{\{.*?\}\}', re.DOTALL)
CANDIDATE = re.compile(r'[A-Za-z0-9._/-]+')


def named_files(text, files):
    words = CANDIDATE.findall(EXPRESSION.sub(' ', text))
    return {word[2:] if word.startswith('./') else word for word in words} & files


def local_target(uses, files, workflow):
    """The tracked file a local `uses: ./...` runs: the workflow file, or the action's action.yml."""
    path = uses[2:].rstrip('/')
    prefix = f'{path}/' if path else ''
    for target in (path, f'{prefix}action.yml', f'{prefix}action.yaml'):
        if target in files:
            return target
    raise GateError(f'{workflow} uses {uses!r}, which names no tracked workflow or action file')


def tracked_files(root):
    return frozenset(f for f in git(root, 'ls-tree', '-r', '--name-only', '--full-tree', 'HEAD',
                                    what='list the tracked tree').split('\n') if f)


def gate_paths(root):
    """Every tracked file a required check reads, derived from the workflows at HEAD."""
    files = tracked_files(root)
    parsed = {name: workflow_jobs(git(root, 'cat-file', 'blob', f'HEAD:{name}', what=f'read {name}'), name)
              for name in sorted(files) if name.startswith(WORKFLOW_DIR + '/')
              and name.endswith(('.yml', '.yaml'))}
    derived = set()
    for implicit in IMPLICIT:
        if implicit not in files:
            raise GateError(f'{implicit} is not tracked, so a check reading it implicitly reads nothing')
        derived.add(implicit)
    pending = []
    for context in REQUIRED_CHECKS:
        job = context.split(' (', maxsplit=1)[0]
        defining = [(name, job) for name, jobs in parsed.items() if job in jobs]
        if not defining:
            raise GateError(f'no workflow under {WORKFLOW_DIR}/ defines the required job `{job}`: '
                            f'put it back, or update REQUIRED_CHECKS to match the ruleset')
        pending += defining
    seen = set()
    while pending:
        workflow, job = pending.pop()
        if (workflow, job) in seen:
            continue
        seen.add((workflow, job))
        derived.add(workflow)
        fields = parsed[workflow][job]
        for need in fields['needs']:
            if need not in parsed[workflow]:
                raise WorkflowError(f'{workflow}: job `{job}` needs `{need}`, which it does not define')
            pending.append((workflow, need))
        if (fields['uses'] or '').startswith('./'):
            called = local_target(fields['uses'], files, workflow)
            derived.add(called)
            pending += [(called, name) for name in parsed.get(called, ())]
        for step in fields['steps']:
            if (step['uses'] or '').startswith('./'):
                derived.add(local_target(step['uses'], files, workflow))
            derived |= named_files(step['run'] or '', files)
    return sorted(derived - set(EXCLUDED))


def fetch_base(root):
    """Fetch main as it is now, unshallowing a shallow checkout so the head's ancestry is complete."""
    arguments = ['fetch', '--no-tags', '--quiet']
    if git(root, 'rev-parse', '--is-shallow-repository', what='ask whether the checkout is shallow').strip() == 'true':
        arguments.append('--unshallow')
    git(root, *arguments, 'origin', f'+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}',
        what=f'fetch origin/{BASE_BRANCH}')


def stale_commits(root, head, base, paths):
    """[(sha, subject, [paths])] for the commits base holds that head lacks, touching `paths`."""
    if not paths:
        raise GateError('the derived path set is empty, and `--` with no path means every path')
    if sum(len(path) + 1 for path in paths) > PATHSPECS_MAX_BYTES:
        raise GateError(f'the derived path set is past {PATHSPECS_MAX_BYTES} bytes of arguments')
    listing = git(root, 'log', f'{head}..{base}', '--name-only', '--format=%x00%H%x09%s', '--', *paths,
                  what=f'list what {BASE_BRANCH} holds that this head lacks')
    stale = []
    for chunk in listing.split('\0'):
        if chunk.strip():
            header, _, body = chunk.partition('\n')
            sha, _, subject = header.partition('\t')
            stale.append((sha.strip(), subject.strip(), [line for line in body.split('\n') if line.strip()]))
    return stale


def check(root, event):
    head = git(root, 'rev-parse', '--verify', 'HEAD^{commit}', what='resolve the checked-out head').strip()
    fetch_base(root)
    base = git(root, 'rev-parse', '--verify', f'refs/remotes/origin/{BASE_BRANCH}^{{commit}}',
               what=f'resolve origin/{BASE_BRANCH}').strip()
    if event == 'push' and git(root, 'merge-base', head, base, what='find the merge base').strip() == head:
        print(f'This pushed head is already on {BASE_BRANCH}; nothing to compare.')
        return 0
    paths = gate_paths(root)
    stale = stale_commits(root, head, base, paths)
    if not stale:
        print(f'This head carries every commit on {BASE_BRANCH} touching the {len(paths)} '
              f'file(s) a required check reads by name (--print-paths lists them).')
        return 0
    print(f'{BASE_BRANCH} holds {len(stale)} commit(s) this head does not, touching a file a required check reads:')
    for sha, subject, touched in stale:
        print(f'  {sha} {subject}')
        for path in touched or ['(a merge commit; git names no file for it)']:
            print(f'    {path}')
    print(f'Rebase onto {BASE_BRANCH} and push again, so the checks read the files {BASE_BRANCH} reads.')
    return 1


def main(argv):
    root = Path(__file__).resolve().parents[2]
    print_paths = False
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == '--print-paths':
            print_paths = True
        elif argument == '--root' and rest:
            root = Path(rest.pop(0))
        else:
            print('usage: gate_base_freshness.py [--root DIR] [--print-paths]', file=sys.stderr)
            return 2
    try:
        if print_paths:
            print('\n'.join(gate_paths(root)))
            return 0
        return check(root, os.environ.get('GITHUB_EVENT_NAME', ''))
    except (GateError, OSError) as refusal:
        print(f'head freshness: {refusal}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
