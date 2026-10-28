"""Atomic, lease-bound dispatch of the explicit compact Coordinator graph."""
import json
from medic.domain.contracts import DispatchRequest
from medic.domain.planning import DispatchPlan, expand
from medic.p0.common import digest
from medic.runtime.store import Denied


def dispatch_plan(store,binding,value):
    plan=DispatchPlan.model_validate(value)
    with store.connect(binding['run_id']) as db:
        task,source=store.authorize(db,binding)
        if task['role']!='coordinator': raise Denied('dispatch_coordinator_only')
        if source.versions.prompt_bundle_version not in ('medic-p2-roles-v6','medic-p2-roles-v7','medic-p2-roles-v8'): raise Denied('compact_plan_not_enabled')
        if json.loads(task['context_json']).get('rework_plan'): raise Denied('initial_plan_forbidden_during_rework')
        expanded=expand(source,plan)
        existing={(t['material_id'],t['competency_id'],t['role']):t['task_id'] for t in
                  db.execute("SELECT * FROM tasks WHERE run_id=? AND role!='coordinator'",(task['run_id'],))}
        ids=dict(existing);pending=dict(expanded)
        while pending:
            ready=sorted(key for key,node in pending.items() if node['dependencies']<=ids.keys())
            if not ready: raise Denied('dispatch_plan_dependency_missing_or_cycle')
            for key in ready:
                node=pending.pop(key)
                request=DispatchRequest(material_id=key[0],competency_id=key[1],role=key[2],
                    dependencies=[ids[d] for d in sorted(node['dependencies'])],reason=node['reason'])
                ids[key]=store._dispatch(db,task,source,request)['task_id']
        # Refuse a commit if the original lease/deadline expired during expansion.
        store.authorize(db,binding)
        receipt={'plan_sha256':digest(plan.model_dump()),'planned_nodes':len(expanded),
                 'created':len(set(expanded)-set(existing)),'existing':len(set(expanded)&set(existing))}
        store.event(db,task['run_id'],task['task_id'],'agent_dispatched_plan',
                    {**receipt,'lease_epoch':task['lease_epoch'],'plan':plan.model_dump()})
        return receipt
