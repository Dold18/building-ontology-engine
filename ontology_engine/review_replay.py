"""Pure documentary correction planning over frozen evidence. No provider execution.

The effective normalization view is derived, never a rewrite of raw source rows.
Only explicit documented_* fields or existing documentary metadata can ground an
edit. Free text, names, correlations and bearer possession supply no building facts.
"""
import json
import copy
from .identity import uid, unique_source_map
from .models import Assertion, SourceRecord
from .vocabulary import POINTS, EQUIPMENT, equipment_class, canonical_point_class, POINT_NAMES
from .review import fail
from .evidence import eligibility
from .review_models import hashed


def check_shape(action, index):
    selected=[index.claims[u] for u in action.affected_claim_uids]
    entities={c['subject_id'] for c in index.claims.values()} | {
        c['object_id'] for c in index.claims.values() if c['predicate']!='type'}
    subjects={c['subject_id'] for c in selected}
    points={c['object_id'] for c in selected if c['predicate']=='hasPoint'}
    for e in action.correction.edits:
        if e.subject_id not in entities: fail('REVIEW_CORRECTION','Unknown subject.')
        if e.predicate=='type':
            classes=POINTS if e.subject_id.startswith('point/') else EQUIPMENT
            if e.subject_id not in subjects or e.object_id not in classes:
                fail('REVIEW_CORRECTION','Invalid class or out-of-scope entity.')
        else:
            if (e.object_id not in entities or e.subject_id==e.object_id or
                not e.subject_id.startswith('equipment/') or
                not e.object_id.startswith('point/' if e.predicate=='hasPoint' else 'equipment/')):
                fail('REVIEW_CORRECTION','Invalid relationship endpoints.')
            if e.predicate=='hasPoint':
                if e.object_id not in points: fail('REVIEW_CORRECTION','Ownership edit outside selected points.')
            elif not any(c['subject_id']==e.subject_id and c['predicate']==e.predicate for c in selected):
                fail('REVIEW_CORRECTION','Relationship edit outside selected claims.')
        if action.correction.kind=='reject_connection' and not any(
            (c['subject_id'],c['predicate'],c['object_id'])==(e.subject_id,e.predicate,e.object_id) for c in selected):
            fail('REVIEW_CORRECTION','Rejection must name the exact original claim.')
    # Every selected claim must be accounted for, never an implicit group decision.
    if action.correction.kind!='source_clarification':
        for c in selected:
            if not any((e.subject_id==c['subject_id'] and e.predicate==c['predicate']) or
                       (e.predicate==c['predicate']=='hasPoint' and e.object_id==c['object_id'])
                       for e in action.correction.edits):
                fail('REVIEW_CORRECTION','An explicitly selected claim has no edit.')


def correction_plan(action, index, records):
    """Return a complete safe view or withhold the entire explicit action."""
    rows=[r.model_copy(deep=True) for r in records]
    by_uid={r.source_record_uid:r for r in rows}
    cited=[by_uid[u] for u in action.evidence_uids]
    if any(r.parse_status!='ok' for r in cited): return None,'INVALID_DOCUMENTARY_RECORD'
    correction=action.correction
    if correction.kind=='reject_connection':
        for e in correction.edits:
            claim=index.claims[uid('claim',index.site_scope,e.subject_id,e.predicate,e.object_id)]
            previously_supported=any(o['claim_uid']==claim['claim_uid'] and o['documentary_supported'] and
                o['initial_status'] in {'verified','supported_inference'} for o in index.observations.values())
            if claim['status'] in {'verified','supported_inference'} or previously_supported:
                # A local credential cannot negate established documentary support.
                return None,'AUTHORITY_REQUIRED_TO_REJECT_ACCEPTED_DOCUMENTATION'
        return rows,'WITHDRAW_UNACCEPTED_PROPOSAL'
    if correction.kind=='source_clarification':
        field=correction.field
        touched=False
        for r in cited:
            f=r.normalized_fields; value=f.get('documented_'+field,'')
            if not value: return None,'MISSING_DOCUMENTED_CLARIFICATION'
            if f.get(field) and f[field] not in {'unknown','unresolved',value}:
                return None,'CONTRADICTORY_DOCUMENTARY_VALUES'
            if field=='point_class' and (r.table_kind!='points' or value not in POINTS): return None,'UNSUPPORTED_DOCUMENTED_CLASS'
            if field=='point_class' and POINT_NAMES.get(f.get('description','').lower(),'unknown') not in {'unknown',value}:
                return None,'CONTRADICTORY_DOCUMENTARY_VALUES'
            if field=='equipment_type' and (r.table_kind!='equipment' or value not in EQUIPMENT): return None,'UNSUPPORTED_DOCUMENTED_CLASS'
            if field=='equipment_id' and (r.table_kind!='points' or 'equipment/'+value not in
                {c['subject_id'] for c in index.claims.values()}): return None,'UNRESOLVED_DOCUMENTED_OWNER'
            f[field]=value; touched=True
        return (rows,'DOCUMENTED_SOURCE_CLARIFICATION') if touched else (None,'MISSING_EVIDENCE')
    for edit in correction.edits:
        if edit.predicate=='type':
            point=edit.subject_id.startswith('point/')
            kind,identifier,field=('points','point_id','point_class') if point else ('equipment','equipment_id','equipment_type')
            matching=[r for r in cited if r.table_kind==kind and r.normalized_fields.get(identifier)==edit.subject_id.partition('/')[2]]
            if not matching: return None,'MISSING_ENTITY_DOCUMENTATION'
            for r in matching:
                f=r.normalized_fields
                explicit=canonical_point_class(f.get(field,'')) if point else equipment_class(f.get(field,''))
                description=POINT_NAMES.get(f.get('description','').lower(),'unknown') if point else 'unknown'
                if explicit not in {'','unknown','Point','Equipment',edit.object_id} or description not in {'unknown',edit.object_id}:
                    return None,'CONTRADICTORY_DOCUMENTARY_VALUES'
                documented=f.get('documented_'+field,'')
                if documented and documented!=edit.object_id: return None,'CONTRADICTORY_DOCUMENTARY_VALUES'
                if correction.kind=='naming_alias':
                    if f.get('naming_hint')!=correction.alias or documented!=edit.object_id:
                        return None,'ALIAS_NOT_DOCUMENTED_FOR_EACH_MEMBER'
                    if not f.get('review_cause_id') or f.get('review_cause_kind')!='naming_prefix':
                        return None,'MISSING_DOCUMENTED_ALIAS_CAUSE'
                if explicit!=edit.object_id and description!=edit.object_id and documented!=edit.object_id:
                    return None,'CLASS_NOT_DOCUMENTED'
                f[field]=edit.object_id
        elif edit.predicate=='hasPoint':
            matching=[r for r in cited if r.table_kind=='points' and r.normalized_fields.get('point_id')==edit.object_id[6:]]
            if not matching: return None,'MISSING_POINT_DOCUMENTATION'
            for r in matching:
                f=r.normalized_fields; owner=edit.subject_id[10:]
                if f.get('equipment_id') and f['equipment_id']!=owner: return None,'CONTRADICTORY_DOCUMENTARY_VALUES'
                if f.get('documented_equipment_id') and f['documented_equipment_id']!=owner: return None,'CONTRADICTORY_DOCUMENTARY_VALUES'
                if owner not in {f.get('equipment_id'),f.get('documented_equipment_id')}: return None,'OWNERSHIP_NOT_DOCUMENTED'
                f['equipment_id']=owner
        elif any(r.table_kind=='connections' and r.normalized_fields.get('from_equipment')==edit.subject_id[10:]
                 and r.normalized_fields.get('to_equipment')==edit.object_id[10:]
                 and r.normalized_fields.get('status','supported_inference') not in {'supported_inference','verified'} for r in cited):
            return None,'DOCUMENTARY_CONNECTION_WITHHELD'
        # Explicit connections are evaluated against the unchanged connection rows.
        gate=eligibility(edit.subject_id,edit.predicate,edit.object_id,[r.source_id for r in cited],
                         unique_source_map(rows),'review_document',correction.clarification)
        if not gate['eligible']: return None,','.join(gate['reason_codes'])
    return rows,'EXPLICIT_DOCUMENTARY_SUPPORT_UNCONFIRMED_AUTHORITY'


def replay_inputs(files, workspace, events):
    """Deterministic full fallback, using the same candidates/engineering infrastructure.

    Original pre-disposition assertions/proposals stay immutable in the package.
    Replaced hypotheses leave this *active* computation, with prior ledgers retained
    by the parent snapshot and complete before/after diff.
    """
    from .review import ParentIndex
    index=ParentIndex(files,workspace)
    records=[SourceRecord.model_validate(json.loads(line)) for line in files['source_records.jsonl'].splitlines()]
    base=[Assertion.model_validate(a) for a in json.loads(files['assertion_observations.json'])]
    proposals=json.loads(files['proposal_observations.json'])
    from .review import MAX_REPLAY_WORK
    if len(events)*(len(base)+len(proposals)+len(records)+len(events))>MAX_REPLAY_WORK:
        fail('REVIEW_LIMIT','Full deterministic correction replay work limit exceeded.')
    superseded={e.request.supersedes_event_id for e in events if e.request.supersedes_event_id}
    outcomes=[]; affected=set()
    from .candidates import evaluate_proposals
    from .limits import Limits
    limits=Limits.model_validate(json.loads(files['run_manifest.json'])['settings']['limits'])
    current_bundle=evaluate_proposals(base,records,workspace.site_scope,workspace.parent_run_id,proposals,limits)
    for event in events:
        action=event.request
        if event.event_id in superseded:
            outcomes.append({'event_id':event.event_id,'outcome':'superseded','reason':'EXPLICIT_SUCCESSOR'});continue
        check_shape(action,index)
        planned,reason=correction_plan(action,index,records)
        selected=set(action.affected_claim_uids)
        if planned is None:
            outcomes.append({'event_id':event.event_id,'outcome':'pending','reason':reason});continue
        # Any conflict in the original evidence touching a selected hypothesis is
        # still unresolved; selecting a documentary alternative cannot choose truth.
        if any(selected.intersection(c['claim_uids']) for c in index.conflicts) and action.correction.kind!='reject_connection':
            outcomes.append({'event_id':event.event_id,'outcome':'pending','reason':'UNRESOLVED_DOCUMENTARY_CONFLICT'});continue
        previous_records=records;previous_base=copy.deepcopy(base);previous_proposals=copy.deepcopy(proposals)
        records=planned
        if action.correction.kind=='reject_connection':
            for a in base:
                if a.claim_uid in selected: a.status='rejected';a.validation_results.append('Withdrawn by '+event.event_id)
            # Prevent retained orphan proposals from silently resurrecting an edge.
            removed_proposals={p for o in index.observations.values() if o['claim_uid'] in selected for p in o['proposal_observation_uids']}
            for p in proposals:
                if p['observation_uid'] in removed_proposals: p['status']='rejected'
        else:
            edits=list(action.correction.edits)
            if action.correction.kind=='source_clarification':
                from .review_b_models import CorrectionEdit
                field=action.correction.field
                for r in records:
                    if r.source_record_uid not in action.evidence_uids: continue
                    f=r.normalized_fields
                    if field in {'point_class','equipment_type'}:
                        edits.append(CorrectionEdit(subject_id=('point/' if field=='point_class' else 'equipment/')+
                            f['point_id' if field=='point_class' else 'equipment_id'],predicate='type',object_id=f[field]))
                    else: edits.append(CorrectionEdit(subject_id='equipment/'+f[field],predicate='hasPoint',object_id='point/'+f['point_id']))
                # Source clarification must remain scoped to the selected entities.
                for e in edits:
                    if not any((index.claims[u]['subject_id']==e.subject_id and index.claims[u]['predicate']==e.predicate) or
                        (e.predicate=='hasPoint' and index.claims[u]['object_id']==e.object_id) for u in selected):
                        fail('REVIEW_SCOPE','Clarification exceeds explicit claim scope.')
            base=[a for a in base if a.claim_uid not in selected]
            removed_proposals={p for o in index.observations.values() if o['claim_uid'] in selected for p in o['proposal_observation_uids']}
            proposals=[p for p in proposals if p['observation_uid'] not in removed_proposals]
            sources=unique_source_map(records);refs=sorted(r.source_id for r in records if r.source_record_uid in action.evidence_uids)
            for edit in edits:
                claim=uid('claim',workspace.site_scope,edit.subject_id,edit.predicate,edit.object_id)
                observation=uid('review-observation',workspace.parent_run_id,event.event_id,claim)
                base.append(Assertion(assertion_id=observation,observation_uid=observation,claim_uid=claim,
                    subject_entity_uid=uid('entity',workspace.site_scope,edit.subject_id),
                    object_entity_uid=uid('entity',workspace.site_scope,edit.object_id),
                    supporting_source_uids=[sources[s].source_record_uid for s in refs],original_source_ids=refs,
                    supporting_source_ids=refs,subject_id=edit.subject_id,predicate=edit.predicate,object_id=edit.object_id,
                    inference_method='review_document',explanation=action.correction.clarification,
                    status='supported_inference',validation_results=['Documentary interpretation from '+event.event_id],
                    eligibility=eligibility(edit.subject_id,edit.predicate,edit.object_id,refs,sources,'review_document',action.correction.clarification),
                    run_id=workspace.parent_run_id))
        trial=evaluate_proposals(base,records,workspace.site_scope,workspace.parent_run_id,proposals,limits)
        accepted={c.claim_uid for c in trial.claims if c.status in {'verified','supported_inference'}}
        prior_accepted={c.claim_uid for c in current_bundle.claims if c.status in {'verified','supported_inference'}}
        expected={uid('claim',workspace.site_scope,e.subject_id,e.predicate,e.object_id) for e in edits} if action.correction.kind!='reject_connection' else set()
        if not expected<=accepted or not (prior_accepted-selected)<=accepted:
            records=previous_records;base=previous_base;proposals=previous_proposals
            outcomes.append({'event_id':event.event_id,'outcome':'pending','reason':'DETERMINISTIC_CONFLICT_OR_UNRELATED_ACCEPTED_IMPACT'});continue
        current_bundle=trial;affected.update(selected)
        outcomes.append({'event_id':event.event_id,'outcome':'applied_rejection' if action.correction.kind=='reject_connection' else 'applied_supported_inference',
                         'reason':reason,'claim_uids':sorted(selected),'evidence_uids':sorted(action.evidence_uids)})
    entities={index.claims[u]['subject_id'] for u in affected}
    dependencies=sorted(c['claim_uid'] for c in index.claims.values() if c['subject_id'] in entities or c['object_id'] in entities)
    return {'records':[r.model_dump() for r in records], 'assertions':[a.model_dump() for a in base], 'proposals':proposals,
        'outcomes':outcomes,'dependency_analysis':{'mode':'safe_full_deterministic_recomputation',
        'reason':'Engineering rules depend on shared topology, conflicts and missing prerequisites; bounded closure is not proven.',
        'selected_claim_uids':sorted(affected),'known_dependent_claim_uids':dependencies,
        'recomputed':['classifications','claims','conflicts','review_questions','engineering_findings','accepted_graph'],
        'external_calls':0},'input_fingerprint':hashed({'parent':workspace.parent_fingerprint,'events':[e.model_dump() for e in events]})}
