"""Restore a completed synthetic HTTP/model workspace and exercise its real API."""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

from medic.apps.api.database import ApplicationDB
from medic.deploy.backup import create, restore, read_db, file_info, fresh_private
from medic.p0.common import digest, now, save
from medic.runtime import build
from medic.runtime.service_process import ProcessJob


def logical_tables(database):
    """Hash every row/column, with sessions separately visible, without raw output."""
    with closing(read_db(database)) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = db.execute('SELECT * FROM ' + quoted)
            rows = sorted(json.dumps(list(row), ensure_ascii=False, separators=(',', ':')) for row in cursor)
            result[name] = {'rows': len(rows), 'sha256': digest({'columns': [c[0] for c in cursor.description], 'rows': rows})}
        return result


def probe(source, output):
    source = Path(source).resolve(); output = fresh_private(output)
    config = json.loads((source / 'local-access.json').read_text(encoding='utf-8'))
    if config.get('synthetic') is not True:
        raise ValueError('synthetic_workspace_required')
    database = Path(config['database']).resolve(); vault = Path(config['vault']).resolve()
    if database != source / 'runtime.sqlite3' or vault != source / 'vault':
        raise ValueError('source_configuration_outside_workspace')
    with closing(read_db(database)) as db:
        rows = db.execute('SELECT assessment_id,run_id,human_status FROM app_assessments').fetchall()
        if len(rows) != 1 or rows[0]['human_status'] != 'finalized':
            raise ValueError('one_finalized_synthetic_assessment_required')
        assessment, run = rows[0]['assessment_id'], rows[0]['run_id']
        record = db.execute('SELECT * FROM runs WHERE run_id=?', (run,)).fetchone()
        source_input = json.loads(record['input_json'])
        if not source_input['synthetic'] or not record['report_json']:
            raise ValueError('completed_synthetic_run_required')
        report_hash = digest(json.loads(record['report_json']))
        materials = [dict(r) for r in db.execute('SELECT * FROM app_materials')]
        old_requests = db.execute('SELECT count(*) FROM model_requests').fetchone()[0]
    baseline = logical_tables(database)
    before = time.monotonic()
    manifest = create(database, vault, output / 'backup')
    backup_seconds = time.monotonic() - before
    before = time.monotonic()
    restored = restore(output / 'backup', output / 'restored')
    restore_seconds = time.monotonic() - before
    restored_db = output / 'restored/runtime.sqlite3'
    actual = logical_tables(restored_db)
    checks = {'all_non_session_tables_identical':
        {k:v for k,v in actual.items() if k!='app_tokens'} == {k:v for k,v in baseline.items() if k!='app_tokens'},
        'copied_sessions_empty': actual['app_tokens']['rows']==0,
        'source_database_unchanged': logical_tables(database)==baseline,
        'only_database_and_vault_files_backed_up': all(p in ('runtime.sqlite3','vault/snapshots.sqlite3') or p.startswith('vault/') and '/blobs/' in p for p in manifest['files'])}
    if not all(checks.values()):
        raise RuntimeError('restored_data_mismatch')
    save(output / 'table-hashes.json', {'source': baseline, 'restored_before_new_sessions': actual})
    app = ApplicationDB(restored_db)
    tokens = {role: app.provision_user(config['tenant'], role, role=role,
              employee_ref=config['employee_ref'] if role=='employee' else None) for role in ('employee','manager','director')}
    outsider = app.provision_user(config['tenant'], 'backup-scope-probe', role='manager')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    guard = ProcessJob(); server = None
    try:
        with (output / 'restored-api.log').open('xb') as log:
            wrapper = ('import sys,runpy; from medic.runtime.service_process import ProcessJob; '
                       'j=ProcessJob(sys.argv.pop(1)); j.close(); '
                       'runpy.run_module("medic.apps.api",run_name="__main__")')
            server = subprocess.Popen([sys.executable, '-c', wrapper, guard.name,
                '--database', str(restored_db), '--vault', str(output / 'restored/vault'), '--port', str(port)],
                cwd=build.ROOT.parent, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, 'PYTHONUTF8':'1'})
            guard.assign(server)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=20, trust_env=False) as client:
                for _ in range(100):
                    if server.poll() is not None: raise RuntimeError('restored_api_exited')
                    try:
                        if client.get('/health').status_code==200: break
                    except httpx.ConnectError: pass
                    time.sleep(.1)
                else: raise TimeoutError('restored_api_not_ready')
                auth = lambda token: {'Authorization': 'Bearer '+token}
                checks['old_tokens_rejected_over_http'] = all(client.get('/me',headers=auth(token)).status_code==401 for token in config['tokens'].values())
                employee = auth(tokens['employee']); manager = auth(tokens['manager'])
                response = client.get('/assessments/'+assessment, headers=employee); response.raise_for_status()
                detail = response.json()
                checks['final_employee_view_preserved'] = detail == json.loads((source / 'api-final.json').read_text(encoding='utf-8'))
                response = client.get('/assessments/'+assessment, headers=manager); response.raise_for_status()
                checks['manager_grant_and_final_status_preserved'] = response.json()['permissions']['can_review'] and response.json()['human_status']=='finalized'
                checks['ungranted_manager_denied'] = client.get('/assessments/'+assessment,headers=auth(outsider)).status_code==404
                checks['employee_cannot_review_final_result'] = client.post('/assessments/'+assessment+'/reviews',headers={**employee,'Idempotency-Key':'backup-deny-review'},json={
                    'expected_revision':detail['revision'], 'decisions':[{'competency_id':'M2','disposition':'insufficient_evidence','grade':None,'reason':'Synthetic restore denial verification'}]}).status_code==403
                response = client.get('/assessments/'+assessment+'/history',headers=manager); response.raise_for_status()
                with closing(read_db(database)) as db:
                    historical = db.execute('SELECT count(*) FROM app_events WHERE assessment_id=?',(assessment,)).fetchone()[0]
                checks['review_history_count_preserved'] = len(response.json())==historical
                blocks_ok = []; original_ok = []
                for material in materials:
                    mid = material['snapshot_id']
                    response = client.get('/materials/'+mid,headers=employee); response.raise_for_status()
                    snapshot = response.json()
                    response = client.get('/materials/'+mid+'/source',headers=employee); response.raise_for_status()
                    original_ok.append(digest(response.content)==snapshot['source_sha256'])
                    response = client.get('/materials/'+mid+'/blocks',params={'limit':100},headers=employee); response.raise_for_status()
                    page = response.json(); blocks = page['blocks']
                    for offset in range(100,page['total'],100):
                        response=client.get('/materials/'+mid+'/blocks',params={'limit':100,'offset':offset},headers=employee)
                        response.raise_for_status(); blocks.extend(response.json()['blocks'])
                    blocks_ok.append(len(blocks)==page['total'])
                    for block in blocks:
                        response = client.get('/materials/'+mid+'/blocks/'+block['evidence_id'],headers=manager)
                        response.raise_for_status()
                        located=response.json()['block']
                        blocks_ok.append(located['quote']==block['quote'] and located['span_hash']==block['span_hash'])
                checks['original_files_download_and_hash'] = bool(original_ok) and all(original_ok)
                checks['all_evidence_links_resolve_verbatim'] = bool(blocks_ok) and all(blocks_ok)
    finally:
        guard.close()
        if server is not None: server.wait(timeout=10)
    with closing(read_db(restored_db)) as db:
        checks['ai_report_unchanged'] = digest(json.loads(db.execute('SELECT report_json FROM runs WHERE run_id=?',(run,)).fetchone()[0]))==report_hash
        checks['no_new_model_requests'] = db.execute('SELECT count(*) FROM model_requests').fetchone()[0]==old_requests
    checks['source_still_unchanged'] = logical_tables(database)==baseline
    summary = {'created_at':now(), 'status':'pass' if all(checks.values()) else 'fail',
        'scope':'local_sqlite_restore_real_http_existing_real_model_synthetic_finalized_assessment',
        'checks':checks, 'run_id':run, 'runtime_version':build.VERSION,
        'manifest_sha256':build.verify(build.VERSION)['manifest_sha256'],
        'backup_tool_sha256':manifest['backup_tool_sha256'], 'counts':manifest['counts'],
        'backup_manifest_sha256':restored['backup_manifest_sha256'],
        'table_hashes_sha256':file_info(output/'table-hashes.json')['sha256'],
        'backup_seconds':round(backup_seconds,3), 'restore_seconds':round(restore_seconds,3),
        'sessions_invalidated':restored['copied_sessions_invalidated'], 'model_requests_added':0,
        'external_protocol_archive_restored':False, 'postgresql_restore_verified':False,
        'production_recovery_objectives_verified':False, 'business_signoff':False}
    raw = json.dumps(summary)
    if any(token in raw for token in config['tokens'].values()): raise RuntimeError('summary_contains_secret')
    save(output/'verification.json',summary)
    print(json.dumps(summary),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True); parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=probe(args.source,args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
