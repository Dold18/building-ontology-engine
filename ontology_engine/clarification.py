"""FR-04/05/07: source-declared root causes, exact joins and reproducible priority.

No names/prefix similarity creates a group. A source must explicitly supply both
review_cause_id and review_cause_kind. These are unconfirmed source declarations,
not authenticated engineering findings. Every original question remains embedded.
"""
from collections import defaultdict
from .identity import uid
from .review import fail

KINDS = {'naming_prefix', 'ownership_mapping', 'schedule_conflict', 'mechanical_connection'}
SIGNIFICANCE = {'naming_prefix':1, 'ownership_mapping':2, 'schedule_conflict':3, 'mechanical_connection':4,
                'individual':1}


def consolidate(index, scope):
    from .review import MAX_REPLAY_WORK
    work=len(index.questions)*(len(index.observations)+len(index.sources)+len(index.conflicts)+len(index.claims))
    if work>MAX_REPLAY_WORK:fail('REVIEW_LIMIT','Root-cause consolidation work limit exceeded.')
    groups = defaultdict(list)
    for q in index.questions.values():
        if q['review_status'] != 'open': continue
        detail = index.target('question', q['review_id'])
        declarations = {(r['normalized_fields'].get('review_cause_kind'),
                         r['normalized_fields'].get('review_cause_id')) for r in detail['sources']
                        if r['parse_status']=='ok' and r['normalized_fields'].get('review_cause_kind') in KINDS
                        and r['normalized_fields'].get('review_cause_id','').strip()}
        # Multiple distinct declarations do not establish a single underlying cause.
        key = next(iter(declarations)) if len(declarations)==1 else ('individual',q['review_id'])
        groups[key].append((q,detail))
    result=[]
    for (kind,cause), members in sorted(groups.items()):
        claims={c['claim_uid']:c for _,d in members for c in d['claims']}
        obs={o['observation_uid']:o for _,d in members for o in d['observations']}
        sources={r['source_record_uid']:r for _,d in members for r in d['sources']}
        conflicts={c['conflict_uid']:c for _,d in members for c in d['conflicts']}
        alternatives={c['claim_uid']:c for _,d in members for c in d['alternatives']}
        alternative_observations=[o for o in index.observations.values() if o['claim_uid'] in alternatives and o['observation_uid'] not in obs]
        alternative_sources={u for o in alternative_observations for u in o['resolved_source_uids']}
        # Entity and citation dependency closure is descriptive. Replay uses a safe
        # full fallback since current engineering rules also depend on absent inputs.
        entities={c[k] for c in claims.values() for k in ('subject_id','object_id') if c['predicate']!='type' or k=='subject_id'}
        dependent={c['claim_uid'] for c in index.claims.values() if c['subject_id'] in entities or
                   (c['predicate']!='type' and c['object_id'] in entities)}
        complete=sum(bool(o['resolved_source_uids']) and not o['unresolved_source_ids'] for o in obs.values())
        completeness=2 if obs and complete==len(obs) else 1 if complete else 0
        effort=1 if len(claims)<=8 and len(sources)<=8 else 2 if len(claims)<=32 and len(sources)<=32 else 3
        score=100*SIGNIFICANCE[kind]+10*min(len(dependent),100)+5*completeness-3*effort
        result.append({'group_id':uid('root-cause',scope,kind,cause), 'site_scope':scope,'cause_kind':kind,
            'cause_id':cause, 'basis':'explicit_source_declaration' if kind!='individual' else 'unconsolidated_question',
            'question':'Clarify documented cause '+cause if kind!='individual' else members[0][0]['question_or_test'],
            'questions':[q for q,_ in sorted(members,key=lambda m:m[0]['review_id'])],
            'claims':[claims[k] for k in sorted(claims)], 'observations':[obs[k] for k in sorted(obs)],
            'sources':[sources[k] for k in sorted(sources)],'conflicts':[conflicts[k] for k in sorted(conflicts)],
            'alternatives':[alternatives[k] for k in sorted(alternatives)],
            'alternative_observations':sorted(alternative_observations,key=lambda o:o['observation_uid']),
            'alternative_sources':[index.sources[u] for u in sorted(alternative_sources)],
            'dependent_claim_uids':sorted(dependent),'priority_score':score,
            'priority_factors':{'engineering_significance':SIGNIFICANCE[kind], 'dependent_assertions':len(dependent),
                'evidence_completeness_band':completeness,'reviewer_effort_band':effort},
            'reviewer_time_savings':'NOT_MEASURED'})
    return sorted(result,key=lambda g:(-g['priority_score'],g['group_id']))


def make_event(service, action, index, events, timestamp):
    from .review_models import EvidenceReference, hashed
    from .review_b_models import Event
    from .review import ZERO, MAX_EVENTS
    service.replay_budget(index,len(events)+1)
    w=service.workspace
    if action.workspace_id!=w.workspace_id or action.site_scope!=w.site_scope: fail('REVIEW_SCOPE','Foreign scope.')
    if action.parent_fingerprint!=w.parent_fingerprint: fail('REVIEW_STALE_EVIDENCE','Changed parent fingerprint.')
    if action.actor_id!=w.reviewer.actor_id or w.reviewer.role!='reviewer': fail('REVIEW_UNAUTHORIZED','Invalid actor.')
    if action.expected_revision!=len(events): fail('REVIEW_STALE_REVISION','Workspace revision changed.')
    if len(events)>=MAX_EVENTS: fail('REVIEW_LIMIT','Event limit reached.')
    related=index.target(action.target_kind,action.target_id)
    selected=set(action.affected_claim_uids)
    if not selected <= {c['claim_uid'] for c in related['claims']}: fail('REVIEW_SCOPE','Explicit claim scope is outside target.')
    evidence={r['source_record_uid']:r for r in related['sources']}
    if not set(action.evidence_uids)<=set(evidence): fail('REVIEW_EVIDENCE','Unrelated evidence.')
    from .review_replay import check_shape
    check_shape(action,index)
    last=next((e for e in reversed(events) if set(e.claim_uids)&selected),None)
    if last and set(last.claim_uids)!=selected:
        fail('REVIEW_CONFLICT','A replacement must preserve the exact explicit claim scope.')
    if action.supersedes_event_id!=(last.event_id if last else None): fail('REVIEW_CONFLICT','Explicit supersession required.')
    return Event(event_id=uid('review-event',w.workspace_id,action.action_id), revision=len(events)+1,
        timestamp=timestamp,request=action,request_sha256=hashed(action.model_dump()),reviewer=w.reviewer,
        reason_codes=['ENGINEERING_AUTHORITY_NOT_ESTABLISHED','GRAPH_APPLICATION_DEFERRED'],
        claim_uids=sorted(selected),observation_uids=sorted(o['observation_uid'] for o in related['observations'] if o['claim_uid'] in selected),
        conflict_uids=sorted(c['conflict_uid'] for c in related['conflicts']),
        evidence=[EvidenceReference(**{k:evidence[u][k] for k in EvidenceReference.model_fields}) for u in sorted(action.evidence_uids)],
        previous_event_sha256=hashed(events[-1].model_dump()) if events else ZERO)
