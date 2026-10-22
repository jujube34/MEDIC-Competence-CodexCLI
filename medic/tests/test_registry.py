import json
import tempfile
import unittest
from pathlib import Path

from medic.domain.registry import load_candidate, validate_bundle
from medic.p0.common import CODES, ROOT, digest


class CandidateRegistry(unittest.TestCase):
    def fixture(self):
        policy = json.loads((ROOT / "medic/p0/policy.candidate.json").read_text(encoding="utf-8"))
        return {"status": "awaiting_business_signoff", "official_assessment_allowed": False,
                "competencies": {c: {} for c in CODES}, "dimensions": {c: [] for c in "MEDIC"}, "policy": policy,
                "recommendations": [{"job_key": f"synthetic-job-{i}", "competency_id": c, "grade": "B", "score": 2}
                                    for i in range(49) for c in CODES]}

    def test_old_matrix_or_auto_publication_rejected(self):
        value = self.fixture()
        validate_bundle(value)
        value["recommendations"] = value["recommendations"][:697]
        with self.assertRaisesRegex(ValueError, "49_jobs_833"):
            validate_bundle(value)
        value = self.fixture()
        value["official_assessment_allowed"] = True
        with self.assertRaisesRegex(ValueError, "cannot_publish"):
            validate_bundle(value)

    def test_changed_immutable_snapshot_and_path_traversal_rejected(self):
        value = self.fixture()
        ident = digest(value)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / (ident + ".json")
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_candidate(root, ident), value)
            value["recommendations"][0]["grade"] = "C"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot_changed"):
                load_candidate(root, ident)
            with self.assertRaisesRegex(ValueError, "invalid_registry_id"):
                load_candidate(root, "../secrets")
