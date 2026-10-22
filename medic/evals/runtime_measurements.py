"""Read-only runtime artifact accounting, without retroactive experiment binding.

This proves local database/archive consistency, not provider signatures, expert
agreement, or authentic execution against a prospectively frozen A/B/C plan.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import zipfile

from medic.evals.comparison import _read, _write_private, digest
from medic.runtime.database_location import classify


def _require(condition,code):
    if not condition: raise ValueError(code)


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def read_database(location,run_id):
    """One read transaction; never constructs Store or runs schema migrations."""
    backend,value=classify(location)
    if backend=='sqlite':
        db=sqlite3.connect(Path(value).resolve().as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        placeholder='?'
    else:
        import psycopg
        from psycopg.rows import dict_row
        db=psycopg.connect(value,autocommit=True,row_factory=dict_row)
        db.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        placeholder='%s'
    try:
        def rows(query): return [dict(r) for r in db.execute(query.replace('?',placeholder),(run_id,)).fetchall()]
        def one(query):
            result=rows(query);_require(len(result)==1,'missing_or_duplicate_run_metadata');return result[0]
        return {'backend':backend,
            'run':one('SELECT * FROM runs WHERE run_id=?'),
            'limits':one('SELECT * FROM run_limits WHERE run_id=?'),
            'build':one('SELECT * FROM run_builds WHERE run_id=?'),
            'events':rows('SELECT seq,task_id,kind,data_json,created FROM events WHERE run_id=? ORDER BY seq'),
            'ledger':rows('SELECT * FROM model_requests WHERE run_id=? ORDER BY started,request_id'),
            'tasks':rows('SELECT task_id,role,material_id,competency_id,status,lease_epoch,execution_mode,thread_id,turn_id,result_hash FROM tasks WHERE run_id=? ORDER BY task_id'),
            'results':rows('SELECT r.task_id,r.lease_epoch,r.value_json FROM results r JOIN tasks t ON r.task_id=t.task_id AND r.lease_epoch=t.lease_epoch WHERE t.run_id=? AND t.status=\'completed\' ORDER BY r.task_id')}
    finally:
        db.rollback();db.close()


def verify_archive(identity,manifest,archive_path):
    _require(manifest.get('runtime_version')==identity.get('runtime_version') and digest(manifest)==identity.get('manifest_sha256'),'release_manifest_mismatch')
    files=manifest.get('files',{});_require(bool(files),'empty_release_manifest')
    with zipfile.ZipFile(archive_path) as archive:
        names=archive.namelist()
        _require(len(names)==len(set(names))==len(files) and set(names)=={'medic/'+p for p in files},'release_archive_members_mismatch')
        for path,expected in files.items():
            _require(hashlib.sha256(archive.read('medic/'+path)).hexdigest()==expected,'release_archive_file_mismatch')
    return len(files)


def measure(snapshot,gateway,receipt_batches,database):
    run=database['run'];run_id=run['run_id'];source=json.loads(run['input_json'])
    identity=json.loads(database['build']['identity_json']);limits=json.loads(database['limits']['limits_json'])
    report=json.loads(run['report_json']) if run['report_json'] else None
    events=[{'seq':e['seq'],'task_id':e['task_id'],'kind':e['kind'],'data':json.loads(e['data_json'])} for e in database['events']]
    _require(run['input_hash']==digest(source),'database_input_hash_mismatch')
    for key,expected in {'run_id':run_id,'source':source,'input_hash':run['input_hash'],'execution_build':identity,
            'budget_limits':limits,'deadline':database['limits']['deadline'],'state':run['state'],'mode':run['mode'],
            'report':report,'events':events}.items():
        _require(snapshot.get(key)==expected,'snapshot_database_mismatch')
    _require(run['state'] in ('ready_for_human_review','awaiting_human_review','failed','cancelled'),'run_not_terminal')
    _require(identity['runtime_version']==source['versions']['runtime_version'],'input_build_mismatch')
    created=[e['data'] for e in events if e['kind']=='run_created']
    _require(len(created)==1 and created[0].get('input_hash')==run['input_hash'] and
        created[0].get('execution_build')==identity and created[0].get('budget_limits_hash')==digest(limits),'creation_event_mismatch')
    tasks=database['tasks'];task_map={t['task_id']:t for t in tasks}
    _require(len(task_map)==len(tasks)==len(snapshot['tasks']),'task_set_mismatch')
    for task in snapshot['tasks']:
        _require(task['task_id'] in task_map and all(task.get(k)==v for k,v in task_map[task['task_id']].items()),'task_database_mismatch')
    results={r['task_id']:json.loads(r['value_json']) for r in database['results']}
    _require(results==snapshot['results'],'result_database_mismatch')
    for task_id,result in results.items():
        _require(digest(result)==task_map[task_id]['result_hash'],'result_hash_mismatch')
    publication=[e for e in database['events'] if e['kind']=='preview_published']
    if report is not None:
        _require(run['state'] in ('ready_for_human_review','awaiting_human_review') and len(publication)==1,'publication_state_mismatch')
        _require(json.loads(publication[0]['data_json'])=={'candidate_hash':digest(report),'state':run['state']},'publication_hash_mismatch')
        for key,value in {'run_id':run_id,'input_hash':run['input_hash'],'execution_build':identity,
                'versions':source['versions'],'budget_limits':limits,'deadline':snapshot['deadline'],
                'final_employee_decision':False,'human_review_required':True}.items():
            _require(report.get(key)==value,'report_identity_mismatch')
        final=[results[t['task_id']] for t in tasks if t['role']=='reevaluator' and
            (len(source['materials'])==1 or t['material_id']=='__aggregate__') and t['task_id'] in results]
        _require(sorted(map(digest,final))==sorted(map(digest,report['assessments'])),'report_final_result_mismatch')
        terminal=publication[0]['created']
    else:
        _require(not publication and run['state'] in ('failed','cancelled'),'missing_published_report')
        kinds={'cancelled':{'run_cancelled'},'failed':{'task_failed','run_deadline_exhausted'}}[run['state']]
        terminal_events=[e for e in database['events'] if e['kind'] in kinds]
        # Unknown stop time is not reconstructed from file mtime or last model request.
        terminal=max((e['created'] for e in terminal_events),default=None)
    ledger=database['ledger'];durable=gateway['durable_budget']
    _require(durable['limits']==limits and durable['deadline']==snapshot['deadline'] and durable['ledger']==ledger,'gateway_ledger_database_mismatch')
    charged=0;reported=0;estimated=0;reserved=0;ids=set()
    for row in ledger:
        _require(row['run_id']==run_id and row['request_id'] not in ids,'duplicate_or_foreign_reservation');ids.add(row['request_id'])
        _require(type(row['estimate']) is int and row['estimate']>0,'invalid_reservation_estimate')
        _require(all(_number(row[k]) for k in ('started','active_until')) and row['started']<=row['active_until']<=snapshot['deadline'],'invalid_reservation_times')
        if row['status']=='reserved':
            _require(row['charged'] is None and row['settled'] is None,'reserved_settlement_mismatch')
            reserved+=1;charged+=row['estimate']
        else:
            _require(row['status'] in ('reported','estimated') and type(row['charged']) is int and row['charged']>=0 and
                _number(row['settled']) and row['settled']>=row['started'],'invalid_settlement')
            if row['status']=='estimated':
                _require(row['charged']==row['estimate'],'estimated_charge_mismatch');estimated+=1
            else: reported+=1
            charged+=row['charged']
    _require(durable['requests']==len(ledger) and durable['charged_or_reserved_tokens']==charged and durable['unsettled_requests']==reserved,'durable_totals_mismatch')
    receipts={}
    for batch in [gateway['requests'],*receipt_batches]:
        for receipt in batch:
            ident=receipt.get('reservation_id')
            _require(ident in ids,'receipt_without_durable_reservation')
            _require(ident not in receipts or receipts[ident]==receipt,'conflicting_duplicate_receipt')
            receipts[ident]=receipt
    matched=0;measured=0
    for row in ledger:
        receipt=receipts.get(row['request_id'])
        if receipt is None: continue
        if receipt.get('usage_source')=='reported':
            usage=receipt.get('usage',{});tokens=usage.get('total_tokens')
            _require(all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens','total_tokens')) and
                tokens==usage['input_tokens']+usage['output_tokens'],'invalid_reported_usage')
            _require(receipt.get('finished') is True and not receipt.get('accounting_error') and row['status']=='reported' and tokens==row['charged'],'receipt_settlement_mismatch')
            measured+=tokens;matched+=1
        else:
            _require(receipt.get('usage_source') in ('conservative_estimate',None) and row['status']!='reported','unproven_reported_settlement')
    all_measured=matched==len(ledger)
    elapsed=None
    if terminal is not None:
        _require(_number(terminal) and _number(run['created']) and terminal>=run['created'],'invalid_terminal_time')
        elapsed=terminal-run['created']
    assessments=[] if report is None else report['assessments']
    return {'format':'medic-runtime-measurements-v1','run_id':run_id,'input_sha256':run['input_hash'],
        'execution_build':identity,'source_versions':source['versions'],'runtime_state':run['state'],'runtime_mode':run['mode'],
        'database_backend':database['backend'],'database_readset_sha256':digest(database),'snapshot_sha256':digest(snapshot),
        'report_sha256':digest(report) if report else None,'budget_limits':limits,
        'request_reservations':len(ledger),'reported_settlements':reported,'estimated_settlements':estimated,'unsettled_reservations':reserved,
        'receipt_verified_requests':matched,'missing_receipts':len(ids-set(receipts)),
        'provider_reported_token_subtotal':measured,'total_tokens':measured if all_measured else None,
        'charged_or_reserved_tokens':charged,
        'elapsed_seconds':elapsed,'elapsed_definition':'run creation to terminal event, including queue and downtime',
        'technical_requeues':sum(e['kind']=='attempt_requeued' for e in events),'transport_retries':None,
        'reported_predictions':[{'code':a['competency_id'],'grade':a['grade'],'evidence_valid':None,'anchors_supported':None} for a in assessments],
        'stored_gate':report.get('gate') if report else None,'human_review_seconds':None,
        'comparison_admission':'not_admitted_requires_prospective_schedule_and_input_binding',
        'assurance':'local_database_archive_and_receipt_consistency_only',
        'limitations':['Gateway receipts are local records, not provider-signed attestations.',
            'No role protocol, hard gate, evidence support or expert judgment is independently re-evaluated here.',
            'Reservations are not necessarily sent model requests; transport retries and human review time remain unknown.',
            'Estimated or unsettled reservations and missing usage receipts keep actual total_tokens unknown.']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--folder',required=True);choice=p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--database');choice.add_argument('--database-env')
    p.add_argument('--manifest',required=True);p.add_argument('--archive',required=True)
    p.add_argument('--receipt-file',action='append',default=[]);p.add_argument('--output',required=True)
    args=p.parse_args()
    try:
        folder=Path(args.folder);snapshot=_read(folder/'run.json');gateway=_read(folder/'gateway-summary.json')
        location=args.database if args.database else os.environ[args.database_env]
        data=read_database(location,snapshot['run_id'])
        count=verify_archive(json.loads(data['build']['identity_json']),_read(args.manifest),args.archive)
        batches=[_read(path)['requests'] for path in args.receipt_file]
        result=measure(snapshot,gateway,batches,data)
        result['archive_files_verified']=count
        result['auditor_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        _write_private(args.output,result)
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}));return 2
    print(json.dumps({'status':'written','receipt_verified_requests':result['receipt_verified_requests'],
        'total_tokens_known':result['total_tokens'] is not None}));return 0


if __name__=='__main__': raise SystemExit(main())
