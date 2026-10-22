"""Recheck a local run with current hard rules and emit a content-free summary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from medic.domain.contracts import AssessmentInput
from medic.domain.graph import AGGREGATE, required_graph
from medic.domain.validation import release_gate
from medic.p0.common import digest, now, save
from medic.prompts import load_bundles


def build_provenance(snapshot, protocols):
    """Verify archived identity links without requiring the auditor's environment."""
    from medic.runtime import build
    identity = snapshot.get('execution_build')
    if not identity:
        return False
    try:
        local = build.verify(identity['runtime_version'])
    except (ValueError, KeyError):
        return False
    contexts = [json.loads(t['context_json'] or '{}') for t in snapshot['tasks']]
    created = [e['data'].get('execution_build') for e in snapshot['events'] if e['kind'] == 'run_created']
    return (identity.get('manifest_sha256') == local['manifest_sha256']
            and identity.get('runtime_version') == snapshot['source']['versions']['runtime_version']
            and isinstance(identity.get('environment'), dict)
            and (snapshot.get('report') or {}).get('execution_build') == identity
            and created == [identity] and len(protocols) == len(contexts) and bool(contexts)
            and all(c.get('execution_build') == identity for c in contexts)
            and all(p.get('execution_build') == identity for p in protocols))


def verify(folder, output):
    folder = Path(folder)
    snapshot = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    source = AssessmentInput.model_validate(snapshot["source"])
    if not source.synthetic:
        raise ValueError("public_verification_summary_requires_synthetic_input")
    gate = release_gate(source, snapshot["results"], snapshot["tasks"], snapshot["mode"], snapshot.get('mailbox'), snapshot.get('rework_plan'))
    gateway = json.loads((folder / "gateway-summary.json").read_text(encoding="utf-8"))
    tasks = snapshot["tasks"]
    completed = {(e["task_id"], e["data"].get("lease_epoch"), e["data"].get("thread_id"), e["data"].get("turn_id"))
                 for e in snapshot["events"] if e["kind"] == "task_completed"}
    expected_roles = {"coordinator", "evaluator", "challenger_scan", "challenger", "reevaluator", "judge"}
    contexts = {t["task_id"]: json.loads(t["context_json"] or "{}") for t in tasks}
    graph = required_graph(source)
    task_keys = [(t["material_id"], t["competency_id"], t["role"]) for t in tasks if t["role"] != "coordinator"]
    all_completed = all(t["status"] == "completed" and (t["task_id"], t["lease_epoch"], t["thread_id"], t["turn_id"]) in completed for t in tasks)
    prompt_hashes = {version: digest(bundle) for version, bundle in load_bundles().items()}
    protocols = []
    raw_protocols = []
    for task in tasks:
        path = folder / 'workers' / task['task_id'] / str(task['lease_epoch']) / 'protocol-summary.json'
        protocol = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        raw_protocols.append(protocol)
        methods = {e['method'] for e in protocol.get('events', [])}
        protocols.append(protocol.get('context_hash') == task['context_hash']
                         and protocol.get('prompt_bundle_hash') == prompt_hashes.get(source.versions.prompt_bundle_version)
                         and {'thread/start', 'turn/start', 'turn/completed'} <= methods)
    checks = {"synthetic_only": source.synthetic,
              "required_graph_completed": len(tasks) == len(graph) + 1 and set(task_keys) == set(graph) and all_completed,
              "independent_blind_context": all(contexts[t['task_id']].get('prior') == {} for t in tasks if t['role'] == 'challenger_scan'),
              "worker_protocol_and_prompt_provenance": all(protocols),
              "live_role_provenance": all(t["execution_mode"] == "live" and t["thread_id"] and t["turn_id"] for t in tasks),
              "distinct_role_threads": len({t["thread_id"] for t in tasks}) == len(tasks),
              "current_hard_gate": gate["passed"],
              "stored_gate_matches": (snapshot.get("report") or {}).get("gate") == gate,
              "ready_for_human_review": snapshot["state"] == "ready_for_human_review",
              "not_final_employee_decision": (snapshot.get("report") or {}).get("final_employee_decision") is False}
    from medic.runtime.build import VERSION
    if snapshot.get('execution_build') or source.versions.runtime_version == VERSION:
        checks['frozen_application_build_provenance'] = build_provenance(snapshot, raw_protocols)
    if len(source.materials) == 1 and len(source.goal.competency_scope) == 1:
        checks['six_distinct_completed_roles'] = len(tasks) == 6 and {t['role'] for t in tasks} == expected_roles and all_completed
    results = {t["role"]: snapshot["results"].get(t["task_id"], {}) for t in tasks}
    summary = {"created_at": now(), "status": "pass" if all(checks.values()) else "fail",
               "run_id": snapshot["run_id"], "input_hash": snapshot["input_hash"], "versions": source.versions.model_dump(),
               "checks": checks, "gate": gate,
               "required_task_count": len(graph) + 1,
               "role_results": [{"role": t["role"], "material_id": t['material_id'], "competency_id": t['competency_id'],
                                 "attempt": t['lease_epoch'], "status": t["status"], "result_hash": t["result_hash"],
                                 "context_hash": t["context_hash"], "read_spans": len(t["read_evidence_ids"])} for t in tasks],
               "first_grade": results.get("evaluator", {}).get("grade"), "second_grade": results.get("reevaluator", {}).get("grade"),
               "challenge_count": len(results.get("challenger", {}).get("challenges", [])),
               "responded_challenge_count": len(results.get("reevaluator", {}).get("challenge_responses", [])),
               "judge_verdict": results.get("judge", {}).get("verdict"),
               "gateway_requests": len(gateway["requests"]), "charged_tokens": gateway["charged_tokens"],
               "run_total_requests": gateway.get("durable_budget", {}).get("requests", len(gateway["requests"])),
               "run_charged_or_reserved_tokens": gateway.get("durable_budget", {}).get("charged_or_reserved_tokens", gateway["charged_tokens"]),
               "run_estimated_usage_requests": sum(r['status'] == 'estimated' for r in gateway.get('durable_budget', {}).get('ledger', [])),
               "run_unsettled_requests": gateway.get('durable_budget', {}).get('unsettled_requests'),
               "estimated_usage_requests": sum(r["usage_source"] != "reported" for r in gateway["requests"]),
               "private_snapshot_hash": digest((folder / "run.json").read_bytes()),
               "quality_claim": "execution_and_boundary_verification_only_not_expert_accuracy"}
    summary['frozen_application_build_verified'] = checks.get('frozen_application_build_provenance', False)
    if snapshot.get('execution_build'):
        summary['execution_build'] = snapshot['execution_build']
    if len(source.materials) > 1 or len(source.goal.competency_scope) > 1:
        # A role name alone is not a unique key in a multi-scope run.
        for key in ('first_grade', 'second_grade', 'challenge_count', 'responded_challenge_count', 'judge_verdict'):
            summary.pop(key)
        summary['final_scopes'] = [
            {'material_id': t['material_id'], 'competency_id': t['competency_id'],
             'status': snapshot['results'].get(t['task_id'], {}).get('status'),
             'grade': snapshot['results'].get(t['task_id'], {}).get('grade')}
            for t in tasks if t['role'] == 'reevaluator' and (len(source.materials) == 1 or t['material_id'] == AGGREGATE)]
        summary['aggregation_evidence'] = [
            {'competency_id': t['competency_id'], 'source_result_count': len(value.get('source_result_hashes', {})),
             'source_event_count': len(value.get('source_groups', {})), 'attribution': value.get('attribution'),
             'independent_corroboration_event_count': len(value.get('corroboration_event_ids', [])),
             'challenge_responses': len(value.get('challenge_responses', []))}
            for t in tasks if t['role'] == 'reevaluator' and t['material_id'] == AGGREGATE
            for value in [snapshot['results'].get(t['task_id'], {})]]
    save(output, summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("output")
    args = parser.parse_args()
    result = verify(args.folder, args.output)
    print(json.dumps({"status": result["status"], "checks": result["checks"], "gate": result["gate"]}))
    raise SystemExit(0 if result["status"] == "pass" else 1)
