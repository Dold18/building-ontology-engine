"""FR-03/04/05/06: bounded deterministic candidate lifecycle, never model truth.

Existing assertions remain the compatibility export. This typed layer preserves their
initial disposition and all citations while recording claim-level conflict outcomes.
"""
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional
from pydantic import Field
from .models import Model, Status, Assertion
from .identity import uid, unique_source_map
from .evidence import eligibility
from .vocabulary import BRICK, POINTS, EQUIPMENT, PREDICATES, POINT_NAMES, canonical_point_class
from .units import unit_problem
from .limits import Limits

VERSION = 'candidate-ledgers-v1'
POLICY_VERSION = 'claim-disposition-v1'
ACCEPTED = {'verified', 'supported_inference'}

class CandidateObservation(Model):
    schema_version: Literal['candidate-ledgers-v1'] = VERSION
    observation_uid: str
    claim_uid: str
    site_scope: str
    run_id: str
    assertion_id: Optional[str] = None
    subject_id: str
    predicate: str
    object_id: str
    original_source_ids: List[str]
    resolved_source_ids: List[str]
    resolved_source_uids: List[str]
    unresolved_source_ids: List[str]
    inference_method: str
    explanation: str
    initial_status: Status
    status: Status
    documentary_supported: bool
    acceptance_eligible: bool
    reason_codes: List[str]
    validation_results: List[str]
    conflicting_claim_uids: List[str] = Field(default_factory=list)
    physical_medium: Optional[str] = None
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    proposal_observation_uids: List[str] = Field(default_factory=list)
    original_proposals: List[Dict[str, Any]] = Field(default_factory=list)
    raw_assertion: Dict[str, Any]

class CandidateClaim(Model):
    schema_version: Literal['candidate-ledgers-v1'] = VERSION
    policy_version: Literal['claim-disposition-v1'] = POLICY_VERSION
    claim_uid: str
    site_scope: str
    subject_id: str
    predicate: str
    object_id: str
    observation_uids: List[str]
    status: Status
    reason_codes: List[str]
    conflicting_claim_uids: List[str]

class ClaimConflict(Model):
    schema_version: Literal['candidate-ledgers-v1'] = VERSION
    policy_version: Literal['claim-disposition-v1'] = POLICY_VERSION
    conflict_uid: str
    kind: Literal['classification', 'exclusive_ownership', 'flow_direction', 'physical_media', 'unit_type']
    status: Literal['needs_review', 'contradicted']
    reason_code: str
    claim_uids: List[str]
    observation_uids: List[str]
    source_ids: List[str]
    constraint: Dict[str, str]
    explanation: str

class CandidateBundle(Model):
    schema_version: Literal['candidate-ledgers-v1'] = VERSION
    policy_version: Literal['claim-disposition-v1'] = POLICY_VERSION
    observations: List[CandidateObservation]
    claims: List[CandidateClaim]
    conflicts: List[ClaimConflict]


@lru_cache(maxsize=1)
def ancestors():
    from rdflib import Graph, RDFS
    from .standards import RESOURCES
    graph = Graph().parse(RESOURCES/'Brick-1.4.4.ttl', format='turtle')
    return {name: set(graph.transitive_objects(BRICK[name], RDFS.subClassOf)) for name in POINTS | EQUIPMENT}


def _observe(a, records, scope, run_id, entities):
    refs = list(a.original_source_ids or a.supporting_source_ids)
    resolved = [s for s in refs if s in records]
    missing = [s for s in refs if s not in records]
    # Re-evaluate ORIGINAL citations; a filtered valid subset never launders missing evidence.
    gate = eligibility(a.subject_id,a.predicate,a.object_id,refs,records,a.inference_method,a.explanation)
    document = eligibility(a.subject_id,a.predicate,a.object_id,refs,records,'direct_document',a.explanation)
    reasons = list(gate['reason_codes'])+list(a.eligibility.get('context_scope_reason_codes',[]))
    relevant = [records[s] for s in resolved if records[s].parse_status=='ok']
    # A conflicting explicit class and recognized description are both source observations,
    # never a reason to promote the description over the explicit field.
    if a.predicate=='type' and a.subject_id.startswith('point/') and not missing and refs and a.explanation.strip():
        matching = [r for r in relevant if r.table_kind=='points' and r.normalized_fields.get('point_id')==a.subject_id[6:]]
        if len(relevant)==len(refs) and any(a.object_id in {
            canonical_point_class(r.normalized_fields.get('point_class','')),
            POINT_NAMES.get(r.normalized_fields.get('description','').lower())} for r in matching):
            document['eligible'] = True
    # Duplicate-tag rows are never accepted or physically merged. Explicit shared
    # ownership constraints may still expose an identity/ownership question for review.
    ambiguous_ownership = bool(a.predicate=='hasPoint' and not missing and refs and a.explanation.strip()
        and any(r.table_kind=='points' and r.errors==['Duplicate identifier']
                and r.normalized_fields.get('point_id')==a.object_id.removeprefix('point/')
                and r.normalized_fields.get('equipment_id')==a.subject_id.removeprefix('equipment/')
                and r.normalized_fields.get('ownership_scope')
                and r.normalized_fields.get('ownership_exclusive','').lower()=='true' for r in [records[s] for s in resolved]))
    if ambiguous_ownership:reasons.append('DUPLICATE_IDENTITY_UNRESOLVED')
    supported = document['eligible'] or ambiguous_ownership
    valid_terms = a.predicate in PREDICATES and (a.predicate!='type' or a.object_id in POINTS | EQUIPMENT)
    if a.predicate=='type':
        if a.subject_id.startswith('point/'):valid_terms = valid_terms and a.object_id in POINTS
        elif a.subject_id.startswith('equipment/'):valid_terms = valid_terms and a.object_id in EQUIPMENT
    endpoints = a.subject_id in entities and (a.predicate=='type' or a.object_id in entities)
    if a.predicate!='type':
        endpoints = endpoints and a.subject_id.startswith('equipment/') and a.object_id.startswith(
            'point/' if a.predicate=='hasPoint' else 'equipment/') and a.subject_id!=a.object_id
    if not valid_terms: reasons.append('UNSUPPORTED_VOCABULARY')
    if not endpoints: reasons.append('UNRESOLVED_ENDPOINT')
    if a.status=='verified': reasons.append('AUTHENTICATED_VERIFICATION_REQUIRED')
    allowed = gate['eligible'] and valid_terms and endpoints and a.status=='supported_inference'
    status = 'supported_inference' if allowed else 'needs_review'
    if a.status in {'contradicted','rejected'}: status=a.status
    if missing or not refs or not a.explanation.strip() or (not valid_terms and a.object_id!='unknown'): status='rejected'
    return CandidateObservation(observation_uid=a.observation_uid,claim_uid=uid('claim',scope,a.subject_id,a.predicate,a.object_id),
        site_scope=scope,run_id=run_id,assertion_id=a.assertion_id,subject_id=a.subject_id,predicate=a.predicate,object_id=a.object_id,
        original_source_ids=refs,resolved_source_ids=resolved,resolved_source_uids=[records[s].source_record_uid for s in resolved],
        unresolved_source_ids=missing,inference_method=a.inference_method,explanation=a.explanation,initial_status=a.status,
        status=status,documentary_supported=supported and valid_terms,acceptance_eligible=allowed,
        reason_codes=sorted(set(reasons)),validation_results=list(a.validation_results),physical_medium=a.physical_medium,
        model_version=a.model_version,prompt_version=a.prompt_version,raw_assertion=a.model_dump())


def evaluate(assertions, source_records, scope, run_id, limits=None):
    """Pure deterministic policy. Input objects are never mutated; no network/tools."""
    limits = limits or Limits()
    limits.require(len(assertions)<=limits.max_candidate_observations,'candidate observations')
    sources=unique_source_map(source_records)
    entities = {'equipment/'+r.normalized_fields['equipment_id'] for r in sources.values()
                if r.table_kind=='equipment' and r.parse_status=='ok' and r.normalized_fields.get('equipment_id')}
    entities |= {'point/'+r.normalized_fields['point_id'] for r in sources.values()
                 if r.table_kind=='points' and r.parse_status=='ok' and r.normalized_fields.get('point_id')}
    observations=[_observe(a,sources,scope,run_id,entities) for a in assertions]
    if len({o.observation_uid for o in observations})!=len(observations): raise ValueError('OBSERVATION_ID_COLLISION')
    groups=defaultdict(list)
    for o in observations:groups[o.claim_uid].append(o)
    conflicts={}; comparisons=0
    def conflict(kind, members, reason, constraint, explanation, status='contradicted'):
        claims=sorted({o.claim_uid for o in members}); ids=sorted({o.observation_uid for o in members})
        refs=sorted({s for o in members for s in o.resolved_source_ids})
        key=uid('conflict',scope,POLICY_VERSION,kind,claims,constraint)
        if key in conflicts:
            old=conflicts[key];old.observation_uids=sorted(set(old.observation_uids)|set(ids));old.source_ids=sorted(set(old.source_ids)|set(refs))
        else:conflicts[key]=ClaimConflict(conflict_uid=key,kind=kind,status=status,reason_code=reason,
            claim_uids=claims,observation_uids=ids,source_ids=refs,constraint=constraint,explanation=explanation)
    def metadata(o):
        fields = [sources[s].normalized_fields for s in o.resolved_source_ids
                  if sources[s].table_kind==('points' if o.predicate=='hasPoint' else 'connections')
                  and (sources[s].parse_status=='ok' or (o.predicate=='hasPoint' and sources[s].errors==['Duplicate identifier']))]
        if o.predicate=='hasPoint':
            return [f for f in fields if f.get('equipment_id')==o.subject_id.removeprefix('equipment/')
                    and f.get('point_id')==o.object_id.removeprefix('point/')]
        return [f for f in fields if f.get('from_equipment')==o.subject_id.removeprefix('equipment/')
                and f.get('to_equipment')==o.object_id.removeprefix('equipment/')]
    def medium(o):
        value=(o.physical_medium or '').strip().casefold()
        if value in {'', 'unknown', 'n/a', 'not_evaluable'}:return None
        return value if any(f.get('medium','').strip().casefold()==value for f in metadata(o)) else None
    def scoped(a,b,scope_field,exclusive_field):
        return {(f[scope_field],g[scope_field]) for f in metadata(a) for g in metadata(b)
            if f.get(scope_field) and f.get(scope_field)==g.get(scope_field)
            and f.get(exclusive_field,'').lower()=='true' and g.get(exclusive_field,'').lower()=='true'}
    # Index only potentially interacting observations; every comparison consumes a budget.
    buckets=defaultdict(list)
    for o in observations:
        if not o.documentary_supported or o.status=='rejected':continue
        if o.predicate=='type':buckets[('type',o.subject_id)].append(o)
        elif o.predicate=='hasPoint':buckets[('ownership',o.object_id)].append(o)
        elif o.predicate=='feeds':buckets[('flow',*sorted((o.subject_id,o.object_id)))].append(o)
        if o.predicate=='type' and o.subject_id.startswith('point/'):
            matching=[sources[s] for s in o.resolved_source_ids if sources[s].table_kind=='points'
                      and sources[s].normalized_fields.get('point_id')==o.subject_id[6:]]
            if any(unit_problem(r.normalized_fields,o.object_id) for r in matching):
                conflict('unit_type',[o],'UNIT_TYPE_INCOMPATIBLE',{},'Explicit unit metadata conflicts with the proposed quantity; no conversion or physical fault inferred.')
    for bucket,items in sorted(buckets.items()):
        for i,a in enumerate(items):
            for b in items[i+1:]:
                comparisons+=1;limits.require(comparisons<=limits.max_conflict_comparisons,'conflict comparisons')
                if bucket[0]=='type' and a.object_id!=b.object_id:
                    if BRICK[a.object_id] in ancestors().get(b.object_id,set()) or BRICK[b.object_id] in ancestors().get(a.object_id,set()):continue
                    conflict('classification',[a,b],'COMPETING_SOURCE_CLASSIFICATIONS',{},
                        'Different source-supported classifications are unresolved alternatives; no OWL disjointness or winner assumed.',status='needs_review')
                if bucket[0]=='ownership' and a.subject_id!=b.subject_id:
                    for shared,_ in scoped(a,b,'ownership_scope','ownership_exclusive'):
                        conflict('exclusive_ownership',[a,b],'EXPLICIT_EXCLUSIVE_OWNERSHIP',{'ownership_scope':shared},
                            'Competing owners violate the same explicitly exclusive source ownership scope; duplicate tags still require identity review.',
                            status='needs_review' if 'DUPLICATE_IDENTITY_UNRESOLVED' in a.reason_codes+b.reason_codes else 'contradicted')
                if bucket[0]=='flow':
                    if a.subject_id==b.object_id and a.object_id==b.subject_id:
                        for shared,_ in scoped(a,b,'connection_scope','direction_exclusive'):
                            # Different/unknown media may be different physical circuits; do not infer opposition.
                            if medium(a) and medium(a)==medium(b):
                                conflict('flow_direction',[a,b],'EXPLICIT_OPPOSING_FLOW',{'connection_scope':shared,'medium':medium(a)},
                                    'Opposing directions claim the same source-declared one-way connection and medium.')
                    if medium(a) and medium(b) and medium(a)!=medium(b):
                        for shared,_ in scoped(a,b,'connection_scope','medium_exclusive'):
                            conflict('physical_media',[a,b],'EXPLICIT_INCOMPATIBLE_MEDIA',{'connection_scope':shared},
                                'The same explicitly single-medium connection has contradictory medium declarations.')
    by_claim=defaultdict(list)
    for c in conflicts.values():
        for key in c.claim_uids:by_claim[key].append(c)
    claims=[]
    for key,items in sorted(groups.items()):
        related=by_claim[key];first=items[0]
        status='supported_inference' if any(o.status=='supported_inference' for o in items) else 'needs_review'
        if all(o.status=='rejected' for o in items):status='rejected'
        if any(o.status=='contradicted' and o.documentary_supported for o in items):status='contradicted'
        if related:status='contradicted' if any(c.status=='contradicted' for c in related) else 'needs_review'
        reasons=sorted({r for o in items for r in o.reason_codes}|{c.reason_code for c in related})
        linked=sorted({k for c in related for k in c.claim_uids if k!=key})
        claims.append(CandidateClaim(claim_uid=key,site_scope=scope,subject_id=first.subject_id,predicate=first.predicate,
            object_id=first.object_id,observation_uids=sorted(o.observation_uid for o in items),status=status,
            reason_codes=reasons,conflicting_claim_uids=linked))
        for o in items:
            o.conflicting_claim_uids=linked
            direct=[c for c in related if o.observation_uid in c.observation_uids]
            o.reason_codes=sorted(set(o.reason_codes)|{c.reason_code for c in direct})
            if direct and o.documentary_supported and o.status!='rejected':
                o.status='contradicted' if any(c.status=='contradicted' for c in direct) else 'needs_review'
            elif related and o.status in ACCEPTED:
                o.status='needs_review';o.reason_codes=sorted(set(o.reason_codes)|{'CLAIM_CONTEXT_DISPUTE'})
    return CandidateBundle(observations=observations,claims=claims,conflicts=sorted(conflicts.values(),key=lambda c:c.conflict_uid))


def project(assertions,bundle):
    """Conservative compatibility projection: never promote an old withheld assertion."""
    claims={c.claim_uid:c for c in bundle.claims};observations={o.observation_uid:o for o in bundle.observations}
    result=[]
    for a in assertions:
        o=observations[a.observation_uid];c=claims[o.claim_uid];updates={}
        if a.status in ACCEPTED and (not o.acceptance_eligible or c.status not in ACCEPTED):updates['status']=o.status if o.status=='contradicted' else 'needs_review'
        elif o.documentary_supported and o.status=='contradicted' and a.status not in {'rejected','contradicted'}:updates['status']='contradicted'
        if updates:
            updates['validation_results'] = list(a.validation_results)+[POLICY_VERSION+': '+updates['status']+'; '+','.join(o.reason_codes)]
        result.append(a.model_copy(update=updates))
    return result


def evaluate_proposals(assertions, source_records, scope, run_id, proposal_audit, limits=None):
    """Preserve proposals skipped by the legacy compatibility assertion builder too."""
    sources=unique_source_map(source_records);all_rows=list(assertions);orphan_ids=set();links=defaultdict(list)
    represented=defaultdict(list)
    for a in assertions:
        for text in {a.explanation,a.explanation.removeprefix('Multiple provider proposals: ').removesuffix(' Invalid/missing evidence references.')} :
            represented[(a.subject_id,a.predicate,a.object_id,tuple(a.original_source_ids),text)].append(a.observation_uid)
    # Duplicate rows remain invalid for entity construction, but explicit documentary
    # exclusivity warrants an inspectable identity/ownership question, never a merge.
    for r in source_records:
        f=r.normalized_fields
        if (r.table_kind=='points' and r.errors==['Duplicate identifier'] and f.get('point_id')
                and f.get('equipment_id') and f.get('ownership_scope') and f.get('ownership_exclusive','').lower()=='true'):
            key=uid('source-ownership-observation',scope,run_id,r.source_record_uid);orphan_ids.add(key)
            all_rows.append(Assertion(assertion_id=key,observation_uid=key,subject_id='equipment/'+f['equipment_id'],
                predicate='hasPoint',object_id='point/'+f['point_id'],original_source_ids=[r.source_id],supporting_source_ids=[r.source_id],
                inference_method='direct_document',explanation='Duplicate point tag with explicit exclusive ownership scope; physical identity requires review.',
                status='needs_review',validation_results=['DUPLICATE_IDENTITY_UNRESOLVED'],run_id=run_id))
    for item in proposal_audit:
        payload=item['proposal'];kind=item['kind'];refs=item['original_source_ids']
        if kind=='relationships':
            subject,predicate,obj=payload['subject_id'],payload['predicate'],payload['object_id']
            if predicate=='type':obj=canonical_point_class(obj)
        else:
            source=sources.get(payload['source_id']);field='equipment_id' if kind=='equipment' else 'point_id'
            tag=source.normalized_fields.get(field) if source else None
            subject=('equipment/' if kind=='equipment' else 'point/')+tag if tag else 'unresolved-source/'+payload['source_id']
            predicate='type';obj=canonical_point_class(payload['equipment_class'] if kind=='equipment' else payload['point_class'])
        matched=represented.get((subject,predicate,obj,tuple(refs),payload['explanation']),[])
        if matched:
            for key in matched:links[key].append(item)
            continue
        key=uid('orphan-observation',scope,run_id,item['observation_uid']);orphan_ids.add(key)
        links[key].append(item)
        all_rows.append(Assertion(assertion_id=key,observation_uid=key,subject_id=subject,predicate=predicate,object_id=obj,
            original_source_ids=refs,supporting_source_ids=[s for s in refs if s in sources],
            inference_method=item.get('inference_method','llm'),explanation=payload['explanation'],
            model_version=item.get('model_version'),prompt_version=item.get('prompt_version'),status=item['status'],validation_results=['proposal retained outside legacy assertion ledger'],run_id=run_id))
    value=evaluate(all_rows,source_records,scope,run_id,limits)
    for o in value.observations:
        if o.observation_uid in orphan_ids:o.assertion_id=None
        o.proposal_observation_uids=[item['observation_uid'] for item in links[o.observation_uid]]
        o.original_proposals=[item['proposal'] for item in links[o.observation_uid]]
    return value
