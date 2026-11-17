"""Project verified evidence into investigations, never into ontology assertions."""
from collections import Counter, defaultdict
import hashlib
import json

from ..engineering import EngineeringLedger
from ..identity import uid
from ..ingest import REQUIRED
from ..models import SourceRecord
from ..vocabulary import equipment_class
from .models import ArtifactEvidence, Finding, Priority, Report, VERSION, POLICY, RULE_VERSION

MAX_FINDINGS = 20_000
MAX_WORK = 1_000_000
MAX_EVIDENCE_BYTES = 16_000_000
ACCEPTED = {'supported_inference', 'verified'}
LIMITATIONS = [
    'Screening finding, not a confirmed malfunction or repair instruction; confirmed_fault is false.',
    'Source descriptions, good-quality/running flags and operating assumptions are supplied evidence, not authenticated field verification.',
    'Schema conformance, correlation, local credentials and priority scores do not establish physical connectivity or engineering authority.',
    'No measurements means operational diagnosis is not_evaluable; no probabilities, urgency, savings or human effort measurements are inferred.',
    'Report is bound to this exact immutable package. New evidence or corrected review requires a new report; no authority transfers.',
]


def generate(files, parent_fingerprint, shacl):
    load = lambda n: json.loads(files[n])
    manifest = load('run_manifest.json'); scope = manifest['settings']['building_scope']
    original = [SourceRecord.model_validate(json.loads(line)) for line in files['source_records.jsonl'].splitlines()]
    effective = [SourceRecord.model_validate(r) for r in load('replay_inputs.json')['records']] if 'replay_inputs.json' in files else original
    sources = {r.source_id:r for r in original}
    if len(sources) != len(original): raise ValueError('COMMISSIONING_SOURCE_COLLISION')
    claims = load('candidate_claims.json')['records']; conflicts = load('claim_conflicts.json')['records']
    observations = load('candidate_observations.json')['records']; questions = load('review_questions.json'); issues = load('issues.json')
    ledger = EngineeringLedger.model_validate(load('engineering_results.json'))
    if ledger.site_scope != scope or any(c['site_scope'] != scope for c in claims): raise ValueError('COMMISSIONING_SCOPE')
    work = 0
    expanded_bytes = 0
    record_sizes = {r.source_id:len(r.model_dump_json().encode()) for r in original}
    def charge(n=1):
        nonlocal work
        work += n
        if work > MAX_WORK: raise ValueError('COMMISSIONING_WORK_LIMIT')
    by_entity = defaultdict(set); by_source = defaultdict(set); by_claim = {c['claim_uid']:c for c in claims}
    for c in claims:
        charge(); by_entity[c['subject_id']].add(c['claim_uid'])
        if c['predicate'] != 'type': by_entity[c['object_id']].add(c['claim_uid'])
    obs_by_claim = defaultdict(set); assertion_claim = {}
    for o in observations:
        charge(1+len(o['original_source_ids']))
        for sid in o['original_source_ids']:
            if sid in sources: by_source[sid].add(o['claim_uid'])
        obs_by_claim[o['claim_uid']].add(o['observation_uid'])
        if o['assertion_id']: assertion_claim[o['assertion_id']] = o['claim_uid']
    conflict_by_claim = defaultdict(set)
    for c in conflicts:
        charge(1+len(c['claim_uids']))
        for key in c['claim_uids']: conflict_by_claim[key].add(c['conflict_uid'])
    questions_by_source = defaultdict(set); questions_by_claim = defaultdict(set)
    for q in questions:
        charge(1+len(q['supporting_evidence_ids'])+len(q['affected_assertion_ids']))
        for sid in q['supporting_evidence_ids']: questions_by_source[sid].add(q['review_id'])
        for a in q['affected_assertion_ids']:
            if a in assertion_claim: questions_by_claim[assertion_claim[a]].add(q['review_id'])
    issues_by_source = defaultdict(set)
    for i in issues:
        charge(1+len(i['source_ids']))
        for sid in i['source_ids']: issues_by_source[sid].add(i['issue_id'])
    dependencies = ['run_manifest.json','ontology.ttl','input_config.json','data_inventory.json','assertions.json',
        'engineering_registry.json','source_records.jsonl','classifications.json','candidate_claims.json',
        'candidate_observations.json','claim_conflicts.json','engineering_results.json','issues.json','review_questions.json']
    if 'replay_inputs.json' in files: dependencies += ['replay_inputs.json','review_basis.json','review_changes.json']
    artifacts = [ArtifactEvidence(path=n, sha256=hashlib.sha256(files[n]).hexdigest()) for n in dependencies]
    findings = {}
    def emit(rule, target, category, outcome, refs=(), observed=None, expected=(), alternatives=(), actions=(),
             significance=2, codes=(), claim_ids=(), conflict_ids=(), operational=False, version=RULE_VERSION, issue_ids=()):
        nonlocal expanded_bytes
        refs = sorted(set(refs)); keys = set(claim_ids) | by_entity.get(target,set())
        for sid in refs: keys.update(by_source[sid])
        charge(1+len(refs)+len(keys))
        if not set(refs) <= sources.keys() or not keys <= by_claim.keys(): raise ValueError('COMMISSIONING_EVIDENCE_SCOPE')
        expanded_bytes += sum(record_sizes[s] for s in refs) + len(json.dumps(observed or {},ensure_ascii=True).encode())
        if expanded_bytes > MAX_EVIDENCE_BYTES:raise ValueError('COMMISSIONING_EVIDENCE_LIMIT')
        linked_conflicts = set(conflict_ids)
        reviews = set(); original_issues = set(issue_ids); obs_ids = set()
        for key in keys:
            linked_conflicts.update(conflict_by_claim[key]); reviews.update(questions_by_claim[key]); obs_ids.update(obs_by_claim[key])
        for sid in refs: reviews.update(questions_by_source[sid]); original_issues.update(issues_by_source[sid])
        charge(len(linked_conflicts)+len(reviews)+len(original_issues)+len(obs_ids))
        evidence = [sources[s] for s in refs]; band = 2 if evidence else 1; effort = 2 if operational else 1
        context = int(operational); rank = significance
        priority = Priority(significance='engineering_review' if rank==3 else 'data_review' if rank==2 else 'prerequisite_review',
            significance_rank=rank,evidence_band=band,operational_context_band=context,effort_rank=effort,
            score=100*rank+10*band+5*context-effort,
            rationale='Policy rank reflects investigation dependency/significance, source evidence and documentary/field steps; operational criticality, likelihood, impact, urgency and human minutes are unknown.')
        identity = uid('commissioning-finding', VERSION,scope,parent_fingerprint,rule,version,target,[r.source_record_uid for r in evidence])
        finding = Finding(finding_uid=identity,site_scope=scope,target_id=target,category=category,rule_id=rule,
            rule_version=version,detection_outcome=outcome,reason_codes=sorted(set(codes)),
            status='not_evaluable' if outcome=='not_evaluable' else 'open',observed_evidence=observed or {},
            expected_condition=list(expected),source_ids=refs,source_uids=[r.source_record_uid for r in evidence],evidence=evidence,
            artifact_evidence=artifacts,claim_uids=sorted(keys),observation_uids=sorted(obs_ids),conflict_uids=sorted(linked_conflicts),
            review_question_ids=sorted(reviews),original_issue_ids=sorted(original_issues),priority=priority,
            uncertainty='Investigation required; this finding is not a confirmed equipment fault.',evidence_limitations=LIMITATIONS,
            alternative_explanations=list(alternatives),recommended_investigation=list(actions),operational_context_established=operational)
        if identity in findings and findings[identity] != finding: raise ValueError('COMMISSIONING_DUPLICATE_CONFLICT')
        findings[identity] = finding
        if len(findings)>MAX_FINDINGS: raise ValueError('COMMISSIONING_FINDING_LIMIT')
    duplicate = defaultdict(list)
    identifiers = {'points':'point_id','equipment':'equipment_id','measurements':'measurement_id','thermal_checks':'check_id','connections':'connection_id'}
    equipment = defaultdict(list); classification_by_source = defaultdict(list)
    for c in load('classifications.json'):
        for sid in c['supporting_source_ids']: classification_by_source[sid].append(c)
    for r in effective:
        charge(); f=r.normalized_fields
        key=identifiers.get(r.table_kind)
        if key and f.get(key): duplicate[(r.table_kind,f[key])].append(r)
        if r.table_kind=='equipment' and f.get('equipment_id'): equipment[f['equipment_id']].append(r)
    for (kind, identifier), rows in sorted(duplicate.items()):
        if len(rows)>1:
            emit('source.duplicate',kind+'/'+identifier,'data_quality','fail',[r.source_id for r in rows],
                observed={'table_kind':kind,'identifier':identifier,'row_count':len(rows),'effective_rows':[r.normalized_fields for r in rows]},
                expected=['One unambiguous identifier per documented entity/sample/check; all original rows preserved.'],
                alternatives=['Repeated export row','Reused identifier for different assets or samples','Conflicting source revisions'],
                actions=['Compare every cited row against schedules or historian export configuration; resolve identity before mapping or physical analysis.'])
    for r in effective:
        charge(); f=r.normalized_fields; kind=r.table_kind
        target=('point/'+f['point_id'] if kind=='points' and f.get('point_id') else
                'equipment/'+f['equipment_id'] if kind=='equipment' and f.get('equipment_id') else 'source/'+r.source_record_uid)
        missing=[k for k in REQUIRED[kind] if not f.get(k)]
        if missing:
            emit('source.identifier',target,'data_quality','fail',[r.source_id],observed={'missing_fields':missing,'effective_fields':f},
                expected=['Nonempty '+k for k in REQUIRED[kind]],alternatives=['Incomplete export','Missing source column or record identifier'],
                actions=['Review the original row and export mapping; supply identifiers from documentation rather than inventing asset names.'])
        errors=[e for e in r.errors if e!='Duplicate identifier']
        if errors:
            emit('source.parse',target,'data_quality','blocked',[r.source_id],observed={'errors':errors,'parse_status':r.parse_status},
                expected=['Parseable supported source record with complete configured columns.'],alternatives=['Malformed row/header','Unimplemented source format or spreadsheet formula'],
                actions=['Inspect the preserved raw row/cells and input mapping; correct the source export and create a new input revision.'])
        if kind=='equipment' and equipment_class(f.get('equipment_type',''))=='Equipment':
            emit('metadata.equipment',target,'data_quality','not_evaluable',[r.source_id],observed={'effective_equipment_type':f.get('equipment_type','')},
                expected=['Documented supported equipment type.'],alternatives=['Vendor abbreviation','Missing or generic schedule type'],
                actions=['Review the equipment schedule and commissioning records; clarify the documented equipment class.'],significance=1)
        if kind=='points':
            cs=classification_by_source[r.source_id]
            if not cs or any(c['point_class'] in {'unknown','Point'} or c['status'] not in ACCEPTED for c in cs):
                emit('metadata.sensor',target,'data_quality','not_evaluable',[r.source_id],observed={'effective_fields':f,'classifications':cs},
                    expected=['Unambiguous evidence-backed point class with compatible quantity/metadata.'],alternatives=['Unknown naming convention','Conflicting class or unit metadata','Invalid point row'],
                    actions=['Clarify the naming legend and documented measured quantity; inspect sensor configuration before selecting a class.'],significance=1)
            charge(len(by_entity.get(target,())))
            ownership=[by_claim[k] for k in by_entity.get(target,()) if by_claim[k]['predicate']=='hasPoint' and by_claim[k]['object_id']==target and by_claim[k]['status'] in ACCEPTED]
            owner=f.get('equipment_id','')
            if len({c['subject_id'] for c in ownership})!=1 or len(equipment.get(owner,[]))!=1 or equipment[owner][0].parse_status!='ok':
                emit('metadata.ownership',target,'data_quality','not_evaluable',[r.source_id],observed={'declared_owner':owner,'accepted_ownership_claims':ownership},
                    expected=['One uniquely documented existing owner and an accepted source-backed ownership claim.'],alternatives=['Missing mapping','Duplicate equipment ID','Unsupported alternative ownership'],
                    actions=['Review point/equipment schedules and control drawings; confirm the physical sensor wiring and owner if documentation is ambiguous.'])
        if kind=='measurements':
            fields=['equipment_id','quantity_kind','value','units','timestamp','quality','operating_state']; absent=[k for k in fields if not f.get(k)]
            if absent:
                emit('metadata.measurement',target,'data_quality','not_evaluable',[r.source_id],observed={'missing_fields':absent,'effective_fields':f},
                    expected=['Explicit '+k+' before operational interpretation' for k in fields],alternatives=['Historian omitted fields','Operating context not recorded'],
                    actions=['Check historian export, units, clock/timezone, quality and operating-state metadata; obtain synchronized context before diagnosing operation.'],significance=1)
    for c in conflicts:
        alternatives=[by_claim[k] for k in c['claim_uids']]
        emit('candidate.conflict',c['conflict_uid'],'model_conflict','fail',c['source_ids'],
            observed={'conflict':c,'competing_claims':alternatives},expected=['Consistent documentary interpretation or explicit unresolved alternatives.'],
            alternatives=['Different physical circuits or drawing revisions','Incorrect labels/direction','Incomplete ownership or equipment schedules'],
            actions=['Compare the exact cited schedules/control drawings and commissioning records; confirm the physical connection or mapping in the field when documents cannot resolve it.'],
            significance=3,codes=[c['reason_code']],claim_ids=c['claim_uids'],conflict_ids=[c['conflict_uid']])
    # A documentary eligibility pass is advisory and cannot dispose of an
    # explicitly pending connection. Preserve it as an investigation even when
    # the older topology check passed; avoid duplicating an existing non-pass check.
    unresolved_in_checks={key for check in ledger.records if check.rule_id=='topology.evidence' and check.outcome!='pass' for key in check.claim_uids}
    by_observation={o['observation_uid']:o for o in observations}
    for c in claims:
        charge()
        if c['predicate'] not in {'feeds','hasPart'} or c['status']!='needs_review' or c['claim_uid'] in unresolved_in_checks:continue
        related=[by_observation[o] for o in c['observation_uids']]
        refs={sid for o in related for sid in o['resolved_source_ids']}
        emit('candidate.unresolved_relationship',c['claim_uid'],'model_conflict','not_evaluable',refs,
            observed={'pending_claim':c,'observations':related},expected=['Documented endpoint identity, relationship and direction before relying on a physical connection.'],
            alternatives=['An explicitly unconfirmed connection hypothesis','Incomplete or superseded equipment/drawing documentation'],
            actions=['Inspect the exact cited schedule/drawing and pending interpretation; clarify endpoint identity and confirm the physical connection if documentary review cannot establish it. A schema or advisory pass is insufficient.'],
            claim_ids=[c['claim_uid']],codes=c['reason_codes'])
    rules={r.rule_id:r for r in ledger.registry}
    range_not_established=0
    for check in ledger.records:
        charge()
        if check.outcome=='pass': continue
        if check.rule_id=='measurement.range' and check.outcome=='not_evaluable' and check.reason_codes==['RANGE_NOT_ESTABLISHED']:
            # The released rule has no applicable range predicate here (e.g.
            # power). Preserve the exact check without inventing a data problem
            # or a request to calibrate an otherwise valid measurement.
            range_not_established+=1
            continue
        definition=rules[check.rule_id]; details=check.details
        operational=(check.rule_id=='thermal.water_balance' and check.outcome=='fail' and details.get('equipment_state')=='running'
            and details.get('measurement_quality')=='good' and details.get('timestamp_alignment')=='exact UTC' and details.get('samples',0)>=2)
        category='potential_operational_issue' if operational else 'model_conflict' if check.rule_id=='topology.evidence' else 'data_quality'
        rank=3 if operational or check.rule_id=='topology.evidence' and check.outcome=='fail' else 1 if check.outcome=='not_evaluable' else 2
        if operational:
            alternatives=['Flow/temperature/thermal-power sensor calibration or wiring error','Incorrect documented fluid properties, sign or steady-state assumption','Unrecorded storage/transient behavior; possible operational abnormality requires investigation']
            actions=['Review supplied assumptions, commissioning records, synchronized trends and channel configuration first.',
                'Arrange independent checks of flow, inlet/outlet temperatures and thermal-power calibration/wiring under a documented stable operating condition; investigate operation if the inconsistency persists. No automatic control or repair is prescribed.']
        elif check.rule_id=='topology.evidence':
            alternatives=['Incomplete or contradictory drawings','Proposed relationship lacks documentary topology; similarity/correlation is insufficient']
            actions=['Inspect cited equipment/control drawings and competing claims; confirm the physical connection/ownership before analysis.']
        else:
            alternatives=['Missing or incorrect units, timestamp, quality, channel role or export fields','Incomplete telemetry or unsupported operating assumptions; this does not establish a malfunction']
            actions=['Review exact cited source records, required inputs and channel units/configuration; verify calibration or wiring only after documentary review warrants a field check.']
        conditions=definition.required_inputs+definition.time_prerequisites+definition.operating_prerequisites
        if check.rule_id=='measurement.range':
            conditions+=['Absolute temperature must be at least 0 K; negative flow is excluded only when explicitly declared unsigned. No other range is assumed.']
        if check.rule_id=='measurement.clock':
            actions=['Check the cited historian timestamp, explicit UTC offset or timezone and daylight-saving ambiguity; align clocks before comparing measurements.']
        emit('engineering.'+check.rule_id,check.target_id,category,check.outcome,check.source_ids,
            observed={'engineering_result_uid':check.result_uid,'details':details,'reason_codes':check.reason_codes},
            expected=conditions,
            alternatives=alternatives,actions=actions,significance=rank,codes=check.reason_codes,
            claim_ids=check.claim_uids,conflict_ids=check.conflict_uids,operational=operational,version=check.rule_version)
    covered_issues={i for f in findings.values() for i in f.original_issue_ids}
    for i in issues:
        if i['issue_id'] in covered_issues: continue
        # Earlier issue categories alone cannot authenticate a potential operational finding.
        category='model_conflict' if i['category']=='model_conflict' else 'data_quality'
        refs=[s for s in i['source_ids'] if s in sources]
        emit('source.issue',i['issue_id'],category,'not_evaluable',refs,
            observed={'original_issue':i,'original_text_authority':'Unconfirmed prior issue text; not an authoritative commissioning instruction.',
                      'unresolved_source_ids':sorted(set(i['source_ids'])-sources.keys())},
            expected=['Resolve the original source-backed issue before relying on the affected analysis.'],
            alternatives=['Incomplete or inconsistent source metadata','An unresolved model interpretation rather than a proven equipment fault'],
            actions=['Inspect the original issue evidence against cited schedules, drawings or export/channel configuration; clarify missing prerequisites before considering field calibration or wiring checks. No automatic controls or repair instruction.'],significance=1,issue_ids=[i['issue_id']])
    measurement_count=sum(r.table_kind=='measurements' for r in original)
    if not measurement_count:
        emit('operational.telemetry','building','data_quality','not_evaluable',observed={'measurement_record_count':0,'input_files':sorted(manifest['input_hashes'])},
            expected=['Time-aligned measurements, documented units/quality and operating context before operational screening.'],
            alternatives=['Telemetry was not exported or not available; equipment condition remains unknown.'],
            actions=['Obtain a scoped historian export with timestamps/timezone, units, quality and operating state; do not diagnose equipment from missing measurements.'],significance=1)
    if shacl['full_brick_shacl']!='passed' or shacl['released_vocabulary']!='passed':
        emit('validation.brick_shacl','accepted_graph','model_conflict','fail',observed=shacl,
            expected=['Released pinned Brick 1.4.4 vocabulary/SHACL conformance.'],alternatives=['Incomplete equipment class or graph metadata','Incompatible modeled relationship'],
            actions=['Review the accepted graph and source-backed model with the pinned validator; a validation failure does not identify a physical fault.'],significance=3)
    ordered=sorted(findings.values(),key=lambda f:(-f.priority.score,f.finding_uid))
    summary={'finding_count':len(ordered),'by_category':dict(sorted(Counter(f.category for f in ordered).items())),
        'by_status':dict(sorted(Counter(f.status for f in ordered).items())), 'by_significance':dict(sorted(Counter(f.priority.significance for f in ordered).items())),
        'check_count':len(ledger.records),'check_outcomes':dict(sorted(Counter(c.outcome for c in ledger.records).items())),
        'confirmed_faults':0,'evidence_truth':'not_evaluable','fault_diagnosis':'not_performed','new_model_calls':0,
        'measurement_records':measurement_count,'selection_operations':work,'actual_brick_shacl':shacl['full_brick_shacl'],
        'range_not_established_checks_retained_outside_queue':range_not_established,
        'reviewer_minutes':'NOT_MEASURED','labor_savings':'NOT_MEASURED'}
    return Report(report_id=uid('commissioning-report',VERSION,POLICY,scope,parent_fingerprint),site_scope=scope,
        parent_run_id=manifest['run_id'],parent_fingerprint=parent_fingerprint,synthetic=manifest['synthetic'],summary=summary,
        findings=ordered,checks=[c.model_dump() for c in ledger.records],limitations=LIMITATIONS)
