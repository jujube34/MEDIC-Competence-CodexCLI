"""Offline comparison metrics. This module does not generate expert labels."""
from __future__ import annotations

from collections import Counter
import math
from statistics import mean

GRADES = "ABCD"


def evaluate(labels, predictions):
    """One frozen expert row and one candidate row per employee/cycle/dimension.

    Null expert grades are excluded from agreement, but remain in policy/error
    checks. Missing predictions reduce coverage, never become grade zero.
    Costs are summed only when measured; missing usage stays visibly unknown.
    """
    def indexed(rows):
        values = {}
        for row in rows:
            key = row["case_id"]
            if key in values:
                raise ValueError("duplicate_case_id")
            if row.get("grade") not in (*GRADES, None):
                raise ValueError("invalid_grade")
            values[key] = row
        return values

    truth, observed = indexed(labels), indexed(predictions)
    if not truth or set(observed) - set(truth):
        raise ValueError("empty_or_mismatched_dataset")
    pairs, policy_errors, unsupported, scored = [], 0, 0, 0
    inappropriate, false_abstentions, correct_abstentions = 0, 0, 0
    for key, label in truth.items():
        if label.get('mode') not in ('suggest','clue_only','human_only','risk_scan') or label.get('cap') not in (*GRADES,None):
            raise ValueError('invalid_label_policy')
        pred = observed.get(key)
        if not pred:
            continue
        grade = pred.get("grade")
        if label['mode']=='suggest':
            if label.get('grade') is None:
                inappropriate += grade is not None
                correct_abstentions += grade is None
            else:
                false_abstentions += grade is None
        if grade is not None:
            scored += 1
            if label["mode"] != "suggest" or grade == "D" or (label.get("cap") and grade > label["cap"]):
                policy_errors += 1
            if pred.get("evidence_valid") is not True or pred.get("anchors_supported") is not True:
                unsupported += 1
        if label["mode"] == "suggest" and label.get("grade") is not None and grade is not None:
            pairs.append((GRADES.index(label["grade"]), GRADES.index(grade)))
    scorable = sum(l["mode"] == "suggest" and l.get("grade") is not None for l in truth.values())
    # Quadratic weighted Cohen's kappa; degenerate marginals are undefined.
    kappa = None
    if pairs:
        n = len(pairs)
        expected_a, expected_b = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
        observed_loss = sum((a - b) ** 2 for a, b in pairs) / n
        expected_loss = sum(ca * cb * (a - b) ** 2 for a, ca in expected_a.items() for b, cb in expected_b.items()) / n**2
        if expected_loss:
            kappa = 1 - observed_loss / expected_loss
    measured_tokens = [p.get("total_tokens") for p in observed.values()]
    if any(t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0) for t in measured_tokens):
        raise ValueError("invalid_measured_usage")
    return {"cases": len(truth), "predictions": len(observed), "missing_predictions": len(truth) - len(observed),
            "scorable_expert_cases": scorable, "paired_grades": len(pairs), "scored_predictions": scored,
            "scorable_coverage": len(pairs) / scorable if scorable else None,
            "exact_agreement": mean(a == b for a, b in pairs) if pairs else None,
            "overestimation_rate": mean(b > a for a, b in pairs) if pairs else None,
            "underestimation_rate": mean(b < a for a, b in pairs) if pairs else None,
            "mean_signed_grade_error": mean(b-a for a,b in pairs) if pairs else None,
            "mean_absolute_grade_error": mean(abs(b-a) for a,b in pairs) if pairs else None,
            "abstained_scorable_predictions": false_abstentions,
            "false_abstention_rate": false_abstentions/scorable if scorable else None,
            "scored_unscorable_expert_cases": inappropriate,
            "correctly_abstained_unscorable_cases": correct_abstentions,
            "quadratic_weighted_kappa": kappa, "policy_violations": policy_errors,
            "unsupported_scored_predictions": unsupported,
            "measured_total_tokens": sum(t for t in measured_tokens if t is not None),
            "missing_usage_cases": sum(t is None for t in measured_tokens),
            "quality_acceptance": "not_assessed_requires_frozen_expert_set_and_signed_thresholds"}
