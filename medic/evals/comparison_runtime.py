"""Prospective C-arm binding. Model execution remains the frozen runtime command."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from medic.domain.contracts import AssessmentInput
from medic.domain.validation import verify_input
from medic.evals.comparison import _read,_write_private,digest,load_frozen,schedule,validate_observations
from medic.evals.runtime_measurements import read_database,measure,verify_archive
from medic.evals.verify_dispute_live import audit
from medic.prompts import load_bundles
from medic.runtime import build
from medic.runtime.budget import BudgetLimits
from medic.runtime.store import Store

BUSINESS_VERSIONS=('competency_model_version','job_matrix_version','ai_policy_version','parser_version')


def content_identity(source):
    """Freeze all business input, independent of arm-specific implementation versions."""
    source=AssessmentInput.model_validate(source).model_dump()
    return digest({**source,'versions':{k:source['versions'][k] for k in BUSINESS_VERSIONS}})


def _check(condition,code):
    if not condition: raise ValueError(code)


def binding(envelope,source,sample_id,repeat,limits):
    dataset,design=load_frozen(envelope)
    source=AssessmentInput.model_validate(source);verify_input(source)
    _check(source.synthetic and dataset.provenance=='synthetic_fixture','production_comparison_not_enabled')
    limits=BudgetLimits.model_validate(limits).model_dump()
    rows=[r for r in schedule(envelope) if (r['sample_id'],r['strategy'],r['repeat'])==(sample_id,'multi_agent',repeat)]
    _check(len(rows)==1,'comparison_slot_not_found');row=rows[0]
    sample=next(s for s in dataset.samples if s.sample_id==sample_id)
    _check(sample.assessment_content_sha256 is not None and content_identity(source.model_dump())==sample.assessment_content_sha256,'full_business_input_mismatch')
    _check(source.employee_ref==sample.employee_id and source.job_key==sample.job_key,'employee_or_job_mismatch')
    _check(set(m.source_event_id for m in source.materials)==set(sample.event_ids),'source_events_mismatch')
    _check(set(m.source_sha256 or m.sha256 for m in source.materials)==set(sample.material_sha256),'source_materials_mismatch')
    _check(set(source.goal.competency_scope)=={l.code for l in sample.labels},'assessment_scope_mismatch')
    _check(all((source.policies[l.code].mode,source.policies[l.code].cap)==(l.mode,l.cap) for l in sample.labels),'policy_mismatch')
    _check(all(getattr(source.versions,k)==getattr(dataset,k) for k in BUSINESS_VERSIONS),'business_versions_mismatch')
    identity=build.verify(source.versions.runtime_version)
    _check(identity['manifest_sha256']==design.implementation_sha256['multi_agent'],'implementation_mismatch')
    _check(digest(load_bundles()[source.versions.prompt_bundle_version])==design.prompt_sha256['multi_agent'],'prompt_mismatch')
    profile=_read(build.ROOT/'p0/results/provider-profile.json')
    _check(source.versions.provider_profile_version==design.provider_profile_version==profile['provider_profile_version'] and design.model==profile['model'],'provider_profile_mismatch')
    baseline=_read(build.ROOT/'p0/results/baseline.lock.json')
    _check(source.versions.codex_revision==baseline['codex_revision'],'codex_revision_mismatch')
    _check(limits['total_tokens']==row['token_budget'],'token_budget_mismatch')
    payload={'format':'medic-comparison-runtime-binding-v1','slot':{k:v for k,v in row.items() if k!='input'},
        'source':source.model_dump(),'full_input_sha256':digest(source.model_dump()),'budget_limits':limits,
        'execution_build':identity,'adapter_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return {'sha256':digest(payload),'payload':payload}


def _save_once(path,value):
    if path.exists(): _check(_read(path)==value,'comparison_artifact_conflict')
    else: _write_private(path,value)


def workspace(envelope,sample_id,repeat,root=None):
    # CLI has one canonical workspace per experiment; no alternate output root.
    root=Path(root) if root is not None else build.ROOT/'artifacts/comparison-runtime'
    # One full hash keeps identity strength without two 64-character path levels.
    return root/digest([envelope['sha256'],sample_id,'multi_agent',repeat])


def creation_identity(envelope,sample_id,repeat):
    key='comparison-'+digest([envelope['sha256'],sample_id,'multi_agent',repeat])
    return key,'run-'+digest(['run-creation',key])[:32]


def prepare(envelope,source,sample_id,repeat,limits,*,root=None):
    expected=binding(envelope,source,sample_id,repeat,limits)
    folder=workspace(envelope,sample_id,repeat,root)
    legacy=folder.parent/envelope['sha256']/digest([sample_id,'multi_agent',repeat])
    _check(not legacy.exists(),'legacy_comparison_workspace_requires_archived_adapter')
    if folder.exists():
        _check((folder/'binding.json').is_file(),'unbound_workspace_exists')
        _check(_read(folder/'binding.json')==expected,'comparison_binding_conflict')
    # Binding is durable before any runtime database/task creation. Replay never changes it.
    _save_once(folder/'binding.json',expected)
    _save_once(folder/'experiment.json',envelope)
    _save_once(folder/'input.json',expected['payload']['source'])
    store=Store(folder/'runtime.sqlite3')
    creation_key,expected_run=creation_identity(envelope,sample_id,repeat)
    run=store.create_run(source,limits=limits,creation_key=creation_key)
    _check(run==expected_run,'creation_identity_mismatch')
    snapshot=store.snapshot(run)
    _check(snapshot['input_hash']==expected['payload']['full_input_sha256'] and
           snapshot['budget_limits']==expected['payload']['budget_limits'] and
           snapshot['execution_build']==expected['payload']['execution_build'],'created_run_binding_mismatch')
    receipt={'run_id':run,'binding_sha256':expected['sha256'],'deadline':snapshot['deadline']}
    _save_once(folder/'run-binding.json',receipt)
    return {'folder':str(folder.resolve()),**receipt,'state':snapshot['state']}


def collect(folder,*,root=None):
    folder=Path(folder);envelope=_read(folder/'experiment.json');sealed=_read(folder/'binding.json')
    payload=sealed['payload'];slot=payload['slot'];source=_read(folder/'input.json')
    expected=binding(envelope,source,slot['sample_id'],slot['repeat'],payload['budget_limits'])
    _check(expected==sealed,'comparison_binding_changed')
    _check(folder.resolve()==workspace(envelope,slot['sample_id'],slot['repeat'],root).resolve(),'noncanonical_comparison_workspace')
    receipt=_read(folder/'run-binding.json');run_id=receipt['run_id']
    _check(receipt['binding_sha256']==sealed['sha256'],'run_binding_mismatch')
    _check(run_id==creation_identity(envelope,slot['sample_id'],slot['repeat'])[1],'run_not_created_for_comparison_slot')
    snapshot=_read(folder/'run.json')
    _check(snapshot['run_id']==run_id and snapshot['source']==source and snapshot['deadline']==receipt['deadline'],'runtime_binding_mismatch')
    database=read_database(folder/'runtime.sqlite3',run_id)
    created=database['run']['created']
    # File timestamps are intentionally not used as proof of temporal order.
    _check(snapshot['input_hash']==payload['full_input_sha256'],'runtime_full_input_mismatch')
    release=payload['execution_build']['runtime_version']
    verify_archive(payload['execution_build'],_read(build.ROOT/'deploy/releases'/f'{release}.manifest.json'),
                   build.ROOT/'artifacts/releases'/release/'source.zip')
    def protocol(task):
        _check(re.fullmatch(r'task-[0-9a-f]{32}',task['task_id']) and type(task['lease_epoch']) is int and task['lease_epoch']>0,'invalid_protocol_path')
        return _read(folder/'workers'/task['task_id']/str(task['lease_epoch'])/'protocol-summary.json')
    batches=[_read(path)['requests'] for path in sorted(folder.glob('gateway-*.json')) if path.name!='gateway-summary.json']
    measured=measure(snapshot,_read(folder/'gateway-summary.json'),batches,database)
    if snapshot['state'] in ('failed','cancelled'):
        status=snapshot['state'];predictions=[]
        execution={'execution_status':'incomplete','terminal_state':status,'successful_protocol_audit':False}
    else:
        execution=audit(snapshot,protocol,source,payload['budget_limits'],payload['execution_build'])
        _check(execution['execution_status']=='pass','role_protocol_audit_failed')
        status='completed'
        # Local quote/ID validity is checked by audit_attempt; behavioral support remains unknown.
        predictions=[{**pred,'evidence_valid':True} for pred in measured['reported_predictions']]
    observation={**slot,'execution_id':run_id,'source_report_sha256':measured['snapshot_sha256'],
        'status':status,'predictions':predictions,'total_tokens':measured['total_tokens'],
        'model_requests':measured['receipt_verified_requests'] if measured['total_tokens'] is not None else None,
        'retries':None,'elapsed_seconds':measured['elapsed_seconds'],'human_review_seconds':None}
    validate_observations(envelope,[observation])
    export={'binding_sha256':sealed['sha256'],'run_created':created,'execution_audit':execution,
        'measurement':measured,'observation':observation,'quality_acceptance':False}
    _save_once(folder/'comparison-export.json',export)
    _save_once(folder/'comparison-observations.json',[observation])
    return export


def main():
    p=argparse.ArgumentParser(description=__doc__);commands=p.add_subparsers(dest='command',required=True)
    c=commands.add_parser('prepare');c.add_argument('--experiment',required=True);c.add_argument('--input',required=True)
    c.add_argument('--sample',required=True);c.add_argument('--repeat',type=int,required=True);c.add_argument('--limits',required=True)
    c=commands.add_parser('collect');c.add_argument('--folder',required=True)
    args=p.parse_args()
    try:
        if args.command=='prepare':
            result=prepare(_read(args.experiment),_read(args.input),args.sample,args.repeat,_read(args.limits))
            print(json.dumps(result));return 0
        result=collect(args.folder)
        print(json.dumps({'status':'collected','run_id':result['observation']['execution_id'],'total_tokens':result['observation']['total_tokens'],'quality_acceptance':False}));return 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}));return 2


if __name__=='__main__': raise SystemExit(main())
