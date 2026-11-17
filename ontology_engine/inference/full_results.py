"""Offline loading and analysis of a completed full development-data inference run (TASK_006 Phase 4).

Nothing here sends a request or reads a credential. Inputs are the local outputs of `scripts/sponsor_l1_full.py run`:
  <full>/full_plan.json                                   the reviewed plan (aggregates)
  <full>/full_live_report_<run>.json                      one per run or continuation
  <full>/restricted/runs/<run>/ledger_rows.json           router ledger rows (one per source row)
  <full>/restricted/runs/<run>/journal.jsonl              one durable entry per sent request (usage, cost, latency, request id)

Schema validity, coverage, usage and cost are measured. Semantic accuracy is NOT measured: there are no independent
reference labels, so the deterministic checks below are plausibility screens, not grades.
"""
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from ..errors import EngineError
from ..vocabulary import point_role

MODEL_ROUTES = ('llm', 'cache_reuse', 'propagated_signature')       # rows whose class came from the hosted model
REPRESENTATIVE_ROUTES = ('llm', 'cache_reuse')


def outcome_category(row):
    """One label per row: specific / generic_point / unknown / abstained / rejected / conflict / no_response / check_failed /
    held / oversized / unresolved."""
    outcome, cls = row.get('outcome'), row.get('candidate_class')
    if outcome == 'resolved':
        return 'generic_point' if cls == 'Point' else 'unknown' if cls == 'unknown' else 'specific'
    return {'resolved_unknown': 'unknown', 'held_preserved': 'held', 'oversized_unresolved': 'oversized'}.get(outcome, outcome or 'unresolved')


def _read_json(path):
    path = Path(path)
    if path.is_symlink():
        raise EngineError('L0_SYMLINK_REFUSED', 'Model results path is a symlink: %s' % path.name)
    return json.loads(path.read_text(encoding='utf-8'))


def journals(full_dir):
    """Every durable request entry from every run (initial and continuations), oldest run first."""
    entries = []
    for path in sorted(Path(full_dir).glob('restricted/runs/*/journal.jsonl')):
        if path.is_symlink():
            raise EngineError('L0_SYMLINK_REFUSED', 'Journal path is a symlink.')
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                entries.append(dict(json.loads(line), run=path.parent.name))
    return entries


def latest_complete(full_dir):
    """(plan, report, rows) for the newest run that finished without error and covers every planned signature, else None."""
    full_dir = Path(full_dir)
    plan_path = full_dir / 'full_plan.json'
    reports = sorted(full_dir.glob('full_live_report_*.json'), reverse=True)
    if not plan_path.is_file() or not reports:
        return None
    plan = _read_json(plan_path)
    for path in reports:
        report = _read_json(path)
        if report.get('stopped_on_error') or report.get('plan_sha256') != plan['plan_sha256']:
            continue
        rows = _read_json(full_dir / report['restricted_run_dir'] / 'ledger_rows.json')
        if {r['signature_id'] for r in rows if r.get('signature_id')} == set(plan['signature_ids']):
            return plan, report, rows
    return None


def load(full_dir, records):
    """Model results in the shape BuildingGraph expects, with lineage checks, or None when no complete full run exists."""
    found = latest_complete(full_dir)
    if found is None:
        return None
    from .router import PROMPT_VERSION
    plan, report, rows = found
    uids = {r.source_id: r.source_record_uid for r in records}
    if any(uids.get(r['source_id']) != r['source_record_uid'] for r in rows):
        raise EngineError('MODEL_RESULTS_LINEAGE', 'Full-run ledger rows do not match the verified source records.')
    if plan.get('prompt_version') != PROMPT_VERSION:
        raise EngineError('MODEL_RESULTS_LINEAGE', 'Full-run prompt version differs from the current router prompt.')
    sent = journals(full_dir)
    models = sorted({r['model'] for r in rows if r.get('model')}) or plan['models']
    return {'ledger_rows': rows, 'run': {'model': ','.join(models), 'prompt_version': PROMPT_VERSION, 'plan_sha256': plan['plan_sha256'],
                                         'request_ids': [e.get('request_id') for e in sent if not e.get('error_code')],
                                         'cost_usd': round(sum(e.get('cost_usd') or 0.0 for e in sent), 9), 'source': 'l1_full',
                                         'vocabulary_version': plan.get('vocabulary_version')}}


def _dist(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    q = lambda p: values[min(len(values) - 1, int(round(p * (len(values) - 1))))]  # noqa: E731
    return {'n': len(values), 'min': values[0], 'median': round(statistics.median(values), 3), 'p90': q(0.9), 'max': values[-1],
            'total': round(sum(values), 3)}


def analyse(plan, rows, sent, ledger_summary=None, facts=None):
    """Values-free summary plus restricted per-signature detail. `facts` (optional) maps source_id -> {name, units, kind}."""
    facts = facts or {}
    by_sig = defaultdict(list)
    for r in rows:
        if r.get('signature_id'):
            by_sig[r['signature_id']].append(r)
    sig_cat, row_cat = Counter(), Counter(outcome_category(r) for r in rows)
    sig_class, row_class, sig_role, row_role = Counter(), Counter(), Counter(), Counter()
    family_cat = defaultdict(Counter)
    check_flags, context_flags, reason_codes = Counter(), Counter(), Counter()
    detail = []
    for sid, group in sorted(by_sig.items()):
        rep = next((r for r in group if r['route'] in REPRESENTATIVE_ROUTES or r['route'] == 'held_oversized'), group[0])
        cat = outcome_category(rep)
        sig_cat[cat] += 1
        family_cat[rep.get('equipment_family') or 'none'][cat] += 1
        check_flags.update(rep.get('check_flags') or [])
        reason_codes.update(rep.get('validation_reason_codes') or [])
        for r in group:
            context_flags.update(r.get('context_flags') or [])
        if cat == 'specific':
            cls = rep['candidate_class']
            sig_class[cls] += 1
            row_class[cls] += len(group)
            sig_role[point_role(cls) or 'unlisted'] += 1
            row_role[point_role(cls) or 'unlisted'] += len(group)
        f = facts.get(rep['source_id'], {})
        detail.append({'signature_id': sid, 'representative': rep['source_id'], 'name': f.get('name'), 'units': rep.get('units_normalized'),
                       'bacnet_type': f.get('kind'), 'equipment_family': rep.get('equipment_family'), 'class': rep['candidate_class'],
                       'outcome': rep['outcome'], 'category': cat, 'alternatives': rep.get('alternatives', []),
                       'rows_covered': len(group), 'check_flags': rep.get('check_flags', []),
                       'rows_with_context_flags': sum(1 for r in group if r.get('context_flags')),
                       'validation_reason_codes': rep.get('validation_reason_codes', []), 'batch_id': rep.get('batch_id')})
    ok = [e for e in sent if not e.get('error_code')]
    usage = {k: sum(int((e.get('usage') or {}).get(k) or 0) for e in ok) for k in ('input_tokens', 'output_tokens')}
    cost = round(sum(e.get('cost_usd') or 0.0 for e in sent), 9)
    items_sent = sum(e.get('items') or 0 for e in ok)
    signatures = len(by_sig)
    resolved_specific = sig_cat.get('specific', 0)
    summary = {
        'plan_sha256': plan['plan_sha256'], 'vocabulary_version': plan.get('vocabulary_version'), 'models': plan.get('models'),
        'semantic_accuracy': 'NOT_MEASURED (no independent reference labels)',
        'execution': {'requests_sent': len(sent), 'requests_ok': len(ok), 'requests_failed': len(sent) - len(ok),
                      'error_codes': dict(Counter(e['error_code'] for e in sent if e.get('error_code'))),
                      'stop_reasons': dict(Counter(e.get('stop_reason') or 'not_recorded' for e in ok)),
                      'runs': sorted({e['run'] for e in sent if e.get('run')}), 'request_ids_recorded': sum(1 for e in ok if e.get('request_id')),
                      'planned_requests': plan.get('requests')},
        'usage': dict(usage, items_sent=items_sent,
                      output_tokens_per_item=round(usage['output_tokens'] / items_sent, 1) if items_sent else None),
        'cost': {'actual_usd_from_reported_usage': cost, 'expected_usd': plan.get('expected_cost_usd'),
                 'worst_case_reserved_usd': plan.get('worst_case_reserved_usd'), 'ledger_cap_usd': plan.get('ledger_cap_usd'),
                 'ledger': {k: v for k, v in (ledger_summary or {}).items() if k != 'disclosure'} or None},
        'latency_s': _dist([e.get('latency_s') for e in ok]),
        'coverage': {'source_rows': len(rows), 'rows_by_category': dict(row_cat), 'signatures': signatures,
                     'signatures_by_category': dict(sig_cat),
                     'specific_class_share_of_signatures': round(resolved_specific / signatures, 3) if signatures else None,
                     'rows_with_specific_class': row_cat.get('specific', 0)},
        'classes': {'distinct_specific_classes': len(sig_class), 'by_signature': dict(sig_class.most_common()),
                    'by_row': dict(row_class.most_common()), 'roles_by_signature': dict(sig_role), 'roles_by_row': dict(row_role)},
        'equipment_family_by_category': {k: dict(v) for k, v in sorted(family_cat.items())},
        'checks': {'class_check_flags_by_signature': dict(check_flags), 'check_failed_signatures': sig_cat.get('check_failed', 0),
                   'context_flags_on_propagated_rows': dict(context_flags),
                   'rows_propagated_with_context_flags': sum(1 for r in rows if r.get('context_flags')),
                   'scope_audit_reason_codes': dict(reason_codes)}}
    return summary, detail


def ambiguous_evidence(ledger_path, plan_dir, scope, sent):
    """Read-only: every AMBIGUOUS ledger request with the local evidence about it (which planned body it is, whether a
    journal entry or cached response exists). Nothing is changed."""
    import sqlite3
    from .anthropic_provider import request_key
    db = sqlite3.connect('file:%s?mode=ro' % Path(ledger_path), uri=True)
    try:
        rows = db.execute("SELECT request_key, model, reserved_usd, note, updated_at FROM requests WHERE state='AMBIGUOUS'").fetchall()
    finally:
        db.close()
    plan_dir = Path(plan_dir)
    plan = _read_json(plan_dir / 'full_plan.json')
    index = {}
    for n, path in enumerate(sorted((plan_dir / plan['restricted_run_dir'] / 'requests').glob('request_*.json'))):
        index[request_key(_read_json(path), scope)] = n
    journaled = {e.get('request_key') for e in sent}
    return [{'request_key': k, 'model': m, 'reserved_max_usd': r, 'ledger_note': note, 'ledger_updated_at': at,
             'planned_request_index': index.get(k), 'journal_entry': k in journaled, 'response_recoverable_locally': k in journaled,
             'possible_charge_usd': {'min': 0.0, 'max': r}} for k, m, r, note, at in rows]
