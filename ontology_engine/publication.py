"""Complete package integrity and atomic directory publication (no overwrite)."""
import csv
import hashlib
import json
import os
from pathlib import Path
from rdflib import Graph
from .models import Assertion
from .csv_safety import decode_row

MANIFEST_VERSION = 'run-manifest-v6'
REQUIRED = ['ontology.ttl', 'classifications.csv', 'relationships.csv', 'issues.csv', 'review_questions.csv',
            'source_records.jsonl', 'assertions.json', 'classifications.json', 'issues.json', 'review_questions.json',
            'candidate_assertions.json', 'candidate_claims.json', 'candidate_observations.json', 'claim_conflicts.json',
            'assertion_observations.json', 'proposal_observations.json', 'retrieval_plan.json', 'context_batches.json',
            'context_candidates.json', 'inference_coverage.json', 'engineering_results.json', 'engineering_registry.json',
            'engineering_results.csv', 'validation.json', 'README.md', 'input_config.json', 'data_inventory.json']


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_contents(directory):
    from .pipeline import build_graph, relationship_csv_row
    directory = Path(directory)
    for name in REQUIRED:
        if not (directory / name).is_file(): raise ValueError('ARTIFACT_MISSING: ' + name)
    manifest = json.loads((directory / 'run_manifest.json').read_text())
    from .standards import require_compatible
    require_compatible(manifest.get('settings', {}).get('semantic_contract'))
    records = [json.loads(line) for line in (directory / 'source_records.jsonl').read_text().splitlines()]
    source_ids = {r['source_id'] for r in records}
    uids = {r['source_record_uid'] for r in records}
    if len(source_ids) != len(records) or len(uids) != len(records): raise ValueError('SOURCE_ID_COLLISION')
    assertions = [Assertion.model_validate(a) for a in json.loads((directory / 'assertions.json').read_text())]
    if len({a.observation_uid for a in assertions}) != len(assertions): raise ValueError('OBSERVATION_ID_COLLISION')
    for a in assertions:
        if not set(a.supporting_source_ids) <= source_ids or not set(a.supporting_source_uids) <= uids:
            raise ValueError('UNRESOLVED_CITATION')
    for issue in json.loads((directory / 'issues.json').read_text()):
        if not set(issue['source_ids']) <= source_ids: raise ValueError('UNRESOLVED_ISSUE_CITATION')
    actual = Graph().parse(directory / 'ontology.ttl', format='turtle')
    expected = build_graph(assertions, manifest['settings']['namespace'])
    if set(actual) != set(expected): raise ValueError('GRAPH_LEDGER_MISMATCH')
    # Recompute the typed candidate/disposition contract from preserved original observations.
    from .candidates import evaluate_proposals, project, VERSION, POLICY_VERSION
    from .models import SourceRecord
    from .limits import Limits
    base = [Assertion.model_validate(a) for a in json.loads((directory / 'assertion_observations.json').read_text())]
    computation_records = records
    proposal_audit = json.loads((directory / 'proposal_observations.json').read_text())
    candidate_run_id = manifest['run_id']
    if manifest['settings'].get('review_derivation'):
        from .review_export import verify_derivation
        derived = verify_derivation(directory, manifest)['replay_inputs.json']
        base = [Assertion.model_validate(a) for a in derived['assertions']]
        computation_records = derived['records']
        proposal_audit = derived['proposals']
        candidate_run_id = manifest['settings']['review_derivation']['parent_run_id']
    bundle = evaluate_proposals(base, [SourceRecord.model_validate(r) for r in computation_records],
        manifest['settings']['building_scope'], candidate_run_id,
        proposal_audit,
        Limits.model_validate(manifest['settings']['limits']))
    for filename, rows in [('candidate_claims.json', bundle.claims), ('candidate_observations.json', bundle.observations),
                           ('claim_conflicts.json', bundle.conflicts)]:
        expected = {'schema_version': VERSION, 'policy_version': POLICY_VERSION, 'records': [r.model_dump() for r in rows]}
        if json.loads((directory / filename).read_text()) != expected:
            raise ValueError('CANDIDATE_LEDGER_MISMATCH: ' + filename)
    if [a.model_dump() for a in project(base, bundle)] != [a.model_dump() for a in assertions]:
        raise ValueError('DISPOSITION_LEDGER_MISMATCH')
    if {c.claim_uid for c in bundle.claims if c.status in {'verified','supported_inference'}} != {
            a.claim_uid for a in assertions if a.status in {'verified','supported_inference'}}:
        raise ValueError('ACCEPTED_CLAIM_LEDGER_MISMATCH')
    from .contextual import verify_context_artifacts
    from .coverage import coverage
    plan,context_results=verify_context_artifacts(directory,[SourceRecord.model_validate(r) for r in records],manifest,
        Limits.model_validate(manifest['settings']['limits']))
    expected_coverage=coverage([SourceRecord.model_validate(r) for r in computation_records],assertions,bundle,plan,context_results)
    if json.loads((directory/'inference_coverage.json').read_text())!=expected_coverage:
        raise ValueError('COVERAGE_LEDGER_MISMATCH')
    from .engineering import evaluate as engineering_evaluate
    engineering = engineering_evaluate([SourceRecord.model_validate(r) for r in computation_records],assertions,bundle,
        manifest['settings']['building_scope'],Limits.model_validate(manifest['settings']['limits']))
    if json.loads((directory/'engineering_results.json').read_text()) != engineering.model_dump():
        raise ValueError('ENGINEERING_LEDGER_MISMATCH')
    if json.loads((directory/'engineering_registry.json').read_text()) != [r.model_dump() for r in engineering.registry]:
        raise ValueError('ENGINEERING_REGISTRY_MISMATCH')
    validation=json.loads((directory/'validation.json').read_text())
    if validation.get('engineering')!=engineering.summary or validation.get('physics')!=engineering.summary['physics'] or manifest['validation']!=validation:
        raise ValueError('ENGINEERING_SUMMARY_MISMATCH')
    for csv_file, json_file, serializer in [('relationships.csv', 'assertions.json', relationship_csv_row),
            ('classifications.csv', 'classifications.json', None), ('issues.csv', 'issues.json', None),
            ('review_questions.csv', 'review_questions.json', None)]:
        data = json.loads((directory / json_file).read_text())
        canonical = [serializer(Assertion.model_validate(row)) if serializer else row for row in data]
        with (directory / csv_file).open(newline='', encoding='utf-8') as stream:
            human = [decode_row(row) for row in csv.DictReader(stream)]
        rendered = [{k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else '' if v is None else str(v)
                     for k, v in row.items()} for row in canonical]
        if human != rendered: raise ValueError('CSV_CANONICAL_MISMATCH: ' + csv_file)
    with (directory/'engineering_results.csv').open(newline='',encoding='utf-8') as stream:
        human=[decode_row(row) for row in csv.DictReader(stream)]
    rendered=[{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else str(v)
        for k,v in r.model_dump().items()} for r in engineering.records]
    if human!=rendered:raise ValueError('CSV_CANONICAL_MISMATCH: engineering_results.csv')
    for name, sha in manifest['input_hashes'].items():
        if digest(directory / 'sources' / name) != sha: raise ValueError('SOURCE_HASH_MISMATCH')
    return manifest


def finalize(directory):
    directory = Path(directory)
    manifest = validate_contents(directory)
    manifest.update(schema_version=MANIFEST_VERSION, completion_status='COMPLETED',
        required_artifacts=REQUIRED + (__import__('ontology_engine.review_export',fromlist=['EXTRA']).EXTRA
            if manifest['settings'].get('review_derivation') else []),
        output_hashes={str(p.relative_to(directory)): digest(p)
        for p in sorted(directory.rglob('*')) if p.is_file() and str(p.relative_to(directory)) not in {'run_manifest.json', 'COMPLETED.json'}})
    (directory / 'run_manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    validate_contents(directory)
    (directory / 'COMPLETED.json').write_text(json.dumps({'status': 'COMPLETED', 'manifest_sha256': digest(directory / 'run_manifest.json')}) + '\n')
    verify_package(directory)
    return manifest


def verify_package(directory):
    directory = Path(directory)
    manifest = validate_contents(directory)
    if manifest.get('schema_version') != MANIFEST_VERSION or manifest.get('completion_status') != 'COMPLETED':
        raise ValueError('PACKAGE_NOT_COMPLETE')
    marker = json.loads((directory / 'COMPLETED.json').read_text())
    if marker.get('manifest_sha256') != digest(directory / 'run_manifest.json'): raise ValueError('MANIFEST_HASH_MISMATCH')
    expected_files = set(manifest['output_hashes']) | {'run_manifest.json', 'COMPLETED.json'}
    actual_files = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file()}
    if expected_files != actual_files: raise ValueError('ARTIFACT_SET_MISMATCH')
    for name, sha in manifest['output_hashes'].items():
        if digest(directory / name) != sha: raise ValueError('OUTPUT_HASH_MISMATCH: ' + name)
    return manifest


def publish(staging, destination):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()): raise ValueError('Output directory must be empty')
    os.rename(staging, destination)
