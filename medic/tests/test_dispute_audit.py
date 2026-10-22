"""Synthetic audit fixtures only; fabricated protocol records are never live proof."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medic.domain.contracts import AssessmentInput
from medic.domain.validation import release_gate
from medic.evals.dispute_cases import CASES, case_input, prepare
from medic.evals.verify_dispute_live import attempts, audit
from medic.p0.common import digest
from medic.prompts import load_bundles
from medic.runtime.store import Store
from medic.runtime import build
from medic.tests import test_rework, test_runtime, test_aggregation


class DisputeAuditContracts(unittest.TestCase):
    @classmethod
    def make_fixture(cls, *, rework=True, unresolved=False, aggregate=False):
        original=Store.complete
        def bound_complete(store,binding,receipt,thread,turn):
            # Explicit unit-test evidence to exercise the auditor's integrity
            # checks. No model requests or production verification files.
            thread=f"unit-thread-{binding['task_id']}-{binding['lease_epoch']}"
            turn=f"unit-turn-{binding['lease_epoch']}"
            store.bind_execution(binding,thread,turn)
            return original(store,binding,receipt,thread,turn)
        harness=test_aggregation.AggregationContracts() if aggregate else test_rework.ReworkContracts() if rework else test_runtime.RuntimeContracts()
        try:
            with patch.object(Store,'complete',bound_complete):
                harness.setUp()
                if aggregate:
                    snapshot=harness.finish_before()
                elif rework:
                    harness.rework(); snapshot=harness.third(unresolved=unresolved)
                else:
                    harness.full_graph(); snapshot=harness.store.snapshot(harness.run)
                gate=release_gate(AssessmentInput.model_validate(snapshot['source']),snapshot['results'],snapshot['tasks'],
                    snapshot['mode'],snapshot['mailbox'],snapshot['rework_plan'])
                harness.store.publish(harness.run,{'synthetic':True,'final_employee_decision':False,'gate':gate})
            snapshot=harness.store.snapshot(harness.run)
            identity=snapshot['execution_build']
            prompt_hash=digest(load_bundles()[snapshot['source']['versions']['prompt_bundle_version']])
            protocols={(t['task_id'],t['lease_epoch']):{'role':t['role'],'context_hash':t['context_hash'],
                'execution_build':identity,'prompt_bundle_hash':prompt_hash,
                'events':[{'method':m} for m in ('thread/start','turn/start','turn/completed')]}
                for t in attempts(snapshot)}
            return snapshot,protocols
        finally:
            harness.tearDown()

    @classmethod
    def setUpClass(cls):
        cls.base,cls.proofs=cls.make_fixture()

    def setUp(self):
        self.snapshot=copy.deepcopy(self.base); self.protocols=copy.deepcopy(self.proofs)

    def check(self,snapshot=None,protocols=None):
        s=snapshot or self.snapshot; p=self.protocols if protocols is None else protocols
        return audit(s,lambda t:p.get((t['task_id'],t['lease_epoch']),{}),self.base['source'],
                     self.base['budget_limits'],self.base['execution_build'])

    def test_completed_history_and_current_round_all_verified(self):
        result=self.check()
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['counts']['completed_attempts'],10)
        self.assertEqual(result['counts']['carried_old_blocking_issues'],1)

    def test_missing_initial_worker_protocol_cannot_be_hidden_by_live_third_round(self):
        old=self.snapshot['task_history'][0]
        del self.protocols[old['task_id'],old['lease_epoch']]
        result=self.check()
        self.assertEqual(result['status'],'fail')
        self.assertFalse(result['checks']['every_current_and_archived_attempt_verified'])

    def test_fixture_initial_result_cannot_be_relabelled_by_current_live_results(self):
        self.snapshot['task_history'][0]['execution_mode']='fixture'
        self.assertEqual(self.check()['status'],'fail')

    def test_omitted_history_is_detected_from_completion_events(self):
        self.snapshot['task_history'].pop()
        self.assertFalse(self.check()['checks']['all_completed_epochs_present'])

    def test_changed_old_result_is_rejected(self):
        old=next(t for t in self.snapshot['task_history'] if t['role']=='reevaluator')
        old['result']['grade']='C'
        self.assertFalse(self.check()['checks']['every_current_and_archived_attempt_verified'])

    def test_changed_mailbox_answer_is_rejected(self):
        self.snapshot['mailbox'][-1]['text']='An unrecorded answer'
        self.assertFalse(self.check()['checks']['messages_bound_to_real_results'])

    def test_message_cannot_move_outside_sender_execution(self):
        posted=next(e for e in self.snapshot['events'] if e['kind']=='dispute_message_posted')
        posted['seq']=0
        self.assertFalse(self.check()['checks']['messages_bound_to_real_results'])

    def test_changed_input_or_budget_is_rejected(self):
        self.snapshot['budget_limits']['requests']+=1
        self.assertFalse(self.check()['checks']['budget_limits_preserved'])

    def test_no_rework_or_mailbox_is_partial_not_full_dispute_success(self):
        snapshot,protocols=self.make_fixture(rework=False)
        result=audit(snapshot,lambda t:protocols[t['task_id'],t['lease_epoch']],snapshot['source'],
                     snapshot['budget_limits'],snapshot['execution_build'])
        self.assertEqual(result['execution_status'],'pass',result)
        self.assertEqual(result['status'],'partial')
        self.assertFalse(result['coverage']['real_mailbox_question_and_answer'])
        self.assertFalse(result['coverage']['four_real_third_round_roles'])

    def test_multimaterial_graph_includes_aggregator_and_all_initial_scopes(self):
        snapshot,protocols=self.make_fixture(rework=False,aggregate=True)
        result=audit(snapshot,lambda t:protocols[t['task_id'],t['lease_epoch']],snapshot['source'],
                     snapshot['budget_limits'],snapshot['execution_build'])
        self.assertEqual(result['execution_status'],'pass',result)
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['checks']['all_initial_scopes_completed'])
        self.assertEqual(len(snapshot['tasks']),31)
        # Removing one scope cannot be hidden by another completed task with
        # the same role name. All protocol records remain explicit fixtures.
        damaged=copy.deepcopy(snapshot)
        next(t for t in damaged['tasks'] if t['role']=='aggregator')['status']='failed'
        result=audit(damaged,lambda t:protocols[t['task_id'],t['lease_epoch']],snapshot['source'],
                     snapshot['budget_limits'],snapshot['execution_build'])
        self.assertFalse(result['checks']['all_initial_scopes_completed'])
        self.assertEqual(result['execution_status'],'fail')

    def test_unresolved_third_round_can_finish_safely_in_human_review(self):
        snapshot,protocols=self.make_fixture(unresolved=True)
        result=audit(snapshot,lambda t:protocols[t['task_id'],t['lease_epoch']],snapshot['source'],
                     snapshot['budget_limits'],snapshot['execution_build'])
        self.assertEqual(result['status'],'pass',result)
        self.assertFalse(result['gate']['passed'])
        self.assertEqual(snapshot['state'],'awaiting_human_review')

    def test_cases_only_create_inputs_and_never_seed_role_results(self):
        for name in CASES:
            source=AssessmentInput.model_validate(case_input(name))
            self.assertTrue(source.synthetic)
            self.assertEqual(len(source.materials),1)
            self.assertEqual([s.quote for s in source.materials[0].spans],CASES[name]['paragraphs'])
            self.assertEqual(source.versions.runtime_version,build.VERSION)
            self.assertEqual(source.goal.competency_scope,['M2'])

    def test_selected_prospective_case_freezes_input_and_budget_before_execution(self):
        case='direction-ledger-attribution'
        limits={'requests':120,'total_tokens':1200000,'concurrency':2,'wall_seconds':3600}
        with tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts') as temporary:
            output=Path(temporary)/'suite'
            prepare(output,case_ids=[case],limits=limits)
            manifest=json.loads((output/'prospective-cases.json').read_text(encoding='utf-8'))
            source=json.loads((output/(case+'.json')).read_text(encoding='utf-8'))
            self.assertEqual(manifest['limits'],limits)
            self.assertEqual(manifest['execution_build'],build.verify(build.VERSION))
            self.assertFalse(manifest['seeded_results'])
            self.assertEqual([c['case_id'] for c in manifest['cases']],[case])
            self.assertEqual(manifest['cases'][0]['input_hash'],digest(source))
            self.assertEqual({p.name for p in output.iterdir()},{case+'.json','limits.json','prospective-cases.json'})
            for invalid in ([],[case,case],['unknown']):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError,'invalid_prospective_case_selection'):
                    prepare(Path(temporary)/'invalid',case_ids=invalid)
                self.assertFalse((Path(temporary)/'invalid').exists())
