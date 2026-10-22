import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from medic.evals.comparison import compare,freeze,schedule,STRATEGIES,_write_private,_read
from medic.evals.metrics import evaluate


def fixture():
    samples=[]
    for i,grade in enumerate(('A','B','C','B')):
        samples.append({'sample_id':f'synthetic-{i}','employee_id':f'person-{i}',
            'event_ids':[f'event-{i}'],'material_sha256':[str(i)*64],
            'split':'development' if i==0 else 'test','job_key':'job-one' if i<3 else 'job-two',
            'labels':[{'code':'M2','mode':'suggest','cap':'C','observable':True,'grade':grade},
                      {'code':'I1','mode':'suggest','cap':'B','observable':False,'grade':None},
                      {'code':'C1','mode':'risk_scan','cap':None,'observable':True,'grade':None}]})
    data={'dataset_id':'synthetic-comparison','provenance':'synthetic_fixture',
        'competency_model_version':'synthetic-model-1','job_matrix_version':'synthetic-jobs-1',
        'ai_policy_version':'synthetic-policy-1','parser_version':'synthetic-parser-1','samples':samples}
    design={'experiment_id':'synthetic-matched','provider_profile_version':'synthetic-provider',
        'model':'synthetic-no-model-called','prompt_sha256':{s:str(i+4)*64 for i,s in enumerate(STRATEGIES)},
        'implementation_sha256':{s:str(i+7)*64 for i,s in enumerate(STRATEGIES)},
        'token_budget':{s:1000 for s in STRATEGIES},'budget_regime':'matched_tokens','repeats':2,'order_seed':7,'split':'test'}
    return data,design


def observations(frozen):
    labels={s['sample_id']:s['labels'] for s in frozen['payload']['dataset']['samples']}
    output=[]
    for i,row in enumerate(schedule(frozen)):
        output.append({k:v for k,v in row.items() if k!='input'} | {
            'execution_id':f'synthetic-execution-{i}','source_report_sha256':f'{i+1:064x}',
            'status':'completed','predictions':[{'code':l['code'],'grade':l['grade'],'evidence_valid':True,'anchors_supported':True}
                for l in labels[row['sample_id']]],
            'total_tokens':100,'model_requests':2,'retries':0,'elapsed_seconds':2.0,'human_review_seconds':None})
    return output


class FrozenComparison(unittest.TestCase):
    def setUp(self):
        self.data,self.design=fixture();self.frozen=freeze(self.data,self.design)

    def test_freeze_detects_any_change_and_unknown_fields(self):
        self.assertEqual(self.frozen,freeze(copy.deepcopy(self.data),copy.deepcopy(self.design)))
        for field,value in [('grade','A'),('cap','B')]:
            changed=copy.deepcopy(self.frozen)
            changed['payload']['dataset']['samples'][1]['labels'][0][field]=value
            with self.assertRaisesRegex(ValueError,'experiment_changed'): compare(changed,[])
        self.data['unknown']='private'
        with self.assertRaises(ValueError): freeze(self.data,self.design)

    def test_employee_event_and_material_cannot_cross_split(self):
        for field in ('employee_id','event_ids','material_sha256'):
            with self.subTest(field=field):
                changed=copy.deepcopy(self.data)
                changed['samples'][1][field]=changed['samples'][0][field]
                with self.assertRaisesRegex(ValueError,'split_leakage'): freeze(changed,self.design)
        # Same event across different employees remains permissible within one partition.
        self.data['samples'][2]['event_ids']=self.data['samples'][1]['event_ids']
        freeze(self.data,self.design)

    def test_transitive_source_connections_and_duplicate_ids(self):
        self.data['samples'][1]['event_ids']=['shared','test-only']
        self.data['samples'][2]['event_ids']=['shared','development-only']
        self.data['samples'][0]['event_ids']=['development-only']
        with self.assertRaisesRegex(ValueError,'split_leakage'): freeze(self.data,self.design)
        data,design=fixture();data['samples'][1]['sample_id']=data['samples'][0]['sample_id']
        with self.assertRaisesRegex(ValueError,'duplicate_sample_id'): freeze(data,design)

    def test_expert_provenance_requires_dual_review_and_adjudication(self):
        self.data['provenance']='authorized_deidentified_expert'
        with self.assertRaisesRegex(ValueError,'dual_review'): freeze(self.data,self.design)
        for sample in self.data['samples']:
            sample['independent_reviewers']=['reviewer-one','reviewer-two']
            sample['adjudication_reference']='declared-review-record'
        frozen=freeze(self.data,self.design)
        self.assertTrue(compare(frozen,[])['quality_acceptance'].startswith('not_assessed'))
        self.data['samples'][0]['independent_reviewers']=['same','same']
        with self.assertRaises(ValueError): freeze(self.data,self.design)

    def test_invalid_policy_observability_and_nonfinite_rejected(self):
        for change in ({'grade':None},{'observable':False},{'code':'X9'}):
            data=copy.deepcopy(self.data);data['samples'][1]['labels'][0].update(change)
            with self.assertRaises(ValueError): freeze(data,self.design)
        changed=copy.deepcopy(self.data);changed['samples'][1]['labels'][0]['cap']='B'
        with self.assertRaisesRegex(ValueError,'inconsistent_frozen_policy'): freeze(changed,self.design)
        run=observations(self.frozen)[0]
        for field,value in [('total_tokens',True),('total_tokens',-1),('elapsed_seconds',math.inf),('elapsed_seconds',math.nan)]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                compare(self.frozen,[run | {field:value}])

    def test_matched_and_configured_budgets_are_distinct(self):
        self.design['token_budget']['multi_agent']=2000
        with self.assertRaisesRegex(ValueError,'unequal_matched_budgets'): freeze(self.data,self.design)
        self.design['budget_regime']='configured'
        self.assertEqual(compare(freeze(self.data,self.design),[])['budget_regime'],'configured')
        self.design['prompt_sha256'].pop('single_agent')
        with self.assertRaisesRegex(ValueError,'three_strategies'): freeze(self.data,self.design)

    def test_blind_reproducible_schedule_covers_every_arm_repeat(self):
        rows=schedule(self.frozen)
        self.assertEqual(rows,schedule(self.frozen));self.assertEqual(len(rows),18)
        self.assertEqual(len({(r['sample_id'],r['strategy'],r['repeat']) for r in rows}),18)
        text=json.dumps(rows)
        for key in ('grade','observable','independent_reviewers','adjudication_reference'):
            self.assertNotIn('"'+key+'"',text)
        self.assertNotIn('synthetic-0',text)
        self.design['order_seed']=8
        self.assertNotEqual([r['sample_id'] for r in rows],[r['sample_id'] for r in schedule(freeze(self.data,self.design))])

    def test_identity_mismatch_duplicates_and_reused_execution_rejected(self):
        rows=observations(self.frozen)
        for field,value in [('experiment_sha256','f'*64),('input_sha256','e'*64),('prompt_sha256','d'*64),
                            ('implementation_sha256','c'*64),
                            ('model','different'),('provider_profile_version','different'),('token_budget',999),
                            ('sample_id','synthetic-0'),('repeat',3)]:
            with self.subTest(field=field),self.assertRaises(ValueError): compare(self.frozen,[rows[0] | {field:value}])
        with self.assertRaisesRegex(ValueError,'duplicate_observation'): compare(self.frozen,[rows[0],rows[0]])
        with self.assertRaisesRegex(ValueError,'reused_execution'): compare(self.frozen,[rows[0],rows[1] | {'execution_id':rows[0]['execution_id']}])
        row=copy.deepcopy(rows[0]);row['predictions'].append({'code':'M1','grade':'C'})
        with self.assertRaisesRegex(ValueError,'outside_scope'): compare(self.frozen,[row])

    def test_analysis_drift_rejected_without_rewriting_frozen_identity(self):
        frozen=copy.deepcopy(self.frozen)
        with patch('medic.evals.comparison.analysis_identity',return_value={'changed':True}):
            with self.assertRaisesRegex(ValueError,'analysis_implementation_changed'): compare(frozen,[])
        self.assertEqual(frozen,self.frozen)

    def test_costs_count_runs_once_not_cells_and_groups_are_scoped(self):
        report=compare(self.frozen,observations(self.frozen))
        for arm in report['arms'].values():
            self.assertEqual(arm['costs']['total_tokens']['measured_sum'],600)
            self.assertEqual(arm['costs']['elapsed_seconds']['p95'],2)
            self.assertEqual(arm['scores']['cases'],18)
            self.assertEqual(arm['scores']['exact_agreement'],1)
            self.assertEqual(arm['scores']['scorable_coverage'],1)
            self.assertEqual(arm['by_dimension']['M']['cases'],6)
            self.assertEqual(arm['by_dimension']['C']['paired_grades'],0)
            self.assertEqual(arm['by_job']['job-two']['cases'],6)
            self.assertEqual(arm['by_repeat']['1']['cases'],9)
            self.assertEqual(arm['costs']['human_review_seconds']['unknown_runs'],6)
            self.assertIsNone(arm['costs']['human_review_seconds']['measured_sum'])

    def test_failed_missing_and_abstained_are_different_and_keep_denominators(self):
        rows=observations(self.frozen)
        strategy=rows[0]['strategy'];arm=[r for r in rows if r['strategy']==strategy]
        missing=arm[0];failed=arm[1];abstained=arm[2]
        rows.remove(missing);failed.update(status='failed',predictions=[],total_tokens=250)
        abstained['predictions'][0]['grade']=None
        result=compare(self.frozen,rows)['arms'][strategy]
        self.assertEqual(result['costs']['expected_runs'],6)
        self.assertEqual(result['costs']['missing_runs'],1)
        self.assertEqual(result['costs']['status_counts']['failed'],1)
        self.assertEqual(result['costs']['total_tokens']['measured_sum'],650)
        self.assertEqual(result['costs']['total_tokens']['unknown_runs'],1)
        self.assertEqual(result['scores']['missing_predictions'],6)
        self.assertEqual(result['scores']['abstained_scorable_predictions'],1)
        self.assertEqual(result['scores']['scorable_coverage'],.5)

    def test_policy_errors_and_overbudget_are_counted_not_dropped(self):
        rows=observations(self.frozen);row=rows[0];row['total_tokens']=1001
        row['predictions'][0]['grade']='D';row['predictions'][1]['grade']='B';row['predictions'][2]['grade']='A'
        row['predictions'][0]['evidence_valid']=False
        result=compare(self.frozen,rows)['arms'][row['strategy']]
        self.assertEqual(result['costs']['budget_exceeded_runs'],1)
        self.assertEqual(result['scores']['policy_violations'],2)
        self.assertEqual(result['scores']['scored_unscorable_expert_cases'],1)
        self.assertEqual(result['scores']['unsupported_scored_predictions'],1)

    def test_paired_error_has_explicit_unpaired_denominator(self):
        rows=observations(self.frozen)
        for row in rows:
            if row['strategy']=='single_agent': row['predictions'][0]['grade']='A'
        rows=[r for r in rows if not(r['strategy']=='multi_agent' and r['sample_id']=='synthetic-1' and r['repeat']==1)]
        paired=compare(self.frozen,rows)['paired_absolute_error']['single_agent']
        self.assertEqual(paired,{'eligible_expert_cells':6,'paired_cells':5,'unpaired_cells':1,
            'multi_better':5,'baseline_better':0,'equal_error':0})

    def test_no_observations_keeps_all_runs_missing(self):
        report=compare(self.frozen,[])
        self.assertEqual(report['expected_runs'],18);self.assertEqual(report['observed_runs'],0)
        for arm in report['arms'].values():
            self.assertEqual(arm['scores']['scorable_coverage'],0)
            self.assertIsNone(arm['scores']['exact_agreement'])
            self.assertEqual(arm['costs']['missing_runs'],6)
            self.assertIsNone(arm['costs']['total_tokens']['measured_sum'])

    def test_cli_round_trip_no_overwrite_no_private_validation_echo(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'artifacts',prefix='comparison-test-') as tmp:
            folder=Path(tmp)
            for name,value in [('dataset',self.data),('design',self.design),('observations',observations(self.frozen))]:
                (folder/(name+'.json')).write_text(json.dumps(value),encoding='utf-8')
            def run(*args):
                return subprocess.run([sys.executable,'-m','medic.evals.comparison',*map(str,args)],capture_output=True,text=True,cwd=root.parent)
            frozen=folder/'frozen.json';planned=folder/'schedule.json';reported=folder/'report.json'
            self.assertEqual(run('freeze','--dataset',folder/'dataset.json','--design',folder/'design.json','--output',frozen).returncode,0)
            self.assertEqual(run('schedule','--experiment',frozen,'--output',planned).returncode,0)
            self.assertEqual(run('report','--experiment',frozen,'--observations',folder/'observations.json','--output',reported).returncode,0)
            self.assertEqual(json.loads(reported.read_text())['observed_runs'],18)
            before=frozen.read_bytes()
            self.assertEqual(run('freeze','--dataset',folder/'dataset.json','--design',folder/'design.json','--output',frozen).returncode,2)
            self.assertEqual(frozen.read_bytes(),before)
            self.data['samples'][0]['labels'][0]['code']='SECRET-PRIVATE-INPUT'
            (folder/'dataset.json').write_text(json.dumps(self.data),encoding='utf-8')
            result=run('freeze','--dataset',folder/'dataset.json','--design',folder/'design.json','--output',folder/'bad.json')
            self.assertEqual(result.returncode,2);self.assertNotIn('SECRET-PRIVATE-INPUT',result.stdout+result.stderr)
            self.assertFalse((folder/'bad.json').exists())
            (folder/'duplicate.json').write_text('{"case":"first","case":"second"}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'duplicate_json_key'): _read(folder/'duplicate.json')
        with self.assertRaisesRegex(ValueError,'private_directory'): _write_private(root/'comparison-public.json',{})


class ExtendedMetrics(unittest.TestCase):
    def test_grade_error_and_abstention_denominators(self):
        labels=[{'case_id':str(i),'grade':g,'mode':'suggest','cap':'C'} for i,g in enumerate(('A','C','B',None,'C'))]
        preds=[{'case_id':str(i),'grade':g} for i,g in enumerate(('B','A',None,None))]
        result=evaluate(labels,preds)
        self.assertEqual(result['mean_signed_grade_error'],-.5)
        self.assertEqual(result['mean_absolute_grade_error'],1.5)
        self.assertEqual(result['underestimation_rate'],.5)
        self.assertEqual(result['false_abstention_rate'],.25)
        self.assertEqual(result['correctly_abstained_unscorable_cases'],1)
        self.assertEqual(result['missing_predictions'],1)

    def test_nonfinite_or_boolean_usage_and_unknown_policy_rejected(self):
        labels=[{'case_id':'a','grade':'B','mode':'suggest','cap':'C'}]
        for usage in (float('inf'),float('nan'),True,-1):
            with self.assertRaises(ValueError): evaluate(labels,[{'case_id':'a','grade':None,'total_tokens':usage}])
        labels[0]['mode']='invented'
        with self.assertRaises(ValueError): evaluate(labels,[])


if __name__=='__main__': unittest.main()
