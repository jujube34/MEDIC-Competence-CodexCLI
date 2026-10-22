import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medic.evals import comparison_suite as suite
from medic.evals.comparison import freeze, digest, schedule, _write_private
from medic.evals.comparison_runtime import content_identity
from medic.tests.test_baseline_b import inputs
from medic.runtime import build


class RequestFeasibility(unittest.TestCase):
    def setUp(self):
        self.source, old = inputs()
        self.dataset = copy.deepcopy(old['payload']['dataset'])
        self.design = copy.deepcopy(old['payload']['design'])
        self.design['prompt_sha256']['single_agent'] = digest(suite.a.PROMPT)
        self.design['implementation_sha256']['single_agent'] = suite.a.implementation()
        self.temp = tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts', prefix='feasibility-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def two_materials(self):
        material = copy.deepcopy(self.source['materials'][0])
        material['material_id'] += '-second'
        for span in material['spans']:
            span['material_id'] = material['material_id']
            span['evidence_id'] += '-second'
        self.source['materials'].append(material)

    def frozen(self, requests):
        sample = self.dataset['samples'][1]
        sample['assessment_content_sha256'] = content_identity(self.source)
        sample['event_ids'] = sorted({m['source_event_id'] for m in self.source['materials']})
        sample['material_sha256'] = sorted({m.get('source_sha256') or m['sha256'] for m in self.source['materials']})
        sample['labels'] = [{'code':code, 'mode':p['mode'], 'cap':p['cap'], 'observable':False, 'grade':None}
                            for code,p in self.source['policies'].items()]
        self.dataset['samples'][0]['labels'] = copy.deepcopy(sample['labels'])
        experiment = freeze(self.dataset, self.design)
        limits = {'synthetic-runtime':{'requests':requests,'total_tokens':500000,'concurrency':3,'wall_seconds':900}}
        return experiment, {'synthetic-runtime':self.source}, limits

    def test_original_scale_rejected_before_any_arm_or_workspace(self):
        self.two_materials()
        self.source['policies']['E2'] = {'mode':'human_only','cap':None,'signed_off':False}
        # Fifteen AI competencies, two material reviews and one aggregate review:
        # 15 * (2 * 5 + 5) + Coordinator = at least 226 model requests.
        args = self.frozen(80)
        while schedule(args[0])[0]['strategy']=='multi_agent':
            self.design['order_seed'] += 1
            args = self.frozen(80)
        calls=[]
        with self.assertRaisesRegex(ValueError,'comparison_suite_c_request_budget_infeasible'):
            suite.run(*args,binary='unused',api_example='unused',root=self.root,
                      dispatch=lambda *a,**kw:calls.append(True))
        self.assertEqual(calls,[])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_exact_floor_admitted_but_not_claimed_sufficient(self):
        self.two_materials()
        self.source['policies']['E2'] = {'mode':'human_only','cap':None,'signed_off':False}
        bound = suite.preflight(*self.frozen(226))
        facts=bound['payload']['c_request_feasibility']['synthetic-runtime']
        self.assertEqual(facts['mandatory_downstream_tasks'],225)
        self.assertEqual(facts['minimum_model_requests'],226)
        self.assertEqual(facts['configured_requests'],226)
        self.assertEqual(facts['interpretation'],'necessary_lower_bound_only')
        with self.assertRaisesRegex(ValueError,'request_budget_infeasible'):
            suite.preflight(*self.frozen(225))

    def test_all_human_only_still_requires_real_coordinator(self):
        for code in self.source['policies']:
            self.source['policies'][code]={'mode':'human_only','cap':None,'signed_off':False}
        bound=suite.preflight(*self.frozen(1))
        facts=bound['payload']['c_request_feasibility']['synthetic-runtime']
        self.assertEqual(facts['mandatory_downstream_tasks'],0)
        self.assertEqual(facts['minimum_model_requests'],1)

    def test_single_material_count_and_budget_are_bound(self):
        first=suite.preflight(*self.frozen(81))
        facts=first['payload']['c_request_feasibility']['synthetic-runtime']
        self.assertEqual(facts['mandatory_downstream_tasks'],80)
        self.assertEqual(facts['minimum_model_requests'],81)
        self.assertNotEqual(first['sha256'],suite.preflight(*self.frozen(82))['sha256'])
        with self.assertRaisesRegex(ValueError,'request_budget_infeasible'):
            suite.preflight(*self.frozen(80))

    def test_cli_explains_rejection_without_dispatch_or_private_input(self):
        experiment,sources,limits=self.frozen(80)
        for name,value in [('experiment',experiment),('sources',sources),('c-limits',limits)]:
            _write_private(self.root/(name+'.json'),value)
        argv=['comparison_suite']
        for name in ('experiment','sources','c-limits'):
            argv.extend(['--'+name,str(self.root/(name+'.json'))])
        argv.extend(['--binary','unused','--api-example','unused'])
        output=io.StringIO()
        with patch('sys.argv',argv),redirect_stdout(output),patch.object(suite,'execute_slot') as dispatch:
            self.assertEqual(suite.main(),2)
        dispatch.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()),{'status':'stopped','error_type':'RequestBudgetInfeasible',
            'error_code':'comparison_suite_c_request_budget_infeasible','minimum_requests':81,'configured_requests':80})


if __name__=='__main__': unittest.main()
