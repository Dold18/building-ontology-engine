"""Full development-data inference (Haiku first, Sonnet disabled), built on the existing router, provider, ledger, cache and
scope audit. Offline planning produces the exact first-pass packing and a plan hash; a live run needs every L1 decision plus an
owner-recorded `inference_scope_approved` that covers the plan (or a standing scope within its bounds).

Continuation: completed signatures are served from the integrity-checked inference cache, so a re-run after an interruption
never resends a paid request for them; the remaining items are packed again under the same limits. A request the ledger
already knows (any state) is refused by the ledger itself, and AMBIGUOUS requests block the run until the owner resolves them.
"""
import hashlib
import json
import math
from collections import Counter
from .anthropic_provider import input_upper_bound_tokens, schema_sha256
from .l0 import Recorder, safety_checks
from .pricing import max_cost_usd
from .router import PROMPT_VERSION, SYSTEM_PROMPT, Router, RoutingConfig, evidence_bytes, plan_routes
from .smoke import SmokeGuard, items_of, payload_sha256
from ..contextual import ContextResponse
from ..errors import EngineError
from ..vocabulary import VOCABULARY_VERSION
from . import class_checks

FULL_VERSION = 'l1-full-v1'
ITEMS_PER_REQUEST = 25
OUTPUT_BYTES_PER_ITEM = 600            # calibrated from the L1 smoke test (190 output tokens per item ~ 570 bytes)
LEDGER_CAP_USD = 0.50
DATA_CLASS = 'raw_bas_point_metadata'
MEASURED = {'output_tokens_per_item': 190.3, 'input_tokens_per_body_byte': 9387 / 20346, 'source': 'L1 smoke test 2026-11-03'}


def full_config(scope, haiku_settings):
    return RoutingConfig(scope=scope, data_origin='sponsor', max_items_per_batch=ITEMS_PER_REQUEST, haiku_max_tokens=haiku_settings.max_tokens,
                         escalation_model=None, expected_output_bytes_per_item=OUTPUT_BYTES_PER_ITEM)


def plan_hash(summary):
    keys = ('full_version', 'scope', 'source_sha256', 'config', 'vocabulary_version', 'system_prompt_sha256', 'response_schema_sha256',
            'prompt_version', 'class_checks_version', 'signature_ids', 'settings', 'ledger_cap_usd', 'models', 'data_class')
    return hashlib.sha256(json.dumps({k: summary[k] for k in keys}, sort_keys=True).encode()).hexdigest()


def dry_run(records, cfg, haiku_settings, pricing, gate, sponsor_index, authorized_file, authorized_sha256):
    """Exact first-pass packing with the transport-free recorder. Returns (summary, bodies)."""
    recorder = Recorder(haiku_settings)
    router = Router(cfg, records, {cfg.haiku_model: recorder}, gate=gate, pricing=pricing, sponsor_index=sponsor_index)
    plan = plan_routes(records, cfg)
    ledger = router.run(plan)
    bodies = recorder.bodies
    price = pricing.price(cfg.haiku_model)
    rows, expected = [], 0.0
    for b in bodies:
        n = len(items_of(b))
        body_bytes = len(json.dumps(b, ensure_ascii=False).encode('utf-8'))
        upper = input_upper_bound_tokens(b)
        exp_in = body_bytes * MEASURED['input_tokens_per_body_byte']
        exp_out = n * MEASURED['output_tokens_per_item']
        cost = (exp_in * price.input_usd_per_mtok + exp_out * price.output_usd_per_mtok) / 1e6
        expected += cost
        rows.append({'items': n, 'body_bytes': body_bytes, 'evidence_bytes': evidence_bytes(items_of(b)), 'input_token_upper_bound': upper,
                     'reserved_max_usd': round(max_cost_usd(price, upper, b['max_tokens']), 6), 'expected_usd': round(cost, 6),
                     'expected_output_share_of_max_tokens': round(exp_out / b['max_tokens'], 2), 'request_sha256': payload_sha256(b)})
    checks, detail = safety_checks(bodies, records, authorized_file, authorized_sha256, sponsor_index)
    worst = round(sum(r['reserved_max_usd'] for r in rows), 6)
    problems = []
    if worst > LEDGER_CAP_USD:
        problems.append('WORST_CASE_EXCEEDS_LEDGER_CAP')
    if router.oversized:
        problems.append('OVERSIZED_ITEMS')
    if any(r['expected_output_share_of_max_tokens'] > 0.8 for r in rows):
        problems.append('OUTPUT_TRUNCATION_RISK')
    if checks['records_outside_authorized_workbook'] or checks['unexpected_item_fields'] or checks['findings'].get('target_not_in_authorized_records'):
        problems.append('SCOPE_CHECK_FAILED')
    routes = Counter(r['route'] for r in ledger)
    summary = {'full_version': FULL_VERSION, 'scope': cfg.scope, 'source_sha256': authorized_sha256, 'config': cfg.model_dump(),
               'vocabulary_version': VOCABULARY_VERSION, 'system_prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
               'response_schema_sha256': schema_sha256(ContextResponse), 'prompt_version': PROMPT_VERSION,
               'class_checks_version': class_checks.VERSION, 'signature_ids': sorted(g['signature_id'] for g in plan['groups']),
               'settings': {'model': haiku_settings.model, 'max_tokens': haiku_settings.max_tokens, 'timeout_seconds': haiku_settings.timeout_seconds,
                            'max_retries': haiku_settings.max_retries, 'api_key_env': haiku_settings.api_key_env},
               'ledger_cap_usd': LEDGER_CAP_USD, 'models': [cfg.haiku_model], 'data_class': DATA_CLASS, 'sonnet': 'disabled',
               'transmitted': False, 'api_key_read': False, 'records_total': len(records), 'held_preserved': routes.get('held_source_error', 0) + routes.get('held_kind_unclear', 0),
               'signatures': len(plan['groups']), 'rows_covered_by_signatures': sum(len(g['rows']) for g in plan['groups']),
               'requests': len(rows), 'items': sum(r['items'] for r in rows), 'request_rows': rows,
               'expected_cost_usd': round(expected, 4), 'worst_case_reserved_usd': worst,
               'expected_basis': 'measured L1 smoke usage (190 output tokens per item; tokens per body byte) applied to the exact packed bodies',
               'safety_checks': checks, 'problems': problems, 'ready': not problems}
    summary['plan_sha256'] = plan_hash(summary)
    return summary, {'bodies': bodies, 'detail': detail}


def covers(scope, summary):
    """Does an owner-recorded inference scope authorize this plan?"""
    reasons = []
    if not set(summary['models']) <= set(scope['models']):
        reasons.append('MODEL_OUTSIDE_SCOPE')
    if summary['data_class'] not in scope['data_classes']:
        reasons.append('DATA_CLASS_OUTSIDE_SCOPE')
    if summary['ledger_cap_usd'] > scope['max_spend_usd'] + 1e-12:
        reasons.append('SPEND_OUTSIDE_SCOPE')
    if scope['standing'] is False and scope['plan_sha256'] != summary['plan_sha256']:
        reasons.append('PLAN_NOT_THE_APPROVED_PLAN')
    return reasons


def live_run(records, cfg, provider, approved, gate, pricing, sponsor_index, ledger, cache, journal):
    """Run (or continue) the approved first pass. `approved` is the reviewed plan summary."""
    plan = plan_routes(records, cfg)
    if sorted(g['signature_id'] for g in plan['groups']) != approved['signature_ids']:
        raise EngineError('FULL_PLAN_CHANGED', 'The signature set differs from the approved plan; nothing sent.')
    targets = {g['rep']['source_id'] for g in plan['groups']}
    guard = SmokeGuard(provider, approved['requests'] + 5, ITEMS_PER_REQUEST, allowed_targets=targets, journal=journal)
    router = Router(cfg, records, {cfg.haiku_model: guard}, cache=cache, gate=gate, pricing=pricing, sponsor_index=sponsor_index)
    error, ledger_rows = None, []
    try:
        ledger_rows = router.run(plan)
    except EngineError as exc:
        error = {'code': exc.code, 'message': str(exc)}
    calls = guard.calls
    return {'full_version': FULL_VERSION, 'plan_sha256': approved['plan_sha256'], 'stopped_on_error': error,
            'requests_attempted_this_run': len(calls), 'cache_hits': router.stats['cache_hits'].get(cfg.haiku_model, 0),
            'usage_this_run': {k: sum(int((c.get('usage') or {}).get(k) or 0) for c in calls) for k in ('input_tokens', 'output_tokens')},
            'cost_usd_this_run': round(sum(c.get('cost_usd') or 0.0 for c in calls), 9),
            'latency_s': [c.get('latency_s') for c in calls], 'errors': [c['error_code'] for c in calls if c.get('error_code')],
            'router_summary': router.summary(ledger_rows) if ledger_rows else None, 'ledger': ledger.summary(), 'ledger_rows': ledger_rows}
