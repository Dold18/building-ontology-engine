"""Deterministic evidence gate, graph construction, checks and export (TASK_001)."""
import csv
import hashlib
import json
import subprocess
import uuid
import importlib.metadata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from rdflib import Graph, Namespace, RDF
from . import __version__
from .ingest import ingest
from .config import load_config
from .inventory import inventory
from .units import unit_problem
from .sources import snapshot_input
from .identity import uid, building_scope, unique_source_map, VERSION as IDENTITY_VERSION
from .evidence import eligibility, VERSION as EVIDENCE_VERSION
from .csv_safety import encode_cell, VERSION as CSV_VERSION
from .models import Assertion, Classification, Issue, ReviewItem, RunManifest
from . import providers, contextual
from .vocabulary import BRICK, EQUIPMENT, POINTS, PREDICATES, equipment_class, canonical_point_class

ACCEPTED = {'verified', 'supported_inference'}
LIMITATIONS = [
    'TASK_003 Phase A candidate foundation; sponsor ontology/live inference not established by fixture runs.',
    'CSV/XLSX tables supported; drawing extraction, telemetry analysis, UI and correction loop pending.',
    'Default runs check supported Brick vocabulary; use validate for pinned external SHACL. Neither proves evidence truth.',
    'Engineering screening is prerequisite-gated and advisory; it cannot prove evidence truth or equipment faults.',
    'Fixture provider is deterministic testing only, not production AI.',
    'Explicit source tables are supplied evidence, not independently authenticated facts.',
    'Live OpenAI integration not tested; API cost/accuracy unknown.',
]

def now():
    return datetime.now(timezone.utc).isoformat()


def stable_id(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def write_csv(path, models, fields, row_serializer=None):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for model in models:
            row = row_serializer(model) if row_serializer else model.model_dump()
            writer.writerow({k: encode_cell(json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
                             for k, v in row.items()})


def relationship_csv_row(assertion):
    """Expose canonical input IDs while retaining internal keys for RDF/audit joins."""
    row = assertion.model_dump()
    row['subject_entity_id'] = assertion.subject_id
    row['object_entity_id'] = assertion.object_id

    def canonical_id(entity_id):
        # Remove exactly one internal scope prefix, preserving slashes/prefixes
        # that are part of the original identifier. These keys are not RDF IRIs.
        if entity_id.startswith(('equipment/', 'point/')):
            return entity_id.partition('/')[2]
        return entity_id

    row['subject_id'] = canonical_id(assertion.subject_id)
    if assertion.predicate != 'type':
        row['object_id'] = canonical_id(assertion.object_id)
    row['object_or_value'] = row['object_id']
    return row


def build_graph(assertions, namespace):
    graph = Graph()
    graph.bind('brick', BRICK)
    graph.bind('bldg', Namespace(namespace))
    for a in assertions:
        if a.status not in ACCEPTED:
            continue
        subject = entity_iri(namespace, a.subject_id)
        predicate = RDF.type if a.predicate == 'type' else BRICK[a.predicate]
        obj = BRICK[a.object_id] if a.predicate == 'type' else entity_iri(namespace, a.object_id)
        graph.add((subject, predicate, obj))
    return graph


def entity_iri(namespace, entity):
    return Namespace(namespace)[quote(entity, safe='')]


def validate_graph(graph):
    """FR-06 initial checks: syntax, supported terms and relationship endpoint types."""
    errors = []
    parsed = Graph().parse(data=graph.serialize(format='turtle'), format='turtle')
    for s, p, o in parsed:
        if p == RDF.type:
            if o not in {BRICK[c] for c in EQUIPMENT | POINTS}:
                errors.append('Unsupported class: ' + str(o))
        elif p not in {BRICK[p] for p in PREDICATES - {'type'}}:
            errors.append('Unsupported predicate: ' + str(p))
        else:
            st = set(parsed.objects(s, RDF.type))
            ot = set(parsed.objects(o, RDF.type))
            equipment_types = {BRICK[c] for c in EQUIPMENT}
            point_types = {BRICK[c] for c in POINTS}
            if not st & equipment_types:
                errors.append('Relationship subject must be typed equipment: ' + str(s))
            expected = point_types if p == BRICK.hasPoint else equipment_types
            if not ot & expected:
                errors.append('Relationship object has missing/incompatible type: ' + str(o))
            if s == o:
                errors.append('Self relationship: ' + str(s))
    return {'rdf_parse': 'passed', 'supported_subset': 'failed' if errors else 'passed',
            'evidence_truth': 'not_evaluable', 'errors': errors, 'full_brick_shacl': 'not_evaluable', 'external_validation_execution': 'not_run', 'physics': 'not_evaluable'}


def _run_core(input_dir, output_dir, provider='fixture', model=None, allow_external=False, building_id=None, config_path=None, _context=None):
    if Path(input_dir).is_symlink():
        raise ValueError('SYMLINK_REJECTED: input root')
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    if not input_dir.is_dir():
        raise ValueError('Input directory does not exist')
    if output_dir == input_dir or input_dir in output_dir.parents or output_dir in input_dir.parents:
        raise ValueError('Input and output directories must be separate, non-nested directories')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError('Output directory must be empty; use a new directory to preserve audit history')
    if provider not in {'fixture', 'openai'}:
        raise ValueError('Unsupported LLM provider')
    if provider == 'openai' and not allow_external:
        raise ValueError('External processing requires --allow-external-processing owner authorization')
    config = _context.config if _context else load_config(config_path)
    started, run_id = (_context.started, _context.run_id) if _context else (now(), str(uuid.uuid4()))
    snapshot = _context.snapshot if _context else snapshot_input(input_dir)
    scope = building_scope(input_dir, building_id)
    records, hashes, originals = _context.normalized() if _context else ingest(input_dir, config, snapshot=snapshot, scope=scope)
    if not hashes:
        raise ValueError('No supported CSV/XLSX files found')
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = output_dir / 'sources'
    sources.mkdir()
    for filename, content in originals.items():
        (sources / filename).write_bytes(content)
    (output_dir / 'source_records.jsonl').write_text(''.join(
        r.model_dump_json() + '\n' for r in records), encoding='utf-8')
    write_json(output_dir / 'input_config.json', config.model_dump())
    write_json(output_dir / 'data_inventory.json', inventory(input_dir, records, hashes, snapshot))
    namespace = 'urn:building:' + quote(scope, safe='') + ':'
    issues, assertions, classifications, reviews = [], [], [], []
    by_source = unique_source_map(records)

    def issue(asset, category, evidence, method, source_ids, action):
        issues.append(Issue(issue_id=stable_id(asset, method, source_ids), category=category,
            point_or_asset_id=asset, observed_evidence=evidence, rule_or_detection_method=method,
            source_ids=source_ids, recommended_action=action))

    def assertion(subject, predicate, obj, source_ids, explanation, method='direct_document',
                  status='supported_inference', model_version=None, medium=None):
        if predicate == 'type':
            obj = canonical_point_class(obj)
        if method!='llm':model_version=None
        evidence_valid = bool(source_ids) and all(s in by_source for s in source_ids)
        vocabulary_valid = predicate in PREDICATES and (predicate != 'type' or obj in EQUIPMENT | POINTS)
        checks = [
            'source references resolve' if evidence_valid else 'source references missing/invalid',
            'supported vocabulary' if vocabulary_valid else 'unsupported or unresolved vocabulary',
            'accepted graph checks apply' if status in ACCEPTED else 'withheld from accepted graph',
        ]
        decision = eligibility(subject, predicate, obj, source_ids, by_source, method, explanation)
        if status == 'verified':
            decision['eligible'] = False
            decision['reason_codes'].append('AUTHENTICATED_VERIFICATION_REQUIRED')
        if status in ACCEPTED and (not evidence_valid or not vocabulary_valid or not decision['eligible']):
            status = 'needs_review'
        claim = uid('claim', scope, subject, predicate, obj)
        observation = uid('obs', run_id, len(assertions), claim, source_ids, explanation, method)
        a = Assertion(assertion_id=observation, observation_uid=observation, claim_uid=claim,
            subject_entity_uid=uid('entity', scope, subject), object_entity_uid=uid('entity', scope, obj),
            supporting_source_uids=[by_source[s].source_record_uid for s in source_ids if s in by_source],
            original_source_ids=list(source_ids), eligibility=decision,
            subject_id=subject, predicate=predicate, object_id=obj,
            supporting_source_ids=source_ids, inference_method=method, explanation=explanation,
            status=status, validation_results=checks,
            run_id=run_id, model_version=model_version,
            prompt_version=(contextual.PROMPT_VERSION
                if config.inference_mode=='contextual' else providers.PROMPT_VERSION) if method == 'llm' else None,
            physical_medium=medium)
        assertions.append(a)
        if status not in ACCEPTED:
            reviews.append(ReviewItem(review_id=stable_id(a.assertion_id),
                affected_assertion_ids=[a.assertion_id], question_or_test='Verify: ' + explanation,
                supporting_evidence_ids=source_ids, reason_for_uncertainty=explanation))
        return a

    for r in records:
        f = r.normalized_fields
        asset = f.get('point_id') or f.get('equipment_id') or f.get('connection_id') or r.source_id
        if r.errors:
            issue(asset, 'data_quality', '; '.join(r.errors),
                  'xlsx_parse' if r.source_type == 'xlsx' else 'csv_parse', [r.source_id],
                  'Correct source row/header and rerun; original bytes retained.')
        if r.table_kind == 'points' and not f.get('units'):
            issue(asset, 'data_quality', 'Missing unit metadata', 'missing_units', [r.source_id],
                  'Confirm units from documentation or field verification; do not assume a unit.')
    for kind in ('points', 'equipment'):
        if not any(r.table_kind == kind for r in records):
            issue(kind, 'data_quality', 'Input table absent', 'missing_table', [],
                  'Provide table or review limited coverage.')
    equipment = {r.normalized_fields['equipment_id']: r for r in records
                 if r.table_kind == 'equipment' and r.parse_status == 'ok'}
    points = {r.normalized_fields['point_id']: r for r in records
              if r.table_kind == 'points' and r.parse_status == 'ok'}
    model_version = None
    try:
        if _context:
            proposals, model_version, _ = _context.propose(records, output_dir)
        elif provider == 'fixture':
            proposals = providers.fixture(records)
        else:
            proposals, model_version, payload = providers.openai(
                records, model, allow_external, output_dir / 'provider_response.json')
            providers.save(output_dir / 'provider_request.json', payload)
        write_json(output_dir / 'proposals.json', providers.redact(proposals.model_dump()))
    except Exception as exc:
        write_json(output_dir / 'failure.json', {'run_id': run_id, 'stage': 'provider',
                   'input_hashes': hashes, 'input_config': config.model_dump(),
                   'prompt_version': contextual.PROMPT_VERSION if config.inference_mode=='contextual' else providers.PROMPT_VERSION, 'requested_model': model,
                   'provider_audit': json.loads((output_dir / 'provider_audit.json').read_text())
                       if (output_dir / 'provider_audit.json').exists() else {},
                   'error_type': type(exc).__name__, 'message': 'Provider failed; raw sources preserved. No fixture fallback.'})
        raise ValueError('Provider failed; inspect failure.json and any provider_response.json') from exc
    proposal_observations = []
    for kind, items in [('equipment', proposals.equipment), ('points', proposals.points), ('relationships', proposals.relationships)]:
        for proposal in items:
            refs = [proposal.source_id] if kind != 'relationships' else proposal.supporting_source_ids
            resolved = [by_source[s] for s in refs if s in by_source]
            appropriate = all(r.table_kind == kind and r.parse_status == 'ok' for r in resolved) if kind != 'relationships' else False
            proposal_observations.append({'observation_uid': uid('proposal', run_id, len(proposal_observations)),
                'kind': kind, 'proposal': proposal.model_dump(), 'original_source_ids': refs,
                'inference_method': 'rule' if provider == 'fixture' or config.inference_mode=='contextual' else 'llm', 'model_version': model_version if config.inference_mode!='contextual' else None,
                'prompt_version': providers.PROMPT_VERSION if provider == 'openai' and config.inference_mode=='legacy' else None,
                'resolved_source_uids': [r.source_record_uid for r in resolved],
                'status': 'needs_review' if refs and len(resolved) == len(refs) and appropriate else 'rejected',
                'reason_codes': ['CANDIDATE_ONLY'] if appropriate else ['INELIGIBLE_OR_UNRESOLVED_EVIDENCE']})
    equipment_proposals = {}
    for proposal in proposals.equipment:
        source = by_source.get(proposal.source_id)
        if not source or source.table_kind != 'equipment' or source.parse_status != 'ok':
            issue(proposal.source_id, 'model_conflict', 'Equipment proposal cites absent/invalid source',
                  'invalid_proposal_source', [proposal.source_id] if source else [], 'Review provider proposal.')
            continue
        equipment_proposals.setdefault(proposal.source_id, []).append(proposal)
    for eid, r in equipment.items():
        cls = equipment_class(r.normalized_fields.get('equipment_type', ''))
        proposals_for_equipment = equipment_proposals.get(r.source_id, [])
        if provider == 'openai' and proposals_for_equipment:
            # Model equipment interpretations are candidates, not automatic documentary facts.
            for proposal in proposals_for_equipment:
                suggested = proposal.equipment_class.removeprefix('brick:')
                assertion('equipment/' + eid, 'type', suggested, [r.source_id], proposal.explanation,
                          method='llm', status='needs_review', model_version=model_version)
        assertion('equipment/' + eid, 'type', cls, [r.source_id],
                  'Equipment type mapped from supplied equipment table.', method='rule')
        if cls == 'Equipment':
            issue(eid, 'data_quality', 'Unrecognized/missing equipment type; generic Equipment retained',
                  'unknown_equipment_type', [r.source_id], 'Confirm equipment type from schedule.')
    proposed = {}
    for p in proposals.points:
        r = by_source.get(p.source_id)
        if not r or r.table_kind != 'points' or r.parse_status != 'ok':
            issue(p.source_id, 'model_conflict', 'Proposal cites absent/invalid point source',
                  'invalid_proposal_source', [p.source_id] if r else [], 'Inspect structured provider response.')
            continue
        proposed.setdefault(p.source_id, []).append(p)
    method = 'rule' if provider == 'fixture' or config.inference_mode=='contextual' else 'llm'
    for r in [r for r in records if r.table_kind == 'points']:
        f = r.normalized_fields
        pid = f.get('point_id', '')
        ps = proposed.get(r.source_id, [])
        if len(ps) > 1:
            for proposal in ps:
                assertion('point/' + pid, 'type', proposal.point_class.removeprefix('brick:'),
                          [r.source_id], 'Multiple provider proposals: ' + proposal.explanation,
                          method=method, status='needs_review', model_version=model_version)
        p = ps[0] if len(ps) == 1 and r.parse_status == 'ok' else None
        cls = canonical_point_class(p.point_class) if p else 'unknown'
        explanation = p.explanation if p else 'Invalid source, missing proposal, or multiple proposals; review required.'
        if cls not in POINTS:
            if p and cls != 'unknown':
                issue(pid, 'model_conflict', 'Unsupported proposed point class: ' + cls,
                      'unsupported_point_class', [r.source_id], 'Review provider proposal against supported Brick classes.')
            cls = 'unknown'
        problem = unit_problem(f, cls) or unit_problem(f)
        if problem:
            issue(pid, 'data_quality', problem, 'inconsistent_units', [r.source_id],
                  'Confirm quantity and unit from documentation; no automatic conversion.')
        eid = f.get('equipment_id', '')
        membership_ok = bool(eid and eid in equipment and r.parse_status == 'ok')
        # An LLM cannot override explicit ownership or create accepted ownership without documentation.
        if p and p.equipment_id != eid:
            issue(pid, 'model_conflict', 'Proposed ownership differs from explicit metadata',
                  'ownership_conflict', [r.source_id], 'Confirm equipment association from documentation.')
            if p.equipment_id:
                assertion('equipment/' + p.equipment_id, 'hasPoint', 'point/' + pid,
                          [r.source_id], p.explanation, method=method, status='needs_review',
                          model_version=model_version)
            membership_ok = False
        class_evidence = eligibility('point/' + pid, 'type', cls, [r.source_id], by_source, method, explanation)
        classification_status = 'supported_inference' if cls != 'unknown' and r.parse_status == 'ok' and not problem and class_evidence['eligible'] else 'needs_review'
        classifications.append(Classification(source_point_id=pid, equipment_id=eid if membership_ok else '',
            point_class=cls, status=classification_status, supporting_source_ids=[r.source_id],
            explanation=explanation, units=f.get('units', ''), inference_method=method,
            model_version=model_version if method=='llm' else None, prompt_version=providers.PROMPT_VERSION if method == 'llm' else None))
        if r.parse_status != 'ok':
            assertion('point/' + (pid or r.source_id), 'type', 'unknown', [r.source_id],
                      explanation, status='needs_review', method=method, model_version=model_version)
            continue
        assertion('point/' + pid, 'type', cls, [r.source_id], explanation,
                  method=method, status=classification_status, model_version=model_version)
        # Generic Point is a conservative declared entity, not a specific interpretation.
        if classification_status == 'needs_review':
            assertion('point/' + pid, 'type', 'Point', [r.source_id], 'Row explicitly represents a point.')
        if membership_ok:
            assertion('equipment/' + eid, 'hasPoint', 'point/' + pid,
                      [r.source_id, equipment[eid].source_id], 'Explicit equipment_id in supplied point row.')
        else:
            assertion('equipment/' + (eid or 'unknown'), 'hasPoint', 'point/' + pid,
                      [r.source_id], 'Missing, conflicting or unresolved equipment mapping.', status='needs_review')
    for r in records:
        if r.table_kind != 'connections':
            continue
        f = r.normalized_fields
        source, target = f.get('from_equipment', ''), f.get('to_equipment', '')
        predicate = f.get('predicate', 'feeds').removeprefix('brick:')
        valid = (r.parse_status == 'ok' and source in equipment and target in equipment
                 and source != target and predicate in {'feeds', 'hasPart'})
        # Optional source status can withhold a claim, but cannot authenticate it as verified.
        supplied_status = f.get('status', 'supported_inference')
        status = supplied_status if supplied_status in {'needs_review', 'contradicted', 'rejected'} else 'supported_inference'
        if supplied_status not in ACCEPTED | {'needs_review', 'contradicted', 'rejected'}:
            valid = False
        assertion('equipment/' + source, predicate, 'equipment/' + target, [r.source_id],
                  f.get('source_description') or 'Explicit connection table row; confirm source authority.',
                  status=status if valid else 'needs_review', medium=f.get('medium') or None)
    for p in proposals.relationships:
        valid_sources = [s for s in p.supporting_source_ids if s in by_source]
        known_entities = {'equipment/' + eid for eid in equipment} | {'point/' + pid for pid in points}
        if (not valid_sources or len(valid_sources) != len(p.supporting_source_ids)
                or p.subject_id not in known_entities or p.object_id not in known_entities):
            issue(p.subject_id, 'model_conflict', 'Proposed relation has missing evidence or unknown endpoints',
                  'invalid_relation_proposal', valid_sources, 'Verify source references and entity identities.')
        # Proposed relations never bypass documentary gate; preserve even invalid candidates.
        proposed_assertion = assertion(p.subject_id, p.predicate, p.object_id, valid_sources,
                  p.explanation + (' Invalid/missing evidence references.' if not valid_sources or len(valid_sources) != len(p.supporting_source_ids) else ''),
                  method=method, status='needs_review', model_version=model_version)
        proposed_assertion.original_source_ids = list(p.supporting_source_ids)
    context_results=getattr(_context,'context_results',None) if _context else None
    retrieval_plan=getattr(_context,'retrieval_plan',None) if _context else None
    if context_results:
        from .contextual import PROMPT_VERSION as CONTEXT_PROMPT
        for batch in context_results['batches']:
            for index, entry in enumerate(batch['abstention_audit']):
                if entry['status']=='rejected':continue
                a=entry['abstention'];target=a['target_source_id']
                reviews.append(ReviewItem(review_id=uid('context-abstention-review',run_id,batch['batch_id'],index),
                    affected_assertion_ids=[r.assertion_id for r in assertions if target in r.supporting_source_ids],
                    question_or_test='Model abstention: '+a['explanation'],supporting_evidence_ids=[target],
                    reason_for_uncertainty='; '.join(a['missing_prerequisites'])))
            for candidate in batch['candidate_audit']:
                p=candidate['proposal'];refs=p['supporting_source_ids'];resolved=[s for s in refs if s in by_source]
                row=assertion(p['subject_id'],p['predicate'],p['object_id'],resolved,p['explanation'],method='llm',
                    status=candidate['status'],model_version=batch['returned_model'])
                row.original_source_ids=list(refs)
                row.eligibility['context_scope_reason_codes']=candidate['reason_codes']
                proposal_observations.append({'observation_uid':uid('context-proposal',run_id,batch['batch_id'],candidate['candidate_index']),
                    'kind':'relationships','proposal':p,'original_source_ids':list(refs),
                    'inference_method':'llm','model_version':batch['returned_model'],'prompt_version':CONTEXT_PROMPT,
                    'resolved_source_uids':[by_source[s].source_record_uid for s in resolved],
                    'status':candidate['status'],'reason_codes':candidate['reason_codes'],
                    'batch_id':batch['batch_id'],'grounding':candidate['grounding'],'reported_basis':p['basis']})
    else:
        write_json(output_dir/'retrieval_plan.json',{'mode':'legacy','status':'not_requested'})
        write_json(output_dir/'context_batches.json',{'mode':'legacy','status':'not_requested'})
        write_json(output_dir/'context_candidates.json',{'mode':'legacy','records':[]})
    write_json(output_dir/'proposal_observations.json',proposal_observations)
    # Preserve immutable pre-disposition assertion observations; never erase an old gate result.
    from .candidates import evaluate_proposals, project, VERSION as CANDIDATE_VERSION, POLICY_VERSION
    original_assertions = [a.model_copy(deep=True) for a in assertions]
    bundle = (_context.candidates(assertions, records, proposal_observations) if _context else
              evaluate_proposals(assertions, records, scope, run_id, proposal_observations))
    assertions = project(assertions, bundle)
    claims_by_key = {(c.subject_id, c.predicate, c.object_id): c for c in bundle.claims}
    for classification in classifications:
        claim = claims_by_key.get(('point/' + classification.source_point_id, 'type', classification.point_class))
        if claim and classification.status in ACCEPTED and claim.status not in ACCEPTED:
            classification.status = claim.status
    for conflict in bundle.conflicts:
        affected = [o.assertion_id for o in bundle.observations
                    if o.observation_uid in conflict.observation_uids and o.assertion_id]
        issues.append(Issue(issue_id=uid('conflict-issue', scope, run_id, conflict.conflict_uid),
            category='model_conflict', point_or_asset_id=conflict.claim_uids[0],
            observed_evidence=conflict.explanation, rule_or_detection_method=conflict.reason_code,
            source_ids=conflict.source_ids, recommended_action='Review the cited evidence and explicit scope constraints; no automatic winner.'))
        reviews.append(ReviewItem(review_id=uid('conflict-review', scope, run_id, conflict.conflict_uid),
            affected_assertion_ids=affected, question_or_test=conflict.explanation,
            supporting_evidence_ids=conflict.source_ids, reason_for_uncertainty=conflict.reason_code))
    write_json(output_dir / 'assertion_observations.json', [a.model_dump() for a in original_assertions])
    for filename, rows in [('candidate_claims.json', bundle.claims), ('candidate_observations.json', bundle.observations),
                           ('claim_conflicts.json', bundle.conflicts)]:
        write_json(output_dir / filename, {'schema_version': CANDIDATE_VERSION, 'policy_version': POLICY_VERSION,
                                          'records': [r.model_dump() for r in rows]})
    from .coverage import coverage
    inference_coverage=coverage(records,assertions,bundle,retrieval_plan,context_results)
    write_json(output_dir/'inference_coverage.json',inference_coverage)
    from .engineering import evaluate as engineering_evaluate, RuleResult
    engineering = (_context.engineering(records, assertions, bundle) if _context else
                   engineering_evaluate(records, assertions, bundle, scope))
    write_json(output_dir/'engineering_results.json', engineering.model_dump())
    write_json(output_dir/'engineering_registry.json', [r.model_dump() for r in engineering.registry])
    write_csv(output_dir/'engineering_results.csv', engineering.records, list(RuleResult.model_fields))
    assertion_ids_by_claim={}
    for a in assertions:assertion_ids_by_claim.setdefault(a.claim_uid,[]).append(a.assertion_id)
    existing_reviewed={key for r in reviews for key in r.affected_assertion_ids}
    for finding in engineering.records:
        if finding.outcome == 'pass' or not finding.source_ids: continue
        # Findings challenge evidence; they never authenticate or silently rewrite a claim.
        affected = sorted({key for claim in finding.claim_uids for key in assertion_ids_by_claim.get(claim,[])})
        if finding.rule_id=='topology.evidence' and set(affected)&existing_reviewed:continue
        reviews.append(ReviewItem(review_id=uid('engineering-review',run_id,finding.result_uid),
            affected_assertion_ids=affected, question_or_test=finding.action,
            supporting_evidence_ids=finding.source_ids,
            reason_for_uncertainty=finding.rule_id+': '+', '.join(finding.reason_codes)))
        if finding.outcome in {'fail','blocked','error'}:
            issues.append(Issue(issue_id=uid('engineering-issue',run_id,finding.result_uid),
                category='model_conflict' if finding.rule_id=='topology.evidence' else 'data_quality',
                point_or_asset_id=finding.target_id, observed_evidence=json.dumps(finding.details),
                rule_or_detection_method=finding.rule_id+'@'+finding.rule_version,
                source_ids=finding.source_ids, recommended_action=finding.action,
                uncertainty='Screening finding; not a proven equipment fault.'))
    graph = build_graph(assertions, namespace)
    validation = validate_graph(graph)
    validation['engineering'] = engineering.summary
    validation['physics'] = engineering.summary['physics']
    if validation['errors']:
        write_json(output_dir / 'validation.json', validation)
        raise ValueError('Accepted graph failed supported schema checks; see validation.json')
    graph.serialize(destination=str(output_dir / 'ontology.ttl'), format='turtle')
    # Inspect the actual written export, not just the in-memory graph.
    Graph().parse(output_dir / 'ontology.ttl', format='turtle')
    write_csv(output_dir / 'classifications.csv', classifications, list(Classification.model_fields))
    write_csv(output_dir / 'relationships.csv', assertions,
              list(Assertion.model_fields) + ['subject_entity_id', 'object_entity_id'],
              row_serializer=relationship_csv_row)
    write_csv(output_dir / 'issues.csv', issues, list(Issue.model_fields))
    write_csv(output_dir / 'review_questions.csv', reviews, list(ReviewItem.model_fields))
    write_json(output_dir / 'assertions.json', [a.model_dump() for a in assertions])
    write_json(output_dir / 'classifications.json', [a.model_dump() for a in classifications])
    write_json(output_dir / 'issues.json', [a.model_dump() for a in issues])
    write_json(output_dir / 'review_questions.json', [a.model_dump() for a in reviews])
    write_json(output_dir / 'candidate_assertions.json', [a.model_dump() for a in assertions if a.status not in ACCEPTED])
    write_json(output_dir / 'validation.json', validation)
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).parent,
                                           stderr=subprocess.DEVNULL, text=True).strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=Path(__file__).parent, text=True)
        revision += '+dirty' if dirty else ''
    except (OSError, subprocess.CalledProcessError):
        revision = 'unknown'
    synthetic = provider == 'fixture' or 'SYNTHETIC.txt' in hashes
    manifest = RunManifest(run_id=run_id, started_at=started, finished_at=now(), code_version=__version__,
        code_revision=revision, ontology_version='Brick 1.4.4 supported subset', input_hashes=hashes,
        settings={'provider': provider, 'requested_model': model, 'namespace': namespace,
                  'external_processing': allow_external, 'input_directory': str(input_dir), 'identity_version': IDENTITY_VERSION, 'evidence_policy_version': EVIDENCE_VERSION,
                  'building_scope': scope, 'csv_encoding': CSV_VERSION, 'candidate_schema_version': CANDIDATE_VERSION,
                  'disposition_policy_version': POLICY_VERSION, 'inference_mode':config.inference_mode,
                  'engineering_version':engineering.schema_version, 'engineering_policy':engineering.policy_version,
                  'context_prompt_version': contextual.PROMPT_VERSION if config.inference_mode=='contextual' else None,
                  'code_file_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted(Path(__file__).parent.glob('*.py'))},
                  'dependency_versions': {name: importlib.metadata.version(name)
                                          for name in ('rdflib', 'pydantic', 'openpyxl', 'defusedxml')},
                  'input_config': config.model_dump(),
                  'input_config_hash': stable_id(config.model_dump()),
                  'proposals_hash': stable_id(proposals.model_dump()),
                  'prompt_hash': hashlib.sha256((contextual.SYSTEM_PROMPT if config.inference_mode=='contextual' else providers.SYSTEM_PROMPT).encode()).hexdigest(),
                  'context_results_hash':stable_id(context_results) if context_results else None,
                  'fixture_rules_version': 'fixture-rules-v2' if provider == 'fixture' else None},
        model_version=model_version, prompt_version=contextual.PROMPT_VERSION if config.inference_mode=='contextual' else providers.PROMPT_VERSION,
        counts={'source_records': len(records), 'classifications': len(classifications),
                'assertions': len(assertions), 'accepted_triples': len(graph), 'issues': len(issues),
                'review_items': len(reviews), 'candidate_claims': len(bundle.claims),
                'candidate_observations': len(bundle.observations), 'claim_conflicts': len(bundle.conflicts),
                'engineering_results':len(engineering.records)}, validation=validation,
        user_approvals=['External processing explicitly enabled by CLI flag'] if allow_external else [],
        limitations=LIMITATIONS, synthetic=synthetic,
        provider_audit=json.loads((output_dir / 'provider_audit.json').read_text())
            if (output_dir / 'provider_audit.json').exists() else {'status': 'fixture_only', 'usage': None})
    write_json(output_dir / 'run_manifest.json', manifest.model_dump())
    label = 'SYNTHETIC / FOR TESTING ONLY' if synthetic else 'PROVISIONAL / UNREVIEWED INPUT'
    (output_dir / 'README.md').write_text('# ' + label + '\n\n'
        'TASK_003 Phase A partial ontology; not a completed sponsor deliverable.\n\n'
        f'Provider: {provider}. Accepted triples: {len(graph)}. Review items: {len(reviews)}.\n\n'
        'relationships.csv is the full assertion ledger (including type assertions); only verified and '
        'supported_inference rows enter ontology.ttl. candidate_assertions.json contains withheld claims. '
        'CSV subject_id/object_id use canonical input IDs (type objects are Brick classes); '
        'subject_entity_id/object_entity_id retain internal keys for RDF joins. '
        'Join supporting_source_ids to source_records.jsonl; exact input bytes are under sources/. '
        'candidate_claims.json, candidate_observations.json and claim_conflicts.json retain grouped alternatives, original observations and conflicts. '
        'assertion_observations.json preserves the pre-disposition audit. Schema checks do not verify physical truth or promote assertion status.\n\n' +
        '\n'.join('- ' + limit for limit in LIMITATIONS) + '\n', encoding='utf-8')
    return manifest


def run(input_dir, output_dir, provider='fixture', model=None, allow_external=False, building_id=None, config_path=None, limits=None, api_key=None):
    from .runtime import execute
    return execute(input_dir, output_dir, provider, model, allow_external, building_id, config_path, limits, api_key=api_key)


def resume(output_dir):
    from .runtime import execute
    return execute(None, output_dir, resume=True)
