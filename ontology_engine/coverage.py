"""Measured coverage flags with explicit denominators; never accuracy from model guesses."""
from collections import Counter

VERSION='inference-coverage-v1'


def coverage(records,assertions,bundle,plan=None,results=None):
    results=results or {'batches':[]}
    attempted=set();interpreted=set();unsupported=set();abstained=set()
    for batch in results['batches']:
        response=batch['response']
        for entry in batch['abstention_audit']:
            if entry['status']!='rejected':abstained.add(entry['abstention']['target_source_id'])
        for row in batch['candidate_audit']:
            p=row['proposal'];target=p['target_source_id']
            if row['status']=='rejected':unsupported.add(target)
            elif p['object_id']!='unknown':interpreted.add(target)
    if plan:attempted=set(plan.target_source_ids)
    accepted={s for a in assertions if a.status in {'verified','supported_inference'} for s in a.supporting_source_ids}
    withheld={s for a in assertions if a.status not in {'verified','supported_inference'} for s in a.supporting_source_ids}
    valid={r.source_id for r in records if r.parse_status=='ok' and r.table_kind in {'points','equipment','connections'}}
    unresolved=(valid-interpreted)|abstained
    not_evaluable={r.source_id for r in records}-attempted
    flags={'attempted':attempted,'interpreted':interpreted,'accepted':accepted,'withheld':withheld,
           'unresolved':unresolved,'unsupported':unsupported,'not_evaluable':not_evaluable}
    by_type={}
    for kind in ('points','equipment','connections','measurements','thermal_checks','unknown'):
        ids={r.source_id for r in records if r.table_kind==kind}
        by_type[kind]={'denominator_records':len(ids),'valid_records':len(ids & valid),**{k:len(ids & v) for k,v in flags.items()}}
    return {'schema_version':VERSION,'mode':'contextual' if plan else 'legacy',
        'record_flags_overlap':True,'definitions':{
            'attempted':'unique targets in completed bounded context batches',
            'interpreted':'targets with at least one usable non-unknown model candidate, not correctness',
            'accepted':'records cited by accepted deterministic/assertion observations; not model authority',
            'withheld':'records cited by withheld compatibility observations',
            'unresolved':'valid records without a usable model interpretation or explicitly abstained',
            'unsupported':'targets of rejected scope/citation/schema-term candidates',
            'not_evaluable':'records not attempted by contextual inference (including invalid/unsupported rows)'},
        'source_types':by_type,'denominator_records':len(records),
        'record_totals':{k:len({r.source_id for r in records}&v) for k,v in flags.items()},
        'claim_denominator':len(bundle.claims),'claim_statuses':dict(Counter(c.status for c in bundle.claims)),
        'assertion_denominator':len(assertions),'assertion_statuses':dict(Counter(a.status for a in assertions)),
        'missing_source_types':[k for k in ('equipment','connections') if not any(r.table_kind==k and r.parse_status=='ok' for r in records)],
        'live_semantic_performance':'NOT_TESTED','physics':'not_evaluable'}
