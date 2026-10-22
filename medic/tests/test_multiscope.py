import copy
import tempfile
import unittest
from pathlib import Path

from medic.domain.contracts import AssessmentInput
from medic.domain.validation import InvalidResult, release_gate
from medic.domain.graph import AGGREGATE, required_graph
from medic.p0.common import digest
from medic.runtime.runner import execute
from medic.runtime.store import Denied, REQUIRED, Store
from medic.tests.test_runtime import fixture
from medic.tools.service import ToolService


def multi_fixture():
    source = fixture()
    material = copy.deepcopy(source["materials"][0])
    material["material_id"] = "synthetic-material-2"
    material["version"] = "synthetic-material-2-v1"
    for span in material["spans"]:
        span["material_id"] = material["material_id"]
        span["material_version"] = material["version"]
        span["evidence_id"] = "second-" + span["evidence_id"]
        span["span_hash"] = digest([span["material_version"], span["locator"], span["quote"]])
    source["materials"].append(material)
    source["goal"]["competency_scope"].append("D1")
    source["criteria"]["D1"] = [{**c, "criterion_id": c["criterion_id"].replace("M2", "D1")} for c in source["criteria"]["M2"]]
    source["policies"]["D1"] = {"mode": "suggest", "cap": "B", "signed_off": False}
    return source


class MultiScopeGraph(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "runtime.sqlite3")
        self.source = multi_fixture()
        self.run = self.store.create_run(self.source)
        self.coordinator = self.store.claim_next(self.run)

    def tearDown(self):
        self.temp.cleanup()

    def test_batch_planning_requires_every_scope_and_is_idempotent(self):
        service = ToolService(self.store, self.coordinator)
        manifest = service.call("assessment_get_manifest", {})
        self.assertEqual(len(manifest["required_scopes"]), 4)
        scopes = [(s["material_id"], s["competency_id"]) for s in manifest["required_scopes"]]
        ids = {}
        for role, dependencies in REQUIRED.items():
            requests = [{"material_id": m, "competency_id": c, "role": role,
                         "dependencies": [ids[m, c, d] for d in dependencies], "reason": "Synthetic full scope planning"} for m, c in scopes]
            outcome = service.call("runtime_dispatch_batch", {"tasks": requests})
            self.assertEqual(outcome, service.call("runtime_dispatch_batch", {"tasks": requests}))
            for scope, result in zip(scopes, outcome["outcomes"]):
                ids[*scope, role] = result["task_id"]
            if role != "judge":
                with self.assertRaises(InvalidResult):
                    self.store.submit(self.coordinator, {})
        with self.assertRaises(InvalidResult):
            self.store.submit(self.coordinator, {})
        for key, deps in required_graph(AssessmentInput.model_validate(self.source)).items():
            if key[0] == AGGREGATE:
                ids[key] = self.store.dispatch(self.coordinator, {"material_id": key[0], "competency_id": key[1], "role": key[2],
                    "dependencies": [ids[d] for d in deps], "reason": "Mandatory aggregate adversarial chain"})["task_id"]
        receipt = self.store.submit(self.coordinator, {})["receipt"]
        self.store.complete(self.coordinator, receipt, "test-thread", "test-turn")
        self.assertEqual(len(self.store.snapshot(self.run)["tasks"]), 31)
        first = self.store.lease(ids[scopes[0][0], scopes[0][1], "evaluator"])
        service = ToolService(self.store, first)
        self.assertEqual(len(service.call("assessment_get_manifest", {})["material_catalog"]), 1)
        with self.assertRaises(Denied):
            service.call("evidence_read_span", {"evidence_id": "second-span-1"})
        gate = release_gate(AssessmentInput.model_validate(self.source), {}, self.store.snapshot(self.run)["tasks"], "live")
        self.assertIn("missing_valid_attempt:__aggregate__:M2:aggregator", gate["failures"])

    def test_cross_scope_dependencies_are_rejected(self):
        task = self.store.dispatch(self.coordinator, {"role": "evaluator", "material_id": "synthetic-material-1", "competency_id": "M2",
                                                     "dependencies": [], "reason": "Synthetic"})
        with self.assertRaisesRegex(Denied, "dependency_scope"):
            self.store.dispatch(self.coordinator, {"role": "challenger", "material_id": "synthetic-material-2", "competency_id": "M2",
                                                  "dependencies": [task["task_id"]], "reason": "Forbidden cross-material dependency"})

    def test_human_only_creates_manual_items_and_forbids_scoring_agents(self):
        source = fixture()
        source["policies"]["M2"] = {"mode": "human_only", "cap": None, "signed_off": False}
        run = self.store.create_run(source)
        coordinator = self.store.claim_next(run)
        with self.assertRaisesRegex(Denied, "human_only"):
            self.store.dispatch(coordinator, {"role": "evaluator", "material_id": "synthetic-material-1", "competency_id": "M2",
                                              "dependencies": [], "reason": "Must not create model scoring"})
        receipt = self.store.submit(coordinator, {})["receipt"]
        self.store.complete(coordinator, receipt, "test-thread", "test-turn")
        class NeverWorker:
            def run(self, *args, **kwargs): raise AssertionError("manual-only graph has no model work")
        snapshot = execute(self.store, run, NeverWorker())
        self.assertEqual(snapshot["state"], "awaiting_human_review")
        self.assertEqual(len(snapshot["tasks"]), 1)
        self.assertIsNone(snapshot["report"]["manual_items"][0]["grade"])
