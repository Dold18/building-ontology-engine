"""Development-only Phase D inspection. Never imported by the inference engine.

FR-01–08/11/12; provisional D1–D5/D7/D8. All examples are SYNTHETIC.
Reference answers stay outside inference input directories.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

from rdflib import Graph
from ontology_engine.models import Assertion
from ontology_engine.pipeline import build_graph
from ontology_engine.publication import verify_package
from ontology_engine.review import files_snapshot
from ontology_engine.review_models import hashed

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED = {'verified', 'supported_inference'}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=True) + '\n')


def hashes(path):
    return {n: hashlib.sha256(b).hexdigest() for n, b in files_snapshot(path).items()}


def check_freeze():
    freeze = read(ROOT/'fixtures/task004_final_reference/freeze.json')
    for name, digest in freeze['sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    return hashed(freeze)


def alias_request(service, action_id='documentary-alias', correlation='phase-d-investigation'):
    """Construct explicit simulated-review edits from inspected documentary rows."""
    w = service.workspace
    group = next(g for g in service.groups()['items'] if g['cause_kind'] == 'naming_prefix')
    claims = [c for c in group['claims'] if c['predicate'] == 'type' and c['object_id'] == 'unknown']
    rows = {s['normalized_fields'].get('point_id'): s['normalized_fields'] for s in group['sources']
            if s['table_kind'] == 'points'}
    aliases = {rows[c['subject_id'][6:]]['naming_hint'] for c in claims}
    assert len(aliases) == 1 and claims
    return dict(schema_version='review-v2', workspace_id=w.workspace_id, site_scope=w.site_scope,
        parent_fingerprint=w.parent_fingerprint, actor_id=w.reviewer.actor_id, action_id=action_id,
        correlation_id=correlation, expected_revision=service.history()['revision'], target_kind='group',
        target_id=group['group_id'], action='correct', requested_status='needs_review',
        reason='SYNTHETIC documentary channel register supports these exact edits; authority unconfirmed.',
        evidence_uids=[s['source_record_uid'] for s in group['sources']],
        affected_claim_uids=[c['claim_uid'] for c in claims],
        correction=dict(kind='naming_alias', alias=aliases.pop(), clarification='SYNTHETIC supplied register interpretation.',
            edits=[dict(subject_id=c['subject_id'], predicate='type',
                        object_id=rows[c['subject_id'][6:]]['documented_point_class']) for c in claims]))


def inspect_package(path, parent=None, inspect_local_state=False):
    """Inspect actual bytes as well as the mandatory semantic publication verifier."""
    p = Path(path)
    manifest = verify_package(p)
    claims = read(p/'candidate_claims.json')['records']
    assertions = read(p/'assertions.json')
    accepted_claims = {c['claim_uid'] for c in claims if c['status'] in ACCEPTED}
    accepted_assertions = {a['claim_uid'] for a in assertions if a['status'] in ACCEPTED}
    assert accepted_claims == accepted_assertions
    graph = Graph().parse(p/'ontology.ttl')
    assert set(graph) == set(build_graph([Assertion.model_validate(a) for a in assertions], manifest['settings']['namespace']))
    assert not any(c['status'] == 'verified' for c in claims)
    sources = [json.loads(line) for line in (p/'source_records.jsonl').read_text().splitlines()]
    source_ids = {r['source_id'] for r in sources}
    observations = read(p/'candidate_observations.json')['records']
    for a in assertions:
        assert a['supporting_source_ids'] and set(a['supporting_source_ids']) <= source_ids
    for s in sources:
        assert s['raw_value'] and s['source_hash'] and s['source_location']
    state = Path(manifest['settings']['durable_state'])
    # Manifest's state path is explanatory; exported ZIP remains portable without it.
    if inspect_local_state:
        assert state.is_file(), 'Expected local durable state missing'
        with sqlite3.connect(state.as_uri()+'?mode=ro', uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            assert db.execute('SELECT state FROM runs').fetchone()[0] == 'COMPLETED'
            assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
    result = dict(accepted_triples=len(graph), claims=len(claims), observations=len(observations),
        raw_source_records=len(sources), accepted_claims=len(accepted_claims), verified_claims=0,
        conflicts=len(read(p/'claim_conflicts.json')['records']), package_verifier='passed',
        accepted_graph_claim_assertion_csv_alignment='passed', source_lineage='passed',
        engineering=read(p/'engineering_results.json')['summary'], manifest_sha256=hashes(p)['run_manifest.json'])
    if parent:
        assert hashes(p/'parent_package') == hashes(parent)
        assert (p/'source_records.jsonl').read_bytes() == (Path(parent)/'source_records.jsonl').read_bytes()
        changes = read(p/'review_changes.json')
        for c in changes['removed_claims']:
            assert c['observations'] and c['sources'] and c['sources'][0]['raw_value']
            assert c['claim']['claim_uid'] and c['claim']['status'] and c['sources'][0]['source_hash']
        assert read(p/'before_shacl.json')['full_brick_shacl'] == 'passed'
        assert read(p/'after_shacl.json')['full_brick_shacl'] == 'passed'
        before = {a['assertion_id']: a for a in read(Path(parent)/'assertions.json') if a['predicate'] != 'type' and a['status'] in ACCEPTED}
        after = {a['assertion_id']: a for a in assertions}
        assert all(after.get(k) == a for k, a in before.items())
        result.update(added_triples=len(changes['added_triples']), removed_triples=len(changes['removed_triples']),
            removed_claims_with_evidence=len(changes['removed_claims']), stable_accepted_relationships=len(before),
            pinned_before_after_brick_shacl='passed', immutable_parent_snapshot='passed')
    return result


def revision_receipt(old_service, new_service, old_package, new_package, old_action, new_action):
    """An investigation link is descriptive audit evidence, never a migrated decision."""
    old = old_service.workspace
    new = new_service.workspace
    assert old.site_scope == new.site_scope
    assert old.parent_fingerprint != new.parent_fingerprint and old.workspace_id != new.workspace_id
    old_events = old_service.history()['events']
    new_events = new_service.history()['events']
    assert old_events and new_events
    old_points = {s['source_record_uid'] for s in old_service.parent().sources.values() if s['table_kind'] == 'points'}
    new_points = {s['source_record_uid'] for s in new_service.parent().sources.values() if s['table_kind'] == 'points'}
    assert old_points.isdisjoint(new_points)
    before = set(Graph().parse(Path(old_package)/'ontology.ttl'))
    after = set(Graph().parse(Path(new_package)/'ontology.ttl'))
    return dict(label='SYNTHETIC / FOR TESTING ONLY', link_kind='historical_investigation_to_new_evidence',
        authority_transferred=False, old_workspace=old.workspace_id, new_workspace=new.workspace_id,
        old_parent_fingerprint=old.parent_fingerprint, new_parent_fingerprint=new.parent_fingerprint,
        old_event_ids=[e['event_id'] for e in old_events], new_event_ids=[e['event_id'] for e in new_events],
        old_history_sha256=hashed(old_events), new_history_sha256=hashed(new_events),
        old_source_uids=sorted(old_points), new_source_uids=sorted(new_points),
        reopened_claim_uids=sorted(set(old_action['affected_claim_uids']) & set(new_action['affected_claim_uids'])),
        old_group_id=old_action['target_id'], reopened_group_id=new_action['target_id'],
        reopening='Original unknown claims reappear in new parent; new workspace starts at revision zero. Fresh explicit review required.',
        reviewed_source_revision_diff=dict(added_triples=sorted([list(map(str,t)) for t in after-before]),
                                          removed_triples=sorted([list(map(str,t)) for t in before-after])))
