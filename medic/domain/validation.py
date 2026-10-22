from __future__ import annotations

import json

from medic.domain.contracts import Assessment, AssessmentInput, AggregationAssessment, CHECKS, ChallengeReport, GoalReview, result_type
from medic.domain.graph import AGGREGATE, AGGREGATE_ROLES, MATERIAL_ROLES, required_graph, scoped_spans, source_groups
from medic.p0.common import digest


class InvalidResult(ValueError):
    pass


def verify_input(source: AssessmentInput):
    for material in source.materials:
        if digest([s.quote for s in material.spans]) != material.sha256:
            raise InvalidResult("material_hash_mismatch")
        for span in material.spans:
            if (span.material_id != material.material_id or span.material_version != material.version
                    or span.span_hash != digest([span.material_version, span.locator, span.quote])):
                raise InvalidResult("invalid_evidence_snapshot")


def all_refs(value):
    if isinstance(value, dict):
        if {"evidence_id", "span_hash", "quote"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from all_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_refs(child)


def validate_refs(value, spans):
    known = {s.evidence_id: s for s in spans}
    for ref in all_refs(value):
        span = known.get(ref["evidence_id"])
        if not span:
            raise InvalidResult("evidence_reference_out_of_scope")
        if ref["span_hash"] != span.span_hash:
            raise InvalidResult("evidence_reference_hash_mismatch:" + span.evidence_id)
        # A citation may quote a contiguous excerpt; it must remain verbatim and
        # bind to the full immutable span hash. Never normalize or repair model text.
        if not ref["quote"].strip() or ref["quote"] not in span.quote:
            raise InvalidResult("evidence_reference_quote_not_verbatim:" + span.evidence_id)


def validate_result(role, value, task, source, prior):
    context = json.loads(task.get('context_json') or '{}')
    typed = result_type(role, task["material_id"], context.get('business_round', 1)).model_validate(value)
    if typed.task_id != task["task_id"] or typed.context_hash != task["context_hash"]:
        raise InvalidResult("task_context_mismatch")
    validate_refs(value, scoped_spans(source, task["material_id"]))
    if role == "aggregator":
        role = "evaluator"
    code = task["competency_id"]
    criteria = {c.criterion_id: c for c in source.criteria[code]}
    if isinstance(typed, Assessment):
        if typed.competency_id != code or typed.material_id != task["material_id"]:
            raise InvalidResult("assessment_scope_mismatch")
        context = json.loads(task.get('context_json') or '{}')
        if typed.revision != context.get('business_round', 1 if role == 'evaluator' else 2):
            raise InvalidResult("assessment_revision_mismatch")
        ids = [x.criterion_id for x in typed.criterion_checks]
        if len(set(ids)) != len(ids) or set(ids) != set(criteria):
            raise InvalidResult("criterion_coverage_mismatch:required=" + ",".join(sorted(criteria)))
        claim_ids = [c.claim_id for c in typed.claims]
        if len(set(claim_ids)) != len(claim_ids) or any(c.criterion_id not in criteria for c in typed.claims):
            raise InvalidResult("invalid_claims")
        if any(c.finding == "met" and not c.evidence_refs for c in typed.criterion_checks):
            raise InvalidResult("met_criterion_requires_evidence")
        policy = source.policies[code]
        if policy.mode != "suggest" and typed.grade is not None:
            raise InvalidResult("non_scoring_policy_grade")
        if typed.grade:
            if not policy.cap or "ABCD".index(typed.grade) > "ABCD".index(policy.cap) or typed.grade == "D":
                raise InvalidResult("policy_cap_exceeded")
            if not typed.supporting_evidence or not typed.claims:
                raise InvalidResult("suggestion_without_evidence")
            met = {c.criterion_id for c in typed.criterion_checks if c.finding == "met"}
            required = {c.criterion_id for c in criteria.values() if "ABCD".index(c.grade) <= "ABCD".index(typed.grade)}
            if not required <= met:
                raise InvalidResult("grade_not_supported_by_anchor_checks")
        if policy.mode == "clue_only" and typed.status != "clue_only":
            raise InvalidResult("clue_only_status_required")
        if policy.mode == "risk_scan" and typed.status not in ("risk_signal", "insufficient_evidence"):
            raise InvalidResult("risk_scan_status_required")
        if role == "evaluator" and typed.challenge_responses:
            raise InvalidResult("first_round_cannot_dispose_challenges")
        if role == "reevaluator":
            challenges = prior["challenger"]["challenges"]
            responses = {x.challenge_id: x for x in typed.challenge_responses}
            if len(responses) != len(typed.challenge_responses) or set(responses) != {x["challenge_id"] for x in challenges}:
                raise InvalidResult("unanswered_or_duplicate_challenge")
            for c in challenges:
                response = responses[c["challenge_id"]]
                if c["severity"] == "blocking" and response.disposition == "rejected" and not response.evidence_refs:
                    raise InvalidResult("blocking_rejection_requires_evidence")
            if context.get('business_round') == 3:
                previous = prior.get('previous_challenger', {})
                unresolved = {r['challenge_id'] for r in prior.get('previous_assessment', {}).get('challenge_responses', [])
                              if r['disposition'] == 'unresolved'}
                expected = {c['challenge_id'] for c in previous.get('challenges', [])
                            if c['severity'] == 'blocking' and c['challenge_id'] in unresolved}
                carried = typed.prior_dispute_responses
                if len(carried) != len(expected) or {r.challenge_id for r in carried} != expected:
                    raise InvalidResult('third_round_cannot_drop_prior_blocking_disputes')
                for response in carried:
                    if response.challenge_result_hash != digest(previous):
                        raise InvalidResult('prior_dispute_result_hash_mismatch')
                    if response.disposition == 'rejected' and not response.evidence_refs:
                        raise InvalidResult('prior_blocking_rejection_requires_evidence')
        if isinstance(typed, AggregationAssessment):
            expected = {material: digest(result) for material, result in prior["source_assessments"].items()}
            if set(expected) != {m.material_id for m in source.materials} or typed.source_result_hashes != expected:
                raise InvalidResult("aggregation_source_result_mismatch")
            if typed.source_groups != source_groups(source):
                raise InvalidResult("aggregation_same_event_dedup_mismatch")
            events = typed.corroboration_event_ids
            if len(set(events)) != len(events) or not set(events) <= set(typed.source_groups):
                raise InvalidResult("aggregation_invalid_corroboration_events")
            if typed.attribution == "corroborated" and len(events) < 2:
                raise InvalidResult("same_event_cannot_be_independent_corroboration")
    elif isinstance(typed, ChallengeReport):
        first = prior["evaluator"]
        if typed.independent_findings_hash != digest(prior["challenger_scan"]):
            raise InvalidResult("independent_findings_not_bound")
        claims = {x["claim_id"] for x in first["claims"]}
        if set(typed.checked_claim_ids) != claims or len(set(typed.checked_claim_ids)) != len(typed.checked_claim_ids):
            raise InvalidResult("challenge_claim_coverage")
        ids = [c.challenge_id for c in typed.challenges]
        if len(set(ids)) != len(ids) or any(c.claim_id not in claims for c in typed.challenges):
            raise InvalidResult("unknown_or_duplicate_challenge")
    elif isinstance(typed, GoalReview):
        if typed.goal_version != source.goal.version or typed.candidate_hash != task["candidate_hash"]:
            raise InvalidResult("judge_candidate_mismatch")
        if {c.check for c in typed.checks} != set(CHECKS):
            raise InvalidResult("judge_checklist_mismatch")
        if typed.verdict == "pass" and (typed.blocking_issues or not all(c.passed for c in typed.checks)):
            raise InvalidResult("judge_pass_with_failed_checks")
    return typed.model_dump()


def reviewed_prior(context, group):
    effective = dict(group)
    if context.get('rework_baseline_hash'):
        frozen = context['prior']
        baseline = {'assessment': frozen['previous_assessment'], 'challenge': frozen['previous_challenger'],
                    'use_as_evaluator': context['reuse_previous_assessment']}
        if digest(baseline) != context['rework_baseline_hash'] or digest(baseline['challenge']) != frozen['previous_challenge_result_hash']:
            raise InvalidResult('rework_baseline_mismatch')
        for key in ('previous_assessment', 'previous_challenger', 'previous_challenge_result_hash'):
            effective[key] = frozen[key]
        if baseline['use_as_evaluator']:
            effective['evaluator'] = baseline['assessment']
    return effective


def release_gate(source, results, task_records, execution_mode, mailbox=None, rework=None):
    failures = []
    mailbox = mailbox or []
    if rework and any(d['decision'] == 'human_review' for d in rework['decisions'].values()):
        failures.append('coordinator_requested_human_review')
    from medic.domain.disputes import pending_topics
    if any((m['material_id'], m['competency_id'], 'challenger') not in required_graph(source) for m in mailbox):
        failures.append('mailbox_out_of_scope')
    if execution_mode != "live":
        failures.append("execution_must_be_live")
    try:
        verify_input(source)
    except InvalidResult as exc:
        failures.append(str(exc))
    graph = required_graph(source)
    records = {(t["material_id"], t["competency_id"], t["role"]): t for t in task_records if t["role"] != "coordinator"}
    coordinators = [t for t in task_records if t["role"] == "coordinator"]
    if len(records) + len(coordinators) != len(task_records):
        failures.append("duplicate_task_scope")
    if len(coordinators) != 1 or coordinators[0]["status"] != "completed" or coordinators[0].get("execution_mode") != "live":
        failures.append("missing_live_coordinator")
    if not source.synthetic and any(not p.signed_off for p in source.policies.values()):
        failures.append("unsigned_production_policy")
    if set(records) - set(graph):
        failures.append("unexpected_task_scope")
    by_id = {t["task_id"]: t for t in task_records}
    for key, dependencies in graph.items():
        task = records.get(key)
        if not task or task["status"] != "completed" or task["task_id"] not in results:
            failures.append("missing_valid_attempt:" + ":".join(key))
            continue
        ids = task["dependencies"]
        ids = json.loads(ids) if isinstance(ids, str) else ids
        actual = {(by_id[x]["material_id"], by_id[x]["competency_id"], by_id[x]["role"]) for x in ids if x in by_id}
        if actual != dependencies or len(ids) != len(dependencies):
            failures.append("invalid_task_dependencies:" + ":".join(key))
        value = results[task["task_id"]]
        if task.get("execution_mode") != "live" or digest(value) != task.get("result_hash"):
            failures.append("result_integrity:" + ":".join(key))
        try:
            context = json.loads(task.get("context_json") or "{}")
        except ValueError:
            context = {}
        if context.get("context_hash") != task.get("context_hash") or digest({k: v for k, v in context.items() if k != "context_hash"}) != task.get("context_hash"):
            failures.append("context_integrity:" + ":".join(key))
        if context.get("input_hash") != digest(source.model_dump()):
            failures.append("frozen_input_or_version_changed:" + ":".join(key))
        if set(task.get("read_evidence_ids", [])) != {s.evidence_id for s in scoped_spans(source, key[0])}:
            failures.append("unread_evidence:" + ":".join(key))
    for code, policy in source.policies.items():
        if policy.mode == "human_only":
            failures.append("human_only_requires_human_review:" + code)
            if any(t["competency_id"] == code for t in records.values()):
                failures.append("forbidden_model_task_for_human_only:" + code)
            continue
        scopes = [m.material_id for m in source.materials] + ([AGGREGATE] if len(source.materials) > 1 else [])
        for material in scopes:
            conversation = [m for m in mailbox if m['material_id'] == material and m['competency_id'] == code]
            if pending_topics(conversation):
                failures.append('unanswered_dispute_message:' + material + ':' + code)
            roles = AGGREGATE_ROLES if material == AGGREGATE else MATERIAL_ROLES
            group = {}
            for role in roles:
                task = records.get((material, code, role))
                if task and task["status"] == "completed" and task["task_id"] in results:
                    group["evaluator" if role == "aggregator" else role] = results[task["task_id"]]
            if len(group) != 5:
                continue
            if material == AGGREGATE:
                group["source_assessments"], group["source_reviews"] = {}, {}
                for m in source.materials:
                    for role, bucket in (("reevaluator", "source_assessments"), ("judge", "source_reviews")):
                        t = records.get((m.material_id, code, role))
                        if t and t["status"] == "completed" and t["task_id"] in results:
                            group[bucket][m.material_id] = results[t["task_id"]]
            valid = True
            for role in roles:
                try:
                    task = records[material, code, role]
                    context = json.loads(task.get('context_json') or '{}')
                    effective = reviewed_prior(context, group)
                    validate_result(role, group["evaluator" if role == "aggregator" else role], task, source, effective)
                except (ValueError, KeyError) as exc:
                    failures.append(f"invalid_result:{material}:{code}:{role}:{type(exc).__name__}")
                    valid = False
            if not valid:
                continue
            second, challenge, judge = group["reevaluator"], group["challenger"], group["judge"]
            candidate = {"goal": source.goal.model_dump(), "input_hash": digest(source.model_dump()),
                         "prior": {r: v for r, v in group.items() if r != "judge"}}
            judge_context = json.loads(records[material, code, 'judge']['context_json'])
            candidate['prior'] = reviewed_prior(judge_context, candidate['prior'])
            if conversation:
                candidate['mailbox'] = conversation
            if judge["candidate_hash"] != digest(candidate):
                failures.append("judge_reviewed_different_candidate:" + material + ":" + code)
            if judge["verdict"] != "pass":
                failures.append("judge_not_pass:" + material + ":" + code)
            responses = {x["challenge_id"]: x for x in second["challenge_responses"]}
            if any(c["severity"] == "blocking" and responses.get(c["challenge_id"], {}).get("disposition") not in ("accepted", "rejected")
                   for c in challenge["challenges"]):
                failures.append("unresolved_blocking_challenge:" + material + ":" + code)
            if any(r['disposition'] == 'unresolved' for r in second.get('prior_dispute_responses', [])):
                failures.append('unresolved_prior_blocking_challenge:' + material + ':' + code)
            if (material == AGGREGATE or len(source.materials) == 1) and second["status"] in ("insufficient_evidence", "not_applicable"):
                failures.append("requires_human_review:" + code)
    return {"passed": not failures, "failures": sorted(set(failures)), "execution_mode": execution_mode,
            "publication_scope": "synthetic_preview" if source.synthetic else "human_review_package",
            "final_employee_decision": False}
