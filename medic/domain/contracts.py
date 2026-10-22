from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["coordinator", "evaluator", "challenger_scan", "challenger", "reevaluator", "judge", "aggregator"]
Grade = Literal["A", "B", "C", "D"]
Mode = Literal["suggest", "clue_only", "human_only", "risk_scan"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Versions(Contract):
    competency_model_version: str
    job_matrix_version: str
    ai_policy_version: str
    prompt_bundle_version: str
    parser_version: str
    runtime_version: str
    codex_revision: str
    provider_profile_version: str


class EvidenceSpan(Contract):
    evidence_id: str
    material_id: str
    material_version: str
    locator: str
    quote: str = Field(min_length=1)
    span_hash: str
    quality_flags: list[str]


class EvidenceRef(Contract):
    evidence_id: str
    quote: str = Field(min_length=1)
    span_hash: str


class Criterion(Contract):
    criterion_id: str
    grade: Grade
    description: str


class Policy(Contract):
    mode: Mode
    cap: Grade | None
    signed_off: bool


class AssessmentGoal(Contract):
    goal_id: str
    version: str
    competency_scope: list[str] = Field(min_length=1)
    description: str


class Material(Contract):
    material_id: str
    version: str
    sha256: str
    source_sha256: str | None = None
    snapshot_ref: str | None = None
    title: str
    owner_claim: str
    source_event_id: str = Field(min_length=1)
    spans: list[EvidenceSpan] = Field(min_length=1)


class AssessmentInput(Contract):
    tenant_id: str
    employee_ref: str
    period: str
    job_key: str
    synthetic: bool
    versions: Versions
    goal: AssessmentGoal
    materials: list[Material] = Field(min_length=1)
    criteria: dict[str, list[Criterion]]
    policies: dict[str, Policy]

    @model_validator(mode="after")
    def unique_ids(self):
        material_ids = [m.material_id for m in self.materials]
        if "__aggregate__" in material_ids:
            raise ValueError("reserved_material_id")
        span_ids = [s.evidence_id for m in self.materials for s in m.spans]
        if len(set(material_ids)) != len(material_ids) or len(set(span_ids)) != len(span_ids):
            raise ValueError("duplicate_material_or_evidence_id")
        if set(self.goal.competency_scope) != set(self.criteria) or set(self.criteria) != set(self.policies):
            raise ValueError("scope_registry_mismatch")
        for code, criteria in self.criteria.items():
            ids = [c.criterion_id for c in criteria]
            if not criteria or len(set(ids)) != len(ids) or any(not ident.startswith(code + ".") for ident in ids):
                raise ValueError("invalid_criterion_ids")
        return self


class BoundResult(Contract):
    task_id: str
    context_hash: str


class CriterionCheck(Contract):
    criterion_id: str
    finding: Literal["met", "partial", "not_observed"]
    evidence_refs: list[EvidenceRef]
    reason: str = Field(min_length=1)


class Claim(Contract):
    claim_id: str
    criterion_id: str
    statement: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class ChallengeResponse(Contract):
    challenge_id: str
    disposition: Literal["accepted", "rejected", "unresolved"]
    reason: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef]


class Assessment(BoundResult):
    competency_id: str
    material_id: str
    revision: int = Field(ge=1, le=3)
    status: Literal["suggested", "insufficient_evidence", "not_applicable", "clue_only", "risk_signal"]
    grade: Grade | None
    criterion_checks: list[CriterionCheck] = Field(min_length=1)
    claims: list[Claim]
    attribution: Literal["self_reported", "corroborated", "unclear"]
    supporting_evidence: list[EvidenceRef]
    contradicting_evidence: list[EvidenceRef]
    missing_evidence: list[str]
    limitations: list[str] = Field(min_length=1)
    challenge_responses: list[ChallengeResponse]

    @model_validator(mode="after")
    def no_implicit_grade(self):
        if (self.status == "suggested") != (self.grade is not None):
            raise ValueError("grade_status_mismatch")
        return self


class Observation(Contract):
    finding: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class AggregationAssessment(Assessment):
    source_result_hashes: dict[str, str]
    source_groups: dict[str, list[str]]
    synthesis_reason: str = Field(min_length=1)
    corroboration_event_ids: list[str]


class PriorDisputeResponse(ChallengeResponse):
    challenge_result_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class ThirdAssessment(Assessment):
    prior_dispute_responses: list[PriorDisputeResponse]


class ThirdAggregationAssessment(AggregationAssessment):
    prior_dispute_responses: list[PriorDisputeResponse]


class IndependentFindings(BoundResult):
    observations: list[Observation] = Field(min_length=1)
    open_questions: list[str]


class Challenge(Contract):
    challenge_id: str
    claim_id: str
    kind: Literal["attribution", "criterion_mapping", "omission", "overstatement", "overconservative"]
    severity: Literal["blocking", "advisory"]
    evidence_refs: list[EvidenceRef]
    falsification_question: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ChallengeReport(BoundResult):
    independent_findings_hash: str
    challenges: list[Challenge]
    checked_claim_ids: list[str]
    conclusion: str = Field(min_length=1)


CHECKS = ("coverage", "evidence", "policy", "adversarial_chain", "disputes", "boundaries", "versions_execution")


class GoalCheck(Contract):
    check: Literal["coverage", "evidence", "policy", "adversarial_chain", "disputes", "boundaries", "versions_execution"]
    passed: bool
    reason: str = Field(min_length=1)


class GoalReview(BoundResult):
    goal_version: str
    candidate_hash: str
    checks: list[GoalCheck] = Field(min_length=7, max_length=7)
    blocking_issues: list[str]
    verdict: Literal["pass", "rework", "human_review"]
    reason: str = Field(min_length=1)
    next_tasks: list[str]


class DispatchRequest(Contract):
    role: Literal["evaluator", "challenger_scan", "challenger", "reevaluator", "judge", "aggregator"]
    competency_id: str
    material_id: str
    dependencies: list[str]
    reason: str = Field(min_length=1)


class DisputeMessage(Contract):
    message_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    recipient_task_id: str
    challenge_id: str = Field(min_length=1, max_length=200)
    challenge_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["question", "answer"]
    reply_to: str | None
    text: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[EvidenceRef] = Field(max_length=4)


class ReworkDecision(Contract):
    judge_task_id: str
    decision: Literal['rework', 'human_review']
    reason: str = Field(min_length=1, max_length=2000)


RESULT_TYPES = {"evaluator": Assessment, "reevaluator": Assessment, "challenger_scan": IndependentFindings,
                "challenger": ChallengeReport, "judge": GoalReview, "aggregator": AggregationAssessment}


def result_type(role, material_id, business_round=1):
    if role == 'reevaluator' and business_round == 3:
        return ThirdAggregationAssessment if material_id == '__aggregate__' else ThirdAssessment
    if material_id == "__aggregate__" and role in ("aggregator", "reevaluator"):
        return AggregationAssessment
    return RESULT_TYPES[role]
