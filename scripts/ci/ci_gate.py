#!/usr/bin/env python3
"""Fold every job of the covered gate workflows on a pull request head into one check.

Every POLL_SECONDS it reads the head's pull_request runs; push runs on the same
SHA, this workflow and pr-gate are never read. Green once the newest run of
every COVERED workflow has completed and each of its jobs succeeded or is a
skipped `guard / ` job. Red on any other job, a run with no jobs, a workflow
not finished by the deadline, or an API read failing past its retries.
"""
import json
import os
import subprocess
import sys
import time

# A literal: gate_base_freshness.py reads it from the tree.
COVERED = ('.github/workflows/tests.yml', '.github/workflows/lint.yml', '.github/workflows/types.yml',
           '.github/workflows/audit.yml', '.github/workflows/codeql.yml', '.github/workflows/actionlint.yml',
           '.github/workflows/secrets.yml', '.github/workflows/perf.yml')
GUARD_PREFIX = 'guard / '
POLL_SECONDS = 45
DEADLINE_MINUTES = 55
RETRIES = 3
RETRY_SECONDS = 10
CALL_SECONDS = 30


class GateError(Exception):
    """An API read failed past its retries."""


def api(endpoint):
    command = ['gh', 'api', '-H', 'Cache-Control: no-cache', '--paginate', '--slurp', endpoint]
    reason = ''
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(RETRY_SECONDS)
        try:
            done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=CALL_SECONDS)
            if done.returncode == 0:
                return json.loads(done.stdout)
            reason = done.stderr.strip() or f'exit {done.returncode}'
        except subprocess.TimeoutExpired:
            reason = f'no answer within {CALL_SECONDS} s'
        except ValueError as error:
            reason = f'unreadable output: {error}'
        print(f'ci-gate: {endpoint} failed (attempt {attempt + 1}): {reason}', file=sys.stderr)
    raise GateError(f'{endpoint} failed {RETRIES + 1} times: {reason}')


def newest_runs(repo, sha):
    """{covered path: its newest pull_request run on sha}."""
    runs = {}
    for page in api(f'repos/{repo}/actions/runs?head_sha={sha}&event=pull_request&per_page=100'):
        for run in page['workflow_runs']:
            path = run['path']
            if path in COVERED and run['event'] == 'pull_request' and run['id'] > runs.get(path, {'id': -1})['id']:
                runs[path] = run
    return runs


def run_jobs(repo, run_id):
    return [job for page in api(f'repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100')
            for job in page['jobs']]


def verdict(runs, jobs):
    """(final, failures): final once every covered workflow has a completed run."""
    final, failures = True, []
    for path in COVERED:
        run = runs.get(path)
        if not run or run['status'] != 'completed':
            final = False
            failures.append(f"{path}: run {run['status']}" if run else f'{path}: no run')
            continue
        listed = jobs.get(run['id'], [])
        if not listed:
            failures.append(f'{path}: no jobs')
        failures += [f"{path}: {job['name']} {job['conclusion']}" for job in listed
                     if job['conclusion'] != 'success'
                     and not (job['conclusion'] == 'skipped' and job['name'].startswith(GUARD_PREFIX))]
    return final, failures


def report(runs, jobs, failures, note):
    lines = [f"## ci-gate: {'red' if failures else 'green'}", '', note, *(f'- {failure}' for failure in failures),
             '', '| workflow | run | job | conclusion |', '| --- | --- | --- | --- |']
    for path in COVERED:
        run = runs.get(path)
        listed = jobs.get(run['id'], []) if run else []
        lines += [f"| {path} | {run['id']} | {job['name']} | {job['conclusion']} |" for job in listed] or [
            f"| {path} | {run['status'] if run else 'no run'} | | |"]
    text = '\n'.join(lines) + '\n'
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write(text)


def main():
    repo, sha = os.environ['DISCORD_MAILBOX_REPO'], os.environ['DISCORD_MAILBOX_HEAD_SHA']
    minutes = float(os.environ.get('DISCORD_MAILBOX_CI_GATE_DEADLINE_MINUTES', DEADLINE_MINUTES))
    deadline = time.monotonic() + 60 * minutes
    runs, jobs, attempts, failures = {}, {}, {}, []
    try:
        while True:
            runs = newest_runs(repo, sha)
            for run in runs.values():
                if run['status'] == 'completed' and attempts.get(run['id']) != run['run_attempt']:
                    jobs[run['id']], attempts[run['id']] = run_jobs(repo, run['id']), run['run_attempt']
            final, failures = verdict(runs, jobs)
            left = deadline - time.monotonic()
            if final or left <= 0:
                note = 'Every covered workflow finished.' if final else f'Not every covered workflow finished within {minutes:g} minutes.'
                break
            print('ci-gate: waiting; ' + '; '.join(failures), flush=True)
            time.sleep(min(POLL_SECONDS, left))
    except (GateError, KeyError, TypeError) as error:
        note, failures = 'The Actions API read failed.', [*failures, f'Actions API: {error!r}']
    report(runs, jobs, failures, note)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
