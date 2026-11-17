"""Opt-in Phase B contracts. review-v1 schemas/fingerprints remain unchanged."""
from typing import List, Literal, Optional
from pydantic import Field, model_validator
from .models import Model
from .review_models import (ID, Text, Workspace, ReviewAction, DecisionEvent,
                            contract_fingerprint as v1_fingerprint, hashed)

VERSION = 'review-v2'
POLICY = 'documentary-corrections-v1'


class CorrectionEdit(Model):
    subject_id: ID
    predicate: Literal['type', 'hasPoint', 'feeds', 'hasPart']
    object_id: ID


class Correction(Model):
    kind: Literal['class_choice', 'naming_alias', 'ownership', 'physical_relationship',
                  'reject_connection', 'source_clarification']
    clarification: Text
    edits: List[CorrectionEdit] = Field(default_factory=list, max_length=64)
    alias: Optional[ID] = None
    field: Optional[Literal['point_class', 'equipment_type', 'equipment_id']] = None

    @model_validator(mode='after')
    def shape(self):
        if not self.clarification.strip(): raise ValueError('Nonempty clarification required')
        if len({(e.subject_id,e.predicate,e.object_id) for e in self.edits}) != len(self.edits):
            raise ValueError('Duplicate edit')
        if self.kind == 'source_clarification':
            if self.edits or self.alias or not self.field: raise ValueError('Explicit clarification field required')
        else:
            if not self.edits or self.field: raise ValueError('Explicit edits required')
            if (self.kind == 'naming_alias') != bool(self.alias): raise ValueError('Alias required only for naming_alias')
            allowed = {'class_choice':{'type'}, 'naming_alias':{'type'}, 'ownership':{'hasPoint'},
                       'physical_relationship':{'feeds','hasPart'}, 'reject_connection':{'feeds','hasPart','hasPoint'}}
            if any(e.predicate not in allowed[self.kind] for e in self.edits):
                raise ValueError('Correction kind/predicate mismatch')
        return self


class Action(ReviewAction):
    schema_version: Literal['review-v2'] = VERSION
    target_kind: Literal['claim', 'question', 'group']
    correction: Optional[Correction] = None
    affected_claim_uids: List[ID] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def explicit_scope(self):
        if len(set(self.affected_claim_uids)) != len(self.affected_claim_uids):
            raise ValueError('Duplicate affected claim')
        if self.action != 'correct': raise ValueError('review-v2 uses explicit typed corrections')
        return self


class ReviewWorkspace(Workspace):
    schema_version: Literal['review-v2'] = VERSION
    policy_version: Literal['documentary-corrections-v1'] = POLICY


class Event(DecisionEvent):
    schema_version: Literal['review-v2'] = VERSION
    policy_version: Literal['documentary-corrections-v1'] = POLICY
    request: Action


class ExportBasis(Model):
    version: Literal['reviewed-package-v1'] = 'reviewed-package-v1'
    workspace: ReviewWorkspace
    revision: int = Field(ge=0, le=1000)
    events: List[Event] = Field(max_length=1000)
    publication_destination: str = Field(min_length=1, max_length=4096)


def contract_fingerprint():
    return hashed({'v1':v1_fingerprint(), 'schema':VERSION, 'policy':POLICY,
        'models':{c.__name__:c.model_json_schema() for c in (CorrectionEdit,Correction,Action,ReviewWorkspace,Event,ExportBasis)}})
