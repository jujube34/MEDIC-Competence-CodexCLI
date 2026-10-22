"""Synthetic contract probes; do not represent real model judgments."""
import copy
import tempfile
import unittest
from pathlib import Path

from medic.domain.contracts import AssessmentInput, CHECKS
from medic.domain.validation import release_gate, InvalidResult
from medic.runtime.store import Store, Denied
from medic.tests import test_runtime
from medic.tools.service import ToolService


class MailboxContracts(unittest.TestCase):
    plan = test_runtime.RuntimeContracts.plan
    assessment = test_runtime.RuntimeContracts.assessment
    read_all = test_runtime.RuntimeContracts.read_all
    submit_complete = test_runtime.RuntimeContracts.submit_complete

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'runtime.sqlite3')
        self.source = test_runtime.fixture()
        self.source['versions']['prompt_bundle_version'] = 'medic-p2-roles-v5'
        self.run = self.store.create_run(self.source)
        self.coordinator = self.store.claim_next(self.run)
        self.ids = self.plan()
        first = self.store.lease(self.ids['evaluator'])
        self.ref = self.assessment(first)['supporting_evidence'][1]
        self.submit_complete(first, self.assessment(first))
        self.scan = self.store.lease(self.ids['challenger_scan'])
        self.submit_complete(self.scan, {'task_id': self.scan['task_id'], 'context_hash': self.scan['context']['context_hash'],
            'observations': [{'finding': 'Self-report only.', 'evidence_refs': [self.ref]}], 'open_questions': []})
        self.challenger = self.store.lease(self.ids['challenger'])
        self.read_all(self.challenger)
        self.challenge = {'task_id': self.challenger['task_id'], 'context_hash': self.challenger['context']['context_hash'],
            'independent_findings_hash': self.challenger['context']['independent_findings_hash'],
            'challenges': [{'challenge_id': 'challenge-1', 'claim_id': 'claim-2', 'kind': 'attribution', 'severity': 'blocking',
                'evidence_refs': [self.ref], 'falsification_question': 'Is independent ownership proven?', 'explanation': 'Only self-report.'}],
            'checked_claim_ids': ['claim-1', 'claim-2'], 'conclusion': 'Keep attribution limited.'}
        self.submission = self.store.submit(self.challenger, self.challenge)
        self.question = {'message_id': 'question-1', 'recipient_task_id': self.ids['reevaluator'],
            'challenge_id': 'challenge-1', 'challenge_result_hash': self.submission['result_hash'], 'kind': 'question',
            'reply_to': None, 'text': 'Can the claim remain explicitly limited to this self-reported material?', 'evidence_refs': [self.ref]}

    def tearDown(self):
        self.temp.cleanup()

    def post(self, binding, value):
        return ToolService(self.store, binding).call('runtime_post_message', value)

    def begin_second(self):
        self.store.complete(self.challenger, self.submission['receipt'], 'unit-challenger', 'unit-turn')
        second = self.store.lease(self.ids['reevaluator'])
        self.read_all(second)
        return second

    def answer(self):
        return {**self.question, 'message_id': 'answer-1', 'recipient_task_id': self.ids['challenger'],
                'kind': 'answer', 'reply_to': 'question-1', 'text': 'Yes. No independent corroboration is claimed.'}

    def finish(self, second):
        value = self.assessment(second, 2, [{'challenge_id': 'challenge-1', 'disposition': 'accepted',
            'reason': 'Keep self-report boundary.', 'evidence_refs': [self.ref]}])
        receipt = self.store.submit(second, value)['receipt']
        self.store.complete(second, receipt, 'unit-reevaluator', 'unit-turn')
        judge = self.store.lease(self.ids['judge'])
        self.submit_complete(judge, {'task_id': judge['task_id'], 'context_hash': judge['context']['context_hash'],
            'goal_version': self.source['goal']['version'], 'candidate_hash': judge['context']['candidate_hash'],
            'checks': [{'check': c, 'passed': True, 'reason': 'Synthetic test judgment'} for c in CHECKS],
            'blocking_issues': [], 'verdict': 'pass', 'reason': 'Synthetic fixture', 'next_tasks': []})
        return self.store.snapshot(self.run)

    def gate(self, snapshot):
        return release_gate(AssessmentInput.model_validate(snapshot['source']), snapshot['results'], snapshot['tasks'],
                            snapshot['mode'], snapshot.get('mailbox'))

    def test_scoped_exchange_is_idempotent_and_bound_to_judge(self):
        first = self.post(self.challenger, self.question)
        self.assertFalse(first['duplicate'])
        self.assertTrue(self.post(self.challenger, self.question)['duplicate'])
        with self.assertRaisesRegex(Denied, 'idempotency_conflict'):
            self.post(self.challenger, self.question | {'text': 'Changed'})
        second = self.begin_second()
        self.assertEqual(second['context']['mailbox'], [first['message']])
        self.post(second, self.answer())
        snapshot = self.finish(second)
        self.assertTrue(self.gate(snapshot)['passed'], self.gate(snapshot))
        self.store.publish(self.run, {'gate': self.gate(snapshot), 'mailbox': []})
        self.assertEqual(len(self.store.snapshot(self.run)['report']['mailbox']), 2)
        tampered = copy.deepcopy(snapshot)
        tampered['mailbox'][1]['text'] = 'A different answer after Judge review'
        self.assertFalse(self.gate(tampered)['passed'])

    def test_unanswered_question_blocks_even_a_passing_judge(self):
        self.post(self.challenger, self.question)
        snapshot = self.finish(self.begin_second())
        gate = self.gate(snapshot)
        self.assertIn('unanswered_dispute_message:synthetic-material-1:M2', gate['failures'])
        self.store.publish(self.run, {'gate': gate})
        self.assertEqual(self.store.snapshot(self.run)['state'], 'awaiting_human_review')
        # Omitting the transcript also invalidates the reviewed candidate hash.
        snapshot['mailbox'] = []
        self.assertFalse(self.gate(snapshot)['passed'])

    def test_scope_evidence_challenge_and_old_lease_boundaries(self):
        with self.assertRaisesRegex(Denied, 'recipient_out_of_scope'):
            self.post(self.challenger, self.question | {'recipient_task_id': self.ids['judge']})
        other = self.store.create_run(self.source)
        with self.assertRaisesRegex(Denied, 'recipient_out_of_scope'):
            self.post(self.challenger, self.question | {'recipient_task_id': self.store.snapshot(other)['tasks'][0]['task_id']})
        with self.assertRaisesRegex(Denied, 'unknown_challenge'):
            self.post(self.challenger, self.question | {'challenge_id': 'invented'})
        with self.assertRaises(InvalidResult):
            self.post(self.challenger, self.question | {'evidence_refs': [self.ref | {'evidence_id': 'foreign-employee'}]})
        self.post(self.challenger, self.question)
        second = self.begin_second()
        with self.assertRaises(Denied):
            self.post(self.challenger, self.question | {'message_id': 'late'})
        with self.assertRaisesRegex(Denied, 'reply_sequence_invalid'):
            self.post(second, self.answer() | {'reply_to': 'invented'})

    def test_identical_result_from_new_attempt_can_open_topic(self):
        self.store.fail(self.challenger, 'synthetic_receipt_failure')
        self.store.recover(self.run)
        replacement = self.store.lease(self.ids['challenger'])
        self.read_all(replacement)
        result = self.store.submit(replacement, self.challenge)
        self.assertEqual(result['result_hash'], self.submission['result_hash'])
        self.assertFalse(self.post(replacement, self.question)['duplicate'])
        with self.assertRaises(Denied):
            self.post(self.challenger, self.question)

    def test_two_exchange_limit_survives_store_restart(self):
        self.post(self.challenger, self.question)
        second = self.begin_second()
        self.post(second, self.answer())
        # Isolate mailbox authorization from future business-rework scheduling:
        # emulate a newly active Challenger for the second exchange.
        with self.store.connect(self.run) as db:
            db.execute("UPDATE tasks SET status='submitted' WHERE task_id=?", (self.ids['challenger'],))
        q2 = self.question | {'message_id': 'question-2', 'reply_to': 'answer-1'}
        self.post(self.challenger, q2)
        self.post(second, self.answer() | {'message_id': 'answer-2', 'reply_to': 'question-2'})
        self.store = Store(self.store.path)
        self.assertEqual(len(self.store.snapshot(self.run)['mailbox']), 4)
        with self.assertRaisesRegex(Denied, 'dialogue_budget_exhausted'):
            self.post(self.challenger, self.question | {'message_id': 'question-3', 'reply_to': 'answer-2'})
