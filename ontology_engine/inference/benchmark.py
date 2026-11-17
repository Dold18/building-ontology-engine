"""Offline SYNTHETIC / FOR TESTING ONLY benchmark: full-LLM vs deterministic-first+escalation vs deterministic-only.

Everything here is simulated: the 'model' is a seeded truth-based oracle with configured error/abstain rates, and token
counts are byte-size estimates. Results are PROJECTIONS about workload and cost structure. They say nothing about how
Claude will actually perform on sponsor data; that is only knowable from measured L1+ runs.
"""
import hashlib
import json
import math
from collections import Counter
from ..contextual import ContextResponse, ContextCandidate, ContextAbstention
from ..models import SourceRecord
from ..vocabulary import POINTS
from .anthropic_provider import AnthropicSettings, ProviderResult, build_request
from .pricing import usage_cost_usd
from .router import RoutingConfig, Router, plan_routes, project_costs, ledger_complete

SYN = 'SYNTHETIC / FOR TESTING ONLY'
H = 'Metadata: '
# (name, bacnet kind, units, truth class). 'unknown' = outside the supported Brick subset or genuinely unresolvable.
_T, _B, _N = 'c:NumericPoint', 'c:BooleanPoint', 'c:NumericWritable'
# Central-system functions (40): name suffix, kind, units, truth class. Many are commands/setpoints outside the supported subset.
FUNCS = [('SAT', _T, '°F', 'Supply_Air_Temperature_Sensor'), ('OAT', _T, '°F', 'Outside_Air_Temperature_Sensor'),
         ('MAT', _T, '°F', 'Temperature_Sensor'), ('RAT', _T, '°F', 'Temperature_Sensor'), ('SFStatus', _B, '', 'Fan_Status'),
         ('RFStatus', _B, '', 'Fan_Status'), ('SFCmd', 'c:BooleanWritable', '', 'unknown'), ('RFCmd', 'c:BooleanWritable', '', 'unknown'),
         ('DuctSP', _T, 'in/wc', 'Differential_Pressure_Sensor'), ('DuctSPSet', _N, 'in/wc', 'unknown'),
         ('BldgSP', _T, 'in/wc', 'Differential_Pressure_Sensor'), ('OADmpr', _N, '%', 'Damper_Position_Sensor'),
         ('RADmpr', _N, '%', 'Damper_Position_Sensor'), ('EADmpr', _N, '%', 'Damper_Position_Sensor'), ('SAFlow', _T, 'cfm', 'Air_Flow_Sensor'),
         ('OAFlow', _T, 'cfm', 'Air_Flow_Sensor'), ('CHWValve', _N, '%', 'unknown'), ('HWValve', _N, '%', 'unknown'),
         ('PreheatTemp', _T, '°F', 'Temperature_Sensor'), ('CHWSupT', _T, '°F', 'Temperature_Sensor'), ('CHWRetT', _T, '°F', 'Temperature_Sensor'),
         ('HWSupT', _T, '°F', 'Temperature_Sensor'), ('HWRetT', _T, '°F', 'Temperature_Sensor'), ('CHWFlow', _T, 'gal/min', 'Water_Flow_Sensor'),
         ('HWFlow', _T, 'gal/min', 'Water_Flow_Sensor'), ('Alarm1', _B, '', 'unknown'), ('Alarm2', _B, '', 'unknown'), ('Mode', 'c:EnumPoint', '', 'unknown'),
         ('OccSched', 'c:EnumWritable', '', 'unknown'), ('FreezeStat', _B, '', 'unknown'), ('SmokeDet', _B, '', 'unknown'),
         ('FiltDP', _T, 'in/wc', 'Differential_Pressure_Sensor'), ('Kw', _T, 'kW', 'Electric_Power_Sensor'), ('Kwh', _T, 'kW-hr', 'Electric_Energy_Sensor'),
         ('SFSpeed', _N, '%', 'unknown'), ('SFSpeedSet', _N, '%', 'unknown'), ('Humidity', _T, '%RH', 'unknown'), ('Co2', _T, 'ppm', 'unknown'),
         ('StaticHi', _B, '', 'unknown'), ('Runtime', _T, 's', 'unknown')]
VAV_TEMPLATE = [('SYN_RmT', _T, '°F', 'Zone_Air_Temperature_Sensor'), ('SYN_RmTSet', _N, '°F', 'unknown'),
                ('SYN_DmprPos', _T, '%', 'Damper_Position_Sensor'), ('SYN_Cfm', _T, 'cfm', 'Air_Flow_Sensor'),
                ('SYN_CfmSp', _N, 'cfm', 'unknown'), ('SYN_OccCmd', 'c:BooleanWritable', '', 'unknown'),
                ('SYN_Co2', _T, 'ppm', 'unknown'), ('SYN_HtgOut', _T, '%', 'unknown')]


def _record(index, name, kind, units, uid, container, station, description='', explicit=''):
    cols = ['BACnet Type', 'Fox: Path', 'Fox: Station Name', 'Normal: Name', 'Normal: Display Units', 'Normal: UUID']
    vals = [kind, container, station, name, units, uid]
    raw = {str(i): {'column': H + c, 'value': v or None, 'coordinate': 'Points!%s%d' % ('ABCDEF'[i], index + 2)} for i, (c, v) in enumerate(zip(cols, vals))}
    norm = {(H + c).lower(): (v or '') for c, v in zip(cols, vals)}
    norm.update({'point_id': uid, 'units': {'°F': 'degF', '%': 'percent'}.get(units, units), 'description': description})
    if explicit:
        norm['point_class'] = explicit
    return SourceRecord(source_id='SYN.xlsx:Points:%d' % (index + 2), source_record_uid='src:syn%06d' % index, source_file='SYN.xlsx',
                        source_type='xlsx', source_location='sheet Points; rows %d-%d' % (index + 2, index + 2), source_hash='syn',
                        table_kind='points', sheet_name='Points', raw_value=[v or None for v in vals], raw_record=raw,
                        normalized_fields=norm, parse_status='ok', errors=[])


def synthetic_building(n_systems=28, n_vav=400, description_rate=0.003, explicit_rate=0.0, duplicate_pairs=40, glyph_rate=0.01, seed=7):
    """Synthetic large-building shape: ~4,800 rows, ~1,100 distinct names, sparse metadata, repeated VAV names. All values invented."""
    records, truth = [], {}

    def add(name, kind, units, cls, container, station):
        i = len(records)
        h = int(hashlib.sha256(('%d|%s' % (seed, i)).encode()).hexdigest(), 16)
        desc = {'Supply_Air_Temperature_Sensor': 'supply air temperature', 'Zone_Air_Temperature_Sensor': 'zone temperature'}.get(cls, '') \
            if (h % 100000) / 100000 < description_rate else ''
        explicit = cls if (cls != 'unknown' and ((h >> 40) % 100000) / 100000 < explicit_rate) else ''
        glyph = units.replace('°', 'º') if units.endswith('F') and ((h >> 80) % 10000) / 10000 < glyph_rate else units
        rec = _record(i, name, kind, glyph, 'SYN-UUID-%06d' % i, container, station, desc, explicit)
        records.append(rec)
        truth[rec.source_id] = cls
    for sidx in range(n_systems):
        for n, k, u, c in FUNCS:
            add('SYN_S%02d_%s' % (sidx, n), k, u, c, '/Drivers/BacnetNetwork/SYN_SYS_%02d/points' % sidx, 'SYN_ST%d' % (sidx % 3))
        for n, k, u, c in FUNCS[:17]:  # standby container repeats names (as real exports repeat names across containers)
            add('SYN_S%02d_%s' % (sidx, n), k, u, c, '/Drivers/BacnetNetwork/SYN_SYS_%02d/backup/points' % sidx, 'SYN_ST%d' % (sidx % 3))
    for v in range(n_vav):
        for n, k, u, c in VAV_TEMPLATE:
            add(n, k, u, c, '/Drivers/LonNetwork/SYN_SYS_%02d/TB_%03d/points' % (v % n_systems, v), 'SYN_ST%d' % (v % 3))
    for d in range(duplicate_pairs):  # repeated identifiers: preserved, held, never merged
        src = records[d * 7]
        dup = _record(len(records), src.normalized_fields['metadata: normal: name'], 'c:NumericPoint', '', src.normalized_fields['point_id'],
                      '/Drivers/LonNetwork/SYN_DUP/points', 'SYN_ST0')
        dup.parse_status, dup.errors = 'error', ['Duplicate identifier']
        records.append(dup)
    return records, truth


class SimulatedResponder:
    """Seeded truth oracle. live_enabled is False: it can never reach a network."""
    live_enabled = False

    def __init__(self, model, pricing, truth_by_name, error_rate, abstain_rate, thinking_tokens_per_item=0, bytes_per_token=3.0, seed=11):
        self.settings = AnthropicSettings(model=model, max_tokens=16384 if 'sonnet' in model else 8192)
        self.model, self.pricing, self.truth, self.err, self.abst = model, pricing, truth_by_name, error_rate, abstain_rate
        self.think, self.bpt, self.seed = thinking_tokens_per_item, bytes_per_token, seed
        self.calls = 0

    def make_body(self, system_prompt, content, response_type):
        return build_request(self.settings, system_prompt, content, response_type)

    def _u(self, name, salt):
        return int(hashlib.sha256(('%d|%s|%s|%s' % (self.seed, self.model, name, salt)).encode()).hexdigest(), 16) / 2 ** 256

    def complete(self, body, response_type, scope='', data_origin='synthetic'):
        self.calls += 1
        content = json.loads(body['messages'][0]['content'])
        cands, absts = [], []
        for item in content['untrusted_records']:
            name = item['name']
            prior = item.get('prior_attempt')
            u = self._u(name, 'p')
            err = self.err * (0.5 if prior else 1.0)
            abst = self.abst * (0.4 if prior else 1.0)
            if u < abst:
                absts.append(ContextAbstention(target_source_id=item['target_source_id'], missing_prerequisites=['equipment context'],
                                               explanation='Insufficient evidence in supplied fields.'))
                continue
            truth = self.truth.get(name, 'unknown')
            cls = truth
            if u < abst + err:
                cls = next(c for c in sorted(POINTS) if c not in (truth, 'Point'))
            cands.append(ContextCandidate(target_source_id=item['target_source_id'], subject_id=item['point_key'], predicate='type',
                                          object_id=cls, supporting_source_ids=[item['target_source_id']], basis='naming',
                                          explanation='Simulated classification from name and units.'))
        parsed = ContextResponse(candidates=cands, abstentions=absts)
        in_bytes = len(json.dumps(body, ensure_ascii=False).encode())
        out_bytes = len(parsed.model_dump_json().encode())
        usage = {'input_tokens': math.ceil(in_bytes / self.bpt),
                 'output_tokens': math.ceil(out_bytes / self.bpt) + self.think * len(content['untrusted_records']),
                 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
        cost = usage_cost_usd(self.pricing.price(self.model), usage)
        return ProviderResult(parsed=parsed, usage=usage, cost_usd=cost, audit={'request_id': 'simulated'})


def _metrics(name, records, truth, ledger, router, extra=None):
    ok = [r for r in ledger if r['outcome'] != 'held_preserved']
    classified = [r for r in ledger if r['candidate_class'] not in ('unknown', None)]
    validated = [r for r in ledger if r['outcome'] in ('resolved', 'resolved_unknown')]
    truth_known = [r for r in classified if truth.get(r['source_id']) not in (None,)]
    correct = sum(1 for r in truth_known if truth[r['source_id']] == r['candidate_class'])
    review_sigs = {r['signature_id'] or r['source_id'] for r in ledger if r['status'] == 'needs_review' and r['outcome'] != 'held_preserved'}
    s = router.summary(ledger)
    out = {'strategy': name, 'records': len(ledger), 'held_preserved': len(ledger) - len(ok), 'evaluable_rows': len(ok),
           'requests_total': sum(s['requests'].values()), 'requests_by_model': s['requests'],
           'input_tokens_est': sum(s['input_tokens'].values()), 'output_tokens_est': sum(s['output_tokens'].values()),
           'cost_usd_est': round(sum(s['cost_usd'].values()), 6), 'cost_by_model_usd_est': s['cost_usd'],
           'candidate_class_rows': len(classified), 'unknown_rows': len(ok) - len(classified),
           'candidate_coverage': round(len(classified) / len(ok), 4) if ok else None,
           'validated_evidence_rows': len(validated), 'validated_evidence_coverage': round(len(validated) / len(ok), 4) if ok else None,
           'simulated_accuracy_of_classified': round(correct / len(truth_known), 4) if truth_known else None,
           'review_questions_consolidated': len(review_sigs), 'review_rows_unconsolidated': sum(1 for r in ledger if r['status'] == 'needs_review' and r['outcome'] != 'held_preserved'),
           'ledger_complete': ledger_complete(records, ledger), 'routes': s['routes'], 'verified_count': s['verified_count']}
    if extra:
        out.update(extra)
    return out


def run_benchmark(pricing, records=None, truth=None, error=(0.04, 0.02), abstain=(0.06, 0.03), thinking_tokens_per_item=300):
    if records is None:
        records, truth = synthetic_building()
    truth_by_name = {}
    for r in records:
        truth_by_name.setdefault(r.normalized_fields['metadata: normal: name'], truth.get(r.source_id, 'unknown'))

    def responders(models, think_sonnet=True):
        out = {}
        for m, (e, a) in zip(models, zip(error, abstain)):
            out[m] = SimulatedResponder(m, pricing, truth_by_name, e, a, thinking_tokens_per_item if (think_sonnet and 'sonnet' in m) else 0)
        return out
    results = {}
    base = dict(scope='syn-building', data_origin='synthetic')
    # A: every evaluable row sent to the LLM, no deterministic stage, no grouping, no escalation
    for label, model in (('A_full_llm_haiku', 'claude-haiku-5-5'), ('A_full_llm_sonnet', 'claude-sonnet-5-5')):
        cfg = RoutingConfig(**base, haiku_model=model, escalation_model=None, enable_deterministic=False, group_signatures=False)
        idx = 0 if 'haiku' in model else 1
        router = Router(cfg, records, {model: SimulatedResponder(model, pricing, truth_by_name, error[idx], abstain[idx],
                                                                 thinking_tokens_per_item if 'sonnet' in model else 0)}, pricing=pricing)
        plan = plan_routes(records, cfg)
        ledger = router.run(plan)
        results[label] = _metrics(label, records, truth, ledger, router)
    # B: deterministic-first + signature grouping + bounded Sonnet escalation
    cfg = RoutingConfig(**base)
    router = Router(cfg, records, responders(['claude-haiku-5-5', 'claude-sonnet-5-5']), pricing=pricing)
    plan = plan_routes(records, cfg)
    ledger = router.run(plan)
    results['B_deterministic_first'] = _metrics('B_deterministic_first', records, truth, ledger, router,
                                                 {'signatures_sent': len(plan['groups']), 'deterministic_rows': len(plan['deterministic']),
                                                  'projection': project_costs(plan, pricing)})
    # C: deterministic only
    cfg = RoutingConfig(**base, escalation_model=None)
    router = Router(cfg, records, {}, pricing=pricing)
    plan = plan_routes(records, cfg)
    ledger = router.run(plan)
    results['C_deterministic_only'] = _metrics('C_deterministic_only', records, truth, ledger, router)
    return {'label': SYN, 'disclaimer': 'Simulated oracle and byte-size token estimates; NOT measured API results and NOT sponsor-data results.',
            'dataset': {'records': len(records), 'distinct_names': len(truth_by_name),
                        'truth_classified_rows': sum(1 for v in truth.values() if v != 'unknown')},
            'assumptions': {'oracle_error_rate_haiku_sonnet': error, 'oracle_abstain_rate_haiku_sonnet': abstain,
                            'sonnet_thinking_tokens_per_item': thinking_tokens_per_item, 'bytes_per_token_expected': 3.0},
            'results': results}
