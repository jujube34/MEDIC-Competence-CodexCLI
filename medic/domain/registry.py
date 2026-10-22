"""Content-addressed candidate registry; importing never publishes business policy."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from medic.p0.assets import model, matrix, matrix_profiles, dimensions
from medic.p0.common import CODES, ROOT, digest


def validate_bundle(bundle):
    if bundle["status"] != "awaiting_business_signoff" or bundle["official_assessment_allowed"] is not False:
        raise ValueError("candidate_import_cannot_publish")
    if set(bundle["competencies"]) != set(CODES) or set(bundle["dimensions"]) != set("MEDIC"):
        raise ValueError("registry_model_coverage")
    policy = bundle["policy"]
    codes = [c for group in policy["groups"].values() for c in group]
    if len(codes) != 17 or set(codes) != set(CODES):
        raise ValueError("registry_policy_partition")
    if policy["official_assessment_allowed"] or policy["automatic_D_allowed"]:
        raise ValueError("candidate_policy_not_restricted")
    pairs = [(r["job_key"], r["competency_id"]) for r in bundle["recommendations"]]
    if len(pairs) != len(set(pairs)):
        raise ValueError("registry_duplicate_recommendation")
    jobs = {j for j, _ in pairs}
    if len(jobs) != 49 or len(pairs) != 833:
        raise ValueError("registry_requires_260914_49_jobs_833_pairs")
    # Older content-addressed snapshots remain readable without enrichment.
    if 'job_profiles' in bundle:
        profiles = bundle['job_profiles']
        if set(profiles) != jobs or any(set(p) != {'family','track','title','level'} or
                any(not isinstance(v,str) or not v.strip() for v in p.values()) for p in profiles.values()):
            raise ValueError('registry_job_profiles_mismatch')
    for job in jobs:
        if {c for j, c in pairs if j == job} != set(CODES):
            raise ValueError("registry_job_coverage")
    for row in bundle["recommendations"]:
        if row["grade"] not in tuple("ABCD") or row["score"] != "ABCD".index(row["grade"]) + 1:
            raise ValueError("registry_invalid_recommendation")


def import_candidate(source_root, registry_root):
    source_root, registry_root = Path(source_root), Path(registry_root).resolve()
    # Customer source text must not accidentally become a tracked repository artifact.
    if ROOT in registry_root.parents and not any(p in registry_root.parents or registry_root == p for p in (ROOT / "medic/private", ROOT / "medic/var", ROOT / "medic/artifacts")):
        raise ValueError("customer_registry_requires_private_destination")
    baseline = json.loads((ROOT / "medic/p0/results/assets.manifest.json").read_text(encoding="utf-8"))
    paths = {"delivery_model": source_root / "交付0908/核心胜任力模型260906_MEDIC.xlsx",
             "matrix_260914": source_root / "岗位能力推荐值矩阵/MEDIC各岗位职级能力推荐值矩阵_260914_岗位职级更新版.xlsx"}
    hashes = {k: digest(p.read_bytes()) for k, p in paths.items()}
    if any(h != baseline["source_hashes"][k] for k, h in hashes.items()):
        raise ValueError("source_changed_since_p0_reaudit_required")
    competencies, model_issues = model(paths["delivery_model"])
    recommendations, matrix_issues = matrix(paths["matrix_260914"])
    profiles, profile_issues = matrix_profiles(paths['matrix_260914'])
    if model_issues or matrix_issues or profile_issues:
        raise ValueError("registry_source_validation_failed")
    policy = json.loads((ROOT / "medic/p0/policy.candidate.json").read_text(encoding="utf-8"))
    bundle = {"status": "awaiting_business_signoff", "official_assessment_allowed": False,
              "source_hashes": hashes, "competency_model_version": "sha256:" + hashes["delivery_model"],
              "job_matrix_version": "sha256:" + hashes["matrix_260914"],
              "ai_policy_version": policy["ai_policy_version"], "policy_hash": digest(policy),
              "dimensions": dimensions(paths["delivery_model"]), "competencies": competencies, "policy": policy,
              "job_profiles": profiles,
              "recommendations": [{"job_key": j, "competency_id": c, **v} for (j, c), v in sorted(recommendations.items())]}
    validate_bundle(bundle)
    if any(digest(p.read_bytes()) != hashes[k] for k, p in paths.items()):
        raise ValueError("registry_source_changed_during_import")
    ident = digest(bundle)
    registry_root.mkdir(parents=True, exist_ok=True)
    target = registry_root / (ident + ".json")
    if target.exists():
        load_candidate(registry_root, ident)
    else:
        # Publish only a fully flushed snapshot, without overwriting an existing id.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=registry_root, suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(bundle, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                load_candidate(registry_root, ident)
        finally:
            temporary.unlink()
    return {"bundle_id": ident, "status": bundle["status"], "competencies": len(competencies),
            "jobs": 49, "recommendations": len(recommendations), "policy_hash": bundle["policy_hash"]}


def load_candidate(root, ident):
    if not re.fullmatch(r"[0-9a-f]{64}", ident):
        raise ValueError("invalid_registry_id")
    bundle = json.loads((Path(root) / (ident + ".json")).read_text(encoding="utf-8"))
    if digest(bundle) != ident:
        raise ValueError("registry_snapshot_changed")
    validate_bundle(bundle)
    return bundle


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--registry-root", default=str(ROOT / "medic/private/registry"))
    args = parser.parse_args()
    print(json.dumps(import_candidate(args.source_root, args.registry_root)))
