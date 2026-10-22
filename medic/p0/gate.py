"""P0 feasibility gate. Never grants business publication or production approval."""
from .common import REVISION, now


def evaluate(reports):
    baseline, provider, assets, app = (reports[x] for x in ("baseline", "provider", "assets", "appserver"))
    blockers = []
    if not (baseline.get("status") == "pass" and baseline.get("codex_revision") == REVISION
            and baseline.get("source_matches_revision") and baseline.get("generated_schema_matches")
            and baseline.get("build", {}).get("exit_code") == 0 and baseline.get("binary", {}).get("sha256")):
        blockers.append("fixed_source_build_and_schema")
    required = {"responses.tool_roundtrip", "responses.sse", "responses.json_schema",
                "responses.two_tools", "responses.tool_error_recovery", "responses.bounded_context"}
    passed = {x["name"] for x in provider.get("checks", []) if x["status"] == "pass"}
    if not required <= passed or not provider.get("synthetic_only"):
        blockers.append("real_provider_responses_contract")
    if assets.get("status") != "pass" or not all(assets.get("checks", {}).get(k) for k in ("model_17", "matrix_49_833", "policy_partition")):
        blockers.append("model_matrix_policy_audit")
    expected = {"initialize", "mcp_roundtrip", "output_schema", "resume_after_process_restart", "concurrent_threads", "interrupt"}
    actual = {x["name"] for x in app.get("checks", []) if x["status"] == "pass"}
    if (not expected <= actual or app.get("status") != "pass" or not app.get("synthetic_only")
            or app.get("binary_sha256") != (baseline.get("binary") or {}).get("sha256")
            or not app.get("endpoint_sha256") or app.get("upstream_endpoint_sha256", app.get("endpoint_sha256")) != provider.get("endpoint_sha256")
            or not app.get("model") or app.get("model") != provider.get("model")):
        blockers.append("fixed_binary_appserver_lifecycle")
    route = app.get("route", "responses_direct")
    if route == "responses_namespace_gateway":
        if not any(t.get("namespaced_tools", 0) > 0 and t.get("terminal") == "response.completed" for t in app.get("gateway_trace", [])):
            blockers.append("namespace_gateway_not_proven")
        if not any(t.get("client_disconnected") and t.get("finished") and t.get("terminal") == "not_observed"
                   for t in app.get("gateway_trace", [])):
            blockers.append("gateway_cancellation_not_proven")
    elif route != "responses_direct" or "responses.namespace_tool" not in passed:
        blockers.append("native_namespace_contract")
    return {"created_at": now(), "status": "blocked" if blockers else "pass", "p0_accepted": not blockers,
            "blockers": blockers, "route": route if not blockers else "undecided",
            "next_phase": "P1_synthetic_only" if not blockers else "stop",
            "business_publication_allowed": False, "production_deployment_allowed": False,
            "remaining_approvals": ["business_model_and_policy_signoff", "quality_threshold_signoff"],
            "remaining_security_validation": "container_acl_and_egress_isolation_before_real_employee_data"}
