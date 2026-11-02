"""Explicit Agent-authored plans; fixtures are not model execution evidence."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medic.domain.contracts import AssessmentInput
from medic.domain.graph import required_graph
from medic.prompts import load_bundles
from medic.runtime.store import Store, Denied
from medic.tests.test_baseline_b import inputs
from medic.tools.service import ToolService


def full_source():
    source,_=inputs()
    source['policies']['E2']={'mode':'human_only','cap':None,'signed_off':False}
    second=copy.deepcopy(source['materials'][0]);second['material_id']+='-second'
    for span in second['spans']:
        span['material_id']=second['material_id'];span['evidence_id']+='-second'
    source['materials'].append(second)
    if 'medic-p2-roles-v6' in load_bundles():
        source['versions']['prompt_bundle_version']='medic-p2-roles-v6'
    return source


def explicit_plan(source):
    codes=[c for c in source['goal']['competency_scope'] if source['policies'][c]['mode']!='human_only']
    def dep(role,scope='same_material'): return {'role':role,'scope':scope}
    def node(role,deps): return {'role':role,'dependencies':deps,'reason':'Synthetic explicit role plan: '+role}
    local=[node('evaluator',[]),node('challenger_scan',[]),
           node('challenger',[dep('evaluator'),dep('challenger_scan')]),
           node('reevaluator',[dep('evaluator'),dep('challenger')]),
           node('judge',[dep(r) for r in ('evaluator','challenger_scan','challenger','reevaluator')])]
    aggregate=[node('aggregator',[dep('reevaluator','source_materials'),dep('judge','source_materials')]),
               node('challenger_scan',[]),node('challenger',[dep('aggregator'),dep('challenger_scan')]),
               node('reevaluator',[dep('aggregator'),dep('challenger')]),
               node('judge',[dep(r) for r in ('aggregator','challenger_scan','challenger','reevaluator')])]
    return {'groups':[{'material_ids':[m['material_id'] for m in source['materials']], 'competency_ids':codes,'nodes':local},
                      {'material_ids':['__aggregate__'],'competency_ids':codes,'nodes':aggregate}]}


class CompactPlan(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'runtime.sqlite3')
        self.source=full_source();self.run=self.store.create_run(self.source)
        self.binding=self.store.claim_next(self.run)
        self.service=ToolService(self.store,self.binding)
        self.plan=explicit_plan(self.source)

    def call(self,plan=None):
        return self.service.call('runtime_dispatch_plan',self.plan if plan is None else plan)

    def state(self):
        with self.store.read(self.run) as db:
            queries={'tasks':'SELECT * FROM tasks WHERE run_id=? ORDER BY task_id',
                     'outbox':'SELECT o.* FROM outbox o JOIN tasks t ON t.task_id=o.task_id WHERE t.run_id=? ORDER BY o.id',
                     'events':'SELECT * FROM events WHERE run_id=? ORDER BY seq'}
            return {table:[tuple(row) for row in db.execute(query,(self.run,))] for table,query in queries.items()}

    def test_full225_explicit_plan_one_call_exact_dependencies_and_no_execution(self):
        result=self.call();snapshot=self.store.snapshot(self.run)
        self.assertEqual(result['created'],225);self.assertEqual(result['existing'],0)
        self.assertEqual(result['planned_nodes'],225)
        self.assertNotIn('tasks',result)
        tasks={t['task_id']:t for t in snapshot['tasks'] if t['role']!='coordinator'}
        actual={(t['material_id'],t['competency_id'],t['role']):
            {(tasks[d]['material_id'],tasks[d]['competency_id'],tasks[d]['role']) for d in t['dependencies']} for t in tasks.values()}
        self.assertEqual(actual,required_graph(AssessmentInput.model_validate(self.source)))
        self.assertTrue(all(t['status']=='queued' and t['thread_id'] is None for t in tasks.values()))
        self.assertIsNone(self.store.claim_next(self.run))
        self.assertEqual(len([e for e in snapshot['events'] if e['kind']=='agent_dispatched_task']),225)
        receipt=self.service.call('runtime_finish_plan',{})['receipt']
        self.store.complete(self.binding,receipt,'fixture-thread','fixture-turn')
        self.assertNotEqual(self.store.claim_next(self.run)['context']['role'],'coordinator')

    def test_order_independent_and_identical_replay_no_duplicate_tasks_or_outbox(self):
        self.plan['groups'].reverse()
        for group in self.plan['groups']: group['nodes'].reverse()
        first=self.call();before=self.state();second=self.call();after=self.state()
        self.assertEqual(first['plan_sha256'],second['plan_sha256'])
        self.assertEqual(second['created'],0);self.assertEqual(second['existing'],225)
        self.assertEqual(before['tasks'],after['tasks']);self.assertEqual(before['outbox'],after['outbox'])

    def test_missing_edge_human_only_duplicate_and_foreign_scope_atomic_rejection(self):
        for failure in ('edge','human','duplicate','foreign','cross_scope','missing_node'):
            plan=copy.deepcopy(self.plan)
            if failure=='edge': plan['groups'][-1]['nodes'][-1]['dependencies'].pop()
            elif failure=='human': plan['groups'][0]['competency_ids'].append('E2')
            elif failure=='duplicate': plan['groups'].append(copy.deepcopy(plan['groups'][0]))
            elif failure=='foreign': plan['groups'][0]['material_ids'].append('other-run-material')
            elif failure=='cross_scope': plan['groups'][0]['nodes'][-1]['dependencies'][0]['scope']='source_materials'
            else: plan['groups'][0]['nodes']=plan['groups'][0]['nodes'][1:]
            before=self.state()
            with self.subTest(failure=failure),self.assertRaises(ValueError): self.call(plan)
            after=self.state();self.assertEqual(before['tasks'],after['tasks']);self.assertEqual(before['outbox'],after['outbox'])

    def test_partial_plan_does_not_manufacture_missing_stages(self):
        plan=copy.deepcopy(self.plan);plan['groups']=plan['groups'][:1]
        plan['groups'][0]['nodes']=plan['groups'][0]['nodes'][:1]
        result=self.call(plan);self.assertEqual(result['created'],30)
        with self.assertRaisesRegex(ValueError,'coordinator_plan_incomplete'):
            self.service.call('runtime_finish_plan',{})
        self.assertEqual(len(self.store.snapshot(self.run)['tasks']),31)
        result=self.call();self.assertEqual(result['existing'],30);self.assertEqual(result['created'],195)

    def test_worker_does_not_receive_dispatch_tool(self):
        self.call();receipt=self.service.call('runtime_finish_plan',{})['receipt']
        self.store.complete(self.binding,receipt,'fixture-thread','fixture-turn')
        worker=self.store.claim_next(self.run);service=ToolService(self.store,worker)
        self.assertNotIn('runtime_dispatch_plan',{t['name'] for t in service.definitions()})
        with self.assertRaises(Denied): service.call('runtime_dispatch_plan',self.plan)

    def test_insert_failure_rolls_back_tasks_outbox_and_audit_together(self):
        from medic.runtime.planning import dispatch_plan
        before=self.state();original=self.store._dispatch;count=0
        def fail(db,task,source,request):
            nonlocal count
            count+=1
            if count==5: raise RuntimeError('injected transaction failure')
            return original(db,task,source,request)
        with patch.object(self.store,'_dispatch',side_effect=fail),self.assertRaisesRegex(RuntimeError,'injected'):
            dispatch_plan(self.store,self.binding,self.plan)
        self.assertEqual(count,5);self.assertEqual(self.state(),before)

    def test_lease_recheck_before_commit_rolls_back_expansion(self):
        from medic.runtime.planning import dispatch_plan
        before=self.state();authorize=self.store.authorize;count=0
        def expires(db,binding):
            nonlocal count
            count+=1
            if count==2: raise Denied('injected expired lease at commit')
            return authorize(db,binding)
        with patch.object(self.store,'authorize',side_effect=expires),self.assertRaisesRegex(Denied,'expired lease'):
            dispatch_plan(self.store,self.binding,self.plan)
        self.assertEqual(count,2);self.assertEqual(self.state(),before)

    def test_stale_lease_and_cancelled_run_cannot_dispatch(self):
        from medic.runtime.planning import dispatch_plan
        stale=dict(self.binding);stale['lease_epoch']+=1
        before=self.state()
        with self.assertRaises(Denied): dispatch_plan(self.store,stale,self.plan)
        self.assertEqual(self.state(),before)
        self.store.cancel(self.run);before=self.state()
        with self.assertRaises(Denied): dispatch_plan(self.store,self.binding,self.plan)
        self.assertEqual(self.state(),before)

    def test_rework_mode_forbids_initial_graph_and_v6_retains_dispute_prompts(self):
        from medic.runtime.planning import dispatch_plan
        from medic.runtime.mailbox import enabled
        self.assertTrue(enabled(AssessmentInput.model_validate(self.source)))
        bundles=load_bundles()
        for role in ('evaluator','challenger_scan','challenger','reevaluator','judge','aggregator','common'):
            self.assertEqual(bundles['medic-p2-roles-v5'][role],bundles['medic-p2-roles-v6'][role])
        with self.store.connect(self.run) as db:
            context=json.loads(db.execute('SELECT context_json FROM tasks WHERE task_id=?',(self.binding['task_id'],)).fetchone()[0])
            context['rework_plan']={'status':'planning'}
            db.execute('UPDATE tasks SET context_json=? WHERE task_id=?',(json.dumps(context),self.binding['task_id']))
        service=ToolService(self.store,self.binding)
        self.assertNotIn('runtime_dispatch_plan',{t['name'] for t in service.definitions()})
        with self.assertRaisesRegex(Denied,'rework'): dispatch_plan(self.store,self.binding,self.plan)

    def test_concurrent_identical_calls_create_each_node_once(self):
        from medic.runtime.planning import dispatch_plan
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:dispatch_plan(self.store,self.binding,self.plan),range(2)))
        self.assertEqual(sorted(r['created'] for r in results),[0,225])
        snapshot=self.store.snapshot(self.run)
        self.assertEqual(len(snapshot['tasks']),226)
        self.assertEqual(len([e for e in snapshot['events'] if e['kind']=='agent_dispatched_task']),225)

    def test_expansion_size_and_duplicate_dependencies_rejected(self):
        for mode in ('size','dependency','whitespace'):
            plan=copy.deepcopy(self.plan)
            if mode=='size': plan['groups']*=32
            elif mode=='dependency': plan['groups'][0]['nodes'][-1]['dependencies'].append(copy.deepcopy(plan['groups'][0]['nodes'][-1]['dependencies'][0]))
            else: plan['groups'][0]['nodes'][0]['reason']='  '
            before=self.state()
            with self.subTest(mode=mode),self.assertRaises(ValueError): self.call(plan)
            self.assertEqual(self.state()['tasks'],before['tasks']);self.assertEqual(self.state()['outbox'],before['outbox'])

    def test_loaded_bundles_share_the_compact_planner_surface(self):
        old=copy.deepcopy(self.source);old['versions']['prompt_bundle_version']='medic-p2-roles-v5'
        run=self.store.create_run(old);binding=self.store.claim_next(run);service=ToolService(self.store,binding)
        names={t['name'] for t in service.definitions()}
        self.assertIn('runtime_dispatch_plan',names)
        self.assertTrue({'runtime_dispatch_batch','runtime_dispatch_task'}<=names)


if __name__=='__main__': unittest.main()
