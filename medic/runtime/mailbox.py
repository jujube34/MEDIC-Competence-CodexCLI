"""Bounded, append-only conversations about persisted challenges."""
import json

from medic.domain.contracts import DisputeMessage
from medic.domain.graph import scoped_spans
from medic.domain.disputes import pending_topics
from medic.domain.validation import validate_refs
from medic.p0.common import digest


def enabled(source):
    return source.versions.prompt_bundle_version in ('medic-p2-roles-v5','medic-p2-roles-v6')


def messages(db, run, material=None, code=None):
    sql, params = 'SELECT value_json FROM mailbox WHERE run_id=?', [run]
    if material is not None:
        sql += ' AND material_id=? AND competency_id=?'
        params += [material, code]
    return [json.loads(r[0]) for r in db.execute(sql + ' ORDER BY seq', tuple(params))]


def post(store, binding, request):
    from medic.runtime.store import Denied
    request = DisputeMessage.model_validate(request)
    with store.connect() as db:
        task, source = store.authorize(db, binding)
        if not enabled(source) or task['role'] not in ('challenger', 'reevaluator'):
            raise Denied('mailbox_role_not_allowed')
        recipient = db.execute('SELECT * FROM tasks WHERE task_id=? AND run_id=?',
                               (request.recipient_task_id, task['run_id'])).fetchone()
        other_role = 'reevaluator' if task['role'] == 'challenger' else 'challenger'
        if (not recipient or recipient['role'] != other_role or recipient['material_id'] != task['material_id']
                or recipient['competency_id'] != task['competency_id']):
            raise Denied('mailbox_recipient_out_of_scope')
        payload_hash = digest({'sender_task_id': task['task_id'], **request.model_dump()})
        existing = db.execute('SELECT * FROM mailbox WHERE run_id=? AND message_id=?',
                              (task['run_id'], request.message_id)).fetchone()
        if existing:
            if existing['payload_hash'] != payload_hash:
                raise Denied('mailbox_idempotency_conflict')
            return {'message': json.loads(existing['value_json']), 'duplicate': True}
        challenger = task if task['role'] == 'challenger' else recipient
        topic = digest([challenger['task_id'], request.challenge_result_hash, request.challenge_id])
        scoped = messages(db, task['run_id'], task['material_id'], task['competency_id'])
        thread = [m for m in scoped if m['topic_id'] == topic]
        result = db.execute('SELECT * FROM results WHERE task_id=? AND result_hash=? ORDER BY lease_epoch DESC',
                            (challenger['task_id'], request.challenge_result_hash)).fetchone()
        if (not result or (not thread and (result['lease_epoch'] != challenger['lease_epoch']
                                          or challenger['status'] not in ('submitted', 'completed')))):
            raise Denied('mailbox_challenge_result_not_current')
        challenge = next((c for c in json.loads(result['value_json']).get('challenges', [])
                          if c['challenge_id'] == request.challenge_id), None)
        if not challenge:
            raise Denied('mailbox_unknown_challenge')
        if len(thread) >= 4 or len(scoped) >= 16:
            raise Denied('mailbox_dialogue_budget_exhausted')
        if db.execute('SELECT count(*) FROM mailbox WHERE run_id=?', (task['run_id'],)).fetchone()[0] >= 128:
            raise Denied('mailbox_run_budget_exhausted')
        if not thread:
            if request.kind != 'question' or request.reply_to is not None:
                raise Denied('mailbox_question_required')
        else:
            previous = thread[-1]
            expected_kind = 'answer' if previous['kind'] == 'question' else 'question'
            if (request.reply_to != previous['message_id'] or request.kind != expected_kind
                    or previous['recipient_task_id'] != task['task_id']):
                raise Denied('mailbox_reply_sequence_invalid')
        validate_refs(request.model_dump(), scoped_spans(source, task['material_id']))
        read = {r[0] for r in db.execute('SELECT evidence_id FROM reads WHERE task_id=? AND lease_epoch=?',
                                        (task['task_id'], task['lease_epoch']))}
        if not {r.evidence_id for r in request.evidence_refs} <= read:
            raise Denied('mailbox_cited_evidence_not_read')
        value = {**request.model_dump(), 'topic_id': topic, 'claim_id': challenge['claim_id'],
                 'material_id': task['material_id'], 'competency_id': task['competency_id'],
                 'sender_task_id': task['task_id'], 'sender_role': task['role'], 'sender_lease_epoch': task['lease_epoch'],
                 'sequence': len(thread) + 1, 'trust': 'untrusted_agent_message'}
        db.execute('INSERT INTO mailbox(run_id,material_id,competency_id,message_id,payload_hash,value_json) VALUES(?,?,?,?,?,?)',
                   (task['run_id'], task['material_id'], task['competency_id'], request.message_id, payload_hash,
                    json.dumps(value, ensure_ascii=False)))
        store.event(db, task['run_id'], task['task_id'], 'dispute_message_posted',
                    {'message_id': request.message_id, 'topic_id': topic, 'sequence': value['sequence'], 'message_hash': digest(value)})
        return {'message': value, 'duplicate': False}
