"""Persistent, single-supervisor local queue; no synthetic result fallback."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from medic.runtime import build
from medic.runtime.store import Store, Denied
from medic.runtime.service_process import ProcessJob


def error_details(exc):
    """Safe driver diagnostics; exception text can contain DSNs or private SQL."""
    details = {'error_type': type(exc).__name__}
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', None)
        if type(code) is int:
            details['sqlite_errorcode'] = code
    return details


class SupervisorLock:
    """One supervisor per database; PG connection loss is fatal, never reconnected."""
    def __init__(self, store):
        self.store = store
        self.connection = self.file = None

    def __enter__(self):
        if self.store.postgres:
            import psycopg
            self.connection = psycopg.connect(self.store.path, autocommit=True, connect_timeout=5,
                options='-c statement_timeout=3000', keepalives=1, keepalives_idle=3, keepalives_interval=1, keepalives_count=3)
            if not self.connection.execute('SELECT pg_try_advisory_lock(719031429)').fetchone()[0]:
                self.connection.close(); self.connection = None
                raise RuntimeError('queue_supervisor_already_running')
        else:
            import msvcrt
            self.file = open(self.store.path+'.supervisor.lock', 'a+b')
            try:
                if os.fstat(self.file.fileno()).st_size==0: self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close(); self.file = None
                raise RuntimeError('queue_supervisor_already_running') from None
        return self

    def check(self):
        if self.connection:
            self.connection.execute('SELECT 1').fetchone()
        elif self.file is None or self.file.closed:
            raise RuntimeError('queue_supervisor_lock_lost')

    def __exit__(self, *exc):
        if self.connection: self.connection.close(); self.connection = None
        if self.file: self.file.close(); self.file = None


class Queue:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            if store.postgres: db.connection.execute('SELECT pg_advisory_xact_lock(719031430)')
            db.execute('CREATE TABLE IF NOT EXISTS service_attempts('
                       'run_id TEXT NOT NULL,attempt INTEGER NOT NULL,status TEXT NOT NULL,'
                       'started DOUBLE PRECISION NOT NULL,finished DOUBLE PRECISION,error TEXT,'
                       'PRIMARY KEY(run_id,attempt))')

    def abandon_previous(self):
        # Caller must own SupervisorLock. Never change task leases or budgets here.
        with self.store.connect() as db:
            db.execute("UPDATE service_attempts SET status='interrupted',finished=?,error='supervisor_restart' WHERE status='running'", (time.time(),))

    def pending(self):
        with self.store.read() as db:
            rows = db.execute("SELECT r.run_id FROM runs r WHERE r.state IN ('planning','evaluating') AND r.report_json IS NULL "
                "AND (SELECT COUNT(*) FROM service_attempts a WHERE a.run_id=r.run_id)<3 "
                "AND NOT EXISTS (SELECT 1 FROM service_attempts a WHERE a.run_id=r.run_id AND a.status IN ('blocked','running')) "
                "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.run_id=r.run_id AND t.status IN ('running','submitted') AND t.lease_until>?) "
                "ORDER BY r.created,r.run_id LIMIT 100", (time.time(),)).fetchall()
        return [r[0] for r in rows]

    def start(self, run):
        self.store.expire_run(run)
        with self.store.connect(run) as db:
            row = db.execute('SELECT state,report_json FROM runs WHERE run_id=?',(run,)).fetchone()
            if not row or row['state'] not in ('planning','evaluating') or row['report_json'] is not None:
                return None
            attempts = db.execute('SELECT attempt,status FROM service_attempts WHERE run_id=? ORDER BY attempt',(run,)).fetchall()
            if len(attempts)>=3 or any(r['status'] in ('blocked','running') for r in attempts): return None
            # An operator may have claimed a task after discovery; defer instead of occupying the service.
            if db.execute("SELECT 1 FROM tasks WHERE run_id=? AND status IN ('running','submitted') AND lease_until>?",(run,time.time())).fetchone(): return None
            number=len(attempts)+1
            try: self.store.verify_build(db,run)
            except (Denied,ValueError):
                status,error='blocked','execution_build_mismatch'
            else: status,error='running',None
            db.execute('INSERT INTO service_attempts VALUES(?,?,?,?,?,?)',(run,number,status,time.time(),None,error))
            self.store.event(db,run,None,'service_attempt_started',{'attempt':number,'status':status,'error_code':error})
            return number if status=='running' else None

    def finish(self, run, attempt, status, error=None):
        if status not in ('completed','interrupted','blocked'): raise ValueError('invalid_service_status')
        with self.store.connect(run) as db:
            changed=db.execute("UPDATE service_attempts SET status=?,finished=?,error=? WHERE run_id=? AND attempt=? AND status='running'",
                               (status,time.time(),error,run,attempt)).rowcount
            if changed: self.store.event(db,run,None,'service_attempt_finished',{'attempt':attempt,'status':status,'error_code':error})


def run_child(store, lock, run, attempt, *, binary, api_example, output, stop):
    target=Path(output)/run/('attempt-'+str(attempt)+'-'+uuid.uuid4().hex[:8])
    target.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'MEDIC_SERVICE_DATABASE':store.path,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    job=ProcessJob(); process=None; stage='child_start'
    try:
        with (target/'process.log').open('xb') as log:
            process=subprocess.Popen([sys.executable,'-m','medic.runtime.service_child',job.name,
                '--resume-run',run,'--database-env','MEDIC_SERVICE_DATABASE','--binary',str(Path(binary).resolve()),
                '--api-example',str(Path(api_example).resolve()),'--output',str(target)],
                cwd=build.ROOT.parent,env=env,stdin=subprocess.PIPE,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process)
            process.stdin.write(b'start\n'); process.stdin.close()
            while process.poll() is None:
                if stop.wait(0.5): return 'interrupted','service_stopped'
                stage='supervisor_check'
                lock.check()
                stage='expire_run'
                if store.expire_run(run): return 'completed','run_deadline_expired'
            stage='run_snapshot'
            snapshot=store.snapshot(run)
            if snapshot['report'] is not None or snapshot['state'] in ('failed','cancelled'):
                return 'completed',None if process.returncode==0 else 'runtime_terminal'
            # Unknown failure is quarantined, not retried indefinitely or hidden as a report.
            return 'blocked','runtime_process_incomplete'
    except Exception as exc:
        print(json.dumps({'event':'service_child_error','stage':stage,'run_id':run,
                          'attempt':attempt,**error_details(exc)}),flush=True)
        raise
    finally:
        job.close()
        if process is not None:
            if process.stdin and not process.stdin.closed: process.stdin.close()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def serve(store, *, binary, api_example, output, stop=None, poll_seconds=2, once=False):
    if os.name!='nt': raise RuntimeError('service_requires_windows_job_support')
    if not 0.1<=poll_seconds<=60: raise ValueError('invalid_poll_interval')
    build.verify(build.VERSION)
    from medic.p0.provider import Config
    from medic.p0.common import digest
    provider=Config.load(api_example)
    profile=json.loads((build.ROOT/'p0/results/provider-profile.json').read_text(encoding='utf-8'))
    baseline=json.loads((build.ROOT/'p0/results/baseline.lock.json').read_text(encoding='utf-8'))
    if digest(provider.base_url)!=profile['upstream_endpoint_sha256'] or provider.model!=profile['model']:
        raise ValueError('service_provider_not_verified')
    if digest(Path(binary).read_bytes())!=baseline['binary']['sha256']:
        raise ValueError('service_binary_not_verified')
    stop=stop or threading.Event()
    with SupervisorLock(store) as lock:
        queue=Queue(store); queue.abandon_previous()
        print(json.dumps({'event':'service_ready','runtime_version':build.VERSION}),flush=True)
        while not stop.is_set():
            lock.check(); build.verify(build.VERSION)
            for run in queue.pending():
                if stop.is_set(): break
                attempt=queue.start(run)
                if attempt is None: continue
                print(json.dumps({'event':'service_started_run','run_id':run,'attempt':attempt}),flush=True)
                try:
                    status,error=run_child(store,lock,run,attempt,binary=binary,api_example=api_example,output=output,stop=stop)
                except BaseException:
                    # Preserve a durable interrupted attempt if DB is available; process tree is already closed.
                    try: queue.finish(run,attempt,'interrupted','service_execution_interrupted')
                    except Exception: pass
                    raise
                queue.finish(run,attempt,status,error)
                print(json.dumps({'event':'service_finished_run','run_id':run,'attempt':attempt,'status':status,'error_code':error}),flush=True)
                break
            if once: return
            stop.wait(poll_seconds)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-env',required=True)
    parser.add_argument('--binary',required=True); parser.add_argument('--api-example',required=True)
    parser.add_argument('--output',required=True); parser.add_argument('--poll-seconds',type=float,default=2)
    parser.add_argument('--once',action='store_true',help='One discovery pass, at most one real assessment')
    args=parser.parse_args()
    database=os.environ.get(args.database_env)
    if not database: parser.error('database environment variable is empty')
    output=Path(args.output).resolve()
    if not any(output.is_relative_to(build.ROOT/p) for p in ('artifacts','private','var')):
        parser.error('service output requires an ignored private MEDIC directory')
    stop=threading.Event()
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    try: serve(Store(database),binary=args.binary,api_example=args.api_example,output=output,
               poll_seconds=args.poll_seconds,stop=stop,once=args.once)
    except Exception as exc:
        # No DSN, endpoint, provider exception bodies or credentials in console errors.
        print(json.dumps({'event':'service_stopped',**error_details(exc)}),flush=True)
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
