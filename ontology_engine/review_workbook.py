"""Consolidated reviewer workbook and blind evaluation sample (TASK_006 Phase 5A). Local only.

Workbook: one row per human decision, in priority order:
  P1 traced drawn connections and traced system memberships (the first candidates eligible as physical edges)
  P2 drawing schedule readings, one decision per schedule table (each equipment it names is listed)
  P3 signatures the model abstained on, one decision per chunk of at most 64 explicitly listed rows that share name, units,
     BAS kind and equipment family
  P4 contradicted model classes (a hard deterministic check withheld the class), grouped by signature in the same way
  P5 open AHU / exhaust topology questions (answers recorded, nothing applied)

A group decision never reaches beyond the rows or equipment it lists, and `expand` turns it into one decision record per
affected item, each keeping that item's own source rows. Nothing here decides anything; decisions come only from a human.

Evaluation sample: a stratified, deterministic sample of signatures with a BLIND worksheet (no model output shown) and a
separate restricted key. Accuracy stays NOT_MEASURED until a human fills the labels.
"""
import hashlib
import json
import math
import re
from collections import Counter, defaultdict

from .errors import EngineError
from .vocabulary import POINTS, point_role

VERSION = 'review-workbook-v1'
MAX_ROWS_PER_DECISION = 64
DECISIONS = {'connection': ('confirm', 'reject', 'unknown'), 'membership_group': ('confirm_all', 'reject_all', 'confirm_except', 'unknown'),
             'schedule_reading': ('confirm_all', 'reject_all', 'confirm_except', 'unknown'), 'point_class': ('set_class', 'unsure'),
             'contradicted_class': ('reject', 'set_class', 'unsure'), 'question': ('answer', 'unsure')}


def _chunks(items, n=MAX_ROWS_PER_DECISION):
    return [items[i:i + n] for i in range(0, len(items), n)] or [[]]


def build_workbook(bg):
    """Decision items from a built BuildingGraph (with drawing evidence, trace and model results applied)."""
    items = []

    def add(priority, kind, key, question, covers, evidence, **extra):
        items.append({'decision_id': 'D%04d' % (len(items) + 1), 'priority': priority, 'kind': kind, 'item_key': key, 'question': question,
                      'covers': covers, 'covers_count': len(covers), 'evidence': evidence, 'allowed_decisions': list(DECISIONS[kind]),
                      'decision': '', 'class': '', 'except': '', 'note': '', **extra})
    register = {r['source_id']: r for r in bg.trace_register}
    # P1 traced connections, one decision each (each is a distinct physical claim)
    for a in bg.connections:
        if a.status != 'needs_review':
            continue
        refs = [ref for s in a.supporting_source_ids if s in register for ref in register[s]['refs']]
        add('P1', 'connection', 'connection:%s|%s' % (a.subject_id.split('/', 1)[1], a.object_id.split('/', 1)[1]),
            'Does drawn piping/duct connect %s and %s (no other equipment between them)? Check the path region crop.' % (
                a.subject_id.split('/', 1)[1], a.object_id.split('/', 1)[1]),
            [a.subject_id, a.object_id], {'explanation': a.explanation, 'drawing_refs': refs[:6], 'trace_sources': a.supporting_source_ids})
    # P1 traced memberships, grouped by system (each member listed; decision applies to listed members only)
    by_system = defaultdict(list)
    for a in bg.traced_memberships:
        if a.status == 'needs_review':
            by_system[a.subject_id].append(a)
    for system, ms in sorted(by_system.items()):
        for chunk in _chunks(sorted(ms, key=lambda a: a.object_id)):
            refs = [ref for a in chunk for s in a.supporting_source_ids[:1] if s in register for ref in register[s]['refs'][:1]]
            add('P1', 'membership_group', 'membership_group:%s:%s' % (system, chunk[0].object_id), 'Are these %d equipment items drawn piped into the '
                '%s network? Confirm all, reject all, or confirm except the tags you list.' % (len(chunk), system.split('/', 1)[1]),
                [a.object_id for a in chunk], {'drawing_refs': refs[:6]}, system=system)
    # P2 schedule readings, one decision per schedule table
    tables = defaultdict(dict)
    for tag, e in sorted(bg.equipment.items()):
        for ref in e.get('schedule_refs', [])[:1]:
            tables[(ref['pdf_sha256'], ref['page'], ref.get('sheet_number'))][tag] = e['class']
    for d in getattr(bg, 'drawing_equipment', []):
        for ref in d['schedule_refs'][:1]:
            tables[(ref['pdf_sha256'], ref['page'], ref.get('sheet_number'))][d['tag']] = d['class']
    for (sha, page, sheet), tags in sorted(tables.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        for chunk in _chunks(sorted(tags.items())):
            add('P2', 'schedule_reading', 'schedule_reading:%s:p%s:%s' % (sha[:12], page, chunk[0][0]),
                'Sheet %s (page %s): does the schedule list each of these %d tags as the class shown? Use the crops.' % (sheet, page, len(chunk)),
                ['equipment/%s=%s' % kv for kv in chunk], {'pdf_sha256': sha, 'page': page, 'sheet_number': sheet})
    # P3 abstained signatures; P4 contradicted classes
    rows = {r['source_id']: r for r in (bg.model_results or {}).get('ledger_rows', [])}
    groups = defaultdict(list)
    for sid, o in bg.model_outcomes.items():
        if o['outcome'] == 'abstained':
            groups[('P3', o['signature_id'], None)].append(sid)
        elif o.get('withheld_class'):
            groups[('P4', o['signature_id'], o['withheld_class'])].append(sid)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1]))
    for (prio, sig, withheld), sids in ordered:
        rep = next((s for s in sids if rows.get(s, {}).get('route') in ('llm', 'cache_reuse')), sorted(sids)[0])
        f = bg.facts[rep]
        sids = [rep] + sorted(s for s in sids if s != rep)
        for n, chunk in enumerate(_chunks(sids)):
            flagged = sum(1 for s in chunk if rows.get(s, {}).get('context_flags'))
            what = '%s (units %r, BAS kind %s, family %s)' % (f['name'], f['units'], f['kind'], rows.get(rep, {}).get('equipment_family') or '-')
            if prio == 'P3':
                add('P3', 'point_class', 'point_class:%s#%d' % (sig, n), 'Which Brick point class is %s? Applies to the %d listed rows (%d differ only '
                    'in station/folder). The model abstained: %s' % (what, len(chunk), flagged, '; '.join(rows.get(rep, {}).get('missing_prerequisites') or []) or 'no reason given'),
                    chunk, {'example_path': f['path'], 'signature_id': sig}, point_ids={x: bg.facts[x]['point_id'] for x in chunk})
            else:
                add('P4', 'contradicted_class', 'contradicted_class:%s#%d' % (sig, n), 'The model proposed %s for %s; a deterministic check withheld it (%s). '
                    'Confirm the rejection, or set the correct class for the %d listed rows.' % (withheld, what, ', '.join(rows.get(rep, {}).get('validation_reason_codes') or []), len(chunk)),
                    chunk, {'example_path': f['path'], 'signature_id': sig}, withheld_class=withheld,
                    point_ids={x: bg.facts[x]['point_id'] for x in chunk})
    # P5 open AHU / exhaust questions
    for q in bg.topology_worksheet():
        if q['topic'] in ('air_serving', 'exhaust_composition') or (q['topic'] == 'ahu_internals' and q['priority'] == 'high'):
            add('P5', 'question', 'question:%s:%s' % (q['topic'], q['subject']), q['question'], [q['subject']],
                {'missing_evidence': q['missing_evidence'], 'drawing_refs': q['drawing_refs'][:3], 'current_tier': q['current_evidence_tier']})
    return items


def summarize(items):
    return {'version': VERSION, 'decisions': len(items), 'by_priority': dict(Counter(i['priority'] for i in items)),
            'by_kind': dict(Counter(i['kind'] for i in items)), 'items_covered': sum(i['covers_count'] for i in items),
            'covered_by_priority': {p: sum(i['covers_count'] for i in items if i['priority'] == p) for p in sorted({i['priority'] for i in items})},
            'max_items_per_decision': max((i['covers_count'] for i in items), default=0)}


SIGNOFF_FIELDS = ('name', 'qualification', 'organization', 'authority_basis', 'review_date', 'statement')


def validate_signoff(signoff):
    """A qualified engineering sign-off: every field filled by the reviewer, and a statement that they reviewed the cited
    drawings themselves. Returns a list of problems (empty = valid)."""
    from .inference.gate import _agent_like
    if not signoff:
        return ['NO_SIGNOFF']
    problems = ['MISSING_' + k.upper() for k in SIGNOFF_FIELDS if len(str(signoff.get(k) or '').strip()) < 2]
    if _agent_like(str(signoff.get('name') or '')):
        problems.append('AGENT_NAME')
    if not signoff.get('sha256'):
        problems.append('UNHASHED_SIGNOFF')
    return problems


def expand(items, filled, reviewer, recorded_on, signoff=None, recorded_by=None, team_role=None):
    """Turn filled decisions into one record per affected item. `items` is the generated workbook (the authority on what each
    decision covers); `filled` maps decision_id -> {'decision', 'class', 'except', 'note', 'rationale', 'reviewed_refs'}.

    Authority: a record is `qualified_engineering` only with a valid sign-off naming this reviewer AND a per-decision
    rationale AND the source references the reviewer checked. Everything else is an `annotation`, which the graph builder
    records but never uses to change a status."""
    from .inference.gate import _agent_like
    if not reviewer or len(reviewer.strip()) < 2 or _agent_like(reviewer):
        raise EngineError('REVIEWER_REFUSED', 'Decisions must be recorded by a named human reviewer.')
    qualified_signoff = not validate_signoff(signoff) and str(signoff.get('name', '')).strip().casefold() == reviewer.strip().casefold()
    out = []
    for item in items:
        f = filled.get(item['decision_id'])
        if not f or not (f.get('decision') or '').strip() or f['decision'].strip() == 'unsure':
            continue
        decision = f['decision'].strip()
        if decision not in item['allowed_decisions']:
            raise EngineError('REVIEW_DECISION_INVALID', '%s: %r is not allowed (%s).' % (item['decision_id'], decision, ', '.join(item['allowed_decisions'])))
        cls = (f.get('class') or '').strip()
        if decision == 'set_class' and cls not in POINTS:
            raise EngineError('REVIEW_DECISION_INVALID', '%s: class %r is not in the accepted point vocabulary.' % (item['decision_id'], cls))
        excepted = {x.strip() for x in re.split(r'[,;\s]+', f.get('except') or '') if x.strip()}
        rationale = (f.get('rationale') or '').strip()
        refs = (f.get('reviewed_refs') or '').strip()
        authority = ('qualified_engineering' if qualified_signoff and rationale and refs and decision != 'unknown' else
                     TEAM if team_role and len(team_role.strip()) >= 3 and rationale and refs and decision != 'unknown'
                     and item['kind'] in ('schedule_reading', 'point_class', 'contradicted_class', 'connection', 'membership_group') else 'annotation')
        base = {'reviewer': reviewer.strip(), 'recorded_on': recorded_on, 'group_id': item['decision_id'], 'note': (f.get('note') or '').strip(),
                'workbook_version': VERSION, 'authority': authority, 'rationale': rationale, 'reviewed_refs': refs,
                'recorded_by': (recorded_by or reviewer).strip()}
        if authority == TEAM:
            base.update(team_role=team_role.strip())
        if authority == 'qualified_engineering':
            base.update(qualification=signoff['qualification'], organization=signoff['organization'], authority_basis=signoff['authority_basis'],
                        review_date=signoff['review_date'], signoff_sha256=signoff['sha256'])
        if decision == 'unknown':
            out.append(dict(base, decision_id='%s.000' % item['decision_id'], item_kind=item['kind'], item_key=item['item_key'], decision='unknown'))
            continue

        def rec(n, **kw):
            d = dict(base, decision_id='%s.%03d' % (item['decision_id'], n), **kw)
            out.append(d)
        if item['kind'] == 'connection':
            rec(0, item_kind='connection', item_key=item['item_key'], decision=decision)
        elif item['kind'] == 'membership_group':
            for n, member in enumerate(item['covers']):
                tag = member.split('/', 1)[1]
                ok = decision == 'confirm_all' or (decision == 'confirm_except' and tag not in excepted)
                rec(n, item_kind='membership', item_key='membership:%s|%s' % (item['system'].split('/', 1)[1], tag), decision='confirm' if ok else 'reject')
        elif item['kind'] == 'schedule_reading':
            for n, entry in enumerate(item['covers']):
                ident, cls_ = entry.split('/', 1)[1].split('=', 1)
                ok = decision == 'confirm_all' or (decision == 'confirm_except' and ident not in excepted)
                rec(n, item_kind='equipment_reading', item_key='equipment_reading:' + ident, decision='confirm' if ok else 'reject',
                    equipment_id=ident, **{'class': cls_})
        elif item['kind'] in ('point_class', 'contradicted_class'):
            for n, sid in enumerate(item['covers']):
                pid = item['point_ids'][sid]
                if decision == 'set_class':
                    rec(n, item_kind='point_class', item_key='point_class:%s' % sid, decision='set_class', point_id=pid, row_source_ids=[sid], **{'class': cls})
                else:
                    rec(n, item_kind='point_class', item_key='point_class:%s' % sid, decision='reject', point_id=pid, row_source_ids=[sid],
                        **{'class': item['withheld_class']})
        else:
            rec(0, item_kind='question', item_key=item['item_key'], decision='answer')
    return out


# ---- blind evaluation sample ------------------------------------------------------------------------------

def _bucket(rep):
    o, cls = rep['outcome'], rep['candidate_class']
    if o == 'resolved' and cls not in ('Point', 'unknown'):
        return 'specific_flagged' if (rep.get('check_flags') or []) else 'specific_clean'
    if o == 'resolved' and cls == 'Point':
        return 'generic_point'
    return {'resolved_unknown': 'unknown', 'abstained': 'abstained', 'check_failed': 'check_failed'}.get(o, 'rejected_or_no_response')


def evaluation_sample(ledger_rows, facts, n=100, seed='task006-phase5a-eval-v1'):
    """Deterministic stratified sample of signatures (bucket x equipment family), with every bucket represented.
    Returns (blind_rows, key_rows, summary). Only development-data rows are ever passed in."""
    reps = {}
    for r in ledger_rows:
        if r.get('signature_id') and r['route'] in ('llm', 'cache_reuse', 'held_oversized'):
            reps[r['signature_id']] = r
    sizes = Counter(r['signature_id'] for r in ledger_rows if r.get('signature_id'))
    strata = defaultdict(list)
    for sig, r in reps.items():
        strata[(_bucket(r), r.get('equipment_family') or 'none')].append(sig)
    total = sum(len(v) for v in strata.values())
    alloc = {k: max(1, round(n * len(v) / total)) for k, v in strata.items()}
    buckets = Counter(k[0] for k in strata)
    for b in buckets:                                    # every bucket gets at least 5 (or all it has), so rare outcomes are measurable
        have = sum(alloc[k] for k in strata if k[0] == b)
        pool = sorted((k for k in strata if k[0] == b), key=lambda k: -len(strata[k]))
        i = 0
        while have < min(5, sum(len(strata[k]) for k in pool)) and pool:
            k = pool[i % len(pool)]
            if alloc[k] < len(strata[k]):
                alloc[k] += 1
                have += 1
            i += 1
    while sum(alloc.values()) > n:                       # trim from the largest strata
        k = max(alloc, key=lambda k: (alloc[k] - 5 * (sum(alloc[j] for j in alloc if j[0] == k[0]) <= 5), alloc[k]))
        if alloc[k] <= 1:
            break
        alloc[k] -= 1
    order = lambda sig: hashlib.sha256((seed + sig).encode()).hexdigest()  # noqa: E731
    chosen = []
    for k in sorted(strata):
        chosen += [(k, s) for s in sorted(strata[k], key=order)[:min(alloc[k], len(strata[k]))]]
    blind, key = [], []
    for i, (k, sig) in enumerate(sorted(chosen, key=lambda x: order(x[1]))):
        r = reps[sig]
        f = facts[r['source_id']]
        sid = 'S%03d' % (i + 1)
        blind.append({'sample_id': sid, 'point_name': f['name'], 'units': f['units'], 'bas_kind': f['kind'], 'equipment_family': r.get('equipment_family') or '',
                      'example_bas_path': f['path'], 'rows_with_same_signature': sizes[sig],
                      'reference_class': '', 'reference_role': '', 'labeler_confidence': '', 'evidence_used': '', 'notes': ''})
        from .building_graph import lexicon_class
        key.append({'sample_id': sid, 'signature_id': sig, 'representative_source_id': r['source_id'], 'stratum_bucket': k[0], 'stratum_family': k[1],
                    'baseline_naming_class': lexicon_class(f['name']),
                    'stratum_population': len(strata[k]), 'stratum_sampled': min(alloc[k], len(strata[k])), 'rows_covered': sizes[sig],
                    'model_class': r['candidate_class'], 'model_outcome': r['outcome'], 'check_flags': r.get('check_flags') or [],
                    'withheld_class': (r.get('alternatives') or [None])[0] if r['outcome'] == 'check_failed' else None})
    summary = {'version': VERSION, 'seed': seed, 'sampled_signatures': len(blind), 'population_signatures': total,
               'by_bucket': dict(Counter(k['stratum_bucket'] for k in key)), 'population_by_bucket': dict(Counter(k[0] for k in strata for _ in strata[k])),
               'families': len({k['stratum_family'] for k in key}), 'blind': True,
               'reference_labels': 'NOT_COLLECTED (must be entered by an independent human; never generated by a model)',
               'accuracy': 'NOT_MEASURED'}
    return blind, key, summary


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def _weighted(items, population, weight_rows):
    """Stratified (ratio) estimate of exact agreement. Each labeled item stands for N_h / n_h signatures of its stratum
    (times its own row count when row-weighted). Strata without labels contribute nothing; the represented share says so."""
    num = den = 0.0
    counts = Counter(r['stratum'] for r in items)
    for r in items:
        w = population[r['stratum']] / counts[r['stratum']] * (r['key'].get('rows_covered', 1) if weight_rows else 1)
        num += w * r['exact']
        den += w
    return num / den if den else None


def _bootstrap(items, population, weight_rows, reps=2000, seed='task006-eval-bootstrap-v1'):
    """Stratified bootstrap: resample labeled items with replacement within each stratum (the sampling design)."""
    import random
    rng = random.Random(seed)
    strata = defaultdict(list)
    for r in items:
        strata[r['stratum']].append(r)
    values = []
    for _ in range(reps):
        sample = [rng.choice(v) for v in strata.values() for _ in v]
        est = _weighted(sample, population, weight_rows)
        if est is not None:
            values.append(est)
    if not values:
        return None
    values.sort()
    return [round(values[int(0.025 * (len(values) - 1))], 3), round(values[int(0.975 * (len(values) - 1))], 3)]


def score(labels, key, labeler):
    """Agreement between the model and independent human reference labels on the blind DEVELOPMENT-data sample.

    The sample is stratified by outcome bucket x equipment family, so the unweighted figure is not building-wide. Two design-
    based estimates are given for model-specific signatures: signature-weighted and row-weighted (each signature counts by the
    source rows it propagates to), with stratified-bootstrap 95% intervals. Baseline A (naming rules) is scored on the same
    labels. This is not a validation or holdout result."""
    from .inference.gate import _agent_like
    if not labeler or _agent_like(labeler):
        raise EngineError('LABELER_REFUSED', 'Reference labels must come from a named human labeler.')
    k = {r['sample_id']: r for r in key}
    rows = []
    for lab in labels:
        ref = (lab.get('reference_class') or '').strip()
        if not ref:
            continue
        if ref not in POINTS and ref not in ('unknown', 'Point'):
            raise EngineError('LABEL_INVALID', '%s: %r is not an accepted class (or Point / unknown).' % (lab['sample_id'], ref))
        m = k[lab['sample_id']]
        specific = m['model_outcome'] == 'resolved' and m['model_class'] not in ('Point', 'unknown')
        rows.append({'key': m, 'bucket': m['stratum_bucket'], 'stratum': (m['stratum_bucket'], m['stratum_family']), 'specific': specific,
                     'exact': specific and m['model_class'] == ref, 'role': specific and ref in POINTS and point_role(ref) == point_role(m['model_class']),
                     'ref_specific': ref not in ('Point', 'unknown'), 'ref': ref})
    spec = [r for r in rows if r['specific']]
    by = defaultdict(list)
    for r in spec:
        by[r['bucket']].append(r)
    population = {}
    for m in key:
        if m['model_outcome'] == 'resolved' and m['model_class'] not in ('Point', 'unknown'):
            population[(m['stratum_bucket'], m['stratum_family'])] = m.get('stratum_population', 1)
    labeled_strata = {r['stratum'] for r in spec}
    noncommit = [r for r in rows if not r['specific'] and r['key']['model_outcome'] != 'check_failed']
    withheld = [r for r in rows if r['key']['model_outcome'] == 'check_failed' and r['key'].get('withheld_class')]
    base = [r for r in rows if r['key'].get('baseline_naming_class')]
    exact_n = sum(r['exact'] for r in spec)
    sig_w, row_w = _weighted(spec, population, False), _weighted(spec, population, True)
    return {'scope': 'development data, blind stratified sample; NOT a validation or holdout result', 'labeled': len(rows), 'labeler': labeler,
            'model_specific_labeled': len(spec),
            'sample_exact_class_accuracy_unweighted': round(exact_n / len(spec), 3) if spec else None,
            'sample_exact_class_accuracy_wilson95': wilson(exact_n, len(spec)),
            'sample_role_accuracy_unweighted': round(sum(r['role'] for r in spec) / len(spec), 3) if spec else None,
            'weighted_exact_accuracy_estimate': round(sig_w, 3) if sig_w is not None else None,
            'weighted_exact_accuracy_bootstrap95': _bootstrap(spec, population, False),
            'row_weighted_exact_accuracy_estimate': round(row_w, 3) if row_w is not None else None,
            'row_weighted_exact_accuracy_bootstrap95': _bootstrap(spec, population, True),
            'weighted_population_share_represented': round(sum(population[s] for s in labeled_strata if s in population) / sum(population.values()), 3) if population else None,
            'strata_with_single_label': sum(1 for s in labeled_strata if sum(1 for r in spec if r['stratum'] == s) == 1),
            'exact_by_bucket': {b: round(sum(r['exact'] for r in v) / len(v), 3) for b, v in by.items()},
            'baseline_a_naming_rules': {'labeled_with_a_naming_class': len(base), 'labeled_total': len(rows),
                                        'exact_when_it_answers': round(sum(r['key']['baseline_naming_class'] == r['ref'] for r in base) / len(base), 3) if base else None,
                                        'coverage_on_labeled_sample': round(len(base) / len(rows), 3) if rows else None},
            'baseline_b_one_pass_llm': 'NOT RUN (would require a separate hosted run; the smoke run used a different vocabulary)',
            'abstention': {'labeled_non_specific': len(noncommit), 'appropriate_reference_also_non_specific': sum(1 for r in noncommit if not r['ref_specific']),
                           'missed_reference_is_specific': sum(1 for r in noncommit if r['ref_specific'])},
            'withheld_classes': {'labeled': len(withheld), 'check_prevented_error': sum(1 for r in withheld if r['ref'] != r['key']['withheld_class']),
                                 'check_withheld_a_correct_class': sum(1 for r in withheld if r['ref'] == r['key']['withheld_class'])},
            'note': 'Unweighted figures describe the sample only. Weighted figures cover only labeled strata (see represented share); intervals from '
                    'a stratified bootstrap are unreliable for strata with a single label.'}


# ---- Phase 5C: sheet-level review form, team documentary checks, pathway edges ---------------------------------

TEAM = 'team_documentary_check'
EDGE = __import__('re').compile(r'^\s*([A-Za-z0-9_.-]+)\s*>\s*([A-Za-z0-9_.-]+)\s*$')


def authority_for(reviewer, rationale, refs, signoff=None, team_role=None):
    """qualified_engineering: valid sign-off naming this reviewer + rationale + references.
    team_documentary_check: a named team member with a stated role + rationale + references (can never verify; see the builder).
    annotation: anything else."""
    if not rationale or not refs:
        return 'annotation'
    if signoff and not validate_signoff(signoff) and str(signoff.get('name', '')).strip().casefold() == reviewer.strip().casefold():
        return 'qualified_engineering'
    if team_role and len(team_role.strip()) >= 3:
        return TEAM
    return 'annotation'


def expand_sheet_form(plan, filled, reviewer, recorded_on, signoff=None, recorded_by=None, team_role=None, known_equipment=()):
    """Sheet-level review. `filled` maps row ids ('S01' sheet header, 'S01.r03' relationship rows) to {'decision', 'rationale',
    'reviewed_refs', 'edges', 'note'}. A sheet header's rationale/refs apply ONLY to rows of that sheet that carry their own
    explicit decision; a blank row stays unreviewed. Pathway edges (FROM>TO) are recorded only for qualified engineers."""
    from .inference.gate import _agent_like
    if not reviewer or len(reviewer.strip()) < 2 or _agent_like(reviewer):
        raise EngineError('REVIEWER_REFUSED', 'Decisions must be recorded by a named human reviewer.')
    known = set(known_equipment)
    out = []
    for d in plan:
        head = filled.get(d['decision_id'], {})
        h_rat, h_refs = (head.get('rationale') or '').strip(), (head.get('reviewed_refs') or '').strip()
        if d['kind'] == 'pathway':
            edges = [e for e in __import__('re').split(r'[;\n]+', head.get('edges') or '') if e.strip()]
            auth = authority_for(reviewer, h_rat, h_refs, signoff, None)          # never a team check: flow direction is engineering
            for n, e in enumerate(edges):
                m = EDGE.match(e)
                if not m or m.group(1) not in known or m.group(2) not in known or m.group(1) == m.group(2):
                    raise EngineError('REVIEW_DECISION_INVALID', '%s: edge %r must be FROM>TO with two known, different equipment tags.' % (d['decision_id'], e.strip()))
                out.append(_record(d, n, reviewer, recorded_on, recorded_by, auth, h_rat, h_refs, signoff, head, item_kind='documented_connection',
                                   item_key='documented_connection:%s>%s' % m.groups(), decision='confirm', from_equipment=m.group(1),
                                   to_equipment=m.group(2), predicate='feeds', system=d['system']))
            continue
        for n, row in enumerate(d['rows']):
            f = filled.get('%s.r%02d' % (d['decision_id'], n + 1), {})
            decision = (f.get('decision') or '').strip()
            if not decision:
                continue                                                     # not reviewed: nothing recorded, nothing verified
            if decision not in ('confirm', 'reject', 'unknown'):
                raise EngineError('REVIEW_DECISION_INVALID', '%s.r%02d: %r (confirm / reject / unknown).' % (d['decision_id'], n + 1, decision))
            rat = (f.get('rationale') or '').strip() or h_rat
            refs = (f.get('reviewed_refs') or '').strip() or h_refs
            auth = 'annotation' if decision == 'unknown' else authority_for(reviewer, rat, refs, signoff, team_role)
            extra = {}
            if row['kind'] == 'identity':
                extra = {'equipment_id': row['item_key'].split(':', 1)[1], 'class': row['class']}
            out.append(_record(d, n, reviewer, recorded_on, recorded_by, auth, rat, refs, signoff, f, team_role=team_role,
                               item_kind={'connection': 'connection', 'membership': 'membership', 'identity': 'equipment_reading'}[row['kind']],
                               item_key=row['item_key'], decision=decision, **extra))
    return out


def _record(d, n, reviewer, recorded_on, recorded_by, auth, rationale, refs, signoff, f, team_role=None, **kw):
    rec = {'decision_id': '%s.%03d' % (d['decision_id'], n), 'group_id': d['decision_id'], 'reviewer': reviewer.strip(), 'recorded_on': recorded_on,
           'recorded_by': (recorded_by or reviewer).strip(), 'authority': auth, 'rationale': rationale, 'reviewed_refs': refs,
           'note': (f.get('note') or '').strip(), 'workbook_version': VERSION + '+sheet-review', **kw}
    if auth == TEAM:
        rec['team_role'] = (team_role or '').strip()
    if auth == 'qualified_engineering':
        rec.update(qualification=signoff['qualification'], organization=signoff['organization'], authority_basis=signoff['authority_basis'],
                   review_date=signoff['review_date'], signoff_sha256=signoff['sha256'])
    return rec

