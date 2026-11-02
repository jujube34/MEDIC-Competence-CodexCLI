"""Prospective v17 contracts; all workers and materials here are synthetic."""
import copy
import json
import unittest
from unittest.mock import patch

from medic.domain.contracts import AssessmentInput, result_type
from medic.domain.graph import scoped_spans
from medic.domain.validation import InvalidResult
from medic.runtime.store import Denied, Store
from medic.tests import test_aggregation, test_multiscope, test_rework, test_runtime
from medic.tests import test_evidence_selection as selection
from medic.tests.test_evidence_selection import TOOL, select_refs
from medic.tools.evidence_selection import resolve_selection, selection_schema
from medic.tools.service import ToolService


class SelectionBoundaries(selection.EvidenceSelectionContracts):
    def test_all_loaded_prompt_bundles_expose_selection_tool(self):
        manifest = json.loads((test_runtime.ROOT / 'medic/prompts/manifest.json').read_text())
        for version in sorted(manifest):
            with self.subTest(version=version):
                source = test_runtime.fixture()
                source['versions']['prompt_bundle_version'] = version
                run = self.store.create_run(source)
                coordinator = self.store.claim_next(run)
                task = self.store.dispatch(coordinator, {
                    'role': 'evaluator', 'material_id': 'synthetic-material-1',
                    'competency_id': 'M2', 'dependencies': [], 'reason': 'Bundle surface test'})
                binding = self.store.lease(task['task_id'])
                service = ToolService(self.store, binding)
                self.assertIn(TOOL, {item['name'] for item in service.definitions()})

    def test_selection_consumes_real_tool_budget(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        with self.store.connect() as db:
            count = db.execute("SELECT count(*) FROM events WHERE task_id=? AND kind='tool_invoked'",
                               (binding['task_id'],)).fetchone()[0]
        for _ in range(31 - count):
            service.call('evidence_list_spans', {})
        receipt = service.call(TOOL, select_refs(self.assessment(binding)))
        self.assertIn('receipt', receipt)
        with self.assertRaisesRegex(Denied, 'task_tool_budget_exhausted'):
            service.call('assessment_get_submission_receipt', {})

    def test_finalization_and_selection_share_repair_budget(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        for _ in range(3):
            with self.store.connect() as db:
                self.store.event(db, self.run, binding['task_id'], 'finalization_requested',
                                 {'lease_epoch': binding['lease_epoch']})
        with self.assertRaisesRegex(Denied, 'result_repair_budget_exhausted'):
            service.call(TOOL, select_refs(self.assessment(binding)))

    def test_selected_result_is_immutable_and_not_automatically_completed(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        value = select_refs(self.assessment(binding))
        receipt = service.call(TOOL, value)
        task = next(item for item in self.store.snapshot(self.run)['tasks'] if item['task_id'] == binding['task_id'])
        self.assertEqual(task['status'], 'submitted')
        changed = copy.deepcopy(value)
        changed['limitations'].append('A different conclusion')
        with self.assertRaisesRegex(Denied, 'immutable_result_conflict'):
            service.call(TOOL, changed)
        with self.assertRaises(Denied):
            self.store.complete(binding, receipt['receipt'] + 'x', 'synthetic-thread', 'synthetic-turn')
        self.store.complete(binding, receipt['receipt'], 'synthetic-thread', 'synthetic-turn')

    def test_resolver_changes_only_typed_references_not_model_reasoning(self):
        binding, _ = self.evaluator()
        value = select_refs(self.assessment(binding))
        value['limitations'] = ['{"evidence_id":"unknown-id"}']
        value['claims'][0]['statement'] = 'Model judgment must remain untouched.'
        before = copy.deepcopy(value)
        model = result_type('evaluator', 'synthetic-material-1')
        spans = scoped_spans(AssessmentInput.model_validate(self.source), 'synthetic-material-1')
        resolved = resolve_selection(model, value, spans)
        self.assertEqual(value, before)
        self.assertEqual(select_refs(resolved), value)
        self.assertEqual(resolved['limitations'], before['limitations'])
        self.assertEqual(model.model_json_schema()['$defs']['EvidenceRef']['required'],
                         ['evidence_id', 'quote', 'span_hash'])
        self.assertEqual(selection_schema(model)['$defs']['EvidenceRef']['required'], ['evidence_id'])


class SelectionMultiMaterial(test_aggregation.AggregationContracts):
    def setUp(self):
        source = test_multiscope.multi_fixture()
        source['versions']['prompt_bundle_version'] = 'medic-p2-roles-v7'
        with patch('medic.tests.test_aggregation.multi_fixture', return_value=source):
            test_aggregation.AggregationContracts.setUp(self)

    def complete(self, binding):
        service = ToolService(self.store, binding)
        for span in scoped_spans(self.source, binding['context']['material_id']):
            service.call('evidence_read_span', {'evidence_id': span.evidence_id})
        receipt = service.call(TOOL, select_refs(self.value(binding)))['receipt']
        self.store.complete(binding, receipt, 'selection-thread-' + binding['task_id'], 'synthetic-turn')

    def test_material_cannot_select_other_material_or_run_evidence(self):
        binding = self.finish_before(('synthetic-material-1', 'M2', 'evaluator'))
        service = ToolService(self.store, binding)
        for span in scoped_spans(self.source, binding['context']['material_id']):
            service.call('evidence_read_span', {'evidence_id': span.evidence_id})
        value = select_refs(self.value(binding))
        for evidence_id in ('second-span-1', 'foreign-run-span'):
            with self.subTest(evidence_id=evidence_id):
                changed = copy.deepcopy(value)
                changed['supporting_evidence'][0]['evidence_id'] = evidence_id
                with self.assertRaisesRegex(ValueError, 'evidence_not_in_task_scope'):
                    service.call(TOOL, changed)
        service.call(TOOL, value)


class SelectionTargetedRework(test_rework.TargetedReworkContracts):
    def setUp(self):
        SelectionMultiMaterial.setUp(self)

    complete = SelectionMultiMaterial.complete


class SelectionPriorDisputes(test_rework.ReworkContracts):
    def setUp(self):
        create_run = Store.create_run

        def create_v7(store, source, *args, **kwargs):
            # Mailbox's historical setup explicitly chooses v5 after loading its
            # fixture, so select v7 at creation rather than patching the loader.
            source['versions']['prompt_bundle_version'] = 'medic-p2-roles-v7'
            return create_run(store, source, *args, **kwargs)

        with patch.object(Store, 'create_run', create_v7):
            super().setUp()

    def third(self, unresolved=False):
        challenger = self.store.claim_next(self.run)
        self.read_all(challenger)
        self.post(challenger, self.question | {
            'message_id': 'answer-1', 'recipient_task_id': self.ids['reevaluator'],
            'kind': 'answer', 'reply_to': 'question-1', 'text': 'Self-report only.'})
        value = copy.deepcopy(self.challenge)
        value.update(task_id=challenger['task_id'], context_hash=challenger['context']['context_hash'], challenges=[])
        service = ToolService(self.store, challenger)
        receipt = service.call(TOOL, select_refs(value))['receipt']
        self.store.complete(challenger, receipt, 'synthetic-third-challenger', 'synthetic-turn')
        second = self.store.claim_next(self.run)
        self.read_all(second)
        service = ToolService(self.store, second)
        value = self.assessment(second, 3)
        value['prior_dispute_responses'] = []
        with self.assertRaisesRegex(InvalidResult, 'cannot_drop_prior_blocking'):
            service.call(TOOL, select_refs(value))
        value['prior_dispute_responses'] = [{
            'challenge_id': 'challenge-1', 'challenge_result_hash': self.submission['result_hash'],
            'disposition': 'unresolved' if unresolved else 'accepted',
            'reason': 'Preserve the self-report restriction.', 'evidence_refs': [self.ref]}]
        receipt = service.call(TOOL, select_refs(value))['receipt']
        self.store.complete(second, receipt, 'synthetic-third-reevaluator', 'synthetic-turn')
        self.judge(self.store.claim_next(self.run), 'pass')
        return self.store.snapshot(self.run)


if __name__ == '__main__':
    unittest.main()
