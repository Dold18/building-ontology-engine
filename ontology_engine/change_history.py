"""FR-04/11: complete claim changes, including deleted observations and provenance."""
import json
from rdflib import Graph


def diff_contents(before, after):
    def load(files,name): return json.loads(files[name])
    def claims(files): return {c['claim_uid']:c for c in load(files,'candidate_claims.json')['records']}
    c1,c2=claims(before),claims(after)
    def detail(files,claim):
        if claim is None: return None
        observations=[o for o in load(files,'candidate_observations.json')['records'] if o['claim_uid']==claim['claim_uid']]
        ids={u for o in observations for u in o['resolved_source_uids']}
        locators={s for o in observations for s in o['original_source_ids']}
        return {'claim':claim,'observations':observations,'sources':[r for r in map(json.loads,files['source_records.jsonl'].splitlines())
                if r['source_record_uid'] in ids or r['source_id'] in locators]}
    changes=[]
    for identifier in sorted(set(c1)|set(c2)):
        a,b=c1.get(identifier),c2.get(identifier)
        prior,current=detail(before,a),detail(after,b)
        if prior==current:continue
        changes.append({'claim_uid':identifier,'change':'added' if a is None else 'removed' if b is None else 'changed',
                        'before':prior,'after':current})
    g1=Graph().parse(data=before['ontology.ttl'],format='turtle')
    g2=Graph().parse(data=after['ontology.ttl'],format='turtle')
    triples=lambda values:sorted([[str(t) for t in row] for row in values])
    bclasses=load(before,'classifications.json');aclasses=load(after,'classifications.json')
    return {'added_triples':triples(set(g2)-set(g1)),'removed_triples':triples(set(g1)-set(g2)),
        'claim_changes':changes,'removed_claims':[c['before'] for c in changes if c['change']=='removed'],
        'changed_claims':[c['after']['claim'] if c['after'] else c['before']['claim'] for c in changes],
        'observations':[o for c in changes for side in ('before','after') if c[side] for o in c[side]['observations']],
        'conflicts':load(after,'claim_conflicts.json')['records'],
        'classifications':{'before':bclasses,'after':aclasses},
        'validation':{'before':load(before,'validation.json'),'after':load(after,'validation.json')}}
