"""Bounded traversal of accepted assertions and projection of existing engineering rules."""
from collections import defaultdict, deque
import hashlib
import json

from ..engineering import EngineeringLedger, class_quantity
from ..identity import uid
from ..models import Assertion, SourceRecord
from ..pipeline import entity_iri
from ..vocabulary import SCHEDULE_CLASSES, LOCATIONS
from .models import Report, Query, Assessment, VERSION, POLICY

ACCEPTED = {'verified', 'supported_inference'}
MAX_WORK = 1_000_000
MAX_OUTPUT_BYTES = 16_000_000
LIMITATIONS = [
    'Package verification and Brick/SHACL conformance establish integrity/schema only, not physical truth.',
    'Accepted supported_inference remains unconfirmed. No status is promoted and no model or control is executed.',
    'Service follows only explicit accepted directed feeds edges to a declared HVAC_Zone. A deterministic shortest witness is shown; alternative paths may exist. No containment, inverse-flow or correlation inference.',
    'Point ownership does not establish valid telemetry. a declared meter component means only the explicit documented equipment association, not exclusive electrical load, consumption or demand attribution.',
    'Ready means the existing sensible-water rule has enough supplied context to compute, even if its screening result fails. It is not field validation or fault diagnosis.',
    'The bounded rule applies only to documented steady-state sensible-water heat exchangers. Other HVAC/electric/grid analyses are unevaluated, not proved impossible.',
    'No savings, efficiency, demand reduction, energy consumption or grid impact is estimated. Source quality/running flags and fluid assumptions are supplied, not field authenticated.',
    'Absence of an accepted path/meter/point means missing knowledge, not proof that the physical component or connection is absent.',
]


def generate(files, parent_fingerprint, shacl):
    load = lambda n: json.loads(files[n])
    if shacl['full_brick_shacl'] != 'passed' or shacl['released_vocabulary'] != 'passed':raise ValueError('READINESS_GRAPH_INVALID')
    manifest = load('run_manifest.json'); scope = manifest['settings']['building_scope']
    original = [SourceRecord.model_validate_json(line) for line in files['source_records.jsonl'].splitlines()]
    effective = [SourceRecord.model_validate(r) for r in load('replay_inputs.json')['records']] if 'replay_inputs.json' in files else original
    claims = load('candidate_claims.json')['records']; observations = load('candidate_observations.json')['records']
    conflicts = load('claim_conflicts.json')['records']
    ledger = EngineeringLedger.model_validate(load('engineering_results.json'))
    if ledger.site_scope != scope or any(c['site_scope'] != scope for c in claims): raise ValueError('READINESS_SCOPE')
    work = 0
    def charge(n=1):
        nonlocal work
        work += n
        if work > MAX_WORK: raise ValueError('READINESS_WORK_LIMIT')
    assertions = [Assertion.model_validate(a) for a in load('assertions.json')]
    accepted = [a for a in assertions if a.status in ACCEPTED]
    types = defaultdict(set); type_claims = defaultdict(set)
    by_claim = {c['claim_uid']:c for c in claims}
    for a in accepted:
        charge()
        if by_claim[a.claim_uid]['status'] not in ACCEPTED: raise ValueError('READINESS_CLAIM_STATUS')
        if a.predicate == 'type': types[a.subject_id].add(a.object_id); type_claims[a.subject_id].add(a.claim_uid)
    assets = sorted(e for e,t in types.items() if t & SCHEDULE_CLASSES and not t & LOCATIONS)
    zones = sorted(e for e,t in types.items() if t & LOCATIONS)
    meters = {e for e,t in types.items() if t & {'Meter','Electrical_Meter'}}
    asset_set=set(assets); zone_set=set(zones)
    relation = defaultdict(list); incoming = defaultdict(list)
    for a in accepted:
        charge()
        if a.predicate != 'type': relation[a.predicate].append(a)
        if a.predicate == 'feeds' and a.subject_id in asset_set and a.object_id in asset_set|zone_set: incoming[a.object_id].append(a)
    pending = [c for c in claims if c['status'] not in ACCEPTED and c['predicate'] != 'type']
    def proof(edges):
        keys = {a.claim_uid for a in edges}
        for a in edges: keys.update(type_claims[a.subject_id]); keys.update(type_claims[a.object_id])
        charge(len(keys)+len(edges))
        return sorted(keys)
    def answer(entity, edges):
        return dict(entity_id=entity,iri=str(entity_iri(manifest['settings']['namespace'],entity)),
            types=sorted(types[entity]),claim_uids=proof(edges),
            path=[dict(subject_id=a.subject_id,predicate=a.predicate,object_id=a.object_id,
                       assertion_id=a.assertion_id,claim_uid=a.claim_uid,status=a.status,
                       source_ids=a.supporting_source_ids,source_uids=a.supporting_source_uids) for a in edges])
    queries = []
    def query(kind,target,answers,withheld):
        charge(1+len(answers)+len(withheld))
        state = 'ready' if answers else 'contradicted' if any(c['status']=='contradicted' for c in withheld) else 'unknown' if kind=='serves_zone' else 'missing'
        queries.append(Query(query_id=uid('readiness-query',VERSION,scope,parent_fingerprint,kind,target),kind=kind,target_id=target,state=state,
            answers=answers,withheld_claim_uids=sorted(c['claim_uid'] for c in withheld),
            limitation=LIMITATIONS[2 if kind=='serves_zone' else 3]))
    for zone in zones:
        # Reverse BFS selects one stable shortest accepted witness per entity; cycles are bounded.
        paths={zone:[]}; queue=deque([zone])
        while queue:
            target=queue.popleft()
            for a in sorted(incoming[target],key=lambda x:(x.subject_id,x.claim_uid,x.assertion_id)):
                charge()
                if a.subject_id not in paths:
                    paths[a.subject_id]=[a]+paths[target];queue.append(a.subject_id)
        withheld=[c for c in pending if c['predicate']=='feeds' and (c['object_id'] in paths or c['subject_id'] in paths)]
        query('serves_zone',zone,[answer(e,paths[e]) for e in sorted(paths) if e in assets],withheld)
    for asset in assets:
        query('asset_points',asset,[dict(answer(a.object_id,[a]),quantity_kinds=sorted({q for t in types[a.object_id] if (q:=class_quantity(t))}))
              for a in sorted(relation['hasPoint'],key=lambda a:(a.object_id,a.assertion_id)) if a.subject_id==asset],
              [c for c in pending if c['predicate']=='hasPoint' and c['subject_id']==asset])
        query('equipment_meters',asset,[answer(a.object_id,[a]) for a in sorted(relation['hasPart'],key=lambda a:(a.object_id,a.assertion_id))
              if a.subject_id==asset and a.object_id in meters],
              [c for c in pending if (c['predicate']=='hasPart' and c['subject_id']==asset and c['object_id'] in meters or c['predicate']=='meters' and c['object_id']==asset)])
        charge(len(relation['hasPoint'])+len(relation['hasPart'])+2*len(pending))
    checks_by_target = defaultdict(list)
    for c in ledger.records:
        charge()
        if c.rule_id=='thermal.water_balance': checks_by_target[c.details.get('equipment_id','')].append(c)
    rows = {r.source_id:r for r in effective}
    point_rows = defaultdict(list); readings = defaultdict(list)
    for r in effective:
        charge()
        if r.table_kind=='points':point_rows[r.normalized_fields.get('point_id','')].append(r)
        if r.table_kind=='measurements':readings[r.normalized_fields.get('point_id','')].append(r)
    assessments=[]
    for asset in sorted(set(assets)|{'equipment/'+e for e in checks_by_target if e}):
        eid=asset.removeprefix('equipment/'); thermal=checks_by_target.get(eid,[])
        if not thermal:
            unknown = not types[asset] or types[asset]=={'Equipment'}
            state='unknown' if unknown else 'not_evaluable' if 'Heat_Exchanger' in types[asset] else 'inapplicable'
            assessments.append(Assessment(target_id=asset,state=state,reason_codes=['NO_THERMAL_CHECK_CONFIGURATION'] if state=='not_evaluable' else ['APPLICABILITY_UNKNOWN' if unknown else 'OUTSIDE_BOUNDED_WATER_RULE'],
                prerequisites=[dict(requirement='Explicit thermal_checks configuration and source-backed prerequisites',state='missing' if state=='not_evaluable' else state)],
                next_action='Supply documented asset identity, applicable water-side configuration, point roles/ownership, fluid properties, aligned good-quality running telemetry and units. Other analyses require their own requirements.'))
            continue
        for c in thermal:
            charge(len(c.source_ids)+len(c.reason_codes))
            params=rows[c.target_id].normalized_fields
            prerequisites=[]
            for role in ('inlet','outlet','flow','power'):
                pid=params.get(role+'_point_id',''); pr=point_rows[pid]; samples=readings[pid]
                ownership=[a for a in relation['hasPoint'] if a.subject_id==asset and a.object_id=='point/'+pid]
                role_codes=[code for code in c.reason_codes if role.upper() in code]
                refs={x.source_id for x in pr+samples}
                relevant=[r for r in ledger.records if set(r.source_ids)&refs and r.rule_id in {'measurement.dimension','measurement.clock','measurement.range'}]
                role_bad=any(r.outcome in {'blocked','fail','error'} for r in relevant)
                role_unknown=any(r.outcome=='not_evaluable' and r.reason_codes!=['RANGE_NOT_ESTABLISHED'] for r in relevant)
                state='missing' if not pr or not samples or any(code.startswith('INSUFFICIENT_') for code in role_codes) else 'blocked' if not ownership or role_bad else 'not_evaluable' if role_unknown else 'unknown' if role_codes else 'ready'
                prerequisites.append(dict(requirement=role+'_point_and_measurements',state=state,point_id=pid,
                    point_source_ids=[r.source_id for r in pr],measurement_source_ids=[r.source_id for r in samples],
                    supplied_sample_count=len(samples),accepted_ownership_claim_uids=proof(ownership),reason_codes=role_codes,
                    relevant_check_uids=[r.result_uid for r in ledger.records if set(r.source_ids)&{x.source_id for x in pr+samples}],
                    note='Counts are supplied rows, not valid/aligned/in-window counts. See exact engineering checks.'))
                charge(len(pr)+len(samples)+len(ledger.records)+len(relation['hasPoint']))
            successful=c.outcome in {'pass','fail'}
            # Do not recompute physics or infer that an untested requirement passed.
            definition=next(r for r in ledger.registry if r.rule_id==c.rule_id)
            prerequisites.append(dict(requirement='Documented physical, unit, time and operating context',state='ready' if successful else 'not_evaluable',
                required_inputs=definition.required_inputs,unit_dimensions=definition.unit_dimensions,time_prerequisites=definition.time_prerequisites,
                operating_prerequisites=definition.operating_prerequisites,supplied_parameters=params,source_ids=c.source_ids,
                engineering_result_uid=c.result_uid,reason_codes=[] if successful else c.reason_codes))
            dependent_claims={a.claim_uid for a in relation['hasPoint'] if a.subject_id==asset}
            dependent_claims.update(uid('claim',scope,asset,'hasPoint','point/'+params.get(role+'_point_id','')) for role in ('inlet','outlet','flow','power'))
            contradicted=[k for k in dependent_claims if k in by_claim and by_claim[k]['status']=='contradicted']
            invalid=any(code in {'INVALID_SOURCE_RECORD','INVALID_MEASUREMENT','PHYSICALLY_INVALID_MEASUREMENT','ACCEPTED_OWNERSHIP_REQUIRED','DOCUMENTED_ROLE_OR_OWNERSHIP_MISMATCH','MEASUREMENT_EQUIPMENT_MISMATCH','THERMAL_POWER_SENSOR_REQUIRED','EQUIPMENT_IDENTITY_UNRESOLVED'} for code in c.reason_codes)
            state='blocked' if contradicted or invalid or any(p['state']=='blocked' for p in prerequisites) or c.outcome in {'blocked','error'} else 'ready' if successful else 'not_evaluable'
            if contradicted:prerequisites.append(dict(requirement='Uncontradicted accepted ownership',state='contradicted',claim_uids=contradicted))
            calculation=dict(c.details,engineering_result_uid=c.result_uid,source_ids=c.source_ids,source_uids=c.source_uids,
                claim_uids=c.claim_uids,screening_outcome=c.outcome) if successful and state=='ready' else None
            assessments.append(Assessment(target_id=asset,check_source_id=c.target_id,state=state,reason_codes=c.reason_codes,
                prerequisites=prerequisites,engineering_result=c.model_dump(),calculation=calculation,
                next_action='Inspect the cited supplied assumptions and readings; this bounded result is not efficiency, savings or a verified fault.' if calculation else
                'Resolve listed reason codes using cited configuration/point/measurement rows and accepted ownership. Supply missing flow, inlet/outlet and thermal-power readings with compatible units, aligned UTC window/cadence, good quality and running context.'))
    report=Report(report_id=uid('readiness-report',VERSION,POLICY,scope,parent_fingerprint),site_scope=scope,parent_run_id=manifest['run_id'],
        parent_fingerprint=parent_fingerprint,synthetic=manifest['synthetic'],queries=queries,assessments=assessments,
        entities=[dict(entity_id=e,iri=str(entity_iri(manifest['settings']['namespace'],e)),types=sorted(types[e]),claim_uids=sorted(type_claims[e])) for e in sorted(types)],
        claims=claims,observations=observations,conflicts=conflicts,evidence=original,
        artifact_hashes={n:hashlib.sha256(raw).hexdigest() for n,raw in sorted(files.items())},input_hashes=manifest['input_hashes'],
        engineering_registry=[r.model_dump() for r in ledger.registry],engineering_checks=[r.model_dump() for r in ledger.records],limitations=LIMITATIONS)
    if len(report.model_dump_json().encode())>MAX_OUTPUT_BYTES:raise ValueError('READINESS_OUTPUT_LIMIT')
    return report
