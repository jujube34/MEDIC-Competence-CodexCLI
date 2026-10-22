"""Audit all completed epochs of a real synthetic dispute case, including history.

A safe human-review outcome can pass execution checks. Coverage remains partial
unless actual challenges, exchanges and third-round carry-over were observed.
This is a local integrity audit, not a signature or an expert accuracy measure.
"""
import argparse
import json
from pathlib import Path
import re

from medic.domain.contracts import AssessmentInput
from medic.domain.disputes import pending_topics
from medic.domain.graph import required_graph, scoped_spans
from medic.domain.validation import release_gate, validate_refs, validate_result, verify_input
from medic.p0.common import digest, now, save
from medic.prompts import load_bundles
from medic.runtime import build


BUSINESS_FAILURES = ('judge_not_pass:', 'unresolved_blocking_challenge:',
    'unresolved_prior_blocking_challenge:', 'unanswered_dispute_message:', 'requires_human_review:')


def attempts(snapshot):
    current=[{**t,'result':snapshot['results'].get(t['task_id'])} for t in snapshot['tasks']]
    return [*snapshot.get('task_history',[]),*current]


def audit_attempt(task, source, events, protocol, identity, prompt_hash):
    """Every successful historical result needs the same provenance as a current one."""
    try:
        context=json.loads(task['context_json'])
        value=task['result']
        matching=[e for e in events if e['task_id']==task['task_id']
                  and e['data'].get('lease_epoch')==task['lease_epoch']]
        completed=[e for e in matching if e['kind']=='task_completed'
            and e['data'].get('thread_id')==task['thread_id'] and e['data'].get('turn_id')==task['turn_id']]
        bound=[e for e in matching if e['kind'] in ('execution_bound','finalization_bound')
            and e['data'].get('thread_id')==task['thread_id'] and e['data'].get('turn_id')==task['turn_id']]
        submitted=[e for e in matching if e['kind']=='result_submitted' and e['data'].get('result_hash')==task['result_hash']]
        methods={e['method'] for e in protocol.get('events',[])}
        checks=[task['status']=='completed',task['execution_mode']=='live',bool(task['thread_id']),bool(task['turn_id']),
            len(completed)==1,bool(bound),len(submitted)==1,
            context['task_id']==task['task_id'],context['role']==task['role'],
            context['input_hash']==digest(source.model_dump()),context['execution_build']==identity,
            context['context_hash']==task['context_hash'],
            digest({k:v for k,v in context.items() if k!='context_hash'})==task['context_hash'],
            digest(value)==task['result_hash'],protocol.get('role')==task['role'],
            protocol.get('context_hash')==task['context_hash'],protocol.get('execution_build')==identity,
            protocol.get('prompt_bundle_hash')==prompt_hash,
            {'thread/start','turn/start','turn/completed'}<=methods]
        if task['role']=='coordinator':
            checks.append({tuple(k) for k in value['planned_scopes']}==set(required_graph(source)))
        else:
            validate_result(task['role'],value,task,source,context['prior'])
            checks.append(set(task['read_evidence_ids'])=={s.evidence_id for s in scoped_spans(source,task['material_id'])})
        if task['role']=='challenger_scan': checks.append(context['prior']=={})
        return all(checks)
    except (ValueError,TypeError,KeyError):
        return False


def audit_messages(snapshot, records, source):
    """Bind each message to its actual completed sender, challenge and event."""
    by_epoch={(t['task_id'],t['lease_epoch']):t for t in records}
    challenges={t['result_hash']:t for t in records if t['role']=='challenger' and t.get('result')}
    scopes={t['task_id']:(t['material_id'],t['competency_id'],t['role']) for t in records}
    posted=[e for e in snapshot['events'] if e['kind']=='dispute_message_posted']
    seen=set(); topics={}
    try:
        for message in snapshot.get('mailbox',[]):
            sender=by_epoch[message['sender_task_id'],message['sender_lease_epoch']]
            challenger=challenges[message['challenge_result_hash']]
            challenge=next(c for c in challenger['result']['challenges'] if c['challenge_id']==message['challenge_id'])
            scope=(message['material_id'],message['competency_id'])
            other='reevaluator' if sender['role']=='challenger' else 'challenger'
            if (sender['role'] not in ('challenger','reevaluator') or sender['role']!=message['sender_role']
                or scopes[sender['task_id']][:2]!=scope or scopes[challenger['task_id']][:2]!=scope
                or scopes[message['recipient_task_id']]!=(*scope,other)
                or challenge['claim_id']!=message['claim_id'] or message['message_id'] in seen
                or message['topic_id']!=digest([challenger['task_id'],message['challenge_result_hash'],message['challenge_id']])
                or message['trust']!='untrusted_agent_message'):
                return False
            validate_refs(message,scoped_spans(source,message['material_id']))
            if not {r['evidence_id'] for r in message['evidence_refs']}<=set(sender['read_evidence_ids']): return False
            matching=[e for e in posted if e['task_id']==sender['task_id']
                and e['data'].get('message_id')==message['message_id'] and e['data'].get('message_hash')==digest(message)]
            if len(matching)!=1: return False
            starts=[e['seq'] for e in snapshot['events'] if e['kind']=='task_leased' and e['task_id']==sender['task_id']
                    and e['data'].get('lease_epoch')==sender['lease_epoch']]
            ends=[e['seq'] for e in snapshot['events'] if e['kind']=='task_completed' and e['task_id']==sender['task_id']
                  and e['data'].get('lease_epoch')==sender['lease_epoch']]
            if len(starts)!=1 or len(ends)!=1 or not starts[0]<matching[0]['seq']<ends[0]: return False
            thread=topics.setdefault(message['topic_id'],[])
            previous=thread[-1] if thread else None
            if (message['sequence']!=len(thread)+1 or len(thread)>=4
                or message['kind']!=('answer' if previous and previous['kind']=='question' else 'question')
                or message['reply_to']!=(previous['message_id'] if previous else None)
                or (previous and previous['recipient_task_id']!=sender['task_id'])):
                return False
            thread.append(message); seen.add(message['message_id'])
        return len(posted)==len(seen) and len(seen)<=128
    except (ValueError,TypeError,KeyError,StopIteration):
        return False


def audit(snapshot, protocol_loader, expected_source, limits, identity):
    source=AssessmentInput.model_validate(snapshot['source']); verify_input(source)
    expected=AssessmentInput.model_validate(expected_source).model_dump()
    if not source.synthetic: raise ValueError('synthetic_cases_only')
    records=attempts(snapshot)
    prompt_hash=digest(load_bundles()[source.versions.prompt_bundle_version])
    successful=[t for t in records if t['status']=='completed']
    proven=[audit_attempt(t,source,snapshot['events'],protocol_loader(t),identity,prompt_hash) for t in successful]
    keys=[(t['task_id'],t['lease_epoch']) for t in successful]
    completed={(e['task_id'],e['data'].get('lease_epoch')) for e in snapshot['events'] if e['kind']=='task_completed'}
    initial=[t for t in successful if json.loads(t['context_json'])['business_round']<3]
    graph=required_graph(source)
    initial_scopes={(t['material_id'],t['competency_id'],t['role']) for t in initial if t['role']!='coordinator'}
    gate=release_gate(source,snapshot['results'],snapshot['tasks'],snapshot['mode'],snapshot.get('mailbox'),snapshot.get('rework_plan'))
    structural=[f for f in gate['failures'] if f!='coordinator_requested_human_review' and not f.startswith(BUSINESS_FAILURES)]
    created=[e['data'] for e in snapshot['events'] if e['kind']=='run_created']
    result_hashes={t['result_hash'] for t in successful}
    prior_links=all(digest(value) in result_hashes for t in successful
        for value in json.loads(t['context_json']).get('prior',{}).values()
        if isinstance(value,dict) and 'task_id' in value and 'context_hash' in value)
    checks={
        'prospective_input_preserved':source.model_dump()==expected and snapshot['input_hash']==digest(expected),
        'frozen_build_preserved':snapshot['execution_build']==identity and (snapshot.get('report') or {}).get('execution_build')==identity,
        'budget_limits_preserved':snapshot['budget_limits']==limits and len(created)==1 and created[0].get('budget_limits_hash')==digest(limits),
        'all_initial_roles_completed':{t['role'] for t in initial}=={'coordinator',*(key[2] for key in graph)},
        'all_initial_scopes_completed':initial_scopes==set(graph) and any(t['role']=='coordinator' for t in initial),
        'all_completed_epochs_present':set(keys)==completed and len(keys)==len(set(keys)),
        'every_current_and_archived_attempt_verified':bool(successful) and all(proven),
        'prior_results_match_recorded_attempts':prior_links,
        'independent_threads_across_completed_attempts':len({t['thread_id'] for t in successful})==len(successful),
        'current_graph_complete':len(snapshot['tasks'])==len(graph)+1 and all(t['status']=='completed' for t in snapshot['tasks']),
        'no_seeded_initial_event':not any(e['kind']=='seeded_protocol_fixture' for e in snapshot['events']),
        'messages_bound_to_real_results':audit_messages(snapshot,successful,source),
        'gate_has_no_integrity_failures':not structural,
        'stored_gate_matches':(snapshot.get('report') or {}).get('gate')==gate,
        'safe_final_state':snapshot['state']==('ready_for_human_review' if gate['passed'] else 'awaiting_human_review'),
        'one_preview_publication':sum(e['kind']=='preview_published' for e in snapshot['events'])==1,
        'not_final_employee_decision':(snapshot.get('report') or {}).get('final_employee_decision') is False,
    }
    challenges=sum(len(t['result'].get('challenges',[])) for t in successful if t['role']=='challenger')
    responses=sum(len(t['result'].get('challenge_responses',[])) for t in successful if t['role']=='reevaluator')
    third=[t for t in successful if json.loads(t['context_json'])['business_round']==3]
    carried=sum(len(t['result'].get('prior_dispute_responses',[])) for t in third if t['role']=='reevaluator')
    decisions=(snapshot.get('rework_plan') or {}).get('decisions',{})
    coverage={'nonempty_real_challenges':challenges>0,'formal_challenge_responses':responses>0,
        'real_mailbox_question_and_answer':any(m['kind']=='answer' for m in snapshot.get('mailbox',[])),
        'real_coordinator_selected_third_round':any(d['decision']=='rework' for d in decisions.values()),
        'four_real_third_round_roles':{'coordinator','challenger','reevaluator','judge'}<={t['role'] for t in third},
        'old_blocking_issue_explicitly_carried':carried>0}
    execution=all(checks.values())
    return {'status':'pass' if execution and all(coverage.values()) else 'partial' if execution else 'fail',
        'execution_status':'pass' if execution else 'fail','checks':checks,'coverage':coverage,'gate':gate,
        'counts':{'completed_attempts':len(successful),'archived_attempts':len(snapshot.get('task_history',[])),
                  'challenges':challenges,'responses':responses,'messages':len(snapshot.get('mailbox',[])),
                  'pending_topics':len(pending_topics(snapshot.get('mailbox',[]))),'third_round_attempts':len(third),
                  'carried_old_blocking_issues':carried},
        'third_round_or_human_decisions':[d['decision'] for d in decisions.values()]}


def verify(folder, manifest_file, case_id, output):
    folder=Path(folder); manifest_file=Path(manifest_file)
    manifest=json.loads(manifest_file.read_text(encoding='utf-8'))
    entry=next(c for c in manifest['cases'] if c['case_id']==case_id)
    from medic.evals.dispute_cases import CASES
    if case_id not in CASES: raise ValueError('unknown_case')
    expected=json.loads((manifest_file.parent/(case_id+'.json')).read_text(encoding='utf-8'))
    if digest(expected)!=entry['input_hash']: raise ValueError('prospective_input_document_changed')
    identity=build.verify(build.VERSION)
    if manifest['execution_build']!=identity or manifest['seeded_results'] is not False: raise ValueError('prospective_manifest_mismatch')
    snapshot=json.loads((folder/'run.json').read_text(encoding='utf-8'))
    def load(task):
        if not re.fullmatch(r'task-[0-9a-f]{32}',task['task_id']) or type(task['lease_epoch']) is not int or task['lease_epoch']<1:
            return {}
        p=folder/'workers'/task['task_id']/str(task['lease_epoch'])/'protocol-summary.json'
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    summary=audit(snapshot,load,expected,manifest['limits'],identity)
    usage=json.loads((folder/'gateway-summary.json').read_text(encoding='utf-8'))['durable_budget']
    summary.update(created_at=now(),run_id=snapshot['run_id'],case_id=case_id,execution_build=identity,
        scope='all_real_initial_and_historical_roles_dispute_coverage',seeded_results=False,
        requests=usage['requests'],charged_or_reserved_tokens=usage['charged_or_reserved_tokens'],
        unsettled_requests=usage['unsettled_requests'],input_hash=snapshot['input_hash'],
        prospective_manifest_sha256=digest(manifest),private_snapshot_sha256=digest((folder/'run.json').read_bytes()),
        quality_claim=False,business_signoff=False)
    save(output,summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('folder','manifest','case_id','output'): parser.add_argument(name)
    args=parser.parse_args(); result=verify(args.folder,args.manifest,args.case_id,args.output)
    print(json.dumps({k:result[k] for k in ('status','execution_status','checks','coverage','counts')}))
    raise SystemExit(0 if result['status']=='pass' else 3 if result['status']=='partial' else 1)
