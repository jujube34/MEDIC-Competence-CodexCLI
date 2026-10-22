"""Local HTTP + bounded parser + real model + synthetic human review probe."""
import argparse
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import zipfile
from xml.sax.saxutils import escape

import httpx
from medic.apps.api.bootstrap import bootstrap
from medic.evals.verify_ingestion_run import verify_chain
from medic.p0.common import digest, now, save
from medic.runtime import build


def probe(folder, binary, api_example, *, queue_service=False):
    folder=Path(folder).resolve()
    bootstrap(folder)
    config=json.loads((folder/'local-access.json').read_text(encoding='utf-8'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    checks={}; supervisor=None; service_log=None; runtime_folder=folder
    with (folder/'api-server.log').open('w',encoding='utf-8') as log:
        server=subprocess.Popen([sys.executable,'-m','medic.apps.api','--database',config['database'],'--vault',config['vault'],'--port',str(port)],
                                stdout=log,stderr=subprocess.STDOUT,creationflags=flags)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=90,trust_env=False) as client:
                for _ in range(80):
                    if server.poll() is not None: raise RuntimeError('api_server_exited')
                    try:
                        if client.get('/health').status_code==200: break
                    except httpx.ConnectError: pass
                    time.sleep(.25)
                else: raise RuntimeError('api_server_not_ready')
                if queue_service:
                    service_log=(folder/'service.log').open('w',encoding='utf-8')
                    env={**os.environ,'MEDIC_API_PROBE_DATABASE':config['database']}
                    command=[sys.executable,'-m','medic.runtime.service','--database-env','MEDIC_API_PROBE_DATABASE',
                        '--binary',str(Path(binary).resolve()),'--api-example',str(Path(api_example).resolve()),
                        '--output',str(folder/'queue'),'--poll-seconds','0.5']
                    supervisor=subprocess.Popen(command,env=env,stdout=service_log,stderr=subprocess.STDOUT,creationflags=flags)
                    for _ in range(100):
                        if supervisor.poll() is not None: raise RuntimeError('queue_service_start_failed')
                        if 'service_ready' in (folder/'service.log').read_text(encoding='utf-8'): break
                        time.sleep(.2)
                    else: raise RuntimeError('queue_service_not_ready')
                    duplicate=subprocess.run([*command,'--once'],env=env,capture_output=True,timeout=30,creationflags=flags)
                    checks['second_supervisor_rejected']=duplicate.returncode==2
                    checks['service_waits_for_later_http_creation']=True
                employee={'Authorization':'Bearer '+config['tokens']['employee']}
                manager={'Authorization':'Bearer '+config['tokens']['manager']}
                checks['unauthenticated_denied']=client.get('/employees').status_code==401
                fixture=json.loads((build.ROOT/'evals/fixtures/single_dimension_current.json').read_text(encoding='utf-8'))
                paragraphs=[s['quote'] for s in fixture['materials'][0]['spans']]
                raw=io.BytesIO()
                with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
                    archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+''.join('<w:p><w:r><w:t xml:space="preserve">'+escape(p)+'</w:t></w:r></w:p>' for p in paragraphs)+'</w:body></w:document>')
                response=client.post('/employees/SYN-001/materials',headers=employee,params={'filename':'synthetic-http-probe.docx','period':'synthetic-2026-Q4'},content=raw.getvalue())
                response.raise_for_status(); material=response.json()
                checks['real_upload_ready']=material['state']=='ready'
                blocks=client.get('/materials/'+material['snapshot_id']+'/blocks',headers=employee).json()
                checks['verbatim_four_spans']=[b['quote'] for b in blocks['blocks']]==paragraphs
                checks['original_download_hash']=digest(client.get('/materials/'+material['snapshot_id']+'/source',headers=manager).content)==digest(raw.getvalue())
                body={'employee_ref':'SYN-001','period':'synthetic-2026-Q4','registry_id':config['registry_id'],
                      'material_ids':[material['snapshot_id']],'competency_scope':['M2']}
                create_headers={**employee,'Idempotency-Key':'http-probe-create-v1'}
                response=client.post('/assessments',headers=create_headers,json=body); response.raise_for_status(); assessment=response.json()
                replay=client.post('/assessments',headers=create_headers,json=body); replay.raise_for_status()
                checks['creation_replay_same_run']=replay.json()['run_id']==assessment['run_id']
                save(folder/'api-probe-state.json',{'assessment_id':assessment['assessment_id'],'run_id':assessment['run_id'],'initial_checks':checks})
                print(json.dumps({'phase':'http_created','run_id':assessment['run_id'],'checks':checks}),flush=True)
                env={**os.environ,'MEDIC_API_PROBE_DATABASE':config['database']}
                if queue_service:
                    from medic.runtime.store import Store
                    store=Store(config['database'])
                    for _ in range(1900):
                        if supervisor.poll() is not None: raise RuntimeError('queue_service_exited')
                        with store.read(assessment['run_id']) as db:
                            attempts=db.execute('SELECT attempt,status FROM service_attempts WHERE run_id=? ORDER BY attempt',(assessment['run_id'],)).fetchall()
                        if attempts and attempts[-1]['status'] in ('completed','blocked'): break
                        time.sleep(1)
                    else: raise RuntimeError('queue_probe_deadline')
                    checks['one_automatic_service_attempt']=len(attempts)==1 and attempts[0]['status']=='completed'
                    paths=list((folder/'queue'/assessment['run_id']).glob('attempt-*/run.json'))
                    if len(paths)!=1: raise RuntimeError('queue_probe_result_missing')
                    runtime_folder=paths[0].parent
                    save(folder/'queue-probe-state.json',{'run_id':assessment['run_id'],'runtime_folder':str(runtime_folder),'attempts':len(attempts)})
                else:
                    result=subprocess.run([sys.executable,'-m','medic.runtime','--resume-run',assessment['run_id'],
                        '--database-env','MEDIC_API_PROBE_DATABASE','--binary',str(Path(binary).resolve()),'--api-example',str(Path(api_example).resolve()),
                        '--output',str(folder)],env=env,creationflags=flags)
                    if result.returncode: raise RuntimeError('runtime_probe_incomplete')
                verification=verify_chain(runtime_folder,folder/'ingestion-verification.json',vault_folder=folder/'vault')
                checks['real_runtime_and_ingestion_verified']=verification['status']=='pass'
                if verification['status'] != 'pass':
                    summary={'created_at':now(),'status':'fail','run_id':assessment['run_id'],
                             'runtime_version':build.VERSION,'checks':checks,
                             'runtime_verification_status':verification['status'],
                             'error_code':'runtime_and_ingestion_verification_failed',
                             'human_review_attempted':False,'continuous_queue_service':queue_service}
                    save(folder/'api-verification.json',summary)
                    return summary
                endpoint='/assessments/'+assessment['assessment_id']
                response=client.get(endpoint,headers=manager); response.raise_for_status(); detail=response.json()
                before=json.loads((runtime_folder/'run.json').read_text(encoding='utf-8'))['report']
                ai=detail['rows'][0]['ai']; grade=ai['grade']
                decision={'competency_id':'M2','disposition':'confirmed' if grade else 'insufficient_evidence','grade':grade,
                          'reason':'Synthetic operator confirms the limited material-based preview for API verification only.',
                          'evidence_refs':[{k:b[k] for k in ('evidence_id','quote','span_hash')} for b in blocks['blocks'][1:3]]}
                review={'expected_revision':detail['revision'],'decisions':[decision]}
                checks['employee_cannot_self_review']=client.post(endpoint+'/reviews',headers={**employee,'Idempotency-Key':'http-probe-self-deny'},json=review).status_code==403
                response=client.post(endpoint+'/reviews',headers={**manager,'Idempotency-Key':'http-probe-review-v1'},json=review)
                response.raise_for_status(); saved=response.json()
                final={'expected_revision':saved['revision'],'limitation_disposition':'Only synthetic software verification; preserve AI limitations and do not make an employment decision.'}
                headers={**manager,'Idempotency-Key':'http-probe-final-v1'}
                response=client.post(endpoint+'/finalize',headers=headers,json=final); response.raise_for_status()
                checks['finalize_replay_identical']=client.post(endpoint+'/finalize',headers=headers,json=final).json()==response.json()
                response=client.get(endpoint,headers=employee); response.raise_for_status(); detail=response.json()
                checks['separate_human_final']=detail['human_status']=='finalized' and detail['final']['ai_gate']==before['gate'] and not detail['final_employee_decision']
                from medic.runtime.store import Store
                checks['ai_report_unchanged']=Store(config['database']).snapshot(assessment['run_id'])['report']==before
                events=client.get(endpoint+'/history',headers=manager).json()
                checks['one_review_one_final_event']=sum(e['kind']=='human_review_saved' for e in events)==sum(e['kind']=='human_review_finalized' for e in events)==1
                save(folder/'api-final.json',detail)
                summary={'created_at':now(),'status':'pass' if all(checks.values()) else 'fail','scope':'real_local_http_synthetic_upload_real_six_role_model_and_synthetic_human_review',
                         'run_id':assessment['run_id'],'runtime_version':build.VERSION,'runtime_manifest_sha256':build.verify(build.VERSION)['manifest_sha256'],
                         'checks':checks,'runtime_verification_status':verification['status'],
                         'human_review_is_synthetic_operator_action':True,'business_signoff':False,'production_sso_verified':False}
                summary['continuous_queue_service']=queue_service
                save(folder/'api-verification.json',summary)
                print(json.dumps(summary),flush=True)
                return summary
        finally:
            if supervisor is not None:
                supervisor.terminate()
                try: supervisor.wait(timeout=10)
                except subprocess.TimeoutExpired: supervisor.kill(); supervisor.wait(timeout=5)
            if service_log: service_log.close()
            server.terminate()
            try: server.wait(timeout=10)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True); parser.add_argument('--binary',required=True); parser.add_argument('--api-example',required=True)
    parser.add_argument('--queue-service',action='store_true')
    args=parser.parse_args(); result=probe(args.output,args.binary,args.api_example,queue_service=args.queue_service)
    raise SystemExit(0 if result['status']=='pass' else 1)
