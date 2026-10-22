"""Verify real third-round roles separately from an explicitly seeded baseline."""
import argparse
import json
from pathlib import Path

from medic.domain.contracts import AssessmentInput
from medic.domain.disputes import pending_topics
from medic.domain.validation import release_gate
from medic.p0.common import digest, now, save
from medic.prompts import load_bundles


def verify(folder, output):
    folder = Path(folder)
    snapshot = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
    probe = json.loads((folder / 'probe.json').read_text(encoding='utf-8'))
    source = AssessmentInput.model_validate(snapshot['source'])
    if not source.synthetic or probe['initial_results_are_model_outputs'] is not False:
        raise ValueError('requires_explicit_synthetic_seeded_protocol_probe')
    gate = release_gate(source, snapshot['results'], snapshot['tasks'], snapshot['mode'], snapshot['mailbox'], snapshot['rework_plan'])
    tasks = {t['role']: t for t in snapshot['tasks']}
    actual_roles = ('coordinator', 'challenger', 'reevaluator', 'judge')
    expected_hash = digest(load_bundles()[source.versions.prompt_bundle_version])
    completed = {(e['task_id'], e['data'].get('lease_epoch')) for e in snapshot['events'] if e['kind'] == 'task_completed'}
    protocols = []
    for role in actual_roles:
        t = tasks[role]
        file = folder / 'workers' / t['task_id'] / str(t['lease_epoch']) / 'protocol-summary.json'
        p = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
        protocols.append(p.get('prompt_bundle_hash') == expected_hash and p.get('context_hash') == t['context_hash']
                         and any(e['method'] == 'turn/completed' for e in p.get('events', [])))
    second = snapshot['results'].get(tasks['reevaluator']['task_id'], {})
    judge = snapshot['results'].get(tasks['judge']['task_id'], {})
    answered = [m for m in snapshot['mailbox'] if m['kind'] == 'answer'
                and m['sender_task_id'] == tasks['challenger']['task_id'] and m['sender_lease_epoch'] == tasks['challenger']['lease_epoch']]
    checks = {
        'synthetic_seeded_baseline_explicit': probe['run_id'] == snapshot['run_id'],
        'unexecuted_initial_roles_still_fixture': all(tasks[r]['execution_mode'] == 'fixture' for r in ('evaluator', 'challenger_scan')),
        'four_real_third_round_roles_completed': all(tasks[r]['business_round'] == 3 and tasks[r]['status'] == 'completed'
            and tasks[r]['execution_mode'] == 'live' and (tasks[r]['task_id'], tasks[r]['lease_epoch']) in completed for r in actual_roles),
        'four_distinct_real_threads': len({tasks[r]['thread_id'] for r in actual_roles}) == 4,
        'worker_protocol_and_prompt_verified': all(protocols),
        'real_coordinator_selected_rework': any(d['decision'] == 'rework' for d in (snapshot.get('rework_plan') or {}).get('decisions', {}).values()),
        'real_challenger_answered_mailbox': bool(answered) and not pending_topics(snapshot['mailbox']),
        'old_blocking_dispute_explicitly_addressed': second.get('revision') == 3 and len(second.get('prior_dispute_responses', [])) == 1,
        'previous_results_archived': len(snapshot.get('task_history', [])) >= 4,
        'fixture_not_misrepresented_as_full_live_success': not gate['passed'] and snapshot['state'] == 'awaiting_human_review',
        'stored_gate_matches': snapshot['report']['gate'] == gate,
    }
    gateway = json.loads((folder / 'gateway-summary.json').read_text(encoding='utf-8'))
    summary = {'created_at': now(), 'status': 'pass' if all(checks.values()) else 'fail', 'run_id': snapshot['run_id'],
        'scope': 'real_rework_and_mailbox_protocol_on_seeded_baseline_not_full_live_assessment',
        'initial_results_are_model_outputs': False, 'quality_claim': False, 'checks': checks,
        'current_gate': gate, 'third_round_grade': second.get('grade'), 'third_round_judge': judge.get('verdict'),
        'old_dispute_dispositions': [r['disposition'] for r in second.get('prior_dispute_responses', [])],
        'mailbox_messages': len(snapshot['mailbox']), 'real_answer_count': len(answered),
        'total_requests': gateway['durable_budget']['requests'], 'charged_or_reserved_tokens': gateway['durable_budget']['charged_or_reserved_tokens'],
        'private_snapshot_hash': digest((folder / 'run.json').read_bytes())}
    save(output, summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder')
    parser.add_argument('output')
    args = parser.parse_args()
    result = verify(args.folder, args.output)
    print(json.dumps({'status': result['status'], 'checks': result['checks'], 'gate': result['current_gate']}))
    raise SystemExit(0 if result['status'] == 'pass' else 1)
