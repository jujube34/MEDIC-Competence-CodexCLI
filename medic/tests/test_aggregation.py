"""Synthetic contract fixtures, not model-quality or real-execution evidence."""
import copy
import tempfile
import unittest
from pathlib import Path

from medic.domain.contracts import AssessmentInput, CHECKS
from medic.domain.graph import AGGREGATE, required_graph, scoped_spans
from medic.domain.validation import InvalidResult, release_gate
from medic.p0.common import digest
from medic.runtime.store import Denied, Store
from medic.tests.test_multiscope import multi_fixture
from medic.tools.service import ToolService


class AggregationContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "runtime.sqlite3")
        self.source = AssessmentInput.model_validate(multi_fixture())
        self.run = self.store.create_run(self.source.model_dump())
        coordinator = self.store.claim_next(self.run)
        self.ids = {}
        for key, deps in required_graph(self.source).items():
            self.ids[key] = self.store.dispatch(coordinator, {"material_id": key[0], "competency_id": key[1], "role": key[2],
                "dependencies": [self.ids[d] for d in deps], "reason": "Synthetic aggregate contract test"})["task_id"]
        receipt = self.store.submit(coordinator, {})["receipt"]
        self.store.complete(coordinator, receipt, "unit-coordinator", "unit-turn")

    def tearDown(self):
        self.temp.cleanup()

    def value(self, binding):
        c = binding["context"]
        base = {"task_id": binding["task_id"], "context_hash": c["context_hash"]}
        spans = scoped_spans(self.source, c["material_id"])
        refs = [{k: s.model_dump()[k] for k in ("evidence_id", "quote", "span_hash")} for s in spans]
        role = c["role"]
        if role in ("evaluator", "aggregator", "reevaluator"):
            value = {**base, "competency_id": c["competency_id"], "material_id": c["material_id"], "revision": 2 if role == "reevaluator" else 1,
                     "status": "suggested", "grade": "B", "attribution": "self_reported", "supporting_evidence": refs[1:3], "contradicting_evidence": [],
                     "criterion_checks": [{"criterion_id": criterion.criterion_id, "finding": "met" if criterion.grade in "AB" else "not_observed",
                                           "evidence_refs": [refs[1 if criterion.grade == "A" else 2]], "reason": "Synthetic anchor check"}
                                          for criterion in self.source.criteria[c["competency_id"]]],
                     "claims": [{"claim_id": "claim-" + grade, "criterion_id": c["competency_id"] + "." + grade + "1",
                                 "statement": "Limited to this submitted self-reported material.", "evidence_refs": [refs[i]]} for grade, i in (("A", 1), ("B", 2))],
                     "missing_evidence": ["Independent corroboration"], "limitations": ["Synthetic, self-reported material only"],
                     "challenge_responses": [] if role != "reevaluator" else [{"challenge_id": ch["challenge_id"], "disposition": "accepted",
                         "reason": "Keep the claim explicitly limited to self-reported material.", "evidence_refs": [refs[2]]} for ch in c["prior"]["challenger"]["challenges"]]}
            if c["material_id"] == AGGREGATE:
                value.update(source_result_hashes=c["source_result_hashes"], source_groups=c["aggregation"]["source_groups"],
                             synthesis_reason="The materials share one source event; duplicate copies add no independent support. Anchors A/B are supported by the source, not by averaging grades.",
                             corroboration_event_ids=[])
            return value
        if role == "challenger_scan":
            return {**base, "observations": [{"finding": "Only self-reporting; duplicated same-event sources are not independent.", "evidence_refs": [refs[2]]}], "open_questions": []}
        if role == "challenger":
            return {**base, "independent_findings_hash": c["independent_findings_hash"], "checked_claim_ids": ["claim-A", "claim-B"],
                    "challenges": [{"challenge_id": "challenge-1", "claim_id": "claim-B", "kind": "attribution", "severity": "blocking", "evidence_refs": [refs[2]],
                                    "falsification_question": "Does the claim imply independent validation?", "explanation": "Self-report only."}], "conclusion": "Preserve this limit."}
        return {**base, "goal_version": c["goal"]["version"], "candidate_hash": c["candidate_hash"],
                "checks": [{"check": check, "passed": True, "reason": "Synthetic scoped check"} for check in CHECKS],
                "blocking_issues": [], "verdict": "pass", "reason": "Scoped contract fixture passes", "next_tasks": []}

    def complete(self, binding):
        service = ToolService(self.store, binding)
        for span in scoped_spans(self.source, binding["context"]["material_id"]):
            service.call("evidence_read_span", {"evidence_id": span.evidence_id})
        receipt = service.call("assessment_submit_result", self.value(binding))["receipt"]
        self.store.complete(binding, receipt, "unit-thread-" + binding["task_id"], "unit-turn")

    def finish_before(self, key=None):
        for node, task in self.ids.items():
            if node == key:
                return self.store.lease(task)
            self.complete(self.store.lease(task))
        return self.store.snapshot(self.run)

    def gate(self, snapshot):
        return release_gate(self.source, snapshot["results"], snapshot["tasks"], "live")

    def test_full_multidimension_aggregate_chain_and_missing_review(self):
        snapshot = self.finish_before()
        self.assertEqual(len(snapshot["tasks"]), 31)
        self.assertTrue(self.gate(snapshot)["passed"], self.gate(snapshot))
        self.store.publish(self.run, {"gate": self.gate(snapshot)})
        self.assertEqual(self.store.snapshot(self.run)["state"], "ready_for_human_review")
        snapshot["results"].pop(self.ids[AGGREGATE, "M2", "judge"])
        self.assertFalse(self.gate(snapshot)["passed"])

    def test_duplicate_source_cannot_create_independent_corroboration(self):
        binding = self.finish_before((AGGREGATE, "M2", "aggregator"))
        value = self.value(binding)
        self.assertEqual(len(value["source_groups"]), 1)
        value["attribution"] = "corroborated"
        value["corroboration_event_ids"] = list(value["source_groups"])
        with self.assertRaisesRegex(InvalidResult, "independent_corroboration"):
            self.store.submit(binding, value)
        value = self.value(binding)
        value["source_groups"] = {"invented-independent-source": ["synthetic-material-2"]}
        with self.assertRaisesRegex(InvalidResult, "dedup_mismatch"):
            self.store.submit(binding, value)

    def test_stale_source_result_hash_and_blind_scan_boundaries(self):
        binding = self.finish_before((AGGREGATE, "M2", "aggregator"))
        value = self.value(binding)
        value["source_result_hashes"] = {k: "stale" for k in value["source_result_hashes"]}
        with self.assertRaisesRegex(InvalidResult, "source_result_mismatch"):
            self.store.submit(binding, value)
        blind = self.store.lease(self.ids[AGGREGATE, "M2", "challenger_scan"])
        self.assertEqual(blind["context"]["prior"], {})
        self.assertNotIn("source_result_hashes", blind["context"])
        self.assertEqual(len(scoped_spans(self.source, AGGREGATE)), 8)
        with self.assertRaises(Denied):
            ToolService(self.store, blind).call("evidence_read_span", {"evidence_id": "foreign-employee"})

    def test_old_judge_cannot_authorize_changed_source_assessment(self):
        snapshot = self.finish_before()
        task = next(t for t in snapshot["tasks"] if t["task_id"] == self.ids["synthetic-material-1", "M2", "reevaluator"])
        value = snapshot["results"][task["task_id"]]
        value["limitations"].append("Changed after aggregate review")
        task["result_hash"] = digest(value)
        self.assertFalse(self.gate(snapshot)["passed"])
