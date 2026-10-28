"""Explicit whole-span references for the v7 submission protocol.

This is a distinct input representation, never a repair of a legacy quotation.
Only schema-typed EvidenceRef slots resolve; all other model decisions survive
unchanged. The caller must pass the lease-authorized frozen span collection and
submit the result through the ordinary Store validators.
"""
from copy import deepcopy

import jsonschema

REFERENCE = '#/$defs/EvidenceRef'


def policy_schema(schema, policy):
    """Expose the existing semantic policy at the tool's input boundary too."""
    schema = deepcopy(schema)
    if 'grade' not in schema.get('properties', {}):
        return schema
    mode, cap = policy['mode'], policy.get('cap')
    if mode != 'suggest':
        schema['properties']['grade'] = {'type': 'null', 'description': 'This policy does not permit AI grading.'}
        schema['properties']['status'] = {'type': 'string', 'enum':
            ['clue_only'] if mode == 'clue_only' else ['risk_signal', 'insufficient_evidence']}
    else:
        grades = list('ABC'[:'ABCD'.index(cap)+1]) if cap else []
        schema['properties']['grade'] = {'enum': [None, *grades]}
        schema['properties']['status'] = {'enum': ['suggested', 'insufficient_evidence', 'not_applicable']}
    return schema


def selection_schema(model):
    schema = deepcopy(model.model_json_schema())
    if 'EvidenceRef' in schema.get('$defs', {}):
        schema['$defs']['EvidenceRef'] = {
            'type': 'object',
            'description': 'Explicit selection of the ENTIRE frozen original span already read by this task.',
            'properties': {'evidence_id': {'type': 'string', 'minLength': 1}},
            'required': ['evidence_id'],
            'additionalProperties': False,
        }
    return schema


def resolve_selection(model, value, spans):
    schema = selection_schema(model)
    jsonschema.validate(value, schema)
    definitions = schema.get('$defs', {})
    by_id = {span.evidence_id: span for span in spans}

    def walk(node, item):
        if node.get('$ref') == REFERENCE:
            span = by_id.get(item['evidence_id'])
            if span is None:
                raise ValueError('evidence_not_in_task_scope:' + item['evidence_id'])
            return {'evidence_id': span.evidence_id, 'span_hash': span.span_hash, 'quote': span.quote}
        if '$ref' in node:
            ref = node['$ref']
            if not ref.startswith('#/$defs/'):
                raise ValueError('unsupported_evidence_selection_schema_reference')
            return walk(definitions[ref.removeprefix('#/$defs/')], item)
        if 'anyOf' in node:
            matches = [branch for branch in node['anyOf']
                       if jsonschema.Draft202012Validator({'$defs': definitions, **branch}).is_valid(item)]
            if len(matches) != 1:
                raise ValueError('ambiguous_evidence_selection_schema')
            return walk(matches[0], item)
        if node.get('type') == 'object':
            properties = node.get('properties', {})
            return {key: walk(properties[key], val) if key in properties else deepcopy(val)
                    for key, val in item.items()}
        if node.get('type') == 'array':
            return [walk(node['items'], val) for val in item]
        return deepcopy(item)

    return walk(schema, value)
