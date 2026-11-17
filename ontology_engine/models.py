"""FR-01/02/04/11: canonical, JSON-serializable contracts."""
from typing import Dict, List, Literal, Optional, Any
from pydantic import BaseModel, ConfigDict, Field, model_validator

Status = Literal['verified', 'supported_inference', 'needs_review', 'contradicted', 'rejected']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class SourceRecord(Model):
    source_id: str
    source_record_uid: str = ''
    legacy_source_id: str = ''
    source_file: str
    source_type: str
    source_location: str
    source_hash: str
    table_kind: Literal['points', 'equipment', 'connections', 'measurements', 'thermal_checks', 'unknown'] = 'unknown'
    sheet_name: Optional[str] = None
    cell_locations: Dict[str, str] = Field(default_factory=dict)
    raw_value: List[Any]
    raw_record: Dict[str, Any]
    normalized_fields: Dict[str, str]
    parse_status: Literal['ok', 'error']
    errors: List[str] = Field(default_factory=list)

class Classification(Model):
    source_point_id: str
    equipment_id: str
    point_class: str
    status: Status
    supporting_source_ids: List[str]
    explanation: str
    units: str
    inference_method: str
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None

class Assertion(Model):
    assertion_id: str
    claim_uid: str = ''
    observation_uid: str = ''
    subject_entity_uid: str = ''
    object_entity_uid: str = ''
    supporting_source_uids: List[str] = Field(default_factory=list)
    original_source_ids: List[str] = Field(default_factory=list)
    eligibility: Dict[str, Any] = Field(default_factory=dict)
    subject_id: str
    predicate: str
    object_id: str
    predicate_or_class: str = ''
    object_or_value: str = ''
    supporting_source_ids: List[str]
    inference_method: str
    explanation: str
    status: Status
    validation_results: List[str]
    run_id: str
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    physical_medium: Optional[str] = None

    @model_validator(mode='after')
    def canonical_fields(self):
        # Canonical product-contract names coexist with the required CSV names.
        self.predicate_or_class = self.predicate
        self.object_or_value = self.object_id
        return self

class Issue(Model):
    issue_id: str
    category: Literal['data_quality', 'model_conflict', 'potential_operational_issue']
    point_or_asset_id: str
    observed_evidence: str
    rule_or_detection_method: str
    severity_or_priority: str = 'review'
    uncertainty: str = 'Requires source verification'
    alternative_explanations: str = 'Missing or incorrect export metadata'
    recommended_action: str
    source_ids: List[str]
    resolution_status: str = 'open'

class ReviewItem(Model):
    review_id: str
    affected_assertion_ids: List[str]
    question_or_test: str
    supporting_evidence_ids: List[str]
    reason_for_uncertainty: str
    estimated_impact: str = 'unknown'
    priority: str = 'review'
    review_status: str = 'open'
    reviewer_decision: Optional[str] = None
    decision_timestamp: Optional[str] = None

class PointProposal(Model):
    source_id: str
    point_class: str
    equipment_id: str
    explanation: str

class RelationProposal(Model):
    subject_id: str
    predicate: Literal['feeds', 'hasPoint', 'hasPart']
    object_id: str
    supporting_source_ids: List[str]
    explanation: str

class EquipmentProposal(Model):
    source_id: str
    equipment_class: str
    explanation: str

class Proposals(Model):
    equipment: List[EquipmentProposal] = Field(default_factory=list)
    points: List[PointProposal]
    relationships: List[RelationProposal]

class RunManifest(Model):
    run_id: str
    started_at: str
    finished_at: str
    code_version: str
    code_revision: str
    ontology_version: str
    input_hashes: Dict[str, str]
    settings: Dict[str, Any]
    model_version: Optional[str]
    prompt_version: str
    counts: Dict[str, int]
    validation: Dict[str, Any]
    user_approvals: List[str]
    limitations: List[str]
    synthetic: bool
    provider_audit: Dict[str, Any] = Field(default_factory=dict)
    schema_version: str = 'run-manifest-v6'
    completion_status: str = 'RUNNING'
    required_artifacts: List[str] = Field(default_factory=list)
    output_hashes: Dict[str, str] = Field(default_factory=dict)
    data_origin: str = 'unknown'
    execution_origin: str = 'unknown'
    privacy_policy: Dict[str, Any] = Field(default_factory=dict)
