"""Archive an isolated terminal SQLite run, its release, receipts and worker traces.

This is a private forensic archive, not a runnable deployment restore. It excludes
worker bindings, provider configuration and authentication files. It may contain
raw evidence and model text. Hashes attest local consistency, not model quality.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import re

from medic.deploy.backup import plain_path, fresh_private, file_info, read_db, snapshot_db, copy_new, write_new
from medic.evals.comparison import digest
from medic.evals.runtime_measurements import read_database, measure, verify_archive

FORMAT='medic-terminal-execution-archive-v1'
TABLES={'events','mailbox','model_requests','outbox','reads','results','rework_plans','run_builds',
        'run_limits','runs','sqlite_sequence','task_cycles','task_history','tasks'}
TERMINAL={'failed','cancelled','ready_for_human_review','awaiting_human_review'}
TASK=re.compile(r'task-[0-9a-f]{32}')


def require(value,code):
    if not value:raise ValueError(code)


def read(path):
    return json.loads(plain_path(path).read_text(encoding='utf-8'))


def safe_name(name):
    require(isinstance(name,str) and name and '\\' not in name and ':' not in name,'archive_invalid_member')
    parts=PurePosixPath(name).parts
    require(not name.startswith('/') and all(p not in ('','.', '..') for p in name.split('/')),
            'archive_invalid_member')
    require(str(PurePosixPath(name))==name and parts,'archive_invalid_member')
    return name


def allowed_member(name):
    safe_name(name)
    if name in ('runtime.sqlite3','run.json','gateway-summary.json','release/manifest.json','release/source.zip'):
        return True
    if re.fullmatch(r'gateway-\d+\.json',name):return True
    parts=name.split('/')
    if len(parts)<4 or parts[0]!='workers' or not TASK.fullmatch(parts[1]):return False
    if not parts[2].isdecimal() or int(parts[2])<1 or str(int(parts[2]))!=parts[2]:return False
    if parts[3:]==['protocol-summary.json']:return True
    return len(parts)>=6 and parts[3:5]==['codex-home','sessions'] and parts[-1].endswith('.jsonl')


def table_snapshot(path):
    """No Store construction or schema migration, and no application identities."""
    with closing(read_db(path)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        require([r[0] for r in db.execute('PRAGMA integrity_check')]==['ok'],'archive_database_corrupt')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(),'archive_database_foreign_keys')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(tables==TABLES,'archive_isolated_runtime_database_required')
        runs=db.execute('SELECT run_id,state FROM runs').fetchall()
        require(len(runs)==1 and runs[0]['state'] in TERMINAL,'archive_single_terminal_run_required')
        values={}
        for name in sorted(tables):
            rows=[dict(r) for r in db.execute('SELECT * FROM "'+name+'"')]
            values[name]=sorted(rows,key=digest)
        return values


def private_input(path):
    # Source and destination cannot contain one another; callers check pairs.
    return plain_path(path)


def unrelated(a,b):
    require(not a.is_relative_to(b) and not b.is_relative_to(a),'archive_overlapping_paths')


def session_files(folder):
    folder=plain_path(folder)
    if not folder.exists():return []
    files=[]
    for directory,dirs,names in os.walk(folder,followlinks=False):
        plain_path(directory)
        for name in dirs:plain_path(Path(directory)/name)
        for name in names:
            path=plain_path(Path(directory)/name)
            require(path.is_file() and path.suffix=='.jsonl','archive_unexpected_session_file')
            files.append(path)
    return sorted(files)


def worker_inventory(folder,snapshot):
    """Require records for every leased current/historical attempt, not just winners."""
    records=[*snapshot.get('task_history',[]),*snapshot['tasks']]
    recorded={(t['task_id'],t['lease_epoch']):t for t in records if t['lease_epoch']>0}
    leases=[e for e in snapshot['events'] if e['kind']=='task_leased']
    records=[]
    for event in leases:
        epoch=event['data']['lease_epoch'];ident=event['task_id'];key=(ident,epoch)
        task=recorded.get(key)
        if task is None:
            cancelled=recorded.get((ident,epoch+1))
            # Technical retries may have no result/history row. Their lease
            # events still bind the older protocol and must not be discarded.
            bindings=[e['data'] for e in snapshot['events'] if e['task_id']==ident
                      and e['kind']=='execution_bound' and e['data'].get('lease_epoch')==epoch]
            task={'task_id':ident,'lease_epoch':epoch,'role':event['data']['role'],
                  'context_hash':event['data']['context_hash'],
                  'status':'cancelled' if cancelled and cancelled['status']=='cancelled' else 'prior_attempt',
                  'thread_id':bindings[0]['thread_id'] if bindings else None}
        require(task['role']==event['data']['role'] and task['context_hash']==event['data']['context_hash'],
                'archive_lease_record_mismatch')
        records.append(task)
    # Cancellation advances the current fencing epoch without starting a worker;
    # queued, never-leased tasks can also receive that fence. Use lease events as
    # execution authority while requiring all actual submitted/completed records.
    required={key for key,t in recorded.items() if t['status'] in ('running','submitted','completed','failed')}
    require(required<={(t['task_id'],t['lease_epoch']) for t in records},'archive_lease_event_missing')
    attempts={}
    selected={};coverage=[]
    for task in records:
        ident=task['task_id'];epoch=task['lease_epoch']
        require(TASK.fullmatch(ident) and type(epoch) is int and epoch>=0,'archive_invalid_task_identity')
        if epoch==0:continue
        key=(ident,epoch);require(key not in attempts,'archive_duplicate_attempt');attempts[key]=task
        prefix=f'workers/{ident}/{epoch}'
        path=plain_path(folder/prefix/'protocol-summary.json')
        require(path.is_file(),'archive_worker_protocol_missing')
        protocol=read(path)
        require(protocol.get('role')==task['role'] and protocol.get('context_hash')==task['context_hash']
                and protocol.get('execution_build')==snapshot['execution_build'],'archive_worker_binding_mismatch')
        selected[prefix+'/protocol-summary.json']=path
        files=session_files(folder/prefix/'codex-home/sessions')
        require(not task.get('thread_id') or bool(files),'archive_worker_session_missing')
        for file in files:selected[file.relative_to(folder).as_posix()]=file
        coverage.append({'task_id':ident,'lease_epoch':epoch,'role':task['role'],'status':task['status'],
                         'session_files':len(files),'protocol_file':prefix+'/protocol-summary.json'})
    workers=plain_path(folder/'workers')
    if workers.exists():
        for task_dir in workers.iterdir():
            plain_path(task_dir)
            require(task_dir.is_dir() and TASK.fullmatch(task_dir.name),'archive_unexpected_worker_directory')
            for epoch_dir in task_dir.iterdir():
                plain_path(epoch_dir)
                require(epoch_dir.is_dir() and epoch_dir.name.isdecimal() and str(int(epoch_dir.name))==epoch_dir.name
                        and (task_dir.name,int(epoch_dir.name)) in attempts,'archive_unexpected_worker_epoch')
    return selected,sorted(coverage,key=lambda r:(r['task_id'],r['lease_epoch']))


def data_check(folder):
    snapshot=read(folder/'run.json');tables=table_snapshot(folder/'runtime.sqlite3')
    require(snapshot['run_id']==tables['runs'][0]['run_id'],'archive_run_mismatch')
    verify_snapshot_details(snapshot,tables)
    database=read_database(str(folder/'runtime.sqlite3'),snapshot['run_id'])
    gateway=read(folder/'gateway-summary.json')
    receipts=[read(p)['requests'] for p in folder.glob('gateway-*.json') if p.name!='gateway-summary.json']
    metrics=measure(snapshot,gateway,receipts,database)
    _,coverage=worker_inventory(folder,snapshot)
    return snapshot,tables,metrics,coverage


def verify_snapshot_details(snapshot,tables):
    """Bind fields outside the accounting reader, including failed submissions/history."""
    tasks={t['task_id']:t for t in tables['tasks']}
    cycles={t['task_id']:t for t in tables['task_cycles']}
    require(len(tasks)==len(snapshot['tasks']),'archive_task_snapshot_mismatch')
    for item in snapshot['tasks']:
        row=tasks.get(item['task_id']);require(row is not None,'archive_task_snapshot_mismatch')
        for name,value in row.items():
            if name=='token_hash':continue
            if name=='dependencies':value=json.loads(value)
            require(item.get(name)==value,'archive_task_snapshot_mismatch')
        cycle=cycles.get(item['task_id'],{'business_round':2 if row['role'] in ('reevaluator','judge') else 1,'attempts':row['lease_epoch']})
        require(item['business_round']==cycle['business_round'] and item['attempt_in_round']==cycle['attempts'],
                'archive_task_cycle_mismatch')
        reads=[r['evidence_id'] for r in tables['reads'] if (r['task_id'],r['lease_epoch'])==(row['task_id'],row['lease_epoch'])]
        require(sorted(item['read_evidence_ids'])==sorted(reads),'archive_evidence_read_mismatch')
    results={(r['task_id'],r['lease_epoch']):json.loads(r['value_json']) for r in tables['results']}
    history=[{**json.loads(row['task_json']),'result':results.get((row['task_id'],row['lease_epoch']))}
             for row in sorted(tables['task_history'],key=lambda r:(r['task_id'],r['lease_epoch']))]
    require(snapshot.get('task_history',[])==history,'archive_history_snapshot_mismatch')
    messages=[json.loads(row['value_json']) for row in sorted(tables['mailbox'],key=lambda r:r['seq'])]
    require(snapshot.get('mailbox',[])==messages,'archive_mailbox_snapshot_mismatch')
    plans=tables['rework_plans'];require(len(plans)<=1,'archive_rework_snapshot_mismatch')
    plan=None if not plans else {'status':plans[0]['status'],'candidates':json.loads(plans[0]['candidates_json']),
                                 'decisions':json.loads(plans[0]['decisions_json'])}
    require(snapshot.get('rework_plan')==plan,'archive_rework_snapshot_mismatch')


def inventory(folder):
    paths={}
    for directory,dirs,names in os.walk(folder,followlinks=False):
        plain_path(directory)
        for name in dirs:plain_path(Path(directory)/name)
        for name in names:
            path=plain_path(Path(directory)/name);relative=path.relative_to(folder).as_posix()
            safe_name(relative)
            if relative not in ('manifest.json','RESTORED.json'):paths[relative]=file_info(path)
    return paths


def verify(folder):
    folder=private_input(folder);manifest=read(folder/'manifest.json')
    require(set(manifest)=={'payload','sha256'} and digest(manifest['payload'])==manifest['sha256'],'archive_manifest_hash')
    value=manifest['payload'];require(value.get('format')==FORMAT,'archive_format')
    require(value.get('purpose')=='offline_forensic_review' and value.get('services_started') is False
            and value.get('model_execution_claim') is False and value.get('private_raw_evidence') is True,
            'archive_assurance_scope_changed')
    require(isinstance(value.get('files'),dict) and value['files'],'archive_empty_inventory')
    for name in value['files']:require(allowed_member(name),'archive_disallowed_member')
    require(inventory(folder)==value['files'],'archive_file_integrity')
    snapshot,tables,metrics,coverage=data_check(folder)
    require(digest(tables)==value['database_tables_sha256'] and digest(snapshot)==value['snapshot_sha256'],
            'archive_database_snapshot_mismatch')
    require(coverage==value['worker_coverage'],'archive_worker_coverage_changed')
    require(metrics==value['measurements'],'archive_accounting_changed')
    require(snapshot['execution_build']==value['execution_build'],'archive_build_changed')
    verify_archive(snapshot['execution_build'],read(folder/'release/manifest.json'),folder/'release/source.zip')
    if (folder/'RESTORED.json').exists():
        marker=read(folder/'RESTORED.json')
        require(marker=={'manifest_sha256':manifest['sha256'],'services_started':False,'purpose':'offline_forensic_review'},
                'archive_restore_marker_changed')
    return manifest


def create(folder,release_manifest,release_archive,destination):
    folder=private_input(folder);destination=plain_path(destination)
    release_manifest=plain_path(release_manifest);release_archive=plain_path(release_archive)
    unrelated(folder,destination)
    require(not destination.exists(),'archive_destination_exists')
    snapshot,tables,metrics,coverage=data_check(folder)
    verify_archive(snapshot['execution_build'],read(release_manifest),release_archive)
    selected={'run.json':folder/'run.json','gateway-summary.json':folder/'gateway-summary.json',
              'release/manifest.json':release_manifest,'release/source.zip':release_archive}
    for path in folder.glob('gateway-*.json'):
        if path.name!='gateway-summary.json':
            require(re.fullmatch(r'gateway-\d+\.json',path.name),'archive_invalid_gateway_file')
            selected[path.name]=path
    worker_files,_=worker_inventory(folder,snapshot);selected.update(worker_files)
    before={name:file_info(path) for name,path in selected.items()}
    destination=fresh_private(destination)
    snapshot_db(folder/'runtime.sqlite3',destination/'runtime.sqlite3')
    require(table_snapshot(destination/'runtime.sqlite3')==tables,'archive_database_changed_during_copy')
    for name,path in selected.items():copy_new(path,destination/safe_name(name))
    require({name:file_info(path) for name,path in selected.items()}==before,'archive_source_changed_during_copy')
    require(table_snapshot(folder/'runtime.sqlite3')==tables,'archive_database_changed_during_copy')
    _,copied_tables,copied_metrics,copied_coverage=data_check(destination)
    require(copied_tables==tables and copied_metrics==metrics and copied_coverage==coverage,'archive_copy_mismatch')
    value={'format':FORMAT,'run_id':snapshot['run_id'],'runtime_state':snapshot['state'],
           'execution_build':snapshot['execution_build'],'snapshot_sha256':digest(snapshot),
           'database_tables_sha256':digest(tables),'worker_coverage':coverage,'measurements':metrics,
           'files':inventory(destination),'private_raw_evidence':True,
           'excluded':['worker binding.json','codex configuration and auth','provider credentials','application/vault databases','review HTML and arbitrary logs'],
           'purpose':'offline_forensic_review','services_started':False,'model_execution_claim':False}
    manifest={'payload':value,'sha256':digest(value)}
    write_new(destination/'manifest.json',manifest)
    return verify(destination)


def restore(folder,destination):
    folder=private_input(folder);destination=plain_path(destination);unrelated(folder,destination)
    manifest=verify(folder);destination=fresh_private(destination)
    for name in manifest['payload']['files']:copy_new(folder/name,destination/name)
    write_new(destination/'manifest.json',manifest)
    verify(destination)
    write_new(destination/'RESTORED.json',{'manifest_sha256':manifest['sha256'],'services_started':False,'purpose':'offline_forensic_review'})
    return verify(destination)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    create_parser=sub.add_parser('create');create_parser.add_argument('--folder',required=True)
    create_parser.add_argument('--release-manifest',required=True);create_parser.add_argument('--release-archive',required=True)
    create_parser.add_argument('--output',required=True)
    verify_parser=sub.add_parser('verify');verify_parser.add_argument('folder')
    restore_parser=sub.add_parser('restore');restore_parser.add_argument('folder');restore_parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.command=='create':result=create(args.folder,args.release_manifest,args.release_archive,args.output)
        elif args.command=='verify':result=verify(args.folder)
        else:result=restore(args.folder,args.output)
    except Exception as exc:
        # Never echo a database, raw trace, credential or private path in CLI errors.
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}));return 2
    print(json.dumps({'status':'verified','manifest_sha256':result['sha256'],
        'files':len(result['payload']['files']),'worker_attempts':len(result['payload']['worker_coverage']),
        'services_started':False,'purpose':'offline_forensic_review'}));return 0


if __name__=='__main__':raise SystemExit(main())
