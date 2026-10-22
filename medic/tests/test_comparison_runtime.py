import copy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from medic.evals.comparison import digest,freeze,compare
from medic.evals.comparison_runtime import binding,content_identity,prepare,collect,workspace
from medic.prompts import load_bundles
from medic.runtime import build
from medic.runtime.store import Store
from medic.runtime.budget import BudgetLedger


def fixture():
    source=json.loads((build.ROOT/'evals/fixtures/single_dimension_current.json').read_text(encoding='utf-8'))
    source['versions']['runtime_version']=build.VERSION
    versions=source['versions'];identity=build.verify(build.VERSION)
    profile=json.loads((build.ROOT/'p0/results/provider-profile.json').read_text(encoding='utf-8'))
    labels=[{'code':code,'mode':p['mode'],'cap':p['cap'],'observable':True,
             'grade':'B' if p['mode']=='suggest' else None} for code,p in source['policies'].items()]
    sample={'sample_id':'synthetic-runtime','employee_id':source['employee_ref'],'event_ids':sorted({m['source_event_id'] for m in source['materials']}),
        'material_sha256':sorted({m.get('source_sha256') or m['sha256'] for m in source['materials']}),
        'assessment_content_sha256':content_identity(source),'split':'test','job_key':source['job_key'],'labels':labels}
    development={**copy.deepcopy(sample),'sample_id':'synthetic-development','employee_id':'DEV-ONLY',
        'event_ids':['dev-event-only'],'material_sha256':['d'*64],'assessment_content_sha256':'e'*64,'split':'development'}
    dataset={'dataset_id':'runtime-binding-synthetic','provenance':'synthetic_fixture',
        **{k:versions[k] for k in ('competency_model_version','job_matrix_version','ai_policy_version','parser_version')},'samples':[development,sample]}
    design={'experiment_id':'synthetic-c-arm-binding','provider_profile_version':profile['provider_profile_version'],'model':profile['model'],
        'prompt_sha256':{'single_agent':'a'*64,'linear_workflow':'b'*64,'multi_agent':digest(load_bundles()[versions['prompt_bundle_version']])},
        'implementation_sha256':{'single_agent':'c'*64,'linear_workflow':'d'*64,'multi_agent':identity['manifest_sha256']},
        'token_budget':{'single_agent':500000,'linear_workflow':500000,'multi_agent':500000},
        'budget_regime':'matched_tokens','repeats':2,'order_seed':19,'split':'test'}
    limits={'requests':80,'total_tokens':500000,'concurrency':2,'wall_seconds':1800}
    return source,dataset,design,limits


class ComparisonRuntime(unittest.TestCase):
    def setUp(self):
        self.source,self.dataset,self.design,self.limits=fixture();self.experiment=freeze(self.dataset,self.design)
        self.temp=tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts',prefix='comparison-binding-test-')
        self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
    def prepare(self,repeat=1):
        return prepare(self.experiment,self.source,'synthetic-runtime',repeat,self.limits,root=self.root)

    def test_complete_business_content_and_arm_versions_separated(self):
        source=copy.deepcopy(self.source);before=content_identity(source)
        source['versions']['runtime_version']='another-implementation'
        source['versions']['prompt_bundle_version']='another-prompt'
        self.assertEqual(content_identity(source),before)
        for field in ('period','tenant_id','job_key'):
            changed=copy.deepcopy(self.source);changed[field]+='-changed'
            self.assertNotEqual(content_identity(changed),before)
        changed=copy.deepcopy(self.source);changed['criteria']['M2'][0]['description']+=' changed'
        self.assertNotEqual(content_identity(changed),before)

    def test_no_metadata_only_or_changed_business_input_is_executable(self):
        self.dataset['samples'][1]['assessment_content_sha256']=None
        with self.assertRaisesRegex(ValueError,'full_business_input'): binding(freeze(self.dataset,self.design),self.source,'synthetic-runtime',1,self.limits)
        source=copy.deepcopy(self.source);source['goal']['description']+=' changed'
        with self.assertRaisesRegex(ValueError,'full_business_input'): binding(self.experiment,source,'synthetic-runtime',1,self.limits)

    def test_prompt_build_provider_budget_and_repeat_mismatch_rejected(self):
        changes=[('prompt_sha256','multi_agent','f'*64),('implementation_sha256','multi_agent','f'*64)]
        for field,arm,value in changes:
            design=copy.deepcopy(self.design);design[field][arm]=value
            with self.subTest(field=field),self.assertRaises(ValueError): binding(freeze(self.dataset,design),self.source,'synthetic-runtime',1,self.limits)
        design=copy.deepcopy(self.design);design['model']='different-model'
        with self.assertRaisesRegex(ValueError,'provider'): binding(freeze(self.dataset,design),self.source,'synthetic-runtime',1,self.limits)
        with self.assertRaisesRegex(ValueError,'token_budget'): binding(self.experiment,self.source,'synthetic-runtime',1,self.limits|{'total_tokens':400000})
        with self.assertRaisesRegex(ValueError,'slot_not_found'): self.prepare(3)

    def test_declared_metadata_must_match_input_even_with_valid_content_hash(self):
        for field,value in [('employee_id','wrong'),('event_ids',['wrong']),('material_sha256',['f'*64]),('job_key','wrong')]:
            data=copy.deepcopy(self.dataset);data['samples'][1][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): binding(freeze(data,self.design),self.source,'synthetic-runtime',1,self.limits)

    def test_idempotent_creation_preserves_deadline_budget_and_terminal_state(self):
        first=self.prepare();second=self.prepare()
        self.assertEqual(first,second)
        store=Store(Path(first['folder'])/'runtime.sqlite3');store.cancel(first['run_id'])
        third=self.prepare();self.assertEqual(third['run_id'],first['run_id']);self.assertEqual(third['deadline'],first['deadline'])
        self.assertEqual(third['state'],'cancelled')
        with store.read() as db: self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0],1)

    def test_repeats_create_distinct_runs_and_bindings_before_tasks(self):
        first=self.prepare();second=self.prepare(2)
        self.assertNotEqual(first['run_id'],second['run_id'])
        for result in (first,second):
            folder=Path(result['folder']);sealed=json.loads((folder/'binding.json').read_text())
            self.assertEqual(sealed['sha256'],result['binding_sha256'])
            self.assertEqual(sealed['payload']['full_input_sha256'],Store(folder/'runtime.sqlite3').snapshot(result['run_id'])['input_hash'])
            self.assertEqual(BudgetLedger(Store(folder/'runtime.sqlite3'),result['run_id']).snapshot()['requests'],0)

    def test_crash_after_creation_replays_same_run_without_new_budget(self):
        original=Store.create_run
        def crash(store,*args,**kwargs):
            original(store,*args,**kwargs);raise RuntimeError('synthetic_crash_after_create')
        with patch.object(Store,'create_run',crash):
            with self.assertRaisesRegex(RuntimeError,'synthetic_crash'): self.prepare()
        folder=workspace(self.experiment,'synthetic-runtime',1,self.root)
        store=Store(folder/'runtime.sqlite3')
        with store.read() as db: before=tuple(db.execute('SELECT run_id,deadline FROM run_limits').fetchone())
        result=self.prepare();self.assertEqual((result['run_id'],result['deadline']),before)

    def test_unknown_existing_workspace_and_changed_binding_are_not_reused(self):
        folder=workspace(self.experiment,'synthetic-runtime',1,self.root);folder.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'unbound_workspace'): self.prepare()
        self.assertFalse((folder/'runtime.sqlite3').exists())
        result=self.prepare(2);path=Path(result['folder'])/'binding.json'
        value=json.loads(path.read_text());value['payload']['budget_limits']['requests']=1;path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'binding_conflict'): self.prepare(2)

    def test_legacy_long_workspace_cannot_fork_same_slot_into_new_budget(self):
        legacy=self.root/self.experiment['sha256']/digest(['synthetic-runtime','multi_agent',1])
        legacy.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'legacy_comparison_workspace'): self.prepare()
        self.assertFalse(workspace(self.experiment,'synthetic-runtime',1,self.root).exists())

    def test_cancelled_slot_exports_costs_without_successful_role_claim(self):
        result=self.prepare();folder=Path(result['folder']);store=Store(folder/'runtime.sqlite3');store.cancel(result['run_id'])
        (folder/'run.json').write_text(json.dumps(store.snapshot(result['run_id'])),encoding='utf-8')
        (folder/'gateway-summary.json').write_text(json.dumps({'requests':[],'durable_budget':BudgetLedger(store,result['run_id']).snapshot()}),encoding='utf-8')
        # Archive integrity has its own real ZIP tests; this test isolates terminal observation semantics.
        with patch('medic.evals.comparison_runtime.verify_archive',return_value=103): export=collect(folder,root=self.root)
        observation=export['observation'];self.assertEqual(observation['status'],'cancelled');self.assertEqual(observation['predictions'],[])
        self.assertEqual(observation['total_tokens'],0);self.assertFalse(export['execution_audit']['successful_protocol_audit'])
        report=compare(self.experiment,[observation]);self.assertEqual(report['arms']['multi_agent']['costs']['status_counts']['cancelled'],1)
        self.assertEqual(report['arms']['multi_agent']['scores']['scorable_coverage'],0)

    def test_run_from_another_slot_cannot_be_retroactively_collected(self):
        result=self.prepare();folder=Path(result['folder']);path=folder/'run-binding.json'
        value=json.loads(path.read_text());value['run_id']='run-'+'a'*32;path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'not_created_for_comparison_slot'): collect(folder,root=self.root)

    def test_real_codex_initializes_in_generated_worker_workspace(self):
        from medic.p0.appserver import isolated_config
        from medic.p0.provider import Config
        binary=build.ROOT.parent/'codex-rs/target/debug/codex.exe'
        if not binary.is_file(): self.skipTest('local verified Codex executable required')
        startup_root=tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts',prefix='cmp-check-')
        self.addCleanup(startup_root.cleanup)
        folder=workspace(self.experiment,'synthetic-runtime',1,Path(startup_root.name))/'workers'/('task-'+'a'*32)/'1'
        cwd,env,_=isolated_config(Config('http://127.0.0.1:1/v1','synthetic-not-a-secret'),folder)
        request={'id':1,'method':'initialize','params':{'clientInfo':{'name':'medic_comparison_probe','version':'1'},'capabilities':{'experimentalApi':False}}}
        result=subprocess.run([str(binary),'app-server','--listen','stdio://'],input=json.dumps(request)+'\n',
            cwd=cwd,env=env,capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode,0,'app-server must initialize in the generated worker directory')
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(any(r.get('id')==1 and 'result' in r for r in replies))


if __name__=='__main__': unittest.main()
