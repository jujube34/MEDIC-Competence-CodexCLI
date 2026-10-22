from typing import Literal
from pydantic import Field, model_validator
from medic.domain.contracts import Contract, Criterion, Policy, Grade, EvidenceRef


class JobProfile(Contract):
    family: str = Field(min_length=1,max_length=200)
    track: str = Field(min_length=1,max_length=200)
    title: str = Field(min_length=1,max_length=200)
    level: str = Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def nonblank(self):
        if any(not value.strip() for value in self.model_dump().values()):
            raise ValueError('blank_job_profile')
        return self


class Catalog(Contract):
    title: str
    synthetic: bool
    status: Literal['synthetic', 'awaiting_business_signoff']
    competency_model_version: str
    job_matrix_version: str
    ai_policy_version: str
    criteria: dict[str, list[Criterion]]
    policies: dict[str, Policy]
    recommendations: dict[str, dict[str, Grade]]
    job_profiles: dict[str, JobProfile] = Field(default_factory=dict)
    competency_names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_scope(self):
        if not self.criteria or set(self.criteria) != set(self.policies):
            raise ValueError('catalog_scope_mismatch')
        for job in self.recommendations.values():
            if set(job) != set(self.criteria): raise ValueError('catalog_job_coverage')
        if self.synthetic != (self.status == 'synthetic'):
            raise ValueError('catalog_publication_state')
        if self.job_profiles and set(self.job_profiles) != set(self.recommendations):
            raise ValueError('catalog_job_profiles_mismatch')
        if self.competency_names and (set(self.competency_names) != set(self.criteria) or
                any(not value.strip() or len(value)>200 for value in self.competency_names.values())):
            raise ValueError('catalog_competency_names_mismatch')
        for code, policy in self.policies.items():
            if policy.mode != 'suggest' and policy.cap is not None:
                raise ValueError('non_scoring_policy_has_cap')
            if not self.criteria[code] or any(not c.criterion_id.startswith(code+'.') for c in self.criteria[code]):
                raise ValueError('invalid_catalog_criteria')
        return self


class CreateAssessment(Contract):
    employee_ref: str = Field(min_length=1, max_length=100)
    period: str = Field(min_length=1, max_length=64)
    registry_id: str = Field(pattern='^[0-9a-f]{64}$')
    material_ids: list[str] = Field(min_length=1, max_length=20)
    competency_scope: list[str] = Field(min_length=1, max_length=17)


class HumanDecision(Contract):
    competency_id: str
    disposition: Literal['confirmed', 'adjusted', 'assessed', 'insufficient_evidence', 'not_applicable']
    grade: Grade | None
    reason: str = Field(min_length=5, max_length=4000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)
    manual_basis: str = Field(default='', max_length=4000)
    risk_verification: Literal['not_present', 'confirmed', 'requires_investigation'] | None = None

    @model_validator(mode='after')
    def grade_state(self):
        if len(self.reason.strip()) < 5:
            raise ValueError('human_reason_required')
        if (self.disposition in ('insufficient_evidence','not_applicable')) != (self.grade is None):
            raise ValueError('human_grade_state_mismatch')
        if self.grade is not None and not self.evidence_refs and len(self.manual_basis.strip()) < 8:
            raise ValueError('human_grade_requires_evidence_or_manual_basis')
        return self


class ReviewRequest(Contract):
    expected_revision: int = Field(ge=1)
    decisions: list[HumanDecision] = Field(min_length=1, max_length=17)
    supplemental_material_ids: list[str] = Field(default_factory=list, max_length=20)
    acknowledged_quality_issues: list[str] = Field(default_factory=list, max_length=100)


class FinalizeRequest(Contract):
    expected_revision: int = Field(ge=1)
    limitation_disposition: str = Field(min_length=5, max_length=4000)

    @model_validator(mode='after')
    def meaningful_disposition(self):
        if len(self.limitation_disposition.strip()) < 5:
            raise ValueError('limitation_disposition_required')
        return self
