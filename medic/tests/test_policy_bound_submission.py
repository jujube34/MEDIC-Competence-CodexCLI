import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema

from medic.adapters.codex.worker import wait_for_task
from medic.p0.provider import ProbeError
from medic.runtime.store import Store
from medic.tests import test_runtime
from medic.tests.test_evidence_selection import TOOL, select_refs
from medic.tools.service import ToolService


class PolicyBoundSubmission(unittest.TestCase):
    plan = test_runtime.RuntimeContracts.plan
    assessment = test_runtime.RuntimeContracts.assessment
    read_all = test_runtime.RuntimeContracts.read_all

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.database())

    def database(self):
        return Path(self.temp.name)/'runtime.sqlite3'

    def prepare(self, mode='risk_scan', cap=None):
        self.source=test_runtime.fixture()
        self.source['versions']['prompt_bundle_version']='medic-p2-roles-v8'
        self.source['policies']['M2']={'mode':mode,'cap':cap,'signed_off':False}
        self.run=self.store.create_run(self.source);self.coordinator=self.store.claim_next(self.run)
        ids=self.plan();binding=self.store.lease(ids['evaluator']);self.read_all(binding)
        return binding,ToolService(self.store,binding)

    def test_lite_profile_disables_mailbox_and_rework(self):
        _, service = self.prepare('suggest', 'B')
        from medic.domain.contracts import AssessmentInput
        from medic.runtime import mailbox
        source = AssessmentInput.model_validate(self.source)
        self.assertTrue(mailbox.enabled(source))
        with patch.dict(os.environ, {'MEDIC_LITE': '1'}):
            self.assertFalse(mailbox.enabled(source))

    def test_non_scoring_modes_advertise_and_enforce_null_before_submission(self):
        for mode, status in [('clue_only','clue_only'),('risk_scan','risk_signal')]:
            with self.subTest(mode=mode):
                binding,service=self.prepare(mode)
                specs={item['name']:item for item in service.definitions()}
                self.assertNotIn('assessment_submit_result',specs)
                self.assertEqual(specs[TOOL]['inputSchema']['properties']['grade']['type'],'null')
                with self.assertRaises(jsonschema.ValidationError):service.call(TOOL,select_refs(self.assessment(binding)))
                value=self.assessment(binding);value.update(grade=None,status=status)
                receipt=service.call(TOOL,select_refs(value))
                self.assertIn('receipt',receipt)

    def test_suggest_cap_is_exposed_without_weakening_semantic_checks(self):
        binding,service=self.prepare('suggest','B')
        spec=next(item for item in service.definitions() if item['name']==TOOL)
        self.assertEqual(spec['inputSchema']['properties']['grade']['enum'],[None,'A','B'])
        value=select_refs(self.assessment(binding));value['grade']='C'
        with self.assertRaises(jsonschema.ValidationError):service.call(TOOL,value)
        value['grade']='B';self.assertIn('receipt',service.call(TOOL,value))

    def test_suggest_policy_keeps_not_applicable_available(self):
        binding, service = self.prepare('suggest', 'B')
        value = self.assessment(binding)
        value.update(grade=None, status='not_applicable')
        self.assertIn('receipt', service.call(TOOL, select_refs(value)))

    def test_submitted_result_gets_bounded_same_turn_completion_grace(self):
        binding,service=self.prepare('suggest','B');service.call(TOOL,select_refs(self.assessment(binding)))
        client=SimpleNamespace(events=[])
        def completed(deadline):
            client.events.append({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'completed'}}})
        client.pump=completed
        with patch('medic.adapters.codex.worker.time.monotonic',side_effect=[0,3,3]):
            wait_for_task(client,self.store,binding,'thread','turn',timeout=2)
        self.assertEqual(self.store.snapshot(self.run)['state'],'evaluating')
        task=next(t for t in self.store.snapshot(self.run)['tasks'] if t['task_id']==binding['task_id'])
        self.assertEqual(task['status'],'submitted','grace cannot complete an unverified final receipt')

    def test_unsubmitted_or_overdue_turns_still_time_out(self):
        binding,service=self.prepare('suggest','B')
        client=SimpleNamespace(events=[],pump=lambda _:None)
        with patch('medic.adapters.codex.worker.time.monotonic',side_effect=[0,3]), self.assertRaisesRegex(ProbeError,'turn_timeout'):
            wait_for_task(client,self.store,binding,'thread','turn',timeout=2)
        service.call(TOOL,select_refs(self.assessment(binding)))
        with patch('medic.adapters.codex.worker.time.monotonic',side_effect=[0,3,3,40]), self.assertRaisesRegex(ProbeError,'turn_timeout'):
            wait_for_task(client,self.store,binding,'thread','turn',timeout=2)


class PostgreSQLPolicyBound(PolicyBoundSubmission):
    def database(self):
        from medic.tests.test_postgres import database
        return database()


if __name__=='__main__':unittest.main()
