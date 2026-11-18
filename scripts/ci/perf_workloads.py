"""The named workloads scripts/ci/perf_gate.py counts.

--list prints their names. NAME --repeat K imports what NAME needs and runs it
K times, each in a fresh directory under TMPDIR, so --repeat 0 is the start-up
the gate subtracts. Every loop is bounded by a count: no network, no token, no
clock decides how much work a run does.
"""
import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

USAGE = json.dumps({'usage': {
    'claude': {'five_hour': {'pct': 62.0, 'pace_pct': 40.0, 'recover_in': '2h10m', 'resets_in': '3h'},
               'weekly': {'pct': 12.0, 'pace_pct': 50.0, 'resets_in': '4d11h'}},
    'kimi': {'five_hour': {'pct': 40.4, 'pace_pct': 40.0}, 'weekly': {'pct': 81.0, 'pace_pct': 30.0, 'recover_in': '3d2h'}},
    'codex': {'five_hour': {}, 'weekly': None}}})


def log_rotation():
    from discord_mb_lib import storage

    def run(tmp):
        writer = storage._ConnectorLogWriter(tmp / 'connector.log', max_bytes=4096, backup_count=3, lock_root=tmp / 'locks')
        try:
            for i in range(400):
                writer.write(f'status update {i:04d} ' + 'x' * 40)
        finally:
            writer.close()
        if not (tmp / 'connector.log.3').exists():
            raise RuntimeError('the log did not rotate')
    return run


def outbox_dispatch():
    import _gateway
    if not _gateway.have_discord():
        sys.exit('outbox-dispatch needs discord.py')
    import sqlite3  # pylint: disable=unused-import  # the connector's log writer imports it at start-up
    from discord_mb_lib import connector
    # Each connector start hashes and stats every package module: start-up work that grows with the package.
    running, stat = connector.running_fingerprint(), connector.package_stat_signature()
    connector.running_fingerprint, connector.package_stat_signature = lambda: running, lambda: stat

    def run(tmp):
        box = tmp / 'state' / 'outbox'
        box.mkdir(parents=True)
        for i in range(20):
            (box / f'{i:03d}.json').write_text(json.dumps({'to': 'nobody', 'subject': f's{i}', 'body': 'b' * 300}), encoding='utf-8')

        async def script(client):
            for _ in range(1000):
                if not any(box.iterdir()):
                    return
                await asyncio.sleep(0)
            raise RuntimeError('the outbox did not drain')
        if len(_gateway.run(tmp, script, outbox=True).bridge.sent) != 20:
            raise RuntimeError('the outbox was not sent')
    return run


def usage_parse():
    from discord_mb_lib import core

    def run(tmp):
        names = [core.usage_board_plan(provider, block, f'{provider} · live')[0]
                 for _ in range(200) for provider, block in json.loads(USAGE)['usage'].items()]
        if None in names[:2] or names[2] is not None:
            raise RuntimeError(f'unexpected board names: {names[:3]}')
    return run


WORKLOADS = {'log-rotation': log_rotation, 'outbox-dispatch': outbox_dispatch, 'usage-parse': usage_parse}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', nargs='?', choices=sorted(WORKLOADS))
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args(argv)
    if args.list:
        print('\n'.join(WORKLOADS))
        return 0
    if args.name is None or args.repeat < 0:
        parser.error('name a workload and a --repeat of 0 or more')
    run = WORKLOADS[args.name]()
    for _ in range(args.repeat):
        tmp = Path(tempfile.mkdtemp(prefix='perf-'))
        try:
            run(tmp)
        finally:
            shutil.rmtree(tmp)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
