"""Deterministic-first routing and bounded LLM escalation for point classification (FR-02/04/05/11).

Flow: preserve every source record -> hold unusable records (never dropped) -> deterministic exact rules ->
group the rest by signature -> one LLM request item per signature (batched by container family) -> strict validation
through the EXISTING scope audit -> bounded Sonnet escalation of unresolved items -> propagate validated candidates to
group members -> routing ledger. A deterministic route does not raise evidence status: only the existing eligibility
gate can mark a documented class `supported_inference`; naming-based and model-based results stay `needs_review`;
nothing here can produce `verified`. No physical topology (hasPoint/hasPart/feeds) is proposed or accepted.
"""
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional
from pydantic import Field
from ..contextual import ContextResponse, SYSTEM_PROMPT as CONTEXT_PROMPT, scope_audit, abstention_audit
from ..errors import EngineError
from ..evidence import eligibility
from ..models import Model
from ..retrieval import ContextBatch, RetrievedRecord
from ..vocabulary import POINTS, POINT_NAMES, canonical_point_class
from .anthropic_provider import AnthropicSettings, flatten_schema, schema_sha256, build_request, input_upper_bound_tokens
from .cache import cache_key
from .gate import OwnerDecisionGate, require_live_authorization
from .pricing import max_cost_usd
from .provenance import SponsorIndex, trusted_origin
from .cache import cache_components
from .units import normalize_unit_variant
from . import class_checks

POLICY_VERSION = 'routing-policy-v1'
PROMPT_VERSION = 'point-signature-v1'
SYSTEM_PROMPT = CONTEXT_PROMPT + '''
Task: for every item in untrusted_records propose exactly one candidate with predicate "type" for its point_key
(a point class from the allowed list, or "unknown"), citing target_source_id, OR abstain naming the missing prerequisites.
row_count is the number of source rows sharing this signature. Do not propose hasPoint, hasPart or feeds in this task.
example_containers are naming context only and never prove equipment identity or topology.'''


class RoutingConfig(Model):
    scope: str
    data_origin: str = 'unverified'   # a claim only; Router derives the trusted origin from the records
    haiku_model: str = 'claude-haiku-5-5'
    escalation_model: Optional[str] = 'claude-sonnet-5-5'
    max_items_per_batch: int = Field(default=40, ge=1, le=200)
    escalation_items_per_batch: int = Field(default=10, ge=1, le=100)
    escalation_max_batch_bytes: int = Field(default=24_000, ge=200)   # smaller: thinking tokens are billed inside max_tokens
    haiku_max_tokens: int = Field(default=8192, ge=1024, le=32768)       # ~90 tokens/item * 40 items = ~3.6K expected
    escalation_max_tokens: int = Field(default=16384, ge=1024, le=32768)
    max_batch_bytes: int = Field(default=24_000, ge=200)   # exact serialized bytes of untrusted_records per request
    max_example_containers: int = Field(default=3, ge=0, le=10)
    max_escalation_fraction: float = Field(default=0.2, ge=0, le=1)
    min_rows_for_escalation: int = Field(default=1, ge=1)
    require_kind: bool = True
    enable_deterministic: bool = True
    group_signatures: bool = True
    bytes_per_token_expected: float = Field(default=3.0, gt=0)
    expected_output_bytes_per_item: int = Field(default=260, ge=1)
    name_key: str = 'metadata: normal: name'
    path_key: str = 'metadata: fox: path'
    station_key: str = 'metadata: fox: station name'
    kind_key: str = 'metadata: bacnet type'
    units_header: str = 'Metadata: Normal: Display Units'


def provider_settings(cfg):
    """Recommended per-model settings that satisfy the routing config's token bounds."""
    out = {cfg.haiku_model: AnthropicSettings(model=cfg.haiku_model, max_tokens=cfg.haiku_max_tokens)}
    if cfg.escalation_model:
        out[cfg.escalation_model] = AnthropicSettings(model=cfg.escalation_model, max_tokens=cfg.escalation_max_tokens)
    return out


# ---- facts ------------------------------------------------------------------------------------

def container_hint(path):
    segs = [s for s in (path or '').strip('/').split('/') if s]
    if len(segs) >= 2 and segs[-1].lower() == 'points':
        return segs[-2]
    return segs[-1] if segs else ''


def network_of(path):
    segs = [s for s in (path or '').strip('/').split('/') if s]
    return segs[1] if len(segs) > 1 else ''


def extract_facts(record, cfg):
    f = record.normalized_fields
    units_engine = f.get('units', '')
    original = units_engine
    for cell in record.raw_record.values():
        if cell.get('column') == cfg.units_header:
            original = '' if cell.get('value') is None else str(cell['value'])
            break
    unit = normalize_unit_variant(original, units_engine)
    path = f.get(cfg.path_key, '')
    return {'source_id': record.source_id, 'uid': record.source_record_uid, 'point_id': f.get('point_id', ''),
            'name': f.get(cfg.name_key, ''), 'description': f.get('description', ''), 'path': path,
            'hint': container_hint(path), 'network': network_of(path), 'station': f.get(cfg.station_key, ''),
            'kind': f.get(cfg.kind_key, ''), 'units_original': unit.original, 'units': unit.normalized,
            'unit_rule': unit.rule_id, 'explicit_class': canonical_point_class(f.get('point_class', '')),
            'family': _family(path, f.get(cfg.station_key, ''))}


def _family(path, station):
    from ..building_graph import equipment_family
    return equipment_family(path, station)


def signature_id(facts):
    # Equipment family is part of the signature: the same name can mean different things in a VAV box, a lab airflow valve
    # and a fan coil. Station and container are NOT (the same point is often exposed through several stations).
    key = json.dumps([facts['name'].casefold(), facts['units'], facts['kind'], facts['description'].casefold(), facts.get('family', '')],
                     ensure_ascii=False)
    return 'sig:' + hashlib.sha256(key.encode()).hexdigest()[:16]


# ---- deterministic stage ----------------------------------------------------------------------

class ConventionRegistry:
    """Building-scoped, provenance-bearing reusable conventions (e.g. from human-reviewed earlier decisions)."""
    ALLOWED = {'needs_review', 'supported_inference', 'verified'}

    def __init__(self, entries, scope):
        self.by_key = {}
        for e in entries:
            if e.get('scope') != scope:
                raise EngineError('REGISTRY_SCOPE_MISMATCH', 'Convention %r belongs to another building scope; refused.' % e.get('decision_id'))
            if e.get('status') not in self.ALLOWED or canonical_point_class(e.get('point_class', '')) not in POINTS:
                raise EngineError('REGISTRY_INVALID', 'Convention has invalid status or class.')
            if not e.get('decision_id') or not e.get('evidence_source_ids'):
                raise EngineError('REGISTRY_INVALID', 'Convention needs decision_id and evidence_source_ids provenance.')
            if e['status'] == 'verified' and not e.get('verified_by'):
                raise EngineError('REGISTRY_INVALID', '`verified` requires a named documentary/human authority.')
            self.by_key[(e['name'].casefold(), e.get('units') or None)] = e

    def match(self, facts):
        return self.by_key.get((facts['name'].casefold(), facts['units'])) or self.by_key.get((facts['name'].casefold(), None))


def deterministic(record, facts, registry):
    """Return (class, status, rule_id, reason) or None."""
    cls = facts['explicit_class'] if facts['explicit_class'] in POINTS else POINT_NAMES.get(facts['description'].lower())
    if cls:
        subject = 'point/' + facts['point_id']
        gate = eligibility(subject, 'type', cls, [record.source_id], {record.source_id: record}, 'direct_document', 'Documented class or exact description.')
        return cls, ('supported_inference' if gate['eligible'] else 'needs_review'), 'documentary_metadata', gate['reason_codes']
    spaced = re.sub(r'[_\-\s]+', ' ', facts['name']).strip().casefold()
    if spaced in POINT_NAMES:
        return POINT_NAMES[spaced], 'needs_review', 'name_exact_vocabulary', ['NAMING_CUE_ONLY']
    if registry is not None:
        entry = registry.match(facts)
        if entry:
            return (canonical_point_class(entry['point_class']), entry['status'], 'registry:' + entry['decision_id'],
                    ['BUILDING_SCOPED_CONVENTION'])
    return None


def pre_route(record, facts, cfg):
    if record.parse_status != 'ok':
        reasons = ['DUPLICATE_IDENTITY' if 'Duplicate identifier' in record.errors else 'SOURCE_ERROR'] + sorted(set(record.errors))
        return 'held_source_error', reasons
    if not facts['name']:
        return 'held_missing_name', ['NO_POINT_NAME']
    if cfg.require_kind and not facts['kind']:
        return 'held_kind_unclear', ['NO_SOURCE_KIND']
    return None, []


# ---- planning ---------------------------------------------------------------------------------

def item_evidence(group, cfg):
    rep = group['rep']
    hints = Counter(r['hint'] for r in group['rows'] if r['hint'])
    examples = [h for h, _ in sorted(hints.items(), key=lambda kv: (-kv[1], kv[0]))][:cfg.max_example_containers]
    evidence = {'target_source_id': rep['source_id'], 'point_key': 'point/' + rep['point_id'], 'name': rep['name'],
                'units': rep['units'], 'bacnet_type': rep['kind'], 'row_count': str(len(group['rows'])),
                'example_containers': examples, 'station_count': str(len({r['station'] for r in group['rows']})),
                'equipment_family': rep.get('family', '') or 'unknown'}
    if rep['description']:
        evidence['description'] = rep['description']
    if rep['units_original'] != rep['units'] and rep['units_original']:
        evidence['units_original'] = rep['units_original']
    return evidence


def estimate_item_bytes(evidence):
    """Size estimate from field lengths only (no payload serialization): used for sponsor-data planning."""
    return sum(len(k) + (sum(len(x) for x in v) if isinstance(v, list) else len(str(v))) + 8 for k, v in evidence.items()) + 24


def plan_routes(records, cfg, registry=None):
    held, det, groups, order = [], [], {}, {}
    for index, record in enumerate(records):
        facts = extract_facts(record, cfg)
        if record.table_kind != 'points':
            held.append({'facts': facts, 'route': 'out_of_scope_table_kind', 'reasons': ['TABLE_KIND_' + record.table_kind.upper()]})
            continue
        route, reasons = pre_route(record, facts, cfg)
        if route:
            held.append({'facts': facts, 'route': route, 'reasons': reasons})
            continue
        result = deterministic(record, facts, registry) if cfg.enable_deterministic else None
        if result:
            det.append({'facts': facts, 'class': result[0], 'status': result[1], 'rule': result[2], 'reasons': result[3]})
            continue
        sid = signature_id(facts) if cfg.group_signatures else 'row:' + facts['source_id']
        group = groups.setdefault(sid, {'signature_id': sid, 'rep': facts, 'rows': []})
        group['rows'].append(facts)
        order.setdefault(sid, index)
    ordered = sorted(groups.values(), key=lambda g: (g['rep']['network'], g['rep']['hint'], g['rep']['name'].casefold(), g['signature_id']))
    for g in ordered:
        g['evidence'] = item_evidence(g, cfg)
        g['bytes'] = estimate_item_bytes(g['evidence'])
    batches, current, size = [], [], 0
    for g in ordered:
        if current and (len(current) >= cfg.max_items_per_batch or size + g['bytes'] > cfg.max_batch_bytes):
            batches.append(current)
            current, size = [], 0
        current.append(g)
        size += g['bytes']
    if current:
        batches.append(current)
    return {'held': held, 'deterministic': det, 'groups': ordered, 'batches': batches, 'config': cfg}


def project_costs(plan, pricing, escalation_fraction=None):
    """Projection only (estimates, never measurements): Haiku pass plus an assumed Sonnet escalation share."""
    cfg = plan['config']
    f = cfg.max_escalation_fraction if escalation_fraction is None else escalation_fraction
    sys_bytes = len(SYSTEM_PROMPT.encode()) + len(json.dumps(flatten_schema(ContextResponse)).encode())
    n_items = sum(len(b) for b in plan['batches'])
    item_bytes = sum(g['bytes'] for g in plan['groups'])
    def tier(model, items, requests, item_b, max_tokens):
        if not requests:
            return {'model': model, 'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'cost_upper_bound_usd': 0.0}
        in_bytes = requests * (sys_bytes + 200) + item_b
        out_bytes = items * cfg.expected_output_bytes_per_item
        price = pricing.price(model)
        in_tok = math.ceil(in_bytes / cfg.bytes_per_token_expected)
        out_tok = math.ceil(out_bytes / cfg.bytes_per_token_expected)
        rate_in, rate_out = (price.input_usd_per_mtok, price.output_usd_per_mtok)
        upper_in = in_bytes + requests * 256
        upper = (upper_in * rate_in + requests * max_tokens * rate_out) / 1e6
        return {'model': model, 'requests': requests, 'input_tokens': in_tok, 'output_tokens': out_tok,
                'cost_usd': (in_tok * rate_in + out_tok * rate_out) / 1e6, 'cost_upper_bound_usd': upper}
    haiku = tier(cfg.haiku_model, n_items, len(plan['batches']), item_bytes, cfg.haiku_max_tokens)
    esc_items = math.floor(f * n_items)
    esc_requests = math.ceil(esc_items / cfg.escalation_items_per_batch) if esc_items else 0
    esc = tier(cfg.escalation_model, esc_items, esc_requests, item_bytes * (esc_items / n_items if n_items else 0), cfg.escalation_max_tokens) if cfg.escalation_model else None
    # thinking tokens bill as output: escalation output is bounded by max_tokens, not by the item estimate
    return {'assumption': 'estimates from byte sizes; not measured API results', 'haiku': haiku, 'escalation': esc,
            'total_cost_usd': haiku['cost_usd'] + (esc['cost_usd'] if esc else 0.0),
            'total_cost_upper_bound_usd': haiku['cost_upper_bound_usd'] + (esc['cost_upper_bound_usd'] if esc else 0.0)}


# ---- execution --------------------------------------------------------------------------------

def _batch_for(items, records_by_id):
    recs = []
    for g in items:
        rec = records_by_id[g['rep']['source_id']]
        recs.append(RetrievedRecord(source_id=rec.source_id, source_record_uid=rec.source_record_uid, source_hash=rec.source_hash,
                                    table_kind='points', fields={k: v for k, v in g['evidence'].items() if isinstance(v, str)},
                                    reason_codes=['SIGNATURE_REPRESENTATIVE'], truncated_fields=[]))
    ids = [r.source_id for r in recs]
    bid = 'batch:' + hashlib.sha256('|'.join(ids).encode()).hexdigest()[:16]
    return ContextBatch(batch_id=bid, target_source_ids=ids, records=recs, context_bytes=0, omitted_relevant_records=0)


def _item_record(g, extra=None):
    e = dict(g['evidence'])
    if extra and g['signature_id'] in extra:
        e['prior_attempt'] = extra[g['signature_id']]
    return e


def evidence_bytes(records):
    """Exact UTF-8 size of the untrusted_records list as it is embedded in the request content."""
    return len(json.dumps(records, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def _content(items, cfg, extra=None):
    records = [_item_record(g, extra) for g in items]
    return {'task': 'classify_point_signatures', 'site_scope': cfg.scope, 'prompt_version': PROMPT_VERSION, 'untrusted_records': records}


def _resolve(item, audit, abstentions):
    target = item['rep']['source_id']
    mine = [a for a in audit if a['proposal']['target_source_id'] == target]
    extra_reasons = []
    valid = []
    for a in mine:
        if a['proposal']['predicate'] != 'type':
            extra_reasons.append('OUT_OF_PHASE_PREDICATE')
        elif a['status'] == 'needs_review':
            valid.append(a)
    classes = sorted({canonical_point_class(a['proposal']['object_id']) for a in valid})
    reasons = sorted({r for a in mine for r in a['reason_codes'] if r != 'MODEL_CANDIDATE_ONLY'} | set(extra_reasons))
    absts = [x for x in abstentions if x['abstention']['target_source_id'] == target and x['status'] == 'needs_review']
    if len(classes) == 1:
        return {'outcome': 'resolved_unknown' if classes[0] == 'unknown' else 'resolved', 'class': classes[0], 'alternatives': [],
                'reasons': reasons, 'missing': []}
    if len(classes) > 1:
        return {'outcome': 'conflict', 'class': 'unknown', 'alternatives': classes, 'reasons': reasons, 'missing': []}
    if absts:
        return {'outcome': 'abstained', 'class': 'unknown', 'alternatives': [],
                'reasons': reasons, 'missing': sorted({m for x in absts for m in x['abstention']['missing_prerequisites']})}
    return {'outcome': 'rejected' if mine else 'no_response', 'class': 'unknown', 'alternatives': [], 'reasons': reasons, 'missing': []}


UNRESOLVED = {'conflict', 'abstained', 'rejected', 'no_response', 'check_failed'}


class Router:
    def __init__(self, cfg, records, responders, cache=None, gate=None, pricing=None, registry=None, sponsor_index=None):
        self.cfg, self.records = cfg, records
        # Trusted origin: a caller cannot relabel sponsor records as synthetic (security review).
        self.origin = trusted_origin(records, cfg.data_origin, sponsor_index or SponsorIndex())
        self.oversized = []
        self.by_id = {r.source_id: r for r in records}
        self.responders, self.cache, self.gate = responders, cache, gate
        self.pricing, self.registry = pricing, registry
        self.stray = []   # candidates rejected because they targeted rows outside their batch (kept for audit, never attributed to an item)
        self.stats = {'requests': Counter(), 'cache_hits': Counter(), 'input_tokens': Counter(), 'output_tokens': Counter(), 'cost_usd': Counter()}
        self.schema_sha = schema_sha256(ContextResponse)

    def _components(self, model, item, extra):
        settings = self.responders[model].settings
        profile_settings = {'max_tokens': settings.max_tokens, 'effort': settings.effort, 'thinking_policy': 'model-profile'}
        evidence = dict(item['evidence'])
        if extra:
            evidence['prior_attempt'] = extra
        return cache_components(evidence, {'task': 'classify_point_signatures'}, model, PROMPT_VERSION, self.schema_sha, profile_settings,
                                'anthropic', settings.api_version, self.cfg.scope, POLICY_VERSION)

    def _violation(self, model, responder, chunk, extra, max_items, max_bytes):
        """First request limit an exact measurement of this chunk would break, or None."""
        if len(chunk) > max_items:
            return 'ITEM_COUNT'
        if evidence_bytes([_item_record(g, extra) for g in chunk]) > max_bytes:
            return 'EVIDENCE_BYTES'
        body = responder.make_body(SYSTEM_PROMPT, _content(chunk, self.cfg, extra), ContextResponse)
        upper = input_upper_bound_tokens(body)
        if upper > responder.settings.max_input_tokens_per_request:
            return 'REQUEST_INPUT_TOKENS'
        if self.pricing is not None and max_cost_usd(self.pricing.price(model), upper, body['max_tokens']) > responder.settings.max_cost_per_request_usd:
            return 'REQUEST_COST_BOUND'
        if math.ceil(len(chunk) * self.cfg.expected_output_bytes_per_item / self.cfg.bytes_per_token_expected) > 0.8 * body['max_tokens']:
            return 'OUTPUT_TOKENS'   # a truncated response is still billed
        return None

    def _pack(self, model, responder, items, extra, max_items, max_bytes):
        """Greedy packing on exact measurements. An item that cannot fit alone is returned as oversized, never sent."""
        chunks, oversized, current = [], {}, []
        for g in items:
            if self._violation(model, responder, current + [g], extra, max_items, max_bytes) is None:
                current.append(g)
                continue
            if current:
                chunks.append(current)
                current = []
            alone = self._violation(model, responder, [g], extra, max_items, max_bytes)
            if alone is None:
                current = [g]
            else:
                oversized[g['signature_id']] = alone
        if current:
            chunks.append(current)
        return chunks, oversized

    def _cached(self, model, g, extra):
        components = self._components(model, g, extra)
        hit = self.cache.get(components) if self.cache else None
        if not hit:
            return None
        try:
            response = ContextResponse.model_validate(hit['payload']['response'])
        except (KeyError, TypeError, ValueError):
            self.cache._reject(cache_key(components), 'CACHED_PAYLOAD_SCHEMA_INVALID', 'accidental_or_unauthenticated')
            return None
        batch = _batch_for([g], self.by_id)
        res = _resolve(g, scope_audit(response, batch, self.records), abstention_audit(response, batch))  # always re-audited
        provenance = hit.get('provenance') if isinstance(hit.get('provenance'), dict) else {}
        res.update(model=model, cache_hit=True, batch_id=provenance.get('batch_id'))
        return _checked(res, g)

    def _call_model(self, model, items, extra=None, max_items=None, max_bytes=None):
        """Resolve each item for `model`: cache first, then requests whose ACTUAL size obeys every limit."""
        max_items = max_items or self.cfg.max_items_per_batch
        max_bytes = max_bytes or self.cfg.max_batch_bytes
        results, misses = {}, []
        for g in items:
            res = self._cached(model, g, (extra or {}).get(g['signature_id']))
            if res:
                results[g['signature_id']] = res
                self.stats['cache_hits'][model] += 1
            else:
                misses.append(g)
        if not misses:
            return results
        responder = self.responders[model]
        # The gate is checked before any payload is built, on the TRUSTED origin, not the caller's label.
        # A live responder needs explicit L1 authorization whatever the origin; offline responders keep the L0 rule.
        if getattr(responder, 'live_enabled', False):
            require_live_authorization(self.gate)
        (self.gate or OwnerDecisionGate(None)).require('L1' if getattr(responder, 'live_enabled', False) else 'L0', self.origin)
        chunks, oversized = self._pack(model, responder, misses, extra, max_items, max_bytes)
        for sid, reason in oversized.items():
            self.oversized.append({'signature_id': sid, 'model': model, 'limit': reason})
            results[sid] = {'outcome': 'oversized_unresolved', 'class': 'unknown', 'alternatives': [], 'missing': [],
                            'reasons': ['ITEM_EXCEEDS_' + reason], 'model': model, 'cache_hit': False, 'batch_id': None}
        for chunk in chunks:
            content = _content(chunk, self.cfg, extra)
            body = responder.make_body(SYSTEM_PROMPT, content, ContextResponse)
            sent = json.loads(body['messages'][0]['content'])['untrusted_records']
            if len(sent) > max_items or evidence_bytes(sent) > max_bytes:   # exact re-measurement of what will be sent
                raise EngineError('REQUEST_LIMIT_VIOLATION', 'Packed request exceeds its limits; not sent.')
            outcome = responder.complete(body, ContextResponse, self.cfg.scope, self.origin)
            self.stats['requests'][model] += 1
            self.stats['input_tokens'][model] += int(outcome.usage.get('input_tokens') or 0)
            self.stats['output_tokens'][model] += int(outcome.usage.get('output_tokens') or 0)
            self.stats['cost_usd'][model] += outcome.cost_usd or 0.0
            batch = _batch_for(chunk, self.by_id)
            response = outcome.parsed
            audit = scope_audit(response, batch, self.records)
            absts = abstention_audit(response, batch)
            self.stray += [{'batch_id': batch.batch_id, 'model': model, 'reason_codes': a['reason_codes']} for a in audit
                           if a['proposal']['target_source_id'] not in batch.target_source_ids]
            for g in chunk:
                res = _checked(_resolve(g, audit, absts), g)
                res.update(model=model, cache_hit=False, batch_id=batch.batch_id)
                results[g['signature_id']] = res
                if self.cache and res['outcome'] not in ('no_response',):
                    target = g['rep']['source_id']
                    sub = ContextResponse(candidates=[c for c in response.candidates if c.target_source_id == target],
                                          abstentions=[a for a in response.abstentions if a.target_source_id == target])
                    self.cache.put(self._components(model, g, (extra or {}).get(g['signature_id'])),
                                   {'response': sub.model_dump()},
                                   {'batch_id': batch.batch_id, 'request_id': outcome.audit.get('request_id'), 'model': model,
                                    'prompt_version': PROMPT_VERSION})
        return results

    def run(self, plan):
        cfg = self.cfg
        outcome, model_of, escalated = {}, {}, set()
        first = self._call_model(cfg.haiku_model, [g for b in plan['batches'] for g in b]) if (plan['groups'] and cfg.haiku_model in self.responders) else {}
        for sid, res in first.items():
            outcome[sid], model_of[sid] = res, cfg.haiku_model
        if cfg.escalation_model and cfg.escalation_model in self.responders:
            groups = {g['signature_id']: g for g in plan['groups']}
            candidates = sorted((groups[s] for s, r in first.items() if r['outcome'] in UNRESOLVED and len(groups[s]['rows']) >= cfg.min_rows_for_escalation),
                                key=lambda g: (-len(g['rows']), g['signature_id']))
            cap = math.ceil(cfg.max_escalation_fraction * len(plan['groups']))
            chosen = candidates[:cap]
            if chosen:
                extra = {g['signature_id']: {'model': cfg.haiku_model, 'outcome': first[g['signature_id']]['outcome'],
                                             'alternatives': first[g['signature_id']]['alternatives'],
                                             'missing_prerequisites': first[g['signature_id']]['missing']} for g in chosen}
                second = self._call_model(cfg.escalation_model, chosen, extra, cfg.escalation_items_per_batch, cfg.escalation_max_batch_bytes)
                for sid, res in second.items():
                    escalated.add(sid)
                    outcome[sid], model_of[sid] = res, cfg.escalation_model
        return self._ledger(plan, outcome, escalated)

    def _ledger(self, plan, outcome, escalated):
        rows = []

        def base(f):
            return {'source_id': f['source_id'], 'source_record_uid': f['uid'], 'point_key': 'point/' + f['point_id'],
                    'signature_id': None, 'rule_id': None, 'batch_id': None, 'model': None, 'escalated': False, 'cache_hit': False,
                    'derived_from_source_id': None, 'candidate_class': 'unknown', 'status': 'needs_review', 'outcome': None,
                    'alternatives': [], 'missing_prerequisites': [], 'validation_reason_codes': [],
                    'units_original': f['units_original'], 'units_normalized': f['units'], 'unit_rule': f['unit_rule'],
                    'equipment_family': f.get('family', ''), 'check_flags': [], 'context_flags': []}
        for h in plan['held']:
            r = base(h['facts'])
            r.update(route=h['route'], route_reasons=h['reasons'], outcome='held_preserved')
            rows.append(r)
        for d in plan['deterministic']:
            r = base(d['facts'])
            r.update(route='deterministic', route_reasons=d['reasons'], rule_id=d['rule'], candidate_class=d['class'],
                     status=d['status'], outcome='resolved')
            rows.append(r)
        for g in plan['groups']:
            res = outcome.get(g['signature_id'])
            for f in g['rows']:
                r = base(f)
                r['signature_id'] = g['signature_id']
                if res is None:
                    r.update(route='unrouted_no_responder', route_reasons=['NO_RESPONDER_CONFIGURED'], outcome='unresolved')
                else:
                    is_rep = f['source_id'] == g['rep']['source_id']
                    route = 'held_oversized' if res['outcome'] == 'oversized_unresolved' else \
                        ('cache_reuse' if res['cache_hit'] else 'llm') if is_rep else 'propagated_signature'
                    r.update(route=route,
                             route_reasons=['REPRESENTATIVE'] if is_rep else ['SAME_SIGNATURE_AS_REPRESENTATIVE'],
                             model=res['model'], escalated=g['signature_id'] in escalated, cache_hit=res['cache_hit'],
                             batch_id=res['batch_id'], derived_from_source_id=None if is_rep else g['rep']['source_id'],
                             candidate_class=res['class'], outcome=res['outcome'], alternatives=res['alternatives'],
                             missing_prerequisites=res['missing'], validation_reason_codes=res['reasons'],
                             check_flags=list(res.get('check_flags', [])),
                             context_flags=[] if is_rep else _context_flags(f, g['rep']))
                rows.append(r)
        return rows

    def summary(self, ledger):
        routes = Counter(r['route'] for r in ledger)
        statuses = Counter(r['status'] for r in ledger)
        outcomes = Counter(r['outcome'] for r in ledger)
        return {'records': len(ledger), 'routes': dict(routes), 'statuses': dict(statuses), 'outcomes': dict(outcomes),
                'requests': dict(self.stats['requests']), 'cache_hits': dict(self.stats['cache_hits']),
                'input_tokens': dict(self.stats['input_tokens']), 'output_tokens': dict(self.stats['output_tokens']),
                'cost_usd': {k: round(v, 9) for k, v in self.stats['cost_usd'].items()},
                'trusted_origin': self.origin,
                'oversized_items': len(self.oversized),
                'cache_rejections': dict(Counter(x['reason'] for x in (self.cache.rejections if self.cache else []))),
                'out_of_scope_candidates': dict(Counter(r for x in self.stray for r in x['reason_codes'])),
                'unit_normalizations': sum(1 for r in ledger if r['unit_rule'].startswith('unit-glyph')),
                'check_failed': outcomes.get('check_failed', 0),
                'check_flags': dict(Counter(c for r in ledger for c in r.get('check_flags', []))),
                'propagated_with_context_flags': sum(1 for r in ledger if r.get('context_flags')),
                'verified_count': statuses.get('verified', 0)}


def _checked(res, group):
    """Apply deterministic class checks to the representative's documented facts. A hard finding withholds the class (and
    therefore its propagation); soft findings travel with it."""
    if res.get('outcome') != 'resolved':
        return res
    rep = group['rep']
    findings = class_checks.check(res['class'], rep['name'], rep['units'], rep['kind'], rep.get('family'))
    hard = class_checks.hard(findings)
    res = dict(res, check_flags=[code for code, _ in findings])
    if hard:
        res.update(outcome='check_failed', alternatives=sorted(set(res.get('alternatives', [])) | {res['class']}), **{'class': 'unknown'},
                   reasons=sorted(set(res.get('reasons', [])) | set(hard)))
    return res


def _context_flags(row, rep):
    """Documented context in which a propagated row differs from its representative (never silently ignored)."""
    return ['CONTEXT_DIFFERS:' + field for field in ('family', 'network', 'hint', 'station') if row.get(field, '') != rep.get(field, '')]


def ledger_complete(records, ledger):
    """Every source record appears exactly once in the ledger (nothing silently dropped, nothing duplicated)."""
    ids = [r['source_id'] for r in ledger]
    expected = [r.source_id for r in records]
    return len(ids) == len(set(ids)) and sorted(ids) == sorted(expected)
