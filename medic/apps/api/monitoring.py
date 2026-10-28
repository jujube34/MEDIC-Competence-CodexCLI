"""Authorized progress projection: never expose raw prompts or event payloads."""
import json
import re
from collections import Counter

from medic.apps.api.database import AppError
from medic.domain.contracts import AssessmentInput
from medic.domain.graph import required_graph
from medic.p0.common import digest

KINDS = frozenset('run_created task_dispatched task_leased task_completed task_failed task_requeued '
                  'tool_invoked result_submitted result_rejected finalization_requested '
                  'preview_published run_cancelled run_failed plan_dispatched '
                  'service_attempt_started service_attempt_finished rework_prepared '
                  'request_reserved request_settled agent_dispatched_task agent_dispatched_plan '
                  'execution_bound finalization_bound attempt_requeued run_deadline_exhausted '
                  'manual_assessment_required rework_planning_requested business_result_invalidated '
                  'coordinator_rework_decision'.split())


def execution_log(service, principal, ident, after=0, limit=100):
    with service.db.transaction(principal.tenant) as db:
        row = service._assessment(db, principal, ident)
    source = AssessmentInput.model_validate_json(row['source_json'])
    output = {'assessment_id': ident, 'state': 'creation_pending', 'events': [],
              'next_cursor': after, 'has_more': False, 'counts': {}, 'tasks': [],
              'expected_tasks': len(required_graph(source)) + 1}
    if row['run_id']:
        with service.runtime.read(row['run_id']) as db:
            run = db.execute('SELECT state,input_hash FROM runs WHERE run_id=?', (row['run_id'],)).fetchone()
            if not run or run['input_hash'] != digest(json.loads(row['source_json'])):
                raise AppError('assessment_integrity_failure', 409)
            tasks = [dict(item) for item in db.execute(
                'SELECT task_id,role,material_id,competency_id,status,lease_epoch FROM tasks WHERE run_id=? ORDER BY task_id',
                (row['run_id'],))]
            output.update(state=run['state'], tasks=tasks, counts=dict(Counter(item['status'] for item in tasks)))
            requests = db.execute('SELECT status,charged FROM model_requests WHERE run_id=?', (row['run_id'],)).fetchall()
            output['usage'] = {'requests': len(requests),
                               'reported_tokens': sum(item['charged'] or 0 for item in requests if item['status'] == 'reported'),
                               'unknown_usage_requests': sum(item['status'] != 'reported' for item in requests)}
            events = db.execute('SELECT seq,task_id,kind,data_json,created FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                                (row['run_id'], after, limit + 1)).fetchall()
            output['has_more'] = len(events) > limit
            by_id = {item['task_id']: item for item in tasks}
            for event in events[:limit]:
                payload = json.loads(event['data_json'])
                child = by_id.get(payload.get('child_task_id')) if event['kind'] == 'agent_dispatched_task' else None
                task = child or by_id.get(event['task_id'], {})
                item = {key: event[key] for key in ('seq', 'task_id', 'created')}
                if child:
                    item.update(task_id=child['task_id'],caused_by_task_id=event['task_id'])
                item.update(kind=event['kind'] if event['kind'] in KINDS else 'runtime_event',
                            role=task.get('role'), competency_id=task.get('competency_id'),
                            material_id=task.get('material_id'))
                # Positive projection only. Never forward arbitrary error text,
                # quotes, tool arguments, context, credentials or hidden reasoning.
                if event['kind'] == 'tool_invoked':
                    tool = payload.get('tool')
                    if isinstance(tool, str) and re.fullmatch('[a-z_]{1,80}', tool):
                        item['tool'] = tool
                if type(payload.get('lease_epoch')) is int:
                    item['lease_epoch'] = payload['lease_epoch']
                code = payload.get('error_code')
                if event['kind'] == 'task_failed' and isinstance(code, str) and re.fullmatch('[a-z][a-z0-9_]{0,99}', code):
                    item['error_code'] = code
                output['events'].append(item)
            if output['events']:
                output['next_cursor'] = output['events'][-1]['seq']
    # Revocation during the runtime read must prevent release too.
    with service.db.transaction(principal.tenant) as db:
        service._assessment(db, principal, ident)
    return output
