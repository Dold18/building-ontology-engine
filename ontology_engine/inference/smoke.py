"""L1 smoke test: a tiny, owner-reviewable live run built from the EXISTING router, provider, ledger and validation.

Limits (fixed in code, owner-specified): at most 3 Haiku requests of at most 10 signature items each, at most 1 optional
Sonnet request of at most 5 items, a separate durable ledger capped at $0.50, no automatic retries, no cache, and no
resend of any request the smoke ledger has already seen.

The same deterministic plan is built twice: once offline with the transport-free L0 Recorder (the dry run the owner
reviews) and once for the live run. The live run is refused unless the rebuilt plan's hash equals the reviewed one, and
each Haiku body must match a reviewed body hash before it can reach the provider. The Sonnet body depends on Haiku
outcomes, so it cannot be pre-hashed; it is bounded instead (one request, at most 5 items, targets drawn only from the
reviewed Haiku items, built by the same router and validated by the same scope audit).
"""
import hashlib
import json
import math
import time
from collections import Counter
from .anthropic_provider import input_upper_bound_tokens, request_key
from .l0 import Recorder, safety_checks
from .pricing import max_cost_usd
from .router import Router, RoutingConfig, evidence_bytes, plan_routes
from ..errors import EngineError

SMOKE_VERSION = 'l1-smoke-v1'
LIMITS = {'haiku_requests': 3, 'haiku_items_per_request': 10, 'sonnet_requests': 1, 'sonnet_items_per_request': 5,
          'cap_usd': 0.50, 'retries': 0}
DATA_CLASS = 'raw_bas_point_metadata'


def payload_sha256(body):
    """Hash of the exact bytes the provider sends (same serialization as AnthropicProvider.complete)."""
    return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode('utf-8')).hexdigest()


def items_of(body):
    return json.loads(body['messages'][0]['content'])['untrusted_records']


def smoke_config(scope, data_origin, haiku_settings, sonnet_settings):
    n = LIMITS['haiku_requests'] * LIMITS['haiku_items_per_request']
    return RoutingConfig(scope=scope, data_origin=data_origin, max_items_per_batch=LIMITS['haiku_items_per_request'],
                         haiku_max_tokens=haiku_settings.max_tokens,
                         escalation_model=sonnet_settings.model if sonnet_settings else None,
                         escalation_max_tokens=sonnet_settings.max_tokens if sonnet_settings else 16384,
                         escalation_items_per_batch=LIMITS['sonnet_items_per_request'],
                         max_escalation_fraction=LIMITS['sonnet_items_per_request'] / n)


def select_groups(full_plan, n):
    """Deterministic: the n signatures covering the most source rows (largest propagation value), ties by id."""
    return sorted(full_plan['groups'], key=lambda g: (-len(g['rows']), g['signature_id']))[:n]


def smoke_plan(records, cfg):
    """A sub-plan holding only the selected signatures, batched by the router's own packing limits."""
    full = plan_routes(records, cfg)
    n = LIMITS['haiku_requests'] * LIMITS['haiku_items_per_request']
    chosen = {g['signature_id'] for g in select_groups(full, n)}
    groups = [g for g in full['groups'] if g['signature_id'] in chosen]          # keep the planner's ordering
    batches = [groups[i:i + cfg.max_items_per_batch] for i in range(0, len(groups), cfg.max_items_per_batch)]
    return {'held': [], 'deterministic': [], 'groups': groups, 'batches': batches, 'config': cfg,
            'eligible_signatures_total': len(full['groups'])}


class SmokeGuard:
    """Wraps one real responder. Enforces the request cap, the item cap and (for Haiku) the reviewed body hashes BEFORE the
    wrapped provider is called; the provider itself still runs every L1, binding, budget and schema check."""

    def __init__(self, responder, max_requests, max_items, approved_sha256=None, allowed_targets=None, journal=None):
        self.responder, self.max_requests, self.max_items = responder, max_requests, max_items
        self.approved, self.allowed, self.journal = approved_sha256, allowed_targets, journal
        self.attempts, self.calls = 0, []

    settings = property(lambda self: self.responder.settings)
    model = property(lambda self: self.responder.model)
    live_enabled = property(lambda self: getattr(self.responder, 'live_enabled', False))

    def make_body(self, system_prompt, content, response_type):
        return self.responder.make_body(system_prompt, content, response_type)

    def complete(self, body, response_type, scope='', data_origin=''):
        items, sha = items_of(body), payload_sha256(body)
        if self.attempts >= self.max_requests:
            raise EngineError('SMOKE_LIMIT', 'Smoke request cap reached for %s; not sent.' % self.model)
        if len(items) > self.max_items:
            raise EngineError('SMOKE_LIMIT', 'Smoke request has %d items (max %d); not sent.' % (len(items), self.max_items))
        if self.approved is not None and sha not in self.approved:
            raise EngineError('SMOKE_BODY_NOT_APPROVED', 'Request body does not match a reviewed dry-run body; not sent.')
        if self.allowed is not None and not {i.get('target_source_id') for i in items} <= self.allowed:
            raise EngineError('SMOKE_SCOPE', 'Escalation targets outside the reviewed smoke items; not sent.')
        self.attempts += 1
        entry = {'model': self.model, 'items': len(items), 'request_sha256': sha, 'request_key': request_key(body, scope)}
        self.calls.append(entry)
        started = time.monotonic()
        try:
            result = self.responder.complete(body, response_type, scope, data_origin)
        except EngineError as exc:
            entry.update(error_code=exc.code, latency_s=round(time.monotonic() - started, 3))
            raise
        # The parsed (schema-validated) response is kept for the restricted audit; raw request bytes are already on disk.
        entry.update(audit=result.audit, usage=result.usage, cost_usd=result.cost_usd, latency_s=round(time.monotonic() - started, 3),
                     response=result.parsed.model_dump() if hasattr(result.parsed, 'model_dump') else None)
        if self.journal:
            self.journal(entry)          # durable per-request record (restricted), written before the next request starts
        return result


def body_rows(bodies, model, pricing):
    price = pricing.price(model)
    rows = []
    for body in bodies:
        upper = input_upper_bound_tokens(body)
        rows.append({'model': model, 'items': len(items_of(body)), 'evidence_bytes': evidence_bytes(items_of(body)),
                     'body_bytes': len(json.dumps(body, ensure_ascii=False).encode('utf-8')), 'input_token_upper_bound': upper,
                     'max_output_tokens': body['max_tokens'], 'reserved_max_usd': round(max_cost_usd(price, upper, body['max_tokens']), 6),
                     'request_sha256': payload_sha256(body)})
    return rows


def plan_hash(summary):
    keys = ('smoke_version', 'limits', 'scope', 'selected_signatures', 'haiku_request_sha256', 'settings')
    return hashlib.sha256(json.dumps({k: summary[k] for k in keys}, sort_keys=True).encode()).hexdigest()


def dry_run(records, cfg, haiku_settings, sonnet_settings, pricing, gate, sponsor_index, authorized_file, authorized_sha256):
    """Build the exact smoke bodies offline (Recorder: no transport, no credential). Returns (summary, bodies)."""
    plan = smoke_plan(records, cfg)
    responders = {cfg.haiku_model: Recorder(haiku_settings)}
    if sonnet_settings and cfg.escalation_model:
        responders[cfg.escalation_model] = Recorder(sonnet_settings)
    router = Router(cfg, records, responders, gate=gate, pricing=pricing, sponsor_index=sponsor_index)
    router.run(plan)
    haiku = responders[cfg.haiku_model].bodies
    sonnet = responders[cfg.escalation_model].bodies if cfg.escalation_model in responders else []
    rows = body_rows(haiku, cfg.haiku_model, pricing) + (body_rows(sonnet, cfg.escalation_model, pricing) if sonnet else [])
    checks, detail = safety_checks(haiku + sonnet, records, authorized_file, authorized_sha256, sponsor_index)
    problems = []
    if len(haiku) > LIMITS['haiku_requests'] or any(len(items_of(b)) > LIMITS['haiku_items_per_request'] for b in haiku):
        problems.append('HAIKU_LIMIT')
    if len(sonnet) > LIMITS['sonnet_requests'] or any(len(items_of(b)) > LIMITS['sonnet_items_per_request'] for b in sonnet):
        problems.append('SONNET_LIMIT')
    worst = round(sum(r['reserved_max_usd'] for r in rows), 6)
    if worst > LIMITS['cap_usd']:
        problems.append('WORST_CASE_EXCEEDS_SMOKE_CAP')
    if router.oversized:
        problems.append('OVERSIZED_ITEMS')
    if checks['records_outside_authorized_workbook'] or checks['unexpected_item_fields'] or checks['findings'].get('target_not_in_authorized_records'):
        problems.append('SCOPE_CHECK_FAILED')
    summary = {'smoke_version': SMOKE_VERSION, 'limits': LIMITS, 'scope': cfg.scope, 'data_class': DATA_CLASS,
               'transmitted': False, 'api_key_read': False, 'trusted_origin': router.origin,
               'eligible_signatures_total': plan['eligible_signatures_total'],
               'selected_signatures': [g['signature_id'] for g in plan['groups']],
               'rows_covered_by_selected_signatures': sum(len(g['rows']) for g in plan['groups']),
               'haiku_request_sha256': [payload_sha256(b) for b in haiku],
               'sonnet_basis': 'HYPOTHETICAL: built as if every first-pass item were unresolved; the live body depends on Haiku outcomes',
               'settings': {m: {'max_tokens': s.max_tokens, 'timeout_seconds': s.timeout_seconds, 'max_retries': s.max_retries,
                                'api_key_env': s.api_key_env} for m, s in ((cfg.haiku_model, haiku_settings),
                                                                          (cfg.escalation_model, sonnet_settings)) if s},
               'requests': rows, 'worst_case_reserved_usd': worst,
               'expected_note': 'reserved_max_usd is the ledger reservation (input upper bound + full max_tokens); actual cost is usually far lower',
               'safety_checks': checks, 'problems': problems, 'ready': not problems}
    summary['plan_sha256'] = plan_hash(summary)
    return summary, {'haiku': haiku, 'sonnet': sonnet, 'detail': detail}


def refuse_resubmission(ledger, bodies, scope):
    """No automatic repetition: any reviewed body already known to the smoke ledger (any state) blocks the run."""
    seen = [request_key(b, scope) for b in bodies if ledger.state(request_key(b, scope)) is not None]
    open_rows = ledger.summary()['requests_by_state'].get('AMBIGUOUS', 0) + ledger.summary()['requests_by_state'].get('SENDING', 0)
    if seen or open_rows:
        raise EngineError('SMOKE_ALREADY_SUBMITTED', '%d reviewed request(s) already in the smoke ledger and %d unresolved; '
                          'no automatic resend. Resolve with the owner before any new smoke run.' % (len(seen), open_rows))


def live_run(records, cfg, providers, approved, gate, pricing, sponsor_index, ledger):
    """Send the reviewed smoke plan. `providers` are real AnthropicProviders sharing `ledger`; `approved` is the dry-run summary."""
    plan = smoke_plan(records, cfg)
    haiku = SmokeGuard(providers[cfg.haiku_model], LIMITS['haiku_requests'], LIMITS['haiku_items_per_request'],
                       approved_sha256=set(approved['haiku_request_sha256']))
    responders = {cfg.haiku_model: haiku}
    if cfg.escalation_model and cfg.escalation_model in providers:
        targets = {g['rep']['source_id'] for g in plan['groups']}
        responders[cfg.escalation_model] = SmokeGuard(providers[cfg.escalation_model], LIMITS['sonnet_requests'],
                                                      LIMITS['sonnet_items_per_request'], allowed_targets=targets)
    router = Router(cfg, records, responders, cache=None, gate=gate, pricing=pricing, sponsor_index=sponsor_index)
    error, ledger_rows = None, []
    try:
        ledger_rows = router.run(plan)
    except EngineError as exc:   # stop on the first refusal/failure; report what happened, never retry
        error = {'code': exc.code, 'message': str(exc)}
    calls = [c for g in responders.values() for c in g.calls]
    outcomes = {}
    for r in ledger_rows:
        if r['route'] == 'llm':
            outcomes[r['outcome']] = outcomes.get(r['outcome'], 0) + 1
    billed = sum(c.get('cost_usd') or 0.0 for c in calls)
    return {'smoke_version': SMOKE_VERSION, 'plan_sha256': approved['plan_sha256'], 'stopped_on_error': error,
            'requests_attempted': len(calls), 'requests': [{k: v for k, v in c.items() if k not in ('audit', 'response')} | {
                'request_id': (c.get('audit') or {}).get('request_id'), 'stop_reason': (c.get('audit') or {}).get('stop_reason')}
                for c in calls],
            'usage_totals': {k: sum(int((c.get('usage') or {}).get(k) or 0) for c in calls)
                             for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')},
            'cost_usd_from_reported_usage': round(billed, 9),
            'cost_note': 'computed from provider-reported usage x configured list prices; the provider console is authoritative',
            'projected_worst_case_usd': approved['worst_case_reserved_usd'],
            'representative_outcomes': outcomes, 'router_summary': router.summary(ledger_rows) if ledger_rows else None,
            'api_compatibility': {'requests_with_http_200_and_schema_valid_response': sum(1 for c in calls if c.get('audit') and not c.get('error_code')),
                                  'requests_failed': [{'model': c['model'], 'error_code': c['error_code']} for c in calls if c.get('error_code')],
                                  'stop_reasons': dict(Counter((c.get('audit') or {}).get('stop_reason') for c in calls if c.get('audit')))},
            'classification_outcomes': {'by_candidate_class': dict(Counter(r['candidate_class'] for r in ledger_rows if r['route'] == 'llm')),
                                        'rows_including_signature_propagation': dict(Counter(r['outcome'] for r in ledger_rows if r['outcome'])),
                                        'status_counts': dict(Counter(r['status'] for r in ledger_rows))},
            'ledger': ledger.summary(), 'ledger_rows': ledger_rows,
            'responses': [{'request_sha256': c['request_sha256'], 'request_id': (c.get('audit') or {}).get('request_id'),
                           'response': c.get('response')} for c in calls]}
