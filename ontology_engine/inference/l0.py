"""L0: local construction and inspection of the exact intended request bodies. Nothing is ever sent.

Guarantees:
- the owner-decision gate (L0) is checked BEFORE any sponsor record is read or any payload is built;
- request bodies come from the real router, prompt, schema, model settings and packing rules, but the responder is a
  recorder: it has no transport, never reads credentials and returns an empty response;
- Haiku bodies are the intended first pass; Sonnet bodies are HYPOTHETICAL (the escalation set depends on Haiku outcomes),
  built at the escalation cap so that worst-case reservations are visible;
- every figure is a local measurement of bytes or a projection; none is a billed amount.
"""
import json
import math
import re
from collections import Counter
from ..contextual import ContextResponse
from ..errors import EngineError
from .anthropic_provider import AnthropicSettings, ProviderResult, build_request, input_upper_bound_tokens
from .pricing import max_cost_usd
from .router import Router, RoutingConfig, evidence_bytes, plan_routes

L0_VERSION = 'l0-inspection-v1'
ALLOWED_ITEM_FIELDS = {'target_source_id', 'point_key', 'name', 'units', 'units_original', 'bacnet_type', 'row_count',
                       'example_containers', 'station_count', 'description', 'prior_attempt', 'equipment_family'}
INJECTION = re.compile(r'(ignore|disregard|override)\s+(all|any|the|previous|prior|above)|system\s*prompt|you\s+are\s+(now|an?)\b|'
                       r'\b(assistant|system|user)\s*:|<\s*/?\s*[a-z][a-z0-9]*\s*>|```|\{\{|\binstructions?\b|api[\s_-]?key|\bpassword\b|\btoken\b', re.I)
SENSITIVE = {'email': re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), 'url': re.compile(r'https?://|www\.', re.I),
             'ipv4': re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), 'phone': re.compile(r'\+?\d[\d\s().-]{9,}\d'),
             'secret_like': re.compile(r'sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9+/]{40,}={0,2}'),
             'control_chars': re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')}
LONG_VALUE = 256
# Standard (RFC 9562) UUID text form: version nibble 1-8, variant 10xx; hex digits in either case.
_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}'
STANDARD_UUID = re.compile(_UUID)
# A point_key is exempt from the phone pattern only when it IS a technical point identifier: exactly the router's form
# 'point/<UUID>' (router.item_evidence builds 'point/' + point_id; the prefix is case-sensitive), or a bare UUID.
# UUIDs in other fields, embedded in text, with any other prefix/suffix, and nil/odd identifiers are still flagged.
POINT_KEY_UUID = re.compile(r'(?:point/)?' + _UUID)
UUID_PHONE_EXEMPT_FIELDS = {'point_key'}


def phone_exempt_uuid(field, value):
    return field in UUID_PHONE_EXEMPT_FIELDS and POINT_KEY_UUID.fullmatch(value) is not None


class Recorder:
    """Responder stand-in for L0. It records request bodies and has NO transport and NO credential access."""
    live_enabled = False

    def __init__(self, settings):
        self.settings, self.model, self.bodies = settings, settings.model, []

    def make_body(self, system_prompt, content, response_type):
        return build_request(self.settings, system_prompt, content, response_type)

    def complete(self, body, response_type, scope='', data_origin=''):
        self.bodies.append(body)
        return ProviderResult(parsed=ContextResponse(candidates=[], abstentions=[]), usage={}, cost_usd=0.0, audit={'not_sent': True})


def settings_from_decisions(gate):
    """Derive provider settings from GRANTED owner decisions only (the caller has already required the L0 gate)."""
    d = {k: (gate.decisions.get(k) or {}).get('value') for k in ('P-1', 'P-2', 'P-5', 'P-6', 'P-7')}
    if d['P-1'] != 'anthropic':
        raise EngineError('L0_UNSUPPORTED_PROVIDER', 'L0 inspection is implemented for the Anthropic provider only.')
    if 'raw_bas_point_metadata' not in d['P-7']['allowed_data_classes']:
        raise EngineError('L0_DATA_CLASS_NOT_ALLOWED', 'P-7 does not allow raw_bas_point_metadata; no payload is built.')
    models = d['P-2']
    haiku = 'claude-haiku-5-5' if 'claude-haiku-5-5' in models else None
    sonnet = 'claude-sonnet-5-5' if 'claude-sonnet-5-5' in models else None
    if not haiku:
        raise EngineError('L0_PRIMARY_MODEL_MISSING', 'P-2 must include the primary model claude-haiku-5-5.')
    limits = d['P-6']

    def settings(model):
        return AnthropicSettings(model=model, max_tokens=limits['max_tokens'].get(model, 8192 if model == haiku else 16384),
                                 timeout_seconds=limits['timeout_seconds'], max_retries=limits['max_retries'])
    return {'haiku': settings(haiku), 'sonnet': settings(sonnet) if sonnet else None, 'cap_usd': d['P-5']['cap_usd'],
            'allowed_data_classes': d['P-7']['allowed_data_classes']}


def build_l0_requests(records, cfg, haiku_settings, sonnet_settings, pricing, gate, sponsor_index=None):
    responders = {cfg.haiku_model: Recorder(haiku_settings)}
    if sonnet_settings and cfg.escalation_model:
        responders[cfg.escalation_model] = Recorder(sonnet_settings)
    router = Router(cfg, records, responders, gate=gate, pricing=pricing, sponsor_index=sponsor_index)
    plan = plan_routes(records, cfg)
    ledger = router.run(plan)
    return router, plan, ledger, responders


def _items(body):
    return json.loads(body['messages'][0]['content'])['untrusted_records']


def body_metrics(body, model, pricing, cfg):
    items = _items(body)
    body_bytes = len(json.dumps(body, ensure_ascii=False).encode('utf-8'))
    upper = input_upper_bound_tokens(body)
    price = pricing.price(model)
    expected_in = math.ceil(body_bytes / cfg.bytes_per_token_expected)
    expected_out = math.ceil(len(items) * cfg.expected_output_bytes_per_item / cfg.bytes_per_token_expected)
    return {'model': model, 'items': len(items), 'body_bytes': body_bytes, 'evidence_bytes': evidence_bytes(items),
            'input_token_upper_bound': upper, 'max_output_tokens_reserved': body['max_tokens'],
            'reserved_cost_usd': max_cost_usd(price, upper, body['max_tokens']),
            'expected_input_tokens': expected_in, 'expected_output_tokens_excl_thinking': expected_out,
            'expected_cost_usd': (expected_in * price.input_usd_per_mtok + expected_out * price.output_usd_per_mtok) / 1e6}


def safety_checks(bodies, records, authorized_file, authorized_sha256, sponsor_index):
    """Scope, data-class and content checks over the exact bodies. Returns (aggregate, restricted_detail)."""
    record_ids = {r.source_id for r in records}
    out_of_scope_records = [r.source_id for r in records if r.source_file != authorized_file or (r.source_hash or '').lower() != authorized_sha256]
    foreign_names = {n for n in sponsor_index.names if n != authorized_file}
    foreign_hashes = {h for h in sponsor_index.hashes if h != authorized_sha256}
    findings, detail = Counter(), []
    field_bytes = Counter()
    unexpected_fields = Counter()
    for body in bodies:
        text = json.dumps(body, ensure_ascii=False)
        for name in foreign_names:
            if name in text:
                findings['foreign_sponsor_file_named'] += 1
        for h in foreign_hashes:
            if h in text.lower():
                findings['foreign_sponsor_hash_present'] += 1
        if 'sk-ant' in text or 'x-api-key' in text.lower():
            findings['credential_marker'] += 1
        for item in _items(body):
            target = item.get('target_source_id')
            if target not in record_ids:
                findings['target_not_in_authorized_records'] += 1
            for key, value in item.items():
                field_bytes[key] += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
                if key not in ALLOWED_ITEM_FIELDS:
                    unexpected_fields[key] += 1
                values = value if isinstance(value, list) else [value]
                for v in values:
                    if not isinstance(v, str):
                        continue
                    if INJECTION.search(v):
                        findings['injection_like_text'] += 1
                        detail.append({'target_source_id': target, 'field': key, 'check': 'injection_like_text'})
                    for label, pattern in SENSITIVE.items():
                        if pattern.search(v):
                            if label == 'phone' and phone_exempt_uuid(key, v):
                                findings['phone_pattern_classified:standard_uuid_point_key'] += 1
                                continue
                            findings['sensitive_pattern:' + label] += 1
                            detail.append({'target_source_id': target, 'field': key, 'check': label})
                    if len(v) > LONG_VALUE:
                        findings['long_value'] += 1
                        detail.append({'target_source_id': target, 'field': key, 'check': 'long_value'})
    aggregate = {'records_outside_authorized_workbook': len(out_of_scope_records), 'findings': dict(sorted(findings.items())),
                 'unexpected_item_fields': dict(unexpected_fields), 'bytes_by_item_field': dict(field_bytes.most_common()),
                 'data_classes_present': ['raw_bas_point_metadata'], 'drawing_or_ocr_content': False,
                 'note': 'Detail rows (ids and field names only) are in the restricted directory; no values are reported here.'}
    return aggregate, detail


def summarize(router, plan, ledger, responders, pricing, cfg, cap_usd):
    per_model, metrics = {}, []
    for model, rec in responders.items():
        rows = [body_metrics(b, model, pricing, cfg) for b in rec.bodies]
        metrics += rows
        per_model[model] = {'packed_requests': len(rows), 'items': sum(r['items'] for r in rows),
                            'body_bytes_total': sum(r['body_bytes'] for r in rows),
                            'evidence_bytes_max': max((r['evidence_bytes'] for r in rows), default=0),
                            'body_bytes_max': max((r['body_bytes'] for r in rows), default=0),
                            'input_token_upper_bound_total': sum(r['input_token_upper_bound'] for r in rows),
                            'max_output_tokens_reserved_total': sum(r['max_output_tokens_reserved'] for r in rows),
                            'reserved_cost_usd_total': round(sum(r['reserved_cost_usd'] for r in rows), 6),
                            'expected_cost_usd_total': round(sum(r['expected_cost_usd'] for r in rows), 6),
                            'basis': 'first pass' if model == cfg.haiku_model else 'HYPOTHETICAL escalation at the cap (actual set depends on first-pass outcomes)'}
    routes = Counter(r['route'] for r in ledger)
    held = {k: v for k, v in routes.items() if k.startswith('held') or k.startswith('out_of_scope')}
    worst = sum(v['reserved_cost_usd_total'] for v in per_model.values())
    return {'l0_version': L0_VERSION, 'measured_locally': 'byte sizes and token upper bounds of bodies that were built but NOT sent',
            'projection_only': 'expected and reserved costs; no API billing exists',
            'records_total': len(ledger), 'records_held_preserved': sum(held.values()), 'held_by_route': held,
            'deterministic_classifications': routes.get('deterministic', 0),
            'signature_items': len(plan['groups']), 'rows_covered_by_signatures': sum(len(g['rows']) for g in plan['groups']),
            'oversized_items': len(router.oversized),
            'unresolved_after_l0': sum(1 for r in ledger if r['candidate_class'] == 'unknown'),
            'per_model': per_model, 'worst_case_reserved_usd': round(worst, 6),
            'owner_cap_usd': cap_usd, 'worst_case_within_cap': worst <= cap_usd if cap_usd is not None else None}, metrics


def batch_variants(records, cfg_kwargs, haiku_settings, pricing, gate, sponsor_index=None, variants=((20, 3), (40, 3), (60, 3), (40, 0))):
    """Packing alternatives for the first pass only. Accuracy effects are unknown offline and are NOT estimated."""
    out = []
    for items, examples in variants:
        settings = haiku_settings.model_copy(update={'max_tokens': max(haiku_settings.max_tokens, 8192 if items <= 40 else 16384)})
        cfg = RoutingConfig(**{**cfg_kwargs, 'max_items_per_batch': items, 'max_example_containers': examples, 'escalation_model': None,
                               'haiku_max_tokens': settings.max_tokens})
        router, plan, ledger, responders = build_l0_requests(records, cfg, settings, None, pricing, gate, sponsor_index)
        rows = [body_metrics(b, cfg.haiku_model, pricing, cfg) for b in responders[cfg.haiku_model].bodies]
        out.append({'max_items_per_batch': items, 'example_containers': examples, 'max_tokens': settings.max_tokens,
                    'packed_requests': len(rows), 'body_bytes_total': sum(r['body_bytes'] for r in rows),
                    'expected_cost_usd': round(sum(r['expected_cost_usd'] for r in rows), 6),
                    'reserved_cost_usd': round(sum(r['reserved_cost_usd'] for r in rows), 6),
                    'oversized_items': len(router.oversized)})
    return out


def choose_previews(responders, cfg):
    """Three representative bodies: repeated signature, sparse singletons, hypothetical escalation."""
    haiku = responders[cfg.haiku_model].bodies
    previews = {}
    if haiku:
        previews['repeated_signature'] = max(haiku, key=lambda b: max(int(i['row_count']) for i in _items(b)))
        previews['sparse_singletons'] = max(haiku, key=lambda b: sum(1 for i in _items(b) if i['row_count'] == '1') / len(_items(b)))
    if cfg.escalation_model in responders and responders[cfg.escalation_model].bodies:
        previews['hypothetical_escalation'] = responders[cfg.escalation_model].bodies[0]
    return previews


def preview_review(previews, request_bodies, records, authorized_file, authorized_sha256, sponsor_index, pricing, cfg):
    """Owner-facing review of the L0 previews: fields, privacy results, provenance back to source rows, sizes and hashes.
    Returns (aggregate without sponsor values, restricted detail with ids/field names only)."""
    import hashlib
    from .router import SYSTEM_PROMPT, PROMPT_VERSION, extract_facts
    from .anthropic_provider import schema_sha256

    def sha(body):
        return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode('utf-8')).hexdigest()
    by_id = {r.source_id: r for r in records}
    known = {sha(b) for b in request_bodies}
    out, detail = {}, {}
    for name, body in sorted(previews.items()):
        items = _items(body)
        metrics = body_metrics(body, body['model'], pricing, cfg)
        checks, rows = safety_checks([body], records, authorized_file, authorized_sha256, sponsor_index)
        mismatches, resolved = Counter(), 0
        for item in items:
            record = by_id.get(item.get('target_source_id'))
            if record is None:
                mismatches['target_unresolved'] += 1
                continue
            resolved += 1
            facts = extract_facts(record, cfg)
            for field, expected in (('point_key', 'point/' + facts['point_id']), ('name', facts['name']), ('units', facts['units']),
                                    ('bacnet_type', facts['kind'])):
                if item.get(field) != expected:
                    mismatches['field_differs_from_source:' + field] += 1
            if record.parse_status != 'ok':
                mismatches['source_row_not_ok'] += 1
        out[name] = {'model': body['model'],
                     'basis': 'HYPOTHETICAL escalation' if any('prior_attempt' in i for i in items) else 'first pass',
                     'request_sha256': sha(body), 'identical_to_a_built_request_body': sha(body) in known,
                     'items': len(items), 'fields_present': dict(sorted(Counter(k for i in items for k in i).items())),
                     'fields_outside_allow_list': sorted({k for i in items for k in i} - ALLOWED_ITEM_FIELDS),
                     'body_bytes': metrics['body_bytes'], 'evidence_bytes': metrics['evidence_bytes'],
                     'input_token_upper_bound': metrics['input_token_upper_bound'], 'max_output_tokens': metrics['max_output_tokens_reserved'],
                     'reserved_cost_usd': round(metrics['reserved_cost_usd'], 6), 'expected_cost_usd': round(metrics['expected_cost_usd'], 6),
                     'privacy_findings': checks['findings'], 'records_outside_authorized_workbook': checks['records_outside_authorized_workbook'],
                     'provenance': {'targets_resolved_to_authorized_rows': resolved, 'targets_total': len(items),
                                    'payload_vs_source_mismatches': dict(mismatches), 'source_workbook_sha256': authorized_sha256},
                     'system_prompt_sha256': hashlib.sha256(body['system'].encode()).hexdigest(),
                     'system_prompt_is_router_prompt': body['system'] == SYSTEM_PROMPT, 'prompt_version': PROMPT_VERSION,
                     'response_schema_sha256': schema_sha256(ContextResponse), 'max_tokens': body['max_tokens'],
                     'output_config': body.get('output_config', {}).get('effort'), 'thinking': body.get('thinking')}
        detail[name] = rows
    return out, detail
