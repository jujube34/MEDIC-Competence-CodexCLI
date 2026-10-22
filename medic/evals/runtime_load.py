"""Bounded real-process transaction soak; NOT model or service throughput.

Runs only initial Coordinator leases in fresh private databases. No provider,
Codex worker, role result, score, or published report is created. Budget rows
are explicitly simulated accounting units. Natural lease expiration (never
SQL clock edits), a killed process, and late callbacks exercise Store APIs.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from medic.p0.common import digest, now, save
from medic.runtime import build
from medic.runtime.budget import BudgetLedger, BudgetLimits
from medic.runtime.service import Queue, error_details
from medic.runtime.service_process import ProcessJob
from medic.runtime.store import Denied, Store


DB_ENV = 'MEDIC_TRANSACTION_LOAD_DATABASE'
UNITS = 100


def atomic_save(path, value):
    path = Path(path)
    pending = path.with_suffix(path.suffix + '.tmp')
    save(pending, value)
    pending.replace(path)


class Journal:
    def __init__(self, path):
        self.file = Path(path).open('x', encoding='utf-8', buffering=1)

    def record(self, kind, **fields):
        self.file.write(json.dumps({'time': time.time(), 'kind': kind, **fields}) + '\n')

    def call(self, operation, fn, **fields):
        start = time.perf_counter()
        try:
            value = fn()
        except Exception as exc:
            self.record('operation_error', operation=operation, **error_details(exc), **fields)
            raise
        self.record('operation', operation=operation, milliseconds=(time.perf_counter()-start)*1000, **fields)
        return value


def expected_denial(fn):
    try:
        fn()
    except Denied as exc:
        if str(exc) != 'invalid_or_expired_task_lease':
            raise
        return True
    raise AssertionError('invalid_binding_accepted')


def authorize(store, binding):
    with store.connect(binding['run_id']) as db:
        store.authorize(db, binding)


def child(folder, name, job_name, observer=False, fault=False):
    # Join in the actual interpreter, not just the Windows venv launcher.
    job = ProcessJob(job_name)
    job.close()  # Parent retains the owning handle and KILL_ON_JOB_CLOSE.
    if sys.stdin.readline() != 'start\n':
        return 3
    folder = Path(folder)
    journal = Journal(folder / (name+'.jsonl'))
    try:
        config = json.loads((folder/'config.json').read_text(encoding='utf-8'))
        store = Store(os.environ[DB_ENV]); queue = Queue(store)
        atomic_save(folder/(name+'.ready.json'), {'pid': os.getpid()})
        while not (folder/'start.json').exists():
            if (folder/'stop').exists():
                return 0
            time.sleep(.05)
        start = json.loads((folder/'start.json').read_text())['time']
        # Replay callbacks that were in flight when the killed process died.
        # The private checkpoint includes lease tokens and must never be public.
        old = (json.loads((folder/'fault-ready.json').read_text())['pending_bindings']
               if name == 'worker-replacement' else [])
        rng = random.Random(name)
        while not (folder/'stop').exists() or old:
            # Inspect only after another epoch or cancellation fences the old
            # callback. Merely expired matching failure callbacks are legal.
            for binding in old[:]:
                with store.read(binding['run_id']) as db:
                    task = db.execute('SELECT status,lease_epoch FROM tasks WHERE task_id=?',
                                      (binding['task_id'],)).fetchone()
                if task['lease_epoch'] == binding['lease_epoch']:
                    continue
                journal.call('stale_authorize', lambda: expected_denial(lambda: authorize(store, binding)))
                journal.call('stale_complete', lambda: expected_denial(
                    lambda: store.complete(binding, 'never-submitted', 'no-thread', 'no-turn')))
                journal.call('stale_fail', lambda: store.fail(binding, 'load_stale_callback'))
                journal.record('fenced', run=binding['run_id'], task=binding['task_id'], epoch=binding['lease_epoch'])
                old.remove(binding)
            if (folder/'stop').exists():
                if old:
                    raise AssertionError('unfenced_lease_at_stop')
                break
            if observer:
                journal.call('queue_pending', queue.pending)
                with store.read() as db:
                    rows = db.execute('SELECT run_id FROM runs ORDER BY created DESC LIMIT 20').fetchall()
                if rows:
                    run = rng.choice(rows)[0]
                    snapshot = journal.call('snapshot', lambda: store.snapshot(run))
                    budget = journal.call('budget_snapshot', lambda: BudgetLedger(store, run).snapshot())
                    if len(snapshot['tasks']) != 1 or snapshot['results'] or snapshot['report'] is not None:
                        raise AssertionError('unexpected_model_output')
                    if budget['requests'] > 3 or budget['charged_or_reserved_tokens'] > 3*UNITS:
                        raise AssertionError('accounting_bound_exceeded')
                time.sleep(.1)
                continue
            with store.read() as db:
                candidates = db.execute("SELECT t.run_id FROM tasks t JOIN runs r ON t.run_id=r.run_id "
                    "WHERE t.status='queued' AND r.state='planning' ORDER BY r.created LIMIT 100").fetchall()
            if not candidates:
                time.sleep(.05)
                continue
            run = candidates[0][0]  # Intentionally compete for the same oldest item.
            case = json.loads((folder/'cases'/(run+'.json')).read_text(encoding='utf-8'))
            duplicate = journal.call('idempotent_create', lambda: store.create_run(case['source'],
                limits=config['limits'], creation_key=case['key']))
            if duplicate != run:
                raise AssertionError('creation_key_not_idempotent')
            binding = journal.call('claim', lambda: store.claim_next(run, seconds=config['lease_seconds']))
            if binding is None:
                journal.record('claim_miss', run=run)
                continue
            ledger = BudgetLedger(store, run)
            request = journal.call('reserve', lambda: ledger.reserve(UNITS, active_seconds=config['lease_seconds']))
            journal.record('leased', run=run, task=binding['task_id'], epoch=binding['lease_epoch'], request=request)
            if fault and time.time() >= start + config['duration_seconds']/3:
                atomic_save(folder/'fault-ready.json', {'pid': os.getpid(), 'run': run,
                    'task': binding['task_id'], 'epoch': binding['lease_epoch'], 'request': request,
                    'pending_bindings':old+[binding]})
                while True:
                    time.sleep(.1)  # Parent must terminate this live process.
            journal.call('settle', lambda: ledger.settle(request, UNITS))
            journal.call('settle_replay', lambda: ledger.settle(request, UNITS))
            old.append(binding)
            with store.read() as db:
                other = db.execute('SELECT run_id FROM runs WHERE run_id!=? ORDER BY created LIMIT 1', (run,)).fetchone()
            if other:
                journal.call('cross_run_authorize', lambda: expected_denial(
                    lambda: authorize(store, {**binding, 'run_id': other[0]})))
            time.sleep(.02)
        journal.record('finished', pid=os.getpid())
        return 0
    except Exception as exc:
        journal.record('fatal', **error_details(exc))
        return 1
    finally:
        journal.file.close()


def isolated_postgres_uri(dsn, schema):
    """libpq URI query decoding does not treat '+' as a space."""
    from medic.runtime.database_location import classify
    if classify(dsn)[0] != 'postgres':
        raise ValueError('postgres_uri_required')
    parts = urlsplit(dsn)
    if any(k == 'options' for k, _ in parse_qsl(parts.query)):
        raise ValueError('postgres_options_not_supported_for_isolated_probe')
    query = parse_qsl(parts.query) + [('options', '-c search_path='+schema)]
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def prepare_database(folder, postgres_env):
    if not postgres_env:
        return str(folder/'runtime.sqlite3'), None
    # A fresh schema prevents touching any pre-existing application rows.
    # Retain it for independent audit; no destructive cleanup or DSN in reports.
    import psycopg
    from psycopg import sql
    dsn = os.environ[postgres_env]
    schema = 'load_probe_'+uuid.uuid4().hex
    isolated = isolated_postgres_uri(dsn, schema)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    return isolated, schema


def validate_config(duration, interval, lease, workers):
    for value in (duration, interval, lease):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('finite_numeric_load_configuration_required')
    if not 12 <= duration <= 3600 or not .5 <= interval <= 10 or not 1 <= lease <= 10:
        raise ValueError('load_configuration_out_of_bounds')
    if type(workers) is not int or not 2 <= workers <= 4 or duration/interval < 8:
        raise ValueError('load_configuration_out_of_bounds')


def verify_observations(data):
    """Fail closed on missing coverage as well as broken database invariants."""
    config, cases, workers, fault = (data[k] for k in ('config', 'cases', 'workers', 'fault'))
    expected = math.ceil(config['duration_seconds']/config['arrival_interval_seconds'])
    leased = [e for w in workers for e in w['events'] if e['kind'] == 'leased']
    fenced = [e for w in workers for e in w['events'] if e['kind'] == 'fenced']
    unique = {(e['task'], e['epoch']) for e in leased}
    facts = list(cases.values())
    db_leases = {(c['task_id'], e['data']['lease_epoch']) for c in facts
                 for e in c['events'] if e['kind'] == 'task_leased'}
    checks = {
        'configured_arrivals_completed': len(cases) == expected,
        'sustained_arrival_window': bool(facts) and max(c['created_offset'] for c in facts) >= config['duration_seconds']-config['arrival_interval_seconds']-1,
        'arrival_lag_within_declared_limit': all(0 <= c['arrival_lag'] <= config['max_arrival_lag_seconds'] for c in facts),
        'all_runs_cancelled_after_three_attempts': bool(facts) and all(c['state']=='cancelled' and c['attempts']==3 and c['lease_epoch']==4 for c in facts),
        'exactly_three_unique_leases_per_run': len(leased)==3*expected and len(unique)==len(leased) and unique==db_leases,
        'two_natural_recoveries_per_run': all([e['data']['previous_lease_epoch'] for e in c['events'] if e['kind']=='attempt_requeued']==[1,2] and len(c['history'])==2 for c in facts),
        'no_early_recovery': all(all(h['requeued_at']>=h['lease_until'] for h in c['history']) for c in facts),
        'attempt_budget_denial_before_cancel': all(c['attempt_budget_denied'] and sum(e['kind']=='run_cancelled' for e in c['events'])==1 for c in facts),
        'immutable_input_build_limits_and_deadline': all(c['baseline']==c['final_identity'] for c in facts),
        'idempotent_single_run_and_outbox': all(c['task_count']==1 and c['outbox']==[{'kind':'task_created','delivered':1}] and sum(e['kind']=='run_created' for e in c['events'])==1 for c in facts),
        'no_results_reports_or_execution_identity': all(not c['results'] and c['report'] is None and c['execution_mode'] is None and c['thread_id'] is None and c['turn_id'] is None and not any(e['kind'] in ('result_submitted','task_completed','preview_published','execution_bound') for e in c['events']) for c in facts),
        'no_stale_callback_damage': all(not any(e['kind']=='task_failed' for e in c['events']) for c in facts),
        'all_callbacks_including_killed_worker_fenced': {(e['task'],e['epoch']) for e in fenced} == unique and len(fenced)==len(unique),
        'budget_preserved_including_killed_reservation': all(c['budget']['requests']==3 and c['budget']['charged_or_reserved_tokens']==3*UNITS and c['budget']['unsettled_requests']==int(run==fault.get('run')) and all(r['estimate']==UNITS and (r['charged'] is None if r['request_id']==fault.get('request') else r['charged']==UNITS) for r in c['budget']['ledger']) for run,c in cases.items()),
        'fault_happened_during_load_with_live_lease': fault.get('alive_before_kill') is True and fault.get('live_unsettled_lease') is True and 0<fault.get('offset',-1)<config['duration_seconds'],
        'actual_process_death_confirmed': fault.get('handle_signaled') is True and fault.get('launcher_exited') is True,
        'fault_reservation_matches_claim_and_ledger': sum(e['task']==fault.get('task') and e['epoch']==fault.get('epoch') and e['request']==fault.get('request') and e['run']==fault.get('run') for e in leased)==1 and sum(r['request_id']==fault.get('request') and r['charged'] is None for c in facts for r in c['budget']['ledger'])==1,
        'replacement_claimed_work': any(w['name']=='worker-replacement' and any(e['kind']=='leased' for e in w['events']) for w in workers),
        'all_worker_generations_exercised': len(workers)==config['workers']+2 and all(any(e['kind']=='leased' for e in w['events']) for w in workers if w['name']!='observer'),
        'no_unexpected_worker_or_operation_failures': all(not any(e['kind'] in ('fatal','operation_error') for e in w['events']) and (w['expected_kill'] or w['exit_code']==0 and any(e['kind']=='finished' for e in w['events'])) for w in workers),
        'read_polling_and_cross_run_fencing_observed': all(any(e.get('operation')==op for w in workers for e in w['events']) for op in ('snapshot','budget_snapshot','queue_pending','cross_run_authorize','settle_replay','idempotent_create')),
        'build_unchanged_during_probe': data['build_before']==data['build_after'],
        'harness_unchanged_during_probe': config['harness_sha256']==data['harness_after_sha256'],
    }
    return checks


def execute(folder, *, duration=300, interval=2, lease=3, workers=4, postgres_env=None):
    validate_config(duration, interval, lease, workers)
    if os.name != 'nt':
        raise ValueError('windows_required')
    folder = Path(folder).resolve()
    if not any(folder.is_relative_to(build.ROOT/p) for p in ('artifacts','private','var')):
        raise ValueError('fresh_private_probe_directory_required')
    folder.mkdir(parents=True, exist_ok=False)
    (folder/'cases').mkdir()
    identity = build.verify(build.VERSION)
    # Freeze the harness itself too: eval modules are outside the app manifest.
    shutil.copyfile(__file__, folder/'runtime_load.py')
    source = json.loads((build.ROOT/'evals/fixtures/single_dimension.json').read_text(encoding='utf-8'))
    source['versions']['runtime_version'] = build.VERSION
    save(folder/'source.json', source)
    config = {'duration_seconds':duration, 'arrival_interval_seconds':interval, 'lease_seconds':lease,
              'workers':workers, 'max_arrival_lag_seconds':5, 'drain_seconds':90,
              'limits':BudgetLimits(requests=3,total_tokens=3*UNITS,concurrency=2,wall_seconds=math.ceil(duration+150)).model_dump(),
              'synthetic':True, 'model_calls':0, 'workload':'coordinator_lease_transactions_only',
              'budget_units':'simulated_accounting_units_not_model_tokens', 'harness_sha256':digest(Path(__file__).read_bytes()),
              'source_sha256':digest(source)}
    save(folder/'config.json', config)
    database, schema = prepare_database(folder, postgres_env)
    save(folder/'database-location.json', {'backend':'postgresql' if schema else 'sqlite','schema':schema})
    store = Store(database); Queue(store)
    journal = Journal(folder/'parent.jsonl')
    processes, cases, exhausted = [], {}, set()
    fault = {}; status = 'failed'

    def start_child(name, *, observer=False, fault_worker=False):
        job = ProcessJob(); log = (folder/(name+'.log')).open('xb')
        args = [sys.executable,'-m','medic.evals.runtime_load','child','--folder',str(folder),'--name',name,'--job',job.name]
        if observer: args.append('--observer')
        if fault_worker: args.append('--fault')
        proc = None
        try:
            proc = subprocess.Popen(args,cwd=build.ROOT.parent,env={**os.environ,DB_ENV:database,'PYTHONUTF8':'1'},
                stdin=subprocess.PIPE,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            record = {'name':name,'job':job,'process':proc,'log':log,'expected_kill':False}
            processes.append(record)
            job.assign(proc); proc.stdin.write(b'start\n'); proc.stdin.close()
            deadline = time.monotonic()+30
            while not (folder/(name+'.ready.json')).exists():
                if proc.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('load_child_start_failed')
                time.sleep(.05)
            return record
        except Exception:
            job.close()
            if proc is None: log.close()
            raise

    try:
        victim = start_child('worker-0',fault_worker=True)
        for number in range(1,workers): start_child('worker-'+str(number))
        start_child('observer',observer=True)
        started = time.time(); monotonic_start = time.monotonic()
        atomic_save(folder/'start.json', {'time':started,'recorded_at':now()})
        total = math.ceil(duration/interval); index = 0; next_progress = 0
        while True:
            elapsed = time.monotonic()-monotonic_start
            if elapsed > duration+config['drain_seconds']:
                raise TimeoutError('load_drain_deadline_exceeded')
            for record in processes:
                if not record['expected_kill'] and record['process'].poll() is not None:
                    raise RuntimeError('load_child_exited_early')
            if index<total and elapsed>=index*interval:
                key = folder.name+':'+str(index)
                # Publish immutable input before run visibility to avoid a
                # filesystem/database race in duplicate-create contenders.
                from medic.domain.contracts import AssessmentInput
                payload = AssessmentInput.model_validate(source).model_dump()
                run = 'run-'+digest(['run-creation',key])[:32]
                save(folder/'cases'/(run+'.json'), {'source':source,'key':key})
                actual = journal.call('create', lambda: store.create_run(source,limits=config['limits'],creation_key=key))
                if actual != run: raise AssertionError('unexpected_run_id')
                snapshot = store.snapshot(run)
                cases[run] = {'baseline':{'input_hash':digest(payload),'execution_build':identity,
                    'limits':snapshot['budget_limits'],'deadline':snapshot['deadline']},
                    'created_offset':time.time()-started,'arrival_lag':elapsed-index*interval}
                atomic_save(folder/'creation-baselines.json',cases)
                index += 1
            marker = folder/'fault-ready.json'
            if not fault and marker.exists():
                from medic.evals.service_crash_probe import WindowsProcesses
                fault = json.loads(marker.read_text())
                fault.pop('pending_bindings')  # Never include tokens in public summaries.
                api = WindowsProcesses(); handle = api.open(fault['pid'])
                try:
                    fault['alive_before_kill'] = not api.exited(handle)
                    snap = store.snapshot(fault['run']); budget = BudgetLedger(store,fault['run']).snapshot()
                    task = snap['tasks'][0]
                    fault['live_unsettled_lease'] = task['lease_epoch']==fault['epoch'] and task['status']=='running' and task['lease_until']>time.time() and any(r['request_id']==fault['request'] and r['charged'] is None for r in budget['ledger'])
                    if not fault['alive_before_kill'] or not fault['live_unsettled_lease']:
                        raise AssertionError('fault_preconditions_not_met')
                    fault['offset'] = time.time()-started
                    victim['expected_kill'] = True; victim['job'].close()
                    fault['handle_signaled'] = api.exited(handle,5000)
                    victim['process'].wait(timeout=10)
                    fault['launcher_exited'] = victim['process'].poll() is not None
                    save(folder/'fault.json',fault)
                finally:
                    api.api.CloseHandle(handle)
                start_child('worker-replacement')
            with store.read() as db:
                expired = [r[0] for r in db.execute("SELECT run_id FROM tasks WHERE status='running' AND lease_until<?", (time.time(),))]
            for run in expired:
                try:
                    recovered = store.recover(run)
                    journal.record('recover',run=run,count=len(recovered))
                except Denied as exc:
                    if str(exc)!='task_attempt_budget_exhausted': raise
                    exhausted.add(run); store.cancel(run)
                    journal.record('attempt_budget_denied_then_cancelled',run=run)
            with store.read() as db:
                active = db.execute("SELECT count(*) FROM runs WHERE state!='cancelled'").fetchone()[0]
            if elapsed>=next_progress:
                print(json.dumps({'phase':'load','backend':'postgresql' if schema else 'sqlite',
                    'elapsed_seconds':round(elapsed,1),'created':index,'active':active,'fault_completed':bool(fault)}),flush=True)
                next_progress += 30
            if index==total and not active and elapsed>=duration:
                break
            time.sleep(.05)
        (folder/'stop').write_text('stop',encoding='ascii')
        for record in processes:
            if not record['expected_kill']: record['process'].wait(timeout=20)
        observations = {'config':config,'build_before':identity,'build_after':build.verify(build.VERSION),
                        'cases':cases,'workers':[],'fault':fault,
                        'harness_after_sha256':digest(Path(__file__).read_bytes())}
        for record in processes:
            events = [json.loads(line) for line in (folder/(record['name']+'.jsonl')).read_text().splitlines()]
            observations['workers'].append({'name':record['name'],'exit_code':record['process'].returncode,
                'expected_kill':record['expected_kill'],'events':events})
        for run, case in cases.items():
            snap = store.snapshot(run); task = snap['tasks'][0]
            with store.read(run) as db:
                events = [dict(r) for r in db.execute('SELECT kind,data_json,created FROM events WHERE run_id=? ORDER BY seq',(run,))]
                outbox = [dict(r) for r in db.execute('SELECT kind,delivered FROM outbox WHERE task_id=?',(task['task_id'],))]
            events = [{'kind':e['kind'],'data':json.loads(e['data_json']),'created':e['created']} for e in events]
            history = [{'lease_until':h['lease_until'],'requeued_at':next(e['created'] for e in events if e['kind']=='attempt_requeued' and e['data']['previous_lease_epoch']==h['lease_epoch'])} for h in snap['task_history']]
            case.update({'state':snap['state'],'task_id':task['task_id'],'task_count':len(snap['tasks']),
                'attempts':task['attempt_in_round'],'lease_epoch':task['lease_epoch'],'events':events,'history':history,
                'attempt_budget_denied':run in exhausted,'outbox':outbox,'results':snap['results'],'report':snap['report'],
                'execution_mode':task['execution_mode'],'thread_id':task['thread_id'],'turn_id':task['turn_id'],
                'budget':BudgetLedger(store,run).snapshot(),
                'final_identity':{'input_hash':snap['input_hash'],'execution_build':snap['execution_build'],
                    'limits':snap['budget_limits'],'deadline':snap['deadline']}})
        save(folder/'observations.json',observations)
        checks = verify_observations(observations)
        timings = defaultdict(list)
        for worker in observations['workers']:
            for event in worker['events']:
                if event['kind']=='operation': timings[event['operation']].append(event['milliseconds'])
        latencies = {}
        for operation, values in timings.items():
            values.sort()
            latencies[operation] = {'count':len(values),'p50_ms':round(values[math.ceil(len(values)*.5)-1],3),
                'p95_ms':round(values[math.ceil(len(values)*.95)-1],3),'max_ms':round(max(values),3)}
        status = 'pass' if all(checks.values()) else 'failed'
        result = {'status':status,'scope':'local_transaction_soak_not_production_service_or_model_throughput',
            'backend':'postgresql' if schema else 'sqlite','config':config,'execution_build':identity,
            'observations_sha256':digest((folder/'observations.json').read_bytes()),'checks':checks,
            'run_count':len(cases),'lease_count':sum(c['attempts'] for c in cases.values()),
            'simulated_reserved_units':sum(c['budget']['charged_or_reserved_tokens'] for c in cases.values()),
            'unsettled_simulated_reservations':sum(c['budget']['unsettled_requests'] for c in cases.values()),
            'actual_model_calls':0,'reports_published':0,'operation_latency':latencies,
            'limitations':['Only Coordinator lease transactions; no full task DAG or role execution.',
                'Bounded local duration and arrival rate; no customer capacity/SLO acceptance.',
                'Queue discovery polled; production serve() and gateway not executed.',
                'One worker job killed; database and machine remain available.']}
        save(folder/'verification.json',result)
        print(json.dumps({'phase':'complete','status':status,'checks':checks}),flush=True)
        return result
    except Exception as exc:
        save(folder/'failure.json',{'status':'failed',**error_details(exc),'recorded_at':now()})
        raise
    finally:
        (folder/'stop').write_text('stop',encoding='ascii')
        for record in processes:
            record['job'].close()
            try: record['process'].wait(timeout=10)
            except subprocess.TimeoutExpired: record['process'].kill(); record['process'].wait(timeout=5)
            record['log'].close()
        journal.file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    run = sub.add_parser('run'); run.add_argument('--folder',required=True)
    run.add_argument('--duration',type=float,default=300); run.add_argument('--interval',type=float,default=2)
    run.add_argument('--lease',type=float,default=3); run.add_argument('--workers',type=int,default=4)
    run.add_argument('--postgres-env')
    worker = sub.add_parser('child'); worker.add_argument('--folder',required=True)
    worker.add_argument('--name',required=True); worker.add_argument('--job',required=True)
    worker.add_argument('--observer',action='store_true'); worker.add_argument('--fault',action='store_true')
    args = parser.parse_args()
    if args.command=='child':
        return child(args.folder,args.name,args.job,args.observer,args.fault)
    result = execute(args.folder,duration=args.duration,interval=args.interval,lease=args.lease,
                     workers=args.workers,postgres_env=args.postgres_env)
    return 0 if result['status']=='pass' else 1


if __name__=='__main__':
    raise SystemExit(main())
