"""Expand an explicit Agent plan; never invent omitted roles or dependencies."""
from typing import Literal
from pydantic import Field
from medic.domain.contracts import Contract
from medic.domain.graph import AGGREGATE, required_graph

Role = Literal['evaluator','challenger_scan','challenger','reevaluator','judge','aggregator']
MAX_PLAN_NODES = 4096


class Dependency(Contract):
    role: Role
    scope: Literal['same_material','source_materials']


class Node(Contract):
    role: Role
    dependencies: list[Dependency] = Field(max_length=12)
    reason: str = Field(min_length=1,max_length=1000)


class Group(Contract):
    material_ids: list[str] = Field(min_length=1,max_length=64)
    competency_ids: list[str] = Field(min_length=1,max_length=64)
    nodes: list[Node] = Field(min_length=1,max_length=6)


class DispatchPlan(Contract):
    groups: list[Group] = Field(min_length=1,max_length=64)


def expand(source, plan):
    plan=DispatchPlan.model_validate(plan)
    if sum(len(g.material_ids)*len(g.competency_ids)*len(g.nodes) for g in plan.groups)>MAX_PLAN_NODES:
        raise ValueError('dispatch_plan_too_large')
    mandatory=required_graph(source); expanded={}
    for group in plan.groups:
        for material in group.material_ids:
            for competency in group.competency_ids:
                for node in group.nodes:
                    key=(material,competency,node.role)
                    if key in expanded: raise ValueError('dispatch_plan_duplicate_node')
                    if key not in mandatory: raise ValueError('dispatch_plan_out_of_scope')
                    if not node.reason.strip(): raise ValueError('dispatch_plan_blank_reason')
                    dependencies=[]
                    for dep in node.dependencies:
                        if dep.scope=='source_materials':
                            if material!=AGGREGATE: raise ValueError('dispatch_plan_cross_material_dependency')
                            dependencies.extend((m.material_id,competency,dep.role) for m in source.materials)
                        else: dependencies.append((material,competency,dep.role))
                    if len(dependencies)!=len(set(dependencies)): raise ValueError('dispatch_plan_duplicate_dependency')
                    if set(dependencies)!=mandatory[key]: raise ValueError('dispatch_plan_mandatory_dependencies_mismatch')
                    expanded[key]={'dependencies':set(dependencies),'reason':node.reason}
    return expanded
