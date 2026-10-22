"""Native PostgreSQL restore with real loopback HTTP and explicit synthetic fixtures."""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx

from medic.apps.api.service import Service
from medic.deploy import postgres_backup as backup
from medic.deploy.backup import fresh_private, file_info
from medic.p0.common import digest, now, save
from medic.runtime import build
from medic.runtime.service_process import ProcessJob
from medic.tests.test_postgres_backup import PostgreSQLBackupContracts


def probe(output):
    output=fresh_private(output)
    case=PostgreSQLBackupContracts(); guard=ProcessJob(); process=None
    checks={}; manifest=None
    try:
        case.setUp(); case.seed_human_final()
        expected=case.service.detail(case.ep,case.assessment['assessment_id'])
        history=case.service.history(case.mp,case.assessment['assessment_id'])
        with backup.connect(case.dsn) as db: original=backup.table_state(db)
        start=time.monotonic(); manifest=case.create(); backup_seconds=time.monotonic()-start
        start=time.monotonic(); restored=case.restore(); restore_seconds=time.monotonic()-start
        with backup.connect(case.restored_dsn) as db: copied=backup.table_state(db)
        checks['all_non_session_tables_identical']={k:v for k,v in original.items() if k!='app_tokens'}=={k:v for k,v in copied.items() if k!='app_tokens'}
        checks['old_sessions_removed']=copied['app_tokens']['rows']==0
        save(output/'table-hashes.json',{'original':original,'restored_before_new_sessions':copied})
        service=Service(case.restored_dsn,case.recovered/'vault')
        employee=service.db.provision_user(case.tenant,'employee',role='employee',employee_ref=case.employee)
        manager=service.db.provision_user(case.tenant,'manager',role='manager')
        outsider=service.db.provision_user(case.tenant,'http-outsider',role='manager')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        wrapper=('import sys,os; from medic.runtime.service_process import ProcessJob; '
                 'j=ProcessJob(sys.argv[1]); j.close(); '
                 'import uvicorn; from medic.apps.api.app import create_app; '
                 'uvicorn.run(create_app(os.environ["MEDIC_RESTORE_PROBE_DSN"],sys.argv[2]),'
                 'host="127.0.0.1",port=int(sys.argv[3]),access_log=False,proxy_headers=False)')
        with (output/'restored-api.log').open('xb') as log:
            process=subprocess.Popen([sys.executable,'-c',wrapper,guard.name,str(case.recovered/'vault'),str(port)],
                cwd=build.ROOT.parent,env={**os.environ,'MEDIC_RESTORE_PROBE_DSN':case.restored_dsn,'PYTHONUTF8':'1'},
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            guard.assign(process)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=20) as client:
                for _ in range(100):
                    if process.poll() is not None: raise RuntimeError('restored_api_exited')
                    try:
                        if client.get('/health').status_code==200: break
                    except httpx.ConnectError: pass
                    time.sleep(.1)
                else: raise TimeoutError('restored_api_not_ready')
                auth=lambda token:{'Authorization':'Bearer '+token}
                endpoint='/assessments/'+case.assessment['assessment_id']
                checks['old_tokens_rejected_over_http']=all(client.get('/me',headers=auth(t)).status_code==401 for t in (case.employee_token,case.manager_token))
                response=client.get(endpoint,headers=auth(employee)); response.raise_for_status()
                checks['final_employee_view_preserved']=response.json()==expected
                checks['failed_fixture_ai_gate_preserved']=response.json()['final']['ai_gate']['passed'] is False
                response=client.get(endpoint,headers=auth(manager)); response.raise_for_status()
                checks['manager_grant_preserved']=response.json()['permissions']['can_review'] is True
                checks['ungranted_manager_denied']=client.get(endpoint,headers=auth(outsider)).status_code==404
                response=client.get(endpoint+'/history',headers=auth(manager)); response.raise_for_status()
                checks['full_human_history_preserved']=response.json()==history
                mid=case.material['snapshot_id']
                response=client.get('/materials/'+mid+'/source',headers=auth(employee)); response.raise_for_status()
                checks['original_download_identical']=response.content==case.doc.read_bytes()
                response=client.get('/materials/'+mid+'/blocks',headers=auth(employee)); response.raise_for_status()
                block=response.json()['blocks'][0]
                response=client.get('/materials/'+mid+'/blocks/'+block['evidence_id'],headers=auth(manager)); response.raise_for_status()
                checks['evidence_location_identical']=response.json()['block']==block
        guard.close(); process.wait(timeout=10)
        with backup.connect(case.restored_dsn) as db:
            checks['no_model_requests']=db.execute('SELECT count(*) AS n FROM model_requests').fetchone()['n']==0
        with backup.connect(case.dsn) as db:
            checks['source_database_unchanged']=backup.table_state(db)==original
        shutil.copytree(case.target,output/'backup')
        shutil.copytree(case.recovered,output/'restored-files')
        summary={'created_at':now(),'status':'pass' if all(checks.values()) else 'fail',
            'scope':'native_postgresql_restore_real_http_synthetic_seeded_report_no_model_calls',
            'checks':checks,'runtime_version':build.VERSION,'execution_manifest_sha256':build.verify(build.VERSION)['manifest_sha256'],
            'backup_tool_sha256':manifest['backup_tool_sha256'],'native_tools':manifest['native_tools'],
            'server_version':manifest['server_version'],'counts':manifest['counts'],
            'backup_manifest_sha256':restored['backup_manifest_sha256'],
            'table_hashes_sha256':file_info(output/'table-hashes.json')['sha256'],
            'backup_seconds':round(backup_seconds,3),'restore_seconds':round(restore_seconds,3),
            'report_is_explicit_fixture':True,'human_review_is_synthetic':True,'model_requests':0,
            'external_protocol_archives_restored':False,'production_recovery_objectives_verified':False,
            'business_signoff':False}
    finally:
        guard.close()
        if process is not None: process.wait(timeout=10)
        if not case.doCleanups(): raise RuntimeError('owned_test_cleanup_failed')
    with backup.connect(case.admin) as db:
        checks['owned_test_databases_removed']=not db.execute('SELECT 1 FROM pg_database WHERE datname=ANY(%s)',(case.owned,)).fetchone()
    summary['status']='pass' if all(checks.values()) else 'fail'
    save(output/'verification.json',summary)
    print(json.dumps(summary),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',required=True)
    args=parser.parse_args(); result=probe(args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
