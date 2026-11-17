"""Offline, hash-pinned standards and persisted contract compatibility.

No imports, downloads, remote shapes, SHACL-JS or rules are enabled.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
from .errors import EngineError

RESOURCES = Path(__file__).with_name('resources')
VERSIONS = {'canonical_records': 'canonical-v3', 'normalizer': 'normalizer-v4',
            'exporter': 'exporter-v6', 'identity': 'identity-v2', 'evidence': 'evidence-v1',
            'engineering':'engineering-v1', 'engineering_policy':'engineering-screen-v1',
            'retrieval': 'retrieval-v1', 'context_proposals': 'context-proposals-v1', 'coverage':'inference-coverage-v1',
            'candidate_schema': 'candidate-ledgers-v1', 'claim_disposition': 'claim-disposition-v1',
            'csv_encoding': 'apostrophe-text-v1', 'brick': '1.4.4'}
DEPENDENCIES = {'rdflib': '7.1.4', 'pyshacl': '0.30.1', 'owlrl': '7.1.4',
                'pydantic': '2.10.6', 'openpyxl': '3.1.5', 'defusedxml': '0.7.1', 'Pint':'0.24.4', 'flexcache':'0.3', 'flexparser':'0.4', 'platformdirs':'4.4.0'}


def schema_hashes():
    from . import models
    result = {name: hashlib.sha256(json.dumps(getattr(models, name).model_json_schema(),
            sort_keys=True).encode()).hexdigest() for name in
            ('SourceRecord', 'Assertion', 'Classification', 'Issue', 'ReviewItem', 'Proposals', 'RunManifest')}
    from . import candidates
    result.update({name: hashlib.sha256(json.dumps(getattr(candidates, name).model_json_schema(), sort_keys=True).encode()).hexdigest()
                   for name in ('CandidateObservation', 'CandidateClaim', 'ClaimConflict', 'CandidateBundle')})
    from .retrieval import RetrievalPlan,ContextBatch,RetrievedRecord
    from .contextual import ContextCandidate,ContextAbstention,ContextResponse
    from .limits import Limits
    from .config import InputConfig
    from .engineering import RuleDefinition,RuleResult,EngineeringLedger
    for model in (RuleDefinition,RuleResult,EngineeringLedger,RetrievalPlan,ContextBatch,RetrievedRecord,ContextCandidate,ContextAbstention,ContextResponse,Limits,InputConfig):
        result[model.__name__]=hashlib.sha256(json.dumps(model.model_json_schema(),sort_keys=True).encode()).hexdigest()
    return result


def contract():
    pinned = json.loads((RESOURCES / 'standards-lock.json').read_text())
    if pinned['versions'] != VERSIONS or pinned['schemas'] != schema_hashes():
        raise EngineError('VERSION_MISMATCH', 'Persisted schema/version differs from the pinned contract; explicit migration required.')
    from .vocabulary import CLASS_ALIASES
    if pinned.get('class_aliases') != CLASS_ALIASES:
        raise EngineError('VERSION_MISMATCH', 'Class migration differs from the pinned semantic contract.')
    from .candidates import VERSION as CANDIDATE_VERSION, POLICY_VERSION
    if pinned['versions']['candidate_schema'] != CANDIDATE_VERSION or pinned['versions']['claim_disposition'] != POLICY_VERSION:
        raise EngineError('VERSION_MISMATCH', 'Candidate schema/disposition policy differs from pinned contract.')
    for filename, expected in pinned['resources'].items():
        if hashlib.sha256((RESOURCES / filename).read_bytes()).hexdigest() != expected:
            raise EngineError('VERSION_MISMATCH', 'Pinned ontology/shape resource hash differs; restore the released resource.')
    for name, expected in DEPENDENCIES.items():
        if importlib.metadata.version(name) != expected:
            raise EngineError('VERSION_MISMATCH', 'Validation dependency version differs: ' + name)
    return {**pinned, 'dependencies': DEPENDENCIES}


def require_compatible(saved):
    if saved != contract():
        raise EngineError('VERSION_MISMATCH', 'Saved semantic contract differs; no silent replay/migration permitted.')


def validate_external(graph):
    """Validate with released Brick ontology + embedded SHACL, without altering input.

    Passing cannot authenticate a source claim or establish physical truth.
    """
    from rdflib import Graph, RDF, OWL, RDFS
    from .vocabulary import BRICK
    from pyshacl import validate
    pinned = contract()
    ontology = Graph().parse(RESOURCES / 'Brick-1.4.4.ttl', format='turtle')
    conforms, report, text = validate(graph, shacl_graph=ontology, ont_graph=ontology,
        inference='none', advanced=False, do_owl_imports=False, iterate_rules=False,
        inplace=False, js=False)
    undeclared = sorted({str(o) for s, p, o in graph if p == RDF.type and str(o).startswith(str(BRICK))
        and (o, RDF.type, OWL.Class) not in ontology and (o, RDF.type, RDFS.Class) not in ontology})
    return {'released_vocabulary': 'failed' if undeclared else 'passed', 'undeclared_brick_classes': undeclared,
            'full_brick_shacl': 'passed' if conforms else 'failed',
            'validation_scope': 'released Brick 1.4.4 embedded shapes; ontology inoculation, no rules/imports',
            'evidence_truth': 'not_evaluable', 'physics': 'not_evaluable',
            'semantic_contract': pinned, 'report': text}
