import unittest

from medic.evals.metrics import evaluate


class ComparisonMetrics(unittest.TestCase):
    def test_missing_and_abstained_grades_are_not_zeros(self):
        labels = [{"case_id": str(i), "grade": g, "mode": "suggest", "cap": "C"} for i, g in enumerate(("A", "B", "C"))]
        predicted = [{"case_id": "0", "grade": "B", "evidence_valid": True, "anchors_supported": True, "total_tokens": 50},
                     {"case_id": "1", "grade": None}]
        result = evaluate(labels, predicted)
        self.assertEqual(result["missing_predictions"], 1)
        self.assertEqual(result["scorable_coverage"], 1 / 3)
        self.assertEqual(result["overestimation_rate"], 1)
        self.assertEqual(result["missing_usage_cases"], 1)
        self.assertEqual(result["measured_total_tokens"], 50)

    def test_violations_are_separate_from_agreement(self):
        labels = [{"case_id": "risk", "grade": None, "mode": "risk_scan", "cap": None}]
        result = evaluate(labels, [{"case_id": "risk", "grade": "D"}])
        self.assertEqual(result["policy_violations"], 1)
        self.assertEqual(result["unsupported_scored_predictions"], 1)
        self.assertIsNone(result["exact_agreement"])
        self.assertIsNone(result["quadratic_weighted_kappa"])

    def test_known_weighted_kappa_and_invalid_case(self):
        labels = [{"case_id": str(i), "grade": g, "mode": "suggest", "cap": "C"} for i, g in enumerate(("A", "B", "C"))]
        preds = [{"case_id": str(i), "grade": g} for i, g in enumerate(("A", "B", "C"))]
        self.assertEqual(evaluate(labels, preds)["quadratic_weighted_kappa"], 1)
        with self.assertRaises(ValueError):
            evaluate(labels, preds + [preds[0]])
        with self.assertRaises(ValueError):
            evaluate(labels, [{"case_id": "unknown", "grade": None}])
