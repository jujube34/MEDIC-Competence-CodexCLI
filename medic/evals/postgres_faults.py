"""Explicit, synthetic PostgreSQL crash probe; never controls a server itself.

Prepare, hold an uncommitted transaction, stop the isolated test server externally,
probe unavailable, restart externally, then verify. Checkpoint contains a lease
credential and must remain in the ignored artifacts directory.
"""
import argparse
import json
import os
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from medic.p0.common import ROOT, save
from medic.runtime.budget import BudgetLedger
from medic.runtime.store import Denied, Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepare', 'hold', 'unavailable', 'verify'])
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--result')
    args = parser.parse_args()
    checkpoint = Path(args.checkpoint).resolve()
    if not checkpoint.is_relative_to((ROOT / 'medic/artifacts').resolve()):
        parser.error('checkpoint must remain under medic/artifacts')
    dsn = os.environ['MEDIC_TEST_POSTGRES_DSN']
    info = conninfo_to_dict(dsn)
    if not info.get('dbname', '').endswith('_test') or info.get('host') not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('requires an explicit local *_test database')
    if args.stage == 'prepare':
        if checkpoint.exists():
            parser.error('checkpoint already exists; use a new directory for a new crash probe')
        store = Store(dsn)
        source = json.loads((ROOT / 'medic/evals/fixtures/single_dimension.json').read_text(encoding='utf-8'))
        run = store.create_run(source)
        binding = store.claim_next(run)
        reservation = BudgetLedger(store, run).reserve(1000)
        with psycopg.connect(dsn) as db:
            start, version = db.execute('SELECT pg_postmaster_start_time(), version()').fetchone()
        save(checkpoint, {'run_id': run, 'binding': binding, 'reservation': reservation,
                          'postmaster_start': start.isoformat(), 'server_version': version})
        print(json.dumps({'stage': 'prepared', 'run_id': run}))
        return
    data = json.loads(checkpoint.read_text(encoding='utf-8'))
    run = data['run_id']
    if args.stage == 'hold':
        # Raw connection deliberately leaves a write uncommitted across the crash.
        try:
            with psycopg.connect(dsn) as db:
                db.execute("INSERT INTO events(run_id,kind,data_json,created) VALUES(%s,%s,%s,%s)",
                           (run, 'fault_probe_uncommitted', '{}', time.time()))
                save(checkpoint.with_suffix('.ready.json'), {'transaction_open': True})
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    db.execute('SELECT 1')
                raise RuntimeError('crash was not injected before probe deadline')
        except psycopg.OperationalError:
            save(checkpoint.with_suffix('.disconnected.json'), {'active_connection_interrupted': True})
            print('active_connection_interrupted')
        return
    if args.stage == 'unavailable':
        try:
            Store(dsn)
        except psycopg.OperationalError:
            data['unavailable_failed_closed'] = True
            save(checkpoint, data)
            print('database_unavailable_failed_closed')
            return
        raise AssertionError('database was still available')
    store = Store(dsn)
    snapshot = store.snapshot(run)
    usage = BudgetLedger(store, run).snapshot()
    with psycopg.connect(dsn) as db:
        start = db.execute('SELECT pg_postmaster_start_time()').fetchone()[0]
    checks = {
        'server_restarted': start.isoformat() != data['postmaster_start'],
        'active_connection_interrupted': checkpoint.with_suffix('.disconnected.json').exists(),
        'database_unavailable_failed_closed': data.get('unavailable_failed_closed') is True,
        'committed_task_preserved': snapshot['tasks'][0]['lease_epoch'] == 1 and snapshot['tasks'][0]['status'] == 'running',
        'uncommitted_write_rolled_back': not any(e['kind'] == 'fault_probe_uncommitted' for e in snapshot['events']),
        'reservation_preserved': usage['requests'] == 1 and usage['charged_or_reserved_tokens'] == 1000 and usage['unsettled_requests'] == 1,
        'no_report_published': snapshot['report'] is None,
    }
    # Force only this synthetic lease's expiry to avoid waiting eight minutes.
    with store.connect(run) as db:
        db.execute('UPDATE tasks SET lease_until=0 WHERE run_id=?', (run,))
    recovered = store.recover(run)
    replacement = store.claim_next(run)
    checks['lease_recovered_once'] = recovered == [data['binding']['task_id']] and replacement['lease_epoch'] == 2
    try:
        with store.connect(run) as db:
            store.authorize(db, data['binding'])
        checks['old_worker_fenced'] = False
    except Denied:
        checks['old_worker_fenced'] = True
    store.cancel(run)
    result = {'status': 'pass' if all(checks.values()) else 'fail', 'run_id': run,
              'server_version': data['server_version'], 'checks': checks,
              'scope': 'synthetic_database_crash_recovery_not_model_quality_or_production_HA'}
    if args.result:
        save(Path(args.result), result)
    print(json.dumps(result))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
