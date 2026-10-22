import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from medic.evals import comparison_suite as s
from medic.evals.comparison import freeze,digest,schedule,_write_private
from medic.tests.test_baseline_b import inputs
from medic.runtime import build
from medic.runtime.service import SupervisorLock


class Campaign(unittest.TestCase):
    def setUp(self):
        source,old=inputs();design=copy.deepcopy(old['payload']['design'])
        design['prompt_sha256']['single_agent']=digest(s.a.PROMPT)
        design['implementation_sha256']['single_agent']=s.a.implementation()
        self.experiment=freeze(old['payload']['dataset'],design)
        self.sources={'synthetic-runtime':source}
        self.limits={'synthetic-runtime':{'requests':100,'total_tokens':500000,'concurrency':3,'wall_seconds':900}}
        self.temp=tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts',prefix='suite-test-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.calls=[]

    def fake(self,experiment,source,row,limits,mode,**kwargs):
        self.assertEqual(source,self.sources[row['sample_id']]);self.assertNotIn('labels',source)
        self.calls.append((row['strategy'],row['repeat'],mode))
        return {**{k:v for k,v in row.items() if k!='input'},'execution_id':f"synthetic-{row['strategy']}-{row['repeat']}",
                'source_report_sha256':digest(row),'status':'failed','predictions':[],
                'total_tokens':10,'model_requests':1,'retries':None,'elapsed_seconds':1.0,'human_review_seconds':None}

    def run_suite(self,dispatch=None):
        return s.run(self.experiment,self.sources,self.limits,binary='unused',api_example='unused',root=self.root,dispatch=dispatch or self.fake)

    def test_preflight_all_real_arms_same_input_without_task_creation(self):
        bound=s.preflight(self.experiment,self.sources,self.limits)
        self.assertEqual(len(bound['payload']['bindings']),6)
        self.assertEqual(len({s.c.content_identity(v['payload']['source']) for v in bound['payload']['bindings']}),1)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_full_order_failure_costs_and_terminal_replay(self):
        first=self.run_suite();expected=[(r['strategy'],r['repeat'],'execute') for r in schedule(self.experiment)]
        self.assertEqual(self.calls,expected);self.assertEqual(first['report']['observed_runs'],6)
        self.assertTrue(all(r['status']=='failed' and r['total_tokens']==10 for r in first['observations']))
        saved={p.name:p.read_bytes() for p in Path(first['folder']).glob('*.json')};self.calls=[]
        second=self.run_suite();self.assertEqual(second,first)
        self.assertTrue(all(mode=='replay' for _,_,mode in self.calls))
        self.assertEqual(saved,{p.name:p.read_bytes() for p in Path(first['folder']).glob('*.json')})

    def test_interrupted_slot_is_not_started_as_fresh_work(self):
        def crash(*args,**kwargs):
            if len(self.calls)==1: raise RuntimeError('synthetic interruption')
            return self.fake(*args,**kwargs)
        with self.assertRaises(RuntimeError): self.run_suite(crash)
        self.calls=[];self.run_suite()
        self.assertEqual([r[2] for r in self.calls],['replay','interrupted','execute','execute','execute','execute'])

    def test_missing_extra_or_changed_input_rejected_before_dispatch(self):
        for mode in ('missing','extra','content','limits'):
            with self.subTest(mode=mode):
                sources=copy.deepcopy(self.sources);limits=copy.deepcopy(self.limits)
                if mode=='missing': sources.clear()
                elif mode=='extra': sources['other']=copy.deepcopy(next(iter(sources.values())))
                elif mode=='content': sources['synthetic-runtime']['period']='changed'
                else: limits['synthetic-runtime']['total_tokens']=1
                with self.assertRaises(ValueError): s.run(self.experiment,sources,limits,binary='unused',api_example='unused',root=self.root,dispatch=self.fake)
        self.assertEqual(self.calls,[])

    def test_nonprefix_checkpoint_and_concurrent_controller_rejected(self):
        folder=self.root/self.experiment['sha256'];folder.mkdir()
        _write_private(folder/'slot-0001.json',{})
        with self.assertRaisesRegex(ValueError,'nonprefix'): self.run_suite()
        self.assertEqual(self.calls,[])
        with SupervisorLock(SimpleNamespace(postgres=False,path=str(folder/'campaign'))):
            with self.assertRaisesRegex(RuntimeError,'already_running'): self.run_suite()

    def test_reused_execution_id_not_checkpointed(self):
        def wrong(*args,**kwargs):
            result=self.fake(*args,**kwargs);result['execution_id']='same-execution';return result
        with self.assertRaisesRegex(ValueError,'reused_execution'): self.run_suite(wrong)
        folder=self.root/self.experiment['sha256']
        self.assertTrue((folder/'slot-0000.json').exists());self.assertFalse((folder/'slot-0001.json').exists())

    def test_another_valid_slot_cannot_replace_current_slot(self):
        def wrong(experiment,source,row,limits,mode,**kwargs):
            other=next(r for r in schedule(experiment) if (r['strategy'],r['repeat'])!=(row['strategy'],row['repeat']))
            return self.fake(experiment,source,other,limits,mode,**kwargs)
        with self.assertRaisesRegex(ValueError,'returned_slot_mismatch'): self.run_suite(wrong)
        self.assertFalse((self.root/self.experiment['sha256']/'slot-0000.json').exists())

    def test_checkpoint_change_rejected_by_backend_replay(self):
        first=self.run_suite();target=Path(first['folder'])/'slot-0000.json';changed=json.loads(target.read_text())
        changed['observation']['total_tokens']=999;target.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError,'artifact_conflict'): self.run_suite()

    def test_baseline_replay_does_not_launch_missing_or_live_ledger(self):
        row=next(r for r in schedule(self.experiment) if r['strategy']=='single_agent')
        folder=self.root/'a';folder.mkdir()
        with patch.object(s,'_baseline_folder',return_value=folder),patch.object(s.a,'run') as run:
            with self.assertRaisesRegex(ValueError,'missing_baseline'): s.execute_slot(self.experiment,self.sources['synthetic-runtime'],row,{},'replay',binary='unused',api_example='unused',lock=None)
            with closing(sqlite3.connect(folder/'ledger.sqlite3')) as db: db.executescript("CREATE TABLE session(state TEXT); INSERT INTO session VALUES('running');")
            with self.assertRaisesRegex(ValueError,'not_terminal'): s.execute_slot(self.experiment,self.sources['synthetic-runtime'],row,{},'replay',binary='unused',api_example='unused',lock=None)
            run.assert_not_called()

    def test_interrupted_c_without_terminal_artifacts_never_launches(self):
        row=next(r for r in schedule(self.experiment) if r['strategy']=='multi_agent')
        with patch.object(s.c,'workspace',return_value=self.root/'c'),patch.object(s.c,'prepare') as prepare,patch.object(s,'_launch_c') as launch:
            with self.assertRaisesRegex(ValueError,'terminal_artifacts_missing'): s.execute_slot(self.experiment,self.sources['synthetic-runtime'],row,{},'interrupted',binary='unused',api_example='unused',lock=None)
            prepare.assert_not_called();launch.assert_not_called()


if __name__=='__main__': unittest.main()
