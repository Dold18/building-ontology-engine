"""Publish a BuildingGraph ledger as a standard, verifiable run package so the EXISTING review service and web app can load it.

Nothing new is invented: the package is computed with the same functions `pipeline._run_core` uses (candidate policy,
disposition projection, coverage, engineering screening, `build_graph`, `validate_graph`, CSV encoding) and sealed with
`publication.finalize`, so `publication.verify_package` re-derives every ledger from the preserved observations exactly as it
does for pipeline runs. Review decisions then go through the existing authenticated, append-only review workspace; nothing in
this module promotes a candidate.

`ontology.ttl` is the ledger-derived accepted core required by the package contract (accepted point types). The enriched
accepted graph and the provisional graph are included alongside as `accepted_enriched.ttl` and `provisional.ttl`.
"""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from . import __version__
from .candidates import evaluate_proposals, project, VERSION as CANDIDATE_VERSION, POLICY_VERSION
from .coverage import coverage as inference_coverage
from .csv_safety import encode_cell
from .engineering import evaluate as engineering_evaluate, RuleResult
from .limits import Limits
from .models import Assertion, Classification, Issue, ReviewItem, RunManifest
from .pipeline import build_graph, relationship_csv_row, validate_graph
from .standards import contract

ACCEPTED = {'verified', 'supported_inference'}
PACKAGE_LIMITATIONS = [
    'Provisional sponsor building graph built deterministically from the BAS export; no model inference.',
    'Accepted claims are documented export facts (point existence); nothing is field-verified.',
    'Equipment, ownership, feeds and system membership are candidates from BAS containment and folder naming (needs_review).',
    'Physical and thermal relationships are review-queue hypotheses only; no accepted topology exists.',
]


def _csv(path, rows, fields, serializer=None):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            data = serializer(row) if serializer else row.model_dump()
            writer.writerow({k: encode_cell(json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in data.items()})


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


_HIERARCHY = None


def supported_ancestor(cls, allowed):
    """Most specific class in `allowed` that is `cls` or a Brick superclass of it, from the pinned Brick 1.4.4 file."""
    global _HIERARCHY
    from rdflib import Graph, RDFS
    from .standards import RESOURCES
    from .vocabulary import BRICK
    if _HIERARCHY is None:
        _HIERARCHY = Graph().parse(RESOURCES / 'Brick-1.4.4.ttl', format='turtle')
    seen, frontier = set(), [BRICK[cls]]
    while frontier:
        nxt = []
        for node in frontier:
            local = str(node).split('#')[-1]
            if str(node).startswith(str(BRICK)) and local in allowed:
                return local
            for parent in sorted(_HIERARCHY.objects(node, RDFS.subClassOf)):
                if parent not in seen:
                    seen.add(parent)
                    nxt.append(parent)
        frontier = nxt
    return None


def review_ledger(bg):
    """Project the BuildingGraph ledger onto the engine's supported vocabulary for review, without inventing classes.

    A specific class outside the supported subset is represented by its most specific supported Brick superclass (the full
    class stays in the explanation and in provisional.ttl). Point classes whose only supported ancestor is `Point` add
    nothing beyond the accepted point and are left out; system candidates and their memberships have no supported class or
    predicate and stay in provisional.ttl and the review questions. Returns (assertions, omitted counts).
    """
    from .vocabulary import EQUIPMENT, POINTS
    systems = {'equipment/' + t for t, e in bg.equipment.items() if supported_ancestor(e['class'], EQUIPMENT) is None}
    out, omitted = [], {'point_class_only_generic_point': 0, 'system_candidate_type': 0, 'system_membership': 0}
    for a in bg.assertions:
        if a.predicate == 'includes':
            omitted['system_membership'] += 1
            continue
        if a.predicate != 'type' or a.object_id in POINTS | EQUIPMENT:
            out.append(a)
            continue
        kind = 'point' if a.subject_id.startswith('point/') else 'equipment'
        mapped = supported_ancestor(a.object_id, POINTS if kind == 'point' else EQUIPMENT)
        if mapped is None or (kind == 'point' and mapped == 'Point'):
            omitted['system_candidate_type' if a.subject_id in systems else 'point_class_only_generic_point'] += 1
            continue
        explanation = '[review vocabulary: %s for candidate class %s] %s' % (mapped, a.object_id, a.explanation)
        out.append(bg.make_assertion(a.subject_id, 'type', mapped, a.supporting_source_ids, explanation, a.inference_method, a.status))
    return out, omitted


def classifications_for(bg):
    out = []
    for r in bg.records:
        if r.parse_status != 'ok':
            continue
        f = bg.facts[r.source_id]
        cls, status = bg.point_class.get(r.source_id, ('unknown', 'needs_review'))
        owner = bg.point_equipment.get(r.source_id)
        out.append(Classification(source_point_id=f['point_id'], equipment_id='', point_class=cls, status=status,
                                  supporting_source_ids=[r.source_id], units=f['units'], inference_method='naming_lexicon' if cls != 'unknown' else 'none',
                                  explanation=('Candidate owner %s (%s); ' % owner if owner else 'No candidate owner; ') +
                                  ('naming-cue class candidate' if cls != 'unknown' else 'class unresolved')))
    return out


def write_package(bg, records, originals, input_config, staging, extra_files=None, limits=None, code_revision='unknown', ledger_filter=None,
                  reviews=None, label='full'):
    """Write a complete package into the empty directory `staging`, then seal it with publication.finalize."""
    from .publication import finalize
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=False)
    present = {r.source_id for r in records}
    records = list(records) + [r for r in getattr(bg, 'extra_records', []) if r.source_id not in present]   # citable drawing records
    originals = dict(originals)
    if getattr(bg, 'drawing_evidence_bytes', None):
        originals.setdefault('drawing_evidence.json', bg.drawing_evidence_bytes)
    limits = limits or Limits()
    (staging / 'sources').mkdir()
    hashes = {}
    for name, data in originals.items():
        (staging / 'sources' / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    (staging / 'source_records.jsonl').write_text(''.join(r.model_dump_json() + '\n' for r in records), encoding='utf-8')
    _json(staging / 'input_config.json', input_config)
    _json(staging / 'data_inventory.json', {'files': [{'name': n, 'sha256': h} for n, h in sorted(hashes.items())],
                                            'records': len(records), 'by_table_kind': {'points': sum(r.table_kind == 'points' for r in records)}})
    for name, value in (('retrieval_plan.json', {'mode': 'legacy', 'status': 'not_requested'}),
                        ('context_batches.json', {'mode': 'legacy', 'status': 'not_requested'}),
                        ('context_candidates.json', {'mode': 'legacy', 'records': []}), ('proposal_observations.json', [])):
        _json(staging / name, value)
    ledger, omitted = review_ledger(bg)
    if ledger_filter is not None:
        ledger = [a for a in ledger if ledger_filter(a)]
    base = [a.model_copy(deep=True) for a in ledger]
    bundle = evaluate_proposals(base, records, bg.scope, bg.run_id, [], limits)
    assertions = project(base, bundle)
    _json(staging / 'assertion_observations.json', [a.model_dump() for a in base])
    for filename, rows in (('candidate_claims.json', bundle.claims), ('candidate_observations.json', bundle.observations),
                           ('claim_conflicts.json', bundle.conflicts)):
        _json(staging / filename, {'schema_version': CANDIDATE_VERSION, 'policy_version': POLICY_VERSION, 'records': [r.model_dump() for r in rows]})
    _json(staging / 'inference_coverage.json', inference_coverage(records, assertions, bundle, None, None))
    engineering = engineering_evaluate(records, assertions, bundle, bg.scope, limits)
    _json(staging / 'engineering_results.json', engineering.model_dump())
    _json(staging / 'engineering_registry.json', [r.model_dump() for r in engineering.registry])
    _csv(staging / 'engineering_results.csv', engineering.records, list(RuleResult.model_fields))
    graph = build_graph(assertions, str(bg.ns))
    validation = validate_graph(graph)
    validation['engineering'] = engineering.summary
    validation['physics'] = engineering.summary['physics']
    if validation['errors']:
        raise ValueError('Accepted core failed supported schema checks: %s' % validation['errors'][:3])
    graph.serialize(destination=str(staging / 'ontology.ttl'), format='turtle')
    classifications = classifications_for(bg)
    kept = {a.assertion_id for a in assertions}
    reviews = [r.model_copy(update={'affected_assertion_ids': [i for i in r.affected_assertion_ids if i in kept]}) for r in (reviews if reviews is not None else bg.reviews)]
    _csv(staging / 'classifications.csv', classifications, list(Classification.model_fields))
    _csv(staging / 'relationships.csv', assertions, list(Assertion.model_fields) + ['subject_entity_id', 'object_entity_id'], relationship_csv_row)
    _csv(staging / 'issues.csv', bg.issues, list(Issue.model_fields))
    _csv(staging / 'review_questions.csv', reviews, list(ReviewItem.model_fields))
    _json(staging / 'assertions.json', [a.model_dump() for a in assertions])
    _json(staging / 'classifications.json', [c.model_dump() for c in classifications])
    _json(staging / 'issues.json', [i.model_dump() for i in bg.issues])
    _json(staging / 'review_questions.json', [r.model_dump() for r in reviews])
    _json(staging / 'candidate_assertions.json', [a.model_dump() for a in assertions if a.status not in ACCEPTED])
    _json(staging / 'validation.json', validation)
    for name, data in (extra_files or {}).items():
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        (staging / name).write_bytes(data)
    (staging / 'README.md').write_text('# PROVISIONAL / UNREVIEWED SPONSOR BUILDING GRAPH\n\n' + '\n'.join('- ' + x for x in PACKAGE_LIMITATIONS) + '\n'
                                       'Review with the existing review workspace; decisions never rewrite this package.\n', encoding='utf-8')
    stamp = datetime.now(timezone.utc).isoformat()
    manifest = RunManifest(run_id=bg.run_id, started_at=stamp, finished_at=stamp, code_version=__version__, code_revision=code_revision,
        ontology_version='Brick 1.4.4 supported subset', input_hashes=hashes,
        settings={'provider': 'none', 'requested_model': None, 'namespace': str(bg.ns), 'external_processing': False,
                  'building_scope': bg.scope, 'input_config': input_config, 'limits': limits.model_dump(), 'semantic_contract': contract(),
                  'candidate_schema_version': CANDIDATE_VERSION, 'disposition_policy_version': POLICY_VERSION,
                  'builder': 'building-graph', 'package_scope': label, 'builder_version': bg.__class__.__module__ + ':' + __import__('ontology_engine.building_graph', fromlist=['VERSION']).VERSION},
        model_version=None, prompt_version='none', validation=validation, user_approvals=[], limitations=PACKAGE_LIMITATIONS, synthetic=False,
        counts={'source_records': len(records), 'classifications': len(classifications), 'assertions': len(assertions),
                'accepted_triples': len(graph), 'issues': len(bg.issues), 'review_items': len(reviews), 'candidate_claims': len(bundle.claims),
                'candidate_observations': len(bundle.observations), 'claim_conflicts': len(bundle.conflicts),
                'engineering_results': len(engineering.records)},
        provider_audit={'status': 'no_model_used', 'usage': None}, data_origin='sponsor', execution_origin='offline_deterministic',
        privacy_policy={'default_package': 'restricted sponsor data; not distributable', 'retention': 'operator managed'})
    _json(staging / 'run_manifest.json', manifest.model_dump())
    finalize(staging)
    return {'assertions': len(assertions), 'claims': len(bundle.claims), 'conflicts': len(bundle.conflicts),
            'accepted_core_triples': len(graph), 'statuses': _count(a.status for a in assertions), 'omitted_from_review_ledger': omitted,
            'claim_statuses': _count(c.status for c in bundle.claims)}


def _count(values):
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))
