"""Versioned FR-10 sidecar; original ontology/engineering schemas stay pinned."""
from typing import Any, Dict, List, Literal, Optional
from ..models import Model, SourceRecord

VERSION = 'readiness-report-v1'
POLICY = 'accepted-graph-water-readiness-v1'
State = Literal['ready', 'blocked', 'not_evaluable', 'unknown', 'missing', 'contradicted', 'inapplicable']


class Query(Model):
    query_id: str
    kind: Literal['serves_zone', 'asset_points', 'equipment_meters']
    target_id: str
    state: State
    answers: List[Dict[str, Any]]
    withheld_claim_uids: List[str]
    limitation: str


class Assessment(Model):
    target_id: str
    check_source_id: Optional[str] = None
    question: Literal['sensible_water_thermal_performance'] = 'sensible_water_thermal_performance'
    state: State
    reason_codes: List[str]
    prerequisites: List[Dict[str, Any]]
    engineering_result: Optional[Dict[str, Any]] = None
    calculation: Optional[Dict[str, Any]] = None
    next_action: str


class Report(Model):
    schema_version: Literal['readiness-report-v1'] = VERSION
    policy_version: Literal['accepted-graph-water-readiness-v1'] = POLICY
    report_id: str
    site_scope: str
    parent_run_id: str
    parent_fingerprint: str
    synthetic: bool
    queries: List[Query]
    assessments: List[Assessment]
    entities: List[Dict[str, Any]]
    claims: List[Dict[str, Any]]
    observations: List[Dict[str, Any]]
    conflicts: List[Dict[str, Any]]
    evidence: List[SourceRecord]
    artifact_hashes: Dict[str, str]
    input_hashes: Dict[str, str]
    engineering_registry: List[Dict[str, Any]]
    engineering_checks: List[Dict[str, Any]]
    limitations: List[str]


class ReportManifest(Model):
    schema_version: Literal['readiness-report-v1'] = VERSION
    policy_version: Literal['accepted-graph-water-readiness-v1'] = POLICY
    report_id: str
    site_scope: str
    parent_run_id: str
    parent_fingerprint: str
    source_fingerprint: str
    graph_sha256: str
    implementation_fingerprint: str
    output_hashes: Dict[str, str]
