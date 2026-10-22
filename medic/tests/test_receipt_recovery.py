"""Deterministic protocol simulation, not a real model quality test."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import jsonschema

from medic.adapters.codex.worker import CodexWorker
from medic.prompts import load_bundles
from medic.runtime.store import Denied
from medic.tests.test_runtime import RuntimeContracts
from medic.tools.service import ToolService

TOOL='assessment_get_submission_receipt'


class ReceiptRecoveryContracts(unittest.TestCase):
    setUp=RuntimeContracts.setUp
    tearDown=RuntimeContracts.tearDown
    plan=RuntimeContracts.plan
    read_all=RuntimeContracts.read_all
    assessment=RuntimeContracts.assessment

    def evaluator(self):
        ids=self.plan();binding=self.store.lease(ids['evaluator'])
        self.store.bind_execution(binding,'test-thread','turn-1')
        return ids,binding,ToolService(self.store,binding)

    def submit(self,binding):
        self.read_all(binding)
        return self.store.submit(binding,self.assessment(binding))

    def test_unsubmitted_query_cannot_create_a_receipt(self):
        _,binding,service=self.evaluator()
        self.assertEqual(service.call(TOOL,{}),{'status':'not_submitted'})
        with self.assertRaises(Denied):self.store.complete(binding,'invented','test-thread','turn-1')
        self.assertNotIn(binding['task_id'],self.store.snapshot(self.run)['results'])

    def test_query_preserves_result_receipt_lease_and_strict_completion(self):
        _,binding,service=self.evaluator();receipt=self.submit(binding)
        before=next(t for t in self.store.snapshot(self.run)['tasks'] if t['task_id']==binding['task_id'])
        wrong=receipt['receipt'][:18]+'f'+receipt['receipt'][18:]
        with self.assertRaisesRegex(Denied,'receipt_or_terminal'):self.store.complete(binding,wrong,'test-thread','turn-1')
        self.store.request_finalization(binding,'test-thread','turn-1')
        self.store.bind_finalization(binding,'test-thread','turn-1','turn-2')
        self.assertEqual(service.call(TOOL,{}),{'status':'submitted',**receipt})
        self.assertEqual(service.call(TOOL,{}),{'status':'submitted',**receipt})
        after=next(t for t in self.store.snapshot(self.run)['tasks'] if t['task_id']==binding['task_id'])
        for key in ('lease_epoch','lease_until','result_hash','status'):
            self.assertEqual(before[key],after[key])
        with self.assertRaisesRegex(Denied,'identity_mismatch'):self.store.complete(binding,receipt['receipt'],'test-thread','turn-1')
        self.store.complete(binding,receipt['receipt'],'test-thread','turn-2')
        with self.assertRaises(Denied):service.call(TOOL,{})

    def test_cannot_read_another_tasks_receipt(self):
        ids,binding,service=self.evaluator();self.submit(binding)
        other=ToolService(self.store,self.store.lease(ids['challenger_scan']))
        self.assertEqual(other.call(TOOL,{}),{'status':'not_submitted'})
        with self.assertRaises(jsonschema.ValidationError):other.call(TOOL,{'task_id':binding['task_id']})
        forged=dict(binding,task_id=ids['challenger_scan'])
        with self.assertRaises(Denied):ToolService(self.store,forged)

    def test_expired_epoch_cannot_retrieve_previous_receipt(self):
        _,binding,service=self.evaluator();self.submit(binding)
        with self.store.connect() as db:db.execute('UPDATE tasks SET lease_until=0 WHERE task_id=?',(binding['task_id'],))
        with self.assertRaises(Denied):service.call(TOOL,{})
        self.store.recover(self.run);new=self.store.lease(binding['task_id'])
        with self.assertRaises(Denied):service.call(TOOL,{})
        self.assertEqual(ToolService(self.store,new).call(TOOL,{}),{'status':'not_submitted'})

    def test_cancelled_run_cannot_retrieve_receipt(self):
        _,binding,service=self.evaluator();self.submit(binding);self.store.cancel(self.run)
        with self.assertRaises(Denied):service.call(TOOL,{})

    def test_query_remains_within_existing_tool_budget(self):
        _,binding,service=self.evaluator()
        for _ in range(32):self.assertEqual(service.call(TOOL,{}),{'status':'not_submitted'})
        with self.assertRaisesRegex(Denied,'tool_budget_exhausted'):service.call(TOOL,{})

    def test_coordinator_can_recover_only_its_own_submitted_plan(self):
        self.store.bind_execution(self.coordinator,'coordinator-thread','turn-1')
        service=ToolService(self.store,self.coordinator)
        self.assertEqual(service.call(TOOL,{}),{'status':'not_submitted'})
        with patch.object(self.store,'complete'):
            self.plan()
        response=service.call(TOOL,{})
        self.assertEqual(response['status'],'submitted')
        self.store.complete(self.coordinator,response['receipt'],'coordinator-thread','turn-1')

    def test_invalid_submission_never_creates_recoverable_receipt(self):
        _,binding,service=self.evaluator()
        with self.assertRaises(jsonschema.ValidationError):service.call('assessment_submit_result',{})
        self.assertEqual(service.call(TOOL,{}),{'status':'not_submitted'})
        self.assertNotIn(binding['task_id'],self.store.snapshot(self.run)['results'])

    def simulate_worker(self,correct):
        ids=self.plan();binding=self.store.lease(ids['evaluator'])
        fake=SimpleNamespace(events=[],trace=[],prompts=[],receipt=None)
        fake.initialize=lambda:None
        def request(method,params,**kwargs):
            if method=='thread/start':
                fake.events.append({'method':'mcpServer/startupStatus/updated','params':{'threadId':'sim-thread','name':'medic','status':'ready'}})
                return {'thread':{'id':'sim-thread'}}
            return {}
        fake.request=request
        def start(thread,text):
            fake.prompts.append(text);return 'turn-'+str(len(fake.prompts))
        fake.start_turn=start;fake.close=lambda:None
        fake.final_text=lambda thread,turn:json.dumps({'receipt':fake.receipt})
        def wait(*args,**kwargs):
            if len(fake.prompts)==1:
                value=self.submit(binding);fake.exact=value['receipt'];fake.receipt=fake.exact[:18]+'f'+fake.exact[18:]
            elif correct and TOOL in fake.prompts[-1]:
                fake.receipt=ToolService(self.store,binding).call(TOOL,{})['receipt']
        def config(provider,folder):
            (folder/'codex-home').mkdir(parents=True);(folder/'codex-home/config.toml').write_text('')
            return folder,{},None
        worker=CodexWorker.__new__(CodexWorker)
        worker.binary='simulation';worker.provider=SimpleNamespace(model='simulation')
        worker.store=self.store;worker.work_root=Path(self.temp.name)/'worker'
        worker.prompt_bundles=load_bundles()
        with patch('medic.adapters.codex.worker.Client',return_value=fake),patch('medic.adapters.codex.worker.isolated_config',side_effect=config),patch('medic.adapters.codex.worker.wait_for_task',side_effect=wait):
            result=worker.run(binding['task_id'],binding)
        return result,self.store.snapshot(self.run),fake

    def test_worker_recovers_a_submitted_receipt_typo_through_authorized_tool(self):
        result,snapshot,fake=self.simulate_worker(correct=True)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(fake.prompts),2)
        self.assertIn('result_receipt_or_terminal_not_verified',fake.prompts[1])
        events=[e for e in snapshot['events'] if e['task_id']==result['task_id']]
        self.assertEqual(sum(e['kind']=='result_submitted' for e in events),1)
        self.assertEqual(sum(e['kind']=='finalization_requested' for e in events),1)
        self.assertEqual(sum(e['kind']=='tool_invoked' and e['data']['tool']==TOOL for e in events),1)

    def test_worker_still_fails_if_model_repeats_wrong_receipt(self):
        result,snapshot,fake=self.simulate_worker(correct=False)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error'],'finalization_repair_budget_exhausted')
        self.assertEqual(len(fake.prompts),3)
        self.assertEqual(snapshot['state'],'failed')
        self.assertIsNone(snapshot['report'])


if __name__=='__main__':unittest.main()
