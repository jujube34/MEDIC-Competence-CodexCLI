"""Business-round and targeted-invalidation contract tests with synthetic workers."""
import copy
import unittest

from medic.domain.contracts import CHECKS
from medic.domain.validation import InvalidResult
from medic.runtime.rework import prepare, decide
from medic.runtime.store import Denied
from medic.tests import test_mailbox
from medic.tools.service import ToolService
from medic.tests import test_aggregation, test_multiscope
from unittest.mock import patch


class ReworkContracts(unittest.TestCase):
    setUp = test_mailbox.MailboxContracts.setUp
    tearDown = test_mailbox.MailboxContracts.tearDown
    plan = test_mailbox.MailboxContracts.plan
    assessment = test_mailbox.MailboxContracts.assessment
    read_all = test_mailbox.MailboxContracts.read_all
    submit_complete = test_mailbox.MailboxContracts.submit_complete
    begin_second = test_mailbox.MailboxContracts.begin_second
    post = test_mailbox.MailboxContracts.post

    def gate(self, snapshot):
        from medic.domain.contracts import AssessmentInput
        from medic.domain.validation import release_gate
        return release_gate(AssessmentInput.model_validate(snapshot['source']), snapshot['results'], snapshot['tasks'],
                            snapshot['mode'], snapshot['mailbox'], snapshot['rework_plan'])

    def judge(self, binding, verdict):
        self.submit_complete(binding, {'task_id': binding['task_id'], 'context_hash': binding['context']['context_hash'],
            'goal_version': self.source['goal']['version'], 'candidate_hash': binding['context']['candidate_hash'],
            'checks': [{'check': c, 'passed': verdict == 'pass', 'reason': 'Synthetic dispute check'} for c in CHECKS],
            'blocking_issues': [] if verdict == 'pass' else ['Attribution dispute still unresolved'],
            'verdict': verdict, 'reason': 'Clarify the self-report boundary using the same source.', 'next_tasks': []})

    def initial_review(self):
        second = self.begin_second()
        self.post(second, self.question | {'recipient_task_id': self.ids['challenger'],
                                          'text': 'Please clarify whether the claim asserts independent corroboration.'})
        value = self.assessment(second, 2, [{'challenge_id': 'challenge-1', 'disposition': 'unresolved',
                                           'reason': 'Need challenger clarification.', 'evidence_refs': [self.ref]}])
        receipt = self.store.submit(second, value)['receipt']
        self.store.complete(second, receipt, 'unit-second', 'unit-turn')
        self.judge(self.store.lease(self.ids['judge']), 'rework')
        return self.store.snapshot(self.run)

    def rework(self, decision='rework'):
        before = self.initial_review()
        self.assertTrue(prepare(self.store, self.run))
        coordinator = self.store.claim_next(self.run)
        self.assertEqual(coordinator['context']['business_round'], 3)
        service = ToolService(self.store, coordinator)
        self.assertNotIn('runtime_dispatch_task', {t['name'] for t in service.definitions()})
        with self.assertRaisesRegex(InvalidResult, 'decisions_incomplete'):
            self.store.submit(coordinator, {})
        request = {'judge_task_id': self.ids['judge'], 'decision': decision, 'reason': 'Synthetic bounded rework decision'}
        outcome = service.call('runtime_resolve_rework', request)
        self.assertEqual(service.call('runtime_resolve_rework', request), outcome)
        with self.assertRaises(Denied):
            service.call('runtime_resolve_rework', request | {'decision': 'human_review' if decision == 'rework' else 'rework'})
        self.assertIsNone(self.store.claim_next(self.run), 'children wait until Coordinator completes')
        receipt = self.store.submit(coordinator, {})['receipt']
        self.store.complete(coordinator, receipt, 'unit-rework-coordinator', 'unit-turn')
        return before

    def third(self, unresolved=False):
        challenger = self.store.claim_next(self.run)
        self.assertEqual(challenger['context']['role'], 'challenger')
        self.read_all(challenger)
        self.post(challenger, self.question | {'message_id': 'answer-1', 'recipient_task_id': self.ids['reevaluator'],
            'kind': 'answer', 'reply_to': 'question-1', 'text': 'Only a self-reported material claim, no independent corroboration.'})
        value = copy.deepcopy(self.challenge)
        value.update(task_id=challenger['task_id'], context_hash=challenger['context']['context_hash'], challenges=[])
        receipt = self.store.submit(challenger, value)['receipt']
        self.store.complete(challenger, receipt, 'unit-third-challenger', 'unit-turn')
        second = self.store.claim_next(self.run)
        self.read_all(second)
        value = self.assessment(second, 3)
        value['prior_dispute_responses'] = []
        with self.assertRaisesRegex(InvalidResult, 'cannot_drop_prior_blocking'):
            self.store.submit(second, value)
        value['prior_dispute_responses'] = [{'challenge_id': 'challenge-1', 'challenge_result_hash': self.submission['result_hash'],
            'disposition': 'unresolved' if unresolved else 'accepted', 'reason': 'Keep the explicit self-report restriction.', 'evidence_refs': [self.ref]}]
        receipt = self.store.submit(second, value)['receipt']
        self.store.complete(second, receipt, 'unit-third-second', 'unit-turn')
        self.judge(self.store.claim_next(self.run), 'pass')
        return self.store.snapshot(self.run)

    def test_third_round_preserves_history_and_cannot_drop_old_disputes(self):
        before = self.rework()
        snapshot = self.third()
        self.assertTrue(self.gate(snapshot)['passed'], self.gate(snapshot))
        self.assertFalse(prepare(self.store, self.run), 'no fourth business round')
        self.assertEqual(len(snapshot['task_history']), 4)
        old_second = next(t for t in snapshot['task_history'] if t['task_id'] == self.ids['reevaluator'])
        self.assertEqual(old_second['result'], before['results'][self.ids['reevaluator']])
        first_id = self.ids['evaluator']
        self.assertEqual(snapshot['results'][first_id], before['results'][first_id])
        self.assertEqual(next(t for t in snapshot['tasks'] if t['task_id'] == first_id)['lease_epoch'], 1)
        with self.store.connect(self.run) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM results WHERE task_id=?', (self.ids['reevaluator'],)).fetchone()[0], 2)
        self.store.publish(self.run, {'gate': self.gate(snapshot)})
        self.assertEqual(self.store.snapshot(self.run)['state'], 'ready_for_human_review')

    def test_third_round_unresolved_issue_goes_to_human_without_fourth_round(self):
        self.rework()
        snapshot = self.third(unresolved=True)
        self.assertIn('unresolved_prior_blocking_challenge:synthetic-material-1:M2', self.gate(snapshot)['failures'])
        self.assertFalse(prepare(self.store, self.run))
        self.store.publish(self.run, {'gate': self.gate(snapshot)})
        self.assertEqual(self.store.snapshot(self.run)['state'], 'awaiting_human_review')

    def test_coordinator_can_route_to_human_without_reexecuting_children(self):
        self.rework('human_review')
        snapshot = self.store.snapshot(self.run)
        self.assertTrue(all(t['status'] == 'completed' for t in snapshot['tasks']))
        self.assertIn('coordinator_requested_human_review', self.gate(snapshot)['failures'])
        self.assertEqual(len(snapshot['task_history']), 1)

    def test_technical_attempt_limit_is_separate_from_business_round(self):
        self.rework()
        for expected_epoch in (2, 3, 4):
            binding = self.store.claim_next(self.run)
            self.assertEqual(binding['lease_epoch'], expected_epoch)
            self.assertEqual(binding['context']['business_round'], 3)
            self.store.fail(binding, 'synthetic_worker_failure')
            if expected_epoch < 4:
                self.store.recover(self.run)
        with self.assertRaisesRegex(Denied, 'attempt_budget_exhausted'):
            self.store.recover(self.run)


class TargetedReworkContracts(unittest.TestCase):
    tearDown = test_aggregation.AggregationContracts.tearDown
    complete = test_aggregation.AggregationContracts.complete
    finish_before = test_aggregation.AggregationContracts.finish_before

    def setUp(self):
        fixture = test_multiscope.multi_fixture()
        fixture['versions']['prompt_bundle_version'] = 'medic-p2-roles-v5'
        with patch('medic.tests.test_aggregation.multi_fixture', return_value=fixture):
            test_aggregation.AggregationContracts.setUp(self)

    def value(self, binding):
        value = test_aggregation.AggregationContracts.value(self, binding)
        c = binding['context']
        if c['business_round'] == 3 and c['role'] in ('aggregator', 'reevaluator'):
            value['revision'] = 3
            if c['role'] == 'reevaluator':
                value['prior_dispute_responses'] = []
        if c['role'] == 'judge' and c['business_round'] != 3 and c['material_id'] == 'synthetic-material-1' and c['competency_id'] == 'M2':
            value.update(verdict='rework', blocking_issues=['Clarify one material-scoped attribution issue.'])
            value['checks'][4]['passed'] = False
        return value

    def test_runner_invalidates_only_affected_scope_and_aggregate_downstream(self):
        from medic.runtime.runner import execute
        from medic.domain.contracts import AssessmentInput
        from medic.domain.validation import release_gate
        before = self.finish_before()
        calls, harness = [], self
        class Worker:
            def run(self, task_id, binding=None):
                c = binding['context']
                calls.append((c['material_id'], c['competency_id'], c['role']))
                if c['role'] == 'coordinator':
                    for candidate in c['rework_plan']['candidates']:
                        decide(harness.store, binding, {'judge_task_id': candidate['judge_task_id'], 'decision': 'rework',
                                                        'reason': 'Synthetic targeted clarification'})
                    snapshot = harness.store.snapshot(harness.run)
                    queued = [t for t in snapshot['tasks'] if t['status'] == 'queued']
                    harness.assertEqual(len(queued), 7)
                    harness.assertTrue(all(t['competency_id'] == 'M2' for t in queued))
                    harness.assertTrue(all(t['role'] not in ('evaluator', 'challenger_scan') for t in queued))
                    with harness.assertRaises(Denied):
                        harness.store.publish(harness.run, {'gate': {'passed': True}})
                    receipt = harness.store.submit(binding, {})['receipt']
                    harness.store.complete(binding, receipt, 'unit-targeted-coordinator', 'unit-turn')
                else:
                    harness.complete(binding)
                return {'status': 'completed'}
        after = execute(self.store, self.run, Worker())
        self.assertEqual(after['state'], 'ready_for_human_review', after.get('report'))
        self.assertEqual(len(calls), 8)
        self.assertEqual(len(after['task_history']), 8)
        self.assertFalse(prepare(self.store, self.run))
        self.assertTrue(release_gate(AssessmentInput.model_validate(after['source']), after['results'], after['tasks'],
                                    after['mode'], after['mailbox'], after['rework_plan'])['passed'])
        for task in before['tasks']:
            if task['competency_id'] == 'D1':
                self.assertEqual(after['results'][task['task_id']], before['results'][task['task_id']])
