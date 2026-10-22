"""One bounded business-rework pass, planned by the real Coordinator."""
import json

from medic.domain.contracts import AssessmentInput, ReworkDecision
from medic.domain.disputes import pending_topics
from medic.p0.common import digest
from medic.runtime.mailbox import enabled, messages


def cycle(db, task):
    row = db.execute('SELECT * FROM task_cycles WHERE task_id=?', (task['task_id'],)).fetchone()
    return dict(row) if row else {'task_id': task['task_id'], 'business_round': 2 if task['role'] in ('reevaluator', 'judge') else 1,
                                 'attempts': task['lease_epoch'], 'baseline_json': None}


def plan(db, run):
    row = db.execute('SELECT * FROM rework_plans WHERE run_id=?', (run,)).fetchone()
    return {'status': row['status'], 'candidates': json.loads(row['candidates_json']),
            'decisions': json.loads(row['decisions_json'])} if row else None


def archive(db, task):
    value = dict(task)
    value.pop('token_hash', None)
    value['cycle'] = cycle(db, task)
    value['read_evidence_ids'] = [r[0] for r in db.execute('SELECT evidence_id FROM reads WHERE task_id=? AND lease_epoch=?',
                                                         (task['task_id'], task['lease_epoch']))]
    db.execute('INSERT OR IGNORE INTO task_history VALUES(?,?,?)',
               (task['task_id'], task['lease_epoch'], json.dumps(value, ensure_ascii=False)))


def reset(db, task, baseline=None):
    archive(db, task)
    db.execute("UPDATE tasks SET status='queued',token_hash=NULL,lease_until=NULL,context_hash=NULL,candidate_hash=NULL,"
               "context_json=NULL,result_hash=NULL,execution_mode=NULL,error=NULL,thread_id=NULL,turn_id=NULL WHERE task_id=?", (task['task_id'],))
    db.execute('INSERT OR IGNORE INTO task_cycles VALUES(?,?,?,?)', (task['task_id'], 3, 0, None))
    db.execute('UPDATE task_cycles SET business_round=3,attempts=0,baseline_json=? WHERE task_id=?',
               (json.dumps(baseline, ensure_ascii=False) if baseline else None, task['task_id']))
    db.execute('UPDATE outbox SET delivered=0 WHERE task_id=?', (task['task_id'],))


def prepare(store, run):
    """After all initial nodes finish, allow one Coordinator rework decision turn."""
    with store.connect(run) as db:
        store.verify_build(db, run)
        row = db.execute('SELECT * FROM runs WHERE run_id=?', (run,)).fetchone()
        if not row or row['report_json'] or row['state'] in ('failed', 'cancelled') or plan(db, run):
            return False
        source = AssessmentInput.model_validate_json(row['input_json'])
        if not enabled(source):
            return False
        store.check_deadline(db, run)
        tasks = [dict(t) for t in db.execute('SELECT * FROM tasks WHERE run_id=?', (run,))]
        if not tasks or any(t['status'] != 'completed' for t in tasks):
            return False
        transcript = messages(db, run)
        candidates = []
        for task in tasks:
            if task['role'] != 'judge':
                continue
            review = json.loads(db.execute('SELECT value_json FROM results WHERE task_id=? AND lease_epoch=?',
                                           (task['task_id'], task['lease_epoch'])).fetchone()[0])
            pending = pending_topics([m for m in transcript if m['material_id'] == task['material_id']
                                      and m['competency_id'] == task['competency_id']])
            if review['verdict'] == 'rework' or pending:
                candidates.append({'judge_task_id': task['task_id'], 'material_id': task['material_id'],
                    'competency_id': task['competency_id'], 'review_hash': digest(review), 'verdict': review['verdict'],
                    'blocking_issues': review['blocking_issues'], 'reason': review['reason'], 'pending_topics': pending,
                    'next_tasks': review['next_tasks']})
        if not candidates:
            return False
        db.execute('INSERT INTO rework_plans VALUES(?,?,?,?)', (run, 'planning', json.dumps(candidates, ensure_ascii=False), '{}'))
        coordinator = next(t for t in tasks if t['role'] == 'coordinator')
        reset(db, coordinator)
        db.execute("UPDATE runs SET state='planning' WHERE run_id=?", (run,))
        store.event(db, run, coordinator['task_id'], 'rework_planning_requested', {'candidate_count': len(candidates), 'business_round': 3})
        return True


def decide(store, binding, request):
    from medic.runtime.store import Denied
    request = ReworkDecision.model_validate(request)
    with store.connect() as db:
        coordinator, source = store.authorize(db, binding)
        current = plan(db, coordinator['run_id'])
        if coordinator['role'] != 'coordinator' or not enabled(source) or not current or current['status'] != 'planning':
            raise Denied('rework_planning_not_authorized')
        candidate = next((c for c in current['candidates'] if c['judge_task_id'] == request.judge_task_id), None)
        if not candidate:
            raise Denied('rework_candidate_out_of_scope')
        decisions = current['decisions']
        if request.judge_task_id in decisions:
            previous = decisions[request.judge_task_id]
            if any(previous[k] != request.model_dump()[k] for k in ('decision', 'reason')):
                raise Denied('rework_decision_immutable')
            return previous
        affected = []
        if request.decision == 'rework':
            tasks = [dict(t) for t in db.execute('SELECT * FROM tasks WHERE run_id=?', (coordinator['run_id'],))]
            scope = (candidate['material_id'], candidate['competency_id'])
            root = [t for t in tasks if (t['material_id'], t['competency_id']) == scope and t['role'] in ('challenger', 'reevaluator', 'judge')]
            affected_ids = {t['task_id'] for t in root}
            while True:
                expanded = affected_ids | {t['task_id'] for t in tasks if set(json.loads(t['dependencies'])) & affected_ids}
                if expanded == affected_ids:
                    break
                affected_ids = expanded
            affected = [t['task_id'] for t in tasks if t['task_id'] in affected_ids]
            second = next(t for t in root if t['role'] == 'reevaluator')
            # A previously selected material may already have invalidated this
            # aggregate. Its fresh Aggregator becomes the baseline in that case.
            refreshing_aggregate = any(t['role'] == 'aggregator' and (t['material_id'], t['competency_id']) == scope
                                       and t['status'] == 'queued' for t in tasks)
            baselines = {}
            for affected_scope in {(t['material_id'], t['competency_id']) for t in tasks if t['task_id'] in affected_ids}:
                predecessors = {t['role']: t for t in tasks if (t['material_id'], t['competency_id']) == affected_scope}
                values = {}
                for role in ('reevaluator', 'challenger'):
                    predecessor = predecessors[role]
                    result = db.execute('SELECT value_json FROM results WHERE task_id=? AND lease_epoch=?',
                                        (predecessor['task_id'], predecessor['lease_epoch'])).fetchone()
                    values[role] = json.loads(result[0]) if result else None
                if values['reevaluator'] and values['challenger']:
                    baselines[affected_scope] = {'assessment': values['reevaluator'], 'challenge': values['challenger'],
                        'use_as_evaluator': affected_scope == scope and not refreshing_aggregate}
            for task in tasks:
                if task['task_id'] not in affected_ids:
                    continue
                c = cycle(db, task)
                if task['status'] == 'queued' and c['business_round'] == 3 and c['attempts'] == 0:
                    continue
                if task['status'] != 'completed' or c['business_round'] >= 3:
                    raise Denied('business_round_budget_exhausted_or_task_active')
                own_baseline = baselines.get((task['material_id'], task['competency_id']))
                reset(db, task, own_baseline)
                store.event(db, task['run_id'], task['task_id'], 'business_result_invalidated',
                            {'previous_lease_epoch': task['lease_epoch'], 'previous_result_hash': task['result_hash'], 'business_round': 3})
        decision = {**request.model_dump(), 'affected_task_ids': affected}
        decisions[request.judge_task_id] = decision
        db.execute('UPDATE rework_plans SET decisions_json=? WHERE run_id=?',
                   (json.dumps(decisions, ensure_ascii=False), coordinator['run_id']))
        store.event(db, coordinator['run_id'], coordinator['task_id'], 'coordinator_rework_decision', decision)
        return decision
