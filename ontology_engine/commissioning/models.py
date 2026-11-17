"""FR-08/D5: independent versioned report schemas; prior schemas are unchanged."""
from typing import Any, Dict, List, Literal, Optional
from pydantic import Field, model_validator
from ..models import Model, SourceRecord

VERSION = 'commissioning-report-v1'
POLICY = 'commissioning-priority-v1'
RULE_VERSION = '1.0.0'


class ArtifactEvidence(Model):
    path: str
    sha256: str


class Priority(Model):
    policy: Literal['commissioning-priority-v1'] = POLICY
    significance: Literal['engineering_review', 'data_review', 'prerequisite_review']
    significance_rank: int = Field(ge=1, le=3)
    evidence_band: int = Field(ge=1, le=2)
    operational_context_band: int = Field(ge=0, le=1)
    effort_rank: int = Field(ge=1, le=2)
    score: int
    rationale: str
    operational_criticality: Optional[str] = None
    failure_likelihood: Optional[float] = None
    potential_impact: Optional[str] = None
    human_minutes: Optional[float] = None
    urgency: Literal['unknown'] = 'unknown'

    @model_validator(mode='after')
    def consistent(self):
        expected = 100*self.significance_rank + 10*self.evidence_band + 5*self.operational_context_band - self.effort_rank
        if self.score != expected: raise ValueError('COMMISSIONING_PRIORITY_MISMATCH')
        if self.significance != {1:'prerequisite_review',2:'data_review',3:'engineering_review'}[self.significance_rank]:
            raise ValueError('COMMISSIONING_SIGNIFICANCE_MISMATCH')
        if any(v is not None for v in (self.operational_criticality, self.failure_likelihood, self.potential_impact, self.human_minutes)):
            raise ValueError('COMMISSIONING_UNKNOWN_FACTOR_REQUIRED')
        return self


class Finding(Model):
    finding_uid: str
    site_scope: str
    target_id: str
    category: Literal['data_quality', 'model_conflict', 'potential_operational_issue']
    rule_id: str
    rule_version: str
    detection_outcome: Literal['fail', 'blocked', 'error', 'not_evaluable']
    reason_codes: List[str]
    status: Literal['open', 'not_evaluable']
    confirmed_fault: Literal[False] = False
    observed_evidence: Dict[str, Any]
    expected_condition: List[str]
    source_ids: List[str]
    source_uids: List[str]
    evidence: List[SourceRecord]
    artifact_evidence: List[ArtifactEvidence]
    claim_uids: List[str]
    observation_uids: List[str]
    conflict_uids: List[str]
    review_question_ids: List[str]
    original_issue_ids: List[str]
    priority: Priority
    uncertainty: str
    evidence_limitations: List[str]
    alternative_explanations: List[str]
    recommended_investigation: List[str]
    operational_context_established: bool = False

    @model_validator(mode='after')
    def no_false_authority(self):
        if self.category == 'potential_operational_issue' and not (
            self.rule_id == 'engineering.thermal.water_balance' and self.detection_outcome == 'fail' and self.operational_context_established):
            raise ValueError('COMMISSIONING_OPERATIONAL_PREREQUISITES_REQUIRED')
        if (self.status == 'not_evaluable') != (self.detection_outcome == 'not_evaluable'):
            raise ValueError('COMMISSIONING_STATUS_MISMATCH')
        return self


class Report(Model):
    schema_version: Literal['commissioning-report-v1'] = VERSION
    priority_policy: Literal['commissioning-priority-v1'] = POLICY
    report_id: str
    site_scope: str
    parent_run_id: str
    parent_fingerprint: str
    synthetic: bool
    summary: Dict[str, Any]
    findings: List[Finding]
    checks: List[Dict[str, Any]]
    limitations: List[str]


class ReportManifest(Model):
    schema_version: Literal['commissioning-report-v1'] = VERSION
    priority_policy: Literal['commissioning-priority-v1'] = POLICY
    report_id: str
    site_scope: str
    parent_run_id: str
    parent_fingerprint: str
    source_fingerprint: str
    graph_sha256: str
    implementation_fingerprint: str
    output_hashes: Dict[str, str]
