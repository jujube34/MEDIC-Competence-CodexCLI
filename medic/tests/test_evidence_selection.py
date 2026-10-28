"""Real Store/tool contract tests; synthetic inputs, no model invocation."""
import copy
import json
import unittest
import tempfile
from pathlib import Path
import jsonschema

from medic.tests import test_runtime
from medic.runtime.store import Store, Denied
from medic.domain.validation import InvalidResult
from medic.tools.service import ToolService

TOOL = 'assessment_submit_by_evidence_id'


def select_refs(value):
    """Test input producer: explicit whole-span selection, never a runtime repair."""
    if isinstance(value, dict):
        if set(value) == {'evidence_id', 'span_hash', 'quote'}:
            return {'evidence_id': value['evidence_id']}
        return {key: select_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [select_refs(item) for item in value]
    return value


class EvidenceSelectionContracts(unittest.TestCase):
    tearDown = test_runtime.RuntimeContracts.tearDown
    plan = test_runtime.RuntimeContracts.plan
    assessment = test_runtime.RuntimeContracts.assessment
    read_all = test_runtime.RuntimeContracts.read_all

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.source = test_runtime.fixture()
        self.source['versions']['prompt_bundle_version'] = 'medic-p2-roles-v7'
        self.run = self.store.create_run(self.source)
        self.coordinator = self.store.lease(self.store.snapshot(self.run)['tasks'][0]['task_id'])

    def evaluator(self):
        ids = self.plan()
        binding = self.store.lease(ids['evaluator'])
        return binding, ToolService(self.store, binding)

    def test_explicit_whole_span_selection_persists_exact_original_refs(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        original = self.assessment(binding)
        compact = select_refs(original)
        before = copy.deepcopy(compact)
        receipt = service.call(TOOL, compact)
        self.assertEqual(compact, before)
        # snapshot.results intentionally contains completed tasks only.
        with self.store.read(self.run) as db:
            persisted = db.execute('SELECT value_json FROM results WHERE task_id=? AND lease_epoch=?',
                                   (binding['task_id'], binding['lease_epoch'])).fetchone()
        self.assertEqual(json.loads(persisted['value_json']), original)
        self.assertEqual(service.call('assessment_get_submission_receipt', {})['receipt'], receipt['receipt'])
        self.assertEqual(service.call(TOOL, compact), receipt)

    def test_legacy_paraphrase_still_rejected_selection_is_explicit(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        legacy = self.assessment(binding)
        legacy['supporting_evidence'][0]['quote'] = 'Invented shortened quote ...'
        with self.assertRaisesRegex(InvalidResult, 'quote_not_verbatim'):
            service.call('assessment_submit_result', legacy)
        with self.assertRaises(jsonschema.ValidationError):
            service.call(TOOL, legacy)
        self.assertNotIn(binding['task_id'], self.store.snapshot(self.run)['results'])
        service.call(TOOL, select_refs(self.assessment(binding)))

    def test_selection_does_not_count_as_reading(self):
        binding, service = self.evaluator()
        with self.assertRaisesRegex((Denied, InvalidResult), 'read'):
            service.call(TOOL, select_refs(self.assessment(binding)))
        self.assertNotIn(binding['task_id'], self.store.snapshot(self.run)['results'])

    def test_unknown_id_counts_toward_existing_shared_repair_budget(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        value = select_refs(self.assessment(binding))
        value['supporting_evidence'][0]['evidence_id'] = 'not-in-scope'
        for _ in range(3):
            with self.assertRaisesRegex(ValueError, 'evidence_not_in_task_scope'):
                service.call(TOOL, value)
        with self.assertRaisesRegex(Denied, 'result_repair_budget_exhausted'):
            service.call('assessment_submit_result', self.assessment(binding))
        with self.assertRaisesRegex(Denied, 'result_repair_budget_exhausted'):
            service.call(TOOL, select_refs(self.assessment(binding)))

    def test_schema_errors_count_toward_same_budget(self):
        binding, service = self.evaluator()
        for tool in (TOOL, 'assessment_submit_result', TOOL):
            with self.assertRaises(jsonschema.ValidationError):
                service.call(tool, {})
        with self.assertRaisesRegex(Denied, 'result_repair_budget_exhausted'):
            service.call(TOOL, select_refs(self.assessment(binding)))

    def test_context_and_policy_validation_not_bypassed(self):
        binding, service = self.evaluator()
        self.read_all(binding)
        value = select_refs(self.assessment(binding))
        value['context_hash'] = 'wrong'
        with self.assertRaisesRegex(InvalidResult, 'context'):
            service.call(TOOL, value)
        value['context_hash'] = binding['context']['context_hash']
        value['grade'] = 'D'
        with self.assertRaises(InvalidResult):
            service.call(TOOL, value)
        self.assertNotIn(binding['task_id'], self.store.snapshot(self.run)['results'])

    def test_cancelled_and_stale_leases_cannot_resolve_or_submit(self):
        binding, service = self.evaluator()
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET lease_until=0 WHERE task_id=?', (binding['task_id'],))
        with self.assertRaises(Denied): service.call(TOOL, select_refs(self.assessment(binding)))
        self.store.recover(self.run)
        new = self.store.lease(binding['task_id'])
        with self.assertRaises(Denied): service.call(TOOL, select_refs(self.assessment(binding)))
        current = ToolService(self.store, new)
        self.store.cancel(self.run)
        with self.assertRaises(Denied): current.call(TOOL, select_refs(self.assessment(new)))

    def test_coordinator_has_no_evidence_submission_tool(self):
        service = ToolService(self.store, self.coordinator)
        self.assertNotIn(TOOL, {t['name'] for t in service.definitions()})
        with self.assertRaises(Denied): service.call(TOOL, {})

    def test_v7_keeps_compact_dispatch_and_disputes_enabled(self):
        service = ToolService(self.store, self.coordinator)
        self.assertIn('runtime_dispatch_plan', {t['name'] for t in service.definitions()})
        self.assertTrue(service.mailbox_enabled)


if __name__ == '__main__': unittest.main()
