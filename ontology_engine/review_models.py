"""FR-04/07/11: strict, separately versioned human-review contracts.

A recorded decision is a pending proposal, never an applied graph consequence.
Local credential possession does not authenticate engineering verification authority.
"""
import hashlib
import json
from typing import Dict, List, Literal, Optional
from pydantic import Field, StringConstraints, model_validator, field_validator
from datetime import datetime, timezone
from typing import Annotated
from .models import Model, Status

def check_timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError('Timestamp must be timezone-aware UTC')


VERSION = 'review-v1'
POLICY = 'pending-review-v1'
ID = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=False)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
SHA = Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def hashed(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class Reviewer(Model):
    actor_id: ID
    role: Literal['reviewer', 'observer']
    authentication: Literal['local_bearer_sha256'] = 'local_bearer_sha256'
    authority_basis: Literal['local_enrollment_not_engineering_authority'] = 'local_enrollment_not_engineering_authority'
    can_verify: Literal[False] = False


class Workspace(Model):
    schema_version: Literal['review-v1'] = VERSION
    policy_version: Literal['pending-review-v1'] = POLICY
    workspace_id: ID
    site_scope: ID
    parent_run_id: ID
    parent_path: str = Field(min_length=1, max_length=4096)
    parent_fingerprint: SHA
    source_fingerprint: SHA
    graph_sha256: SHA
    manifest_sha256: SHA
    contract_fingerprint: SHA
    reviewer: Reviewer
    created_at: str
    initial_revision: Literal[0] = 0
    graph_changes: Literal['not_applied'] = 'not_applied'
    credential_sha256: SHA

    @field_validator('created_at')
    @classmethod
    def utc_timestamp(cls, value):
        check_timestamp(value)
        return value


class CorrectionRequest(Model):
    kind: Literal['class_choice', 'relationship', 'source_clarification']
    subject_id: Optional[ID] = None
    predicate: Optional[Literal['type', 'feeds', 'hasPoint', 'hasPart']] = None
    object_id: Optional[ID] = None
    clarification: Text

    @model_validator(mode='after')
    def shape(self):
        if not self.clarification.strip():
            raise ValueError('Correction clarification must be nonempty')
        if self.kind == 'source_clarification':
            if any(x is not None for x in (self.subject_id, self.predicate, self.object_id)):
                raise ValueError('Source clarification cannot supply a graph assertion')
        elif not all((self.subject_id, self.predicate, self.object_id)):
            raise ValueError('Correction requires explicit endpoints and predicate')
        elif (self.kind == 'class_choice') != (self.predicate == 'type'):
            raise ValueError('Correction kind and predicate disagree')
        return self


class ReviewAction(Model):
    schema_version: Literal['review-v1'] = VERSION
    workspace_id: ID
    site_scope: ID
    parent_fingerprint: SHA
    action_id: ID
    correlation_id: ID
    actor_id: ID
    expected_revision: int = Field(ge=0, le=10000)
    target_kind: Literal['claim', 'question']
    target_id: ID
    action: Literal['accept', 'reject', 'correct', 'defer']
    requested_status: Status
    reason: Text
    evidence_uids: List[ID] = Field(max_length=64)
    supersedes_event_id: Optional[ID] = None
    correction: Optional[CorrectionRequest] = None

    @model_validator(mode='after')
    def valid_action(self):
        if any(not x.strip() for x in (self.action_id, self.correlation_id, self.actor_id, self.reason)):
            raise ValueError('Empty or whitespace-only identity/reason')
        if len(set(self.evidence_uids)) != len(self.evidence_uids):
            raise ValueError('Duplicate evidence reference')
        if (self.action == 'correct') != (self.correction is not None):
            raise ValueError('Correction payload required only for correct action')
        allowed = {'accept': {'supported_inference', 'verified'}, 'reject': {'rejected'},
                   'correct': {'needs_review'}, 'defer': {'needs_review'}}
        if self.requested_status not in allowed[self.action]:
            raise ValueError('Action/status transition is not permitted')
        if self.action != 'defer' and not self.evidence_uids:
            raise ValueError('Evidence is required for accept/reject/correct proposals')
        return self


class EvidenceReference(Model):
    source_record_uid: ID
    source_id: ID
    source_file: ID
    source_hash: SHA
    source_location: str
    sheet_name: Optional[str] = None
    cell_locations: Dict[str, str]


class DecisionEvent(Model):
    schema_version: Literal['review-v1'] = VERSION
    policy_version: Literal['pending-review-v1'] = POLICY
    event_id: ID
    revision: int = Field(ge=1, le=10000)
    timestamp: str
    request: ReviewAction
    request_sha256: SHA
    reviewer: Reviewer
    outcome: Literal['pending'] = 'pending'
    status: Literal['needs_review'] = 'needs_review'
    reason_codes: List[Literal['ENGINEERING_AUTHORITY_NOT_ESTABLISHED', 'GRAPH_APPLICATION_DEFERRED']]
    graph_consequence: Literal['not_applied'] = 'not_applied'
    claim_uids: List[ID]
    observation_uids: List[ID]
    conflict_uids: List[ID]
    evidence: List[EvidenceReference]
    previous_event_sha256: SHA

    @field_validator('timestamp')
    @classmethod
    def utc_timestamp(cls, value):
        check_timestamp(value)
        return value


def contract_fingerprint():
    return hashed({'schema': VERSION, 'policy': POLICY,
        'models': {c.__name__: c.model_json_schema() for c in
                   (Reviewer, Workspace, CorrectionRequest, ReviewAction, EvidenceReference, DecisionEvent)}})
