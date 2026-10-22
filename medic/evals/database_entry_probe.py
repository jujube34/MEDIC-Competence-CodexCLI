"""Real PostgreSQL API CLI environment entry, restart and local HTTP upload."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit,urlunsplit

import httpx
from psycopg import sql

from medic.apps.api.service import Service
from medic.deploy.postgres_backup import connect,table_state
from medic.deploy.backup import fresh_private
from medic.p0.common import now,save
from medic.runtime import build
from medic.runtime.budget import BudgetLedger
from medic.runtime.service_process import ProcessJob
from medic.tests.test_ingestion import word
from medic.tests.test_postgres import database
from medic.tests.test_runtime import fixture


def probe(output):
    output=fresh_private(output); configured=database(); parsed=urlsplit(configured)
    if parsed.scheme not in ('postgres','postgresql') or parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('loopback_postgres_uri_required')
    name='medic_entry_'+uuid.uuid4().hex[:16]+'_test'
    admin=urlunsplit(parsed._replace(path='/postgres')); dsn=urlunsplit(parsed._replace(path='/'+name))
    with connect(admin,autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
    checks={}; process=None; guard=None; log=None

    def start(number):
        nonlocal process,guard,log
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        guard=ProcessJob(); log=(output/f'api-{number}.log').open('xb')
        wrapper=('import sys,runpy; from medic.runtime.service_process import ProcessJob; '
                 'j=ProcessJob(sys.argv.pop(1)); j.close(); runpy.run_module("medic.apps.api",run_name="__main__")')
        args=[sys.executable,'-c',wrapper,guard.name,'--database-env','MEDIC_ENTRY_PROBE_DB',
              '--vault',str(output/'vault'),'--port',str(port)]
        process=subprocess.Popen(args,cwd=build.ROOT.parent,stdout=log,stderr=subprocess.STDOUT,
            env={**os.environ,'MEDIC_ENTRY_PROBE_DB':dsn,'PYTHONUTF8':'1'},creationflags=subprocess.CREATE_NO_WINDOW)
        guard.assign(process)
        checks['database_uri_absent_from_process_arguments']=dsn not in args
        base=f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base,trust_env=False,timeout=10) as client:
            for _ in range(100):
                if process.poll() is not None: raise RuntimeError('api_cli_exited')
                try:
                    if client.get('/health').status_code==200: return base
                except httpx.ConnectError: pass
                time.sleep(.1)
        raise TimeoutError('api_cli_start_timeout')

    def stop():
        nonlocal process,guard,log
        if guard: guard.close(); guard=None
        if process: process.wait(timeout=10); process=None
        if log: log.close(); log=None

    try:
        first=start(1)
        with connect(dsn) as db:
            checks['api_initialized_real_postgres_tables']={'runs','app_users','app_assessments'}<=set(table_state(db))
        service=Service(dsn,output/'vault')
        if not service.runtime.postgres: raise RuntimeError('postgres_backend_required')
        tenant='synthetic-cli-entry'; employee='SYN-ENTRY'
        service.db.provision_employee(tenant,employee,'synthetic-role',synthetic=True)
        token=service.db.provision_user(tenant,'employee',role='employee',employee_ref=employee)
        source=fixture()
        catalog={'title':'Synthetic database entry probe','synthetic':True,'status':'synthetic',
            **{k:source['versions'][k] for k in ('competency_model_version','job_matrix_version','ai_policy_version')},
            'criteria':source['criteria'],'policies':source['policies'],'recommendations':{'synthetic-role':{'M2':'C'}}}
        registry=service.register_catalog(tenant,catalog); auth={'Authorization':'Bearer '+token}
        original=word('Synthetic database entry and restart evidence.')
        with httpx.Client(base_url=first,trust_env=False,timeout=30) as client:
            response=client.post('/employees/'+employee+'/materials',headers=auth,params={'filename':'synthetic.docx','period':'Q4'},content=original)
            response.raise_for_status(); material=response.json()
            response=client.post('/assessments',headers={**auth,'Idempotency-Key':'entry-create-01'},json={
                'employee_ref':employee,'period':'Q4','registry_id':registry,'material_ids':[material['snapshot_id']],'competency_scope':['M2']})
            response.raise_for_status(); assessment=response.json()
        run=assessment['run_id']; ledger=BudgetLedger(service.runtime,run); ledger.reserve(1234)
        before=service.runtime.snapshot(run); budget=ledger.snapshot()
        with connect(dsn) as db:
            checks['http_created_run_in_postgres']=db.execute('SELECT count(*) AS n FROM runs WHERE run_id=%s',(run,)).fetchone()['n']==1
        stop(); second=start(2)
        with httpx.Client(base_url=second,trust_env=False,timeout=15) as client:
            response=client.get('/assessments/'+assessment['assessment_id'],headers=auth); response.raise_for_status()
            checks['http_state_preserved_after_restart']=response.json()==assessment
            response=client.get('/materials/'+material['snapshot_id']+'/source',headers=auth); response.raise_for_status()
            checks['original_available_after_restart']=response.content==original
        checks['run_and_execution_identity_preserved']=service.runtime.snapshot(run)==before
        checks['unsettled_budget_and_deadline_preserved']=ledger.snapshot()==budget
        checks['no_sqlite_runtime_fallback']=not list(output.rglob('runtime.sqlite3'))
        checks['no_model_task_started']=all(t['status']=='queued' for t in before['tasks'])
        # The one ledger reservation above is deliberate test data, not a model call.
        save(output/'run.json',before); save(output/'budget.json',budget)
        identity=build.verify(build.VERSION)
    finally:
        stop()
        with connect(admin,autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
    with connect(admin) as db:
        checks['owned_test_database_removed']=not db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone()
    summary={'created_at':now(),'status':'pass' if all(checks.values()) else 'fail',
        'scope':'real_postgresql_api_cli_environment_http_upload_creation_restart_no_model',
        'checks':checks,'runtime_version':build.VERSION,'manifest_sha256':identity['manifest_sha256'],
        'model_requests_sent':0,'synthetic_ledger_reservations':1,'synthetic_reserved_tokens':1234,
        'production_signoff':False}
    save(output/'verification.json',summary); print(json.dumps(summary),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',required=True)
    args=parser.parse_args(); result=probe(args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
