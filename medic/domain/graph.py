"""Mandatory assessment graph and evidence scopes; no scheduling or model calls."""
from __future__ import annotations

AGGREGATE = "__aggregate__"
MATERIAL_ROLES = {"evaluator": (), "challenger_scan": (), "challenger": ("evaluator", "challenger_scan"),
                  "reevaluator": ("evaluator", "challenger"), "judge": ("evaluator", "challenger_scan", "challenger", "reevaluator")}
AGGREGATE_ROLES = {"aggregator": (), "challenger_scan": (), "challenger": ("aggregator", "challenger_scan"),
                   "reevaluator": ("aggregator", "challenger"), "judge": ("aggregator", "challenger_scan", "challenger", "reevaluator")}


def required_graph(source):
    graph = {}
    for code in source.goal.competency_scope:
        if source.policies[code].mode == "human_only":
            continue
        for material in source.materials:
            for role, dependencies in MATERIAL_ROLES.items():
                graph[material.material_id, code, role] = {(material.material_id, code, d) for d in dependencies}
        if len(source.materials) > 1:
            for role, dependencies in AGGREGATE_ROLES.items():
                graph[AGGREGATE, code, role] = {(AGGREGATE, code, d) for d in dependencies}
            graph[AGGREGATE, code, "aggregator"] = {(m.material_id, code, r) for m in source.materials for r in ("reevaluator", "judge")}
    return graph


def scoped_materials(source, material_id):
    return list(source.materials) if material_id == AGGREGATE else [m for m in source.materials if m.material_id == material_id]


def scoped_spans(source, material_id):
    return [s for m in scoped_materials(source, material_id) for s in m.spans]


def source_groups(source):
    # Same declared event or identical uploaded bytes cannot become independent
    # corroboration merely by changing a filename or event label on re-upload.
    groups = []
    for material in source.materials:
        matches = [g for g in groups if material.source_event_id in g['events']
                   or material.source_sha256 and material.source_sha256 in g['hashes']]
        group = {'events': {material.source_event_id}, 'hashes': {material.source_sha256} if material.source_sha256 else set(),
                 'materials': [material.material_id]}
        for matched in matches:
            group['events'].update(matched['events'])
            group['hashes'].update(matched['hashes'])
            group['materials'].extend(matched['materials'])
            groups.remove(matched)
        groups.append(group)
    return dict(sorted((min(g['events']), sorted(g['materials'])) for g in groups))
