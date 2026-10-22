import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from medic.domain.contracts import AssessmentInput
from medic.domain.validation import InvalidResult, release_gate
from medic.p0.common import ROOT, digest
from medic.runtime.store import Denied, REQUIRED, Store
from medic.tools.service import ToolService


def fixture():
    from medic.runtime.build import VERSION
    source = json.loads((ROOT / "medic/evals/fixtures/single_dimension.json").read_text(encoding="utf-8"))
    source['versions']['runtime_version'] = VERSION
    return source


class RuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.source = fixture()
        self.run = self.store.create_run(self.source)
        self.coordinator = self.store.lease(self.store.snapshot(self.run)["tasks"][0]["task_id"])

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        ids = {}
        for role, dependencies in REQUIRED.items():
            ids[role] = self.store.dispatch(self.coordinator, {"role": role, "material_id": "synthetic-material-1", "competency_id": "M2",
                "dependencies": [ids[d] for d in dependencies], "reason": "Synthetic unit-test task graph"})["task_id"]
        receipt = self.store.submit(self.coordinator, {})["receipt"]
        self.store.complete(self.coordinator, receipt, "test-thread", "test-turn")
        return ids

    def assessment(self, binding, revision=1, responses=None):
        refs = [{k: s[k] for k in ("evidence_id", "span_hash", "quote")} for s in self.source["materials"][0]["spans"]]
        return {"task_id": binding["task_id"], "context_hash": binding["context"]["context_hash"], "competency_id": "M2",
                "material_id": "synthetic-material-1", "revision": revision, "status": "suggested", "grade": "B",
                "criterion_checks": [{"criterion_id": "M2." + c, "finding": "met" if i < 2 else "not_observed",
                                      "evidence_refs": [refs[1 if i == 0 else 2]], "reason": "Synthetic criterion check"}
                                     for i, c in enumerate(("A1", "B1", "C1", "D1"))],
                "claims": [{"claim_id": "claim-1", "criterion_id": "M2.A1", "statement": "Distinguishes study designs.", "evidence_refs": [refs[1]]},
                           {"claim_id": "claim-2", "criterion_id": "M2.B1", "statement": "Documents limits in this self-reported material.", "evidence_refs": [refs[2]]}],
                "attribution": "self_reported", "supporting_evidence": [refs[1], refs[2]], "contradicting_evidence": [refs[2]],
                "missing_evidence": ["Independent corroboration"], "limitations": ["Only this synthetic self-reported material"],
                "challenge_responses": responses or []}

    def read_all(self, binding):
        service = ToolService(self.store, binding)
        service.call("rubric_get_criterion", {})
        for span in self.source["materials"][0]["spans"]:
            service.call("evidence_read_span", {"evidence_id": span["evidence_id"]})

    def submit_complete(self, binding, value):
        self.read_all(binding)
        receipt = self.store.submit(binding, value)["receipt"]
        self.store.complete(binding, receipt, "test-thread-" + binding["task_id"], "test-turn")

    def full_graph(self):
        ids = self.plan()
        first = self.store.lease(ids["evaluator"])
        self.submit_complete(first, self.assessment(first))
        scan = self.store.lease(ids["challenger_scan"])
        ref = self.assessment(first)["supporting_evidence"][1]
        self.assertEqual(scan["context"]["prior"], {})
        self.submit_complete(scan, {"task_id": scan["task_id"], "context_hash": scan["context"]["context_hash"],
            "observations": [{"finding": "No independent corroboration.", "evidence_refs": [ref]}], "open_questions": ["Can ownership be corroborated?"]})
        challenger = self.store.lease(ids["challenger"])
        self.submit_complete(challenger, {"task_id": challenger["task_id"], "context_hash": challenger["context"]["context_hash"],
            "independent_findings_hash": challenger["context"]["independent_findings_hash"],
            "challenges": [{"challenge_id": "challenge-1", "claim_id": "claim-2", "kind": "attribution", "severity": "blocking",
                            "evidence_refs": [ref], "falsification_question": "Does the claim imply independently corroborated ability?", "explanation": "Only self-reported evidence."}],
            "checked_claim_ids": ["claim-1", "claim-2"], "conclusion": "Limit the claim to submitted material."})
        second = self.store.lease(ids["reevaluator"])
        self.submit_complete(second, self.assessment(second, 2, [{"challenge_id": "challenge-1", "disposition": "accepted",
            "reason": "Explicitly limit to self-reported material.", "evidence_refs": [ref]}]))
        judge = self.store.lease(ids["judge"])
        from medic.domain.contracts import CHECKS
        self.submit_complete(judge, {"task_id": judge["task_id"], "context_hash": judge["context"]["context_hash"],
            "goal_version": self.source["goal"]["version"], "candidate_hash": judge["context"]["candidate_hash"],
            "checks": [{"check": c, "passed": True, "reason": "Synthetic unit-test check"} for c in CHECKS],
            "blocking_issues": [], "verdict": "pass", "reason": "All scoped checks satisfied.", "next_tasks": []})
        return self.store.snapshot(self.run)

    def gate(self, snapshot):
        return release_gate(AssessmentInput.model_validate(snapshot["source"]), snapshot["results"], snapshot["tasks"], snapshot["mode"])

    def test_coordinator_cannot_read_or_impersonate(self):
        service = ToolService(self.store, self.coordinator)
        manifest = service.call("assessment_get_manifest", {})
        self.assertNotIn("quote", json.dumps(manifest))
        with self.assertRaises(Denied):
            service.call("evidence_read_span", {"evidence_id": "span-1"})
        changed = dict(self.coordinator, token="invented-token")
        with self.assertRaises(Denied):
            ToolService(self.store, changed)

    def test_plan_cannot_skip_required_roles_or_dependencies(self):
        with self.assertRaises(InvalidResult):
            self.store.submit(self.coordinator, {})
        with self.assertRaises(Denied):
            self.store.dispatch(self.coordinator, {"role": "judge", "material_id": "synthetic-material-1", "competency_id": "M2", "dependencies": [], "reason": "Skip review"})

    def test_receipt_cannot_be_fabricated(self):
        with self.assertRaises(Denied):
            self.store.complete(self.coordinator, "submitted-made-up", "thread", "turn")
        self.assertEqual(self.store.snapshot(self.run)["tasks"][0]["status"], "running")

    def test_malformed_submissions_consume_repair_budget(self):
        import jsonschema
        ids = self.plan()
        service = ToolService(self.store, self.store.lease(ids["evaluator"]))
        for _ in range(3):
            with self.assertRaises(jsonschema.ValidationError):
                service.call("assessment_submit_result", {})
        with self.assertRaisesRegex(Denied, "result_repair_budget_exhausted"):
            service.call("assessment_submit_result", {})

    def test_restart_requeues_expired_submission_and_fences_old_worker(self):
        ids = self.plan()
        old = self.store.lease(ids["evaluator"])
        self.read_all(old)
        receipt = self.store.submit(old, self.assessment(old))["receipt"]
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE task_id=?", (old["task_id"],))
        restarted = Store(self.store.path)
        self.assertEqual(restarted.recover(self.run), [old["task_id"]])
        new = restarted.lease(old["task_id"])
        self.assertEqual(new["lease_epoch"], old["lease_epoch"] + 1)
        with self.assertRaises(Denied):
            restarted.complete(old, receipt, "late-thread", "late-turn")
        snapshot = restarted.snapshot(self.run)
        self.assertNotIn(old["task_id"], snapshot["results"])
        self.assertEqual(snapshot["tasks"][0]["status"], "completed")
        self.submit_complete(new, self.assessment(new))
        with restarted.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM results WHERE task_id=?", (old["task_id"],)).fetchone()[0], 2)

    def test_recovery_is_bounded_and_cannot_revive_cancellation(self):
        ids = self.plan()
        for _ in range(3):
            binding = self.store.lease(ids["evaluator"])
            self.store.fail(binding, "simulated_worker_loss")
            if binding["lease_epoch"] < 3:
                self.store.recover(self.run)
        with self.assertRaisesRegex(Denied, "attempt_budget"):
            self.store.recover(self.run)
        self.store.cancel(self.run)
        with self.assertRaises(Denied):
            self.store.recover(self.run)

    def test_cross_employee_evidence_and_role_parameters_rejected(self):
        ids = self.plan()
        evaluator = self.store.lease(ids["evaluator"])
        service = ToolService(self.store, evaluator)
        with self.assertRaises(Denied):
            service.call("evidence_read_span", {"evidence_id": "other-employee-secret"})
        with self.assertRaises(Exception):
            service.call("evidence_read_span", {"evidence_id": "span-1", "role": "coordinator"})
        with self.assertRaises(Denied):
            service.call("runtime_dispatch_task", {})

    def test_verbatim_excerpt_keeps_full_snapshot_hash(self):
        ids = self.plan()
        binding = self.store.lease(ids["evaluator"])
        self.read_all(binding)
        value = self.assessment(binding)
        value["supporting_evidence"][0]["quote"] = "区间跨过1，不能断言差异明确。"
        self.store.submit(binding, value)
        value["supporting_evidence"][0]["quote"] = "差异非常明确。"
        with self.assertRaisesRegex(InvalidResult, "quote_not_verbatim"):
            self.store.submit(binding, value)

    def test_full_live_shaped_chain_passes_only_scoped_gate(self):
        snapshot = self.full_graph()
        gate = self.gate(snapshot)
        self.assertTrue(gate["passed"], gate)
        self.assertFalse(gate["final_employee_decision"])

    def test_judge_pass_does_not_override_fake_reference(self):
        snapshot = self.full_graph()
        task = next(t for t in snapshot["tasks"] if t["role"] == "reevaluator")
        value = snapshot["results"][task["task_id"]]
        value["supporting_evidence"][0]["quote"] = "fabricated evidence"
        task["result_hash"] = digest(value)  # Even a consistent result hash cannot legitimize a fake quote.
        self.assertFalse(self.gate(snapshot)["passed"])

    def test_gate_rejects_malformed_result_and_missing_coordinator(self):
        snapshot = self.full_graph()
        judge = next(t for t in snapshot["tasks"] if t["role"] == "judge")
        snapshot["results"][judge["task_id"]] = {}
        judge["result_hash"] = digest({})
        self.assertFalse(self.gate(snapshot)["passed"])
        snapshot = self.full_graph_for_new_run()
        snapshot["tasks"] = [t for t in snapshot["tasks"] if t["role"] != "coordinator"]
        self.assertIn("missing_live_coordinator", self.gate(snapshot)["failures"])

    def test_polling_worker_observes_cancellation_without_model_output(self):
        from medic.adapters.codex.worker import wait_for_task
        store, run = self.store, self.run
        class SilentClient:
            events = []
            def pump(self, deadline):
                store.cancel(run)
        with self.assertRaises(Denied):
            wait_for_task(SilentClient(), store, self.coordinator, "thread", "turn")

    def test_unanswered_challenge_and_mock_mode_block(self):
        snapshot = self.full_graph()
        task = next(t for t in snapshot["tasks"] if t["role"] == "reevaluator")
        value = snapshot["results"][task["task_id"]]
        value["challenge_responses"] = []
        task["result_hash"] = digest(value)
        self.assertFalse(self.gate(snapshot)["passed"])
        snapshot = self.full_graph_for_new_run()
        snapshot["mode"] = "mock"
        self.assertFalse(self.gate(snapshot)["passed"])

    def full_graph_for_new_run(self):
        self.run = self.store.create_run(self.source)
        self.coordinator = self.store.lease(self.store.snapshot(self.run)["tasks"][0]["task_id"])
        return self.full_graph()

    def test_non_scoring_grade_and_cap_are_rejected(self):
        ids = self.plan()
        binding = self.store.lease(ids["evaluator"])
        self.read_all(binding)
        value = self.assessment(binding)
        value["grade"] = "D"
        with self.assertRaisesRegex(InvalidResult, "policy_cap"):
            self.store.submit(binding, value)
        snapshot = self.full_graph_for_new_run()
        snapshot["source"]["policies"]["M2"]["mode"] = "clue_only"
        self.assertFalse(self.gate(snapshot)["passed"])

    def test_cancel_fences_late_results_and_publication(self):
        ids = self.plan()
        binding = self.store.lease(ids["evaluator"])
        self.read_all(binding)
        self.store.cancel(self.run)
        with self.assertRaises(Denied):
            self.store.submit(binding, self.assessment(binding))
        with self.assertRaises(Denied):
            self.store.publish(self.run, {"gate": {"passed": True}})

    def test_unread_material_and_duplicate_publication_rejected(self):
        ids = self.plan()
        binding = self.store.lease(ids["evaluator"])
        with self.assertRaisesRegex(InvalidResult, "coverage"):
            self.store.submit(binding, self.assessment(binding))
        snapshot = self.full_graph_for_new_run()
        report = {"gate": self.gate(snapshot)}
        self.store.publish(self.run, report)
        with self.assertRaises(Denied):
            self.store.publish(self.run, report)

    def test_publication_rechecks_gate_inside_transaction(self):
        with self.assertRaisesRegex(Denied, "incomplete_execution"):
            self.store.publish(self.run, {"gate": {"passed": True}})
        snapshot = self.full_graph()
        gate = self.gate(snapshot)
        forged = dict(gate, passed=False)
        with self.assertRaisesRegex(Denied, "gate_mismatch"):
            self.store.publish(self.run, {"gate": forged})
        self.store.publish(self.run, {"gate": gate, "final_employee_decision": True})
        self.assertFalse(self.store.snapshot(self.run)["report"]["final_employee_decision"])

    def test_changed_snapshot_and_non_synthetic_inputs_rejected(self):
        value = fixture()
        value["materials"][0]["spans"][0]["quote"] = "changed"
        with self.assertRaises(InvalidResult):
            self.store.create_run(value)
        value = fixture()
        value["synthetic"] = False
        with self.assertRaises(Denied):
            self.store.create_run(value)

    def test_mcp_wire_is_utf8_even_under_legacy_windows_encoding(self):
        from medic.p0.common import save
        binding_path = Path(self.temp.name) / "binding.json"
        save(binding_path, {k: v for k, v in self.coordinator.items() if k != "context"} | {"database": self.store.path})
        env = os.environ.copy()
        env.update(MEDIC_TASK_BINDING=str(binding_path), PYTHONIOENCODING="gbk")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "assessment_get_manifest", "arguments": {}}}
        result = subprocess.run([sys.executable, str(ROOT / "medic/tools/server.py")],
                                input=(json.dumps(request) + "\n").encode(), env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        response = json.loads(result.stdout.decode("utf-8"))
        manifest = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(manifest["goal"]["description"], self.source["goal"]["description"])


if __name__ == "__main__":
    unittest.main()
