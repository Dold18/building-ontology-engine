"""FR-01/03/04/05/06/11, D1: building graph from a BAS point export (deterministic; no model call).

Separation of evidence (AGENTS rule 4/5):
- ACCEPTED graph: what the export documents. Each unique, error-free point row becomes a `brick:Point` (through the
  existing evidence gate and `pipeline.build_graph`), with its source label, its QUDT unit when the source unit string maps
  exactly, a Brick `ref:ExternalReference` to the BAS software object, the BAS software containment (a SKOS concept tree:
  station -> container path; software organisation, NOT physical topology), and PROV links to the exact source row.
- PROVISIONAL graph: candidates, every one `needs_review` (or `contradicted`), never `verified`:
  equipment candidates from BAS container names (optionally corroborated by drawing tag mentions), candidate
  `hasPoint` from documented containment under such a container, candidate `feeds` from a BAS folder named for an AHU
  that contains terminal-unit containers, naming-cue point classes, and points whose identifier is not unique. Each
  candidate triple is also reified (rdf:Statement) with status, method, evidence and missing evidence.
- REVIEW QUEUE only (no graph edge): physical and thermodynamic hypotheses whose only support is point naming
  (coils, fans, dampers, hydronic sources, exhaust paths), with the exact evidence that is missing.

Nothing here creates feeds/hasPart/hasPoint or a thermal relationship from naming alone, and nothing is promoted by
schema validation.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from urllib.parse import quote
from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef
from rdflib.namespace import DCTERMS, PROV, SKOS, XSD
from .evidence import eligibility
from .identity import uid
from .models import Assertion, Issue, ReviewItem, SourceRecord
from .vocabulary import BRICK

VERSION = 'building-graph-v1'
REF = Namespace('https://brickschema.org/schema/Brick/ref#')
REC = Namespace('https://w3id.org/rec#')
UNIT = Namespace('http://qudt.org/vocab/unit/')
QUDT = Namespace('http://qudt.org/schema/qudt/')
ACCEPTED = {'verified', 'supported_inference'}

# Exact source unit strings (after the existing normalizer and glyph layer) -> QUDT unit. Anything else: no unit triple.
QUDT_UNITS = {'degF': 'DEG_F', 'cfm': 'FT3-PER-MIN', 'percent': 'PERCENT', 'ppm': 'PPM', 'in/wc': 'IN_H2O', 'psi': 'PSI',
              '%RH': 'PERCENT_RH', 'gal': 'GAL_US', 'gal/min': 'GAL_US-PER-MIN', 'V': 'V', 'BTU': 'BTU_IT', 's': 'SEC',
              'ft/min': 'FT-PER-MIN', 'kW-hr': 'KiloW-HR'}
# The pinned Brick file does not bundle QUDT, so each used unit IRI is declared as a qudt:Unit (QUDT's own typing).
QUDT_LABELS = {'DEG_F': 'degree Fahrenheit', 'FT3-PER-MIN': 'cubic foot per minute', 'PERCENT': 'percent', 'PPM': 'parts per million',
               'IN_H2O': 'inch of water', 'PSI': 'pound-force per square inch', 'PERCENT_RH': 'percent relative humidity',
               'GAL_US': 'US gallon', 'GAL_US-PER-MIN': 'US gallon per minute', 'V': 'volt', 'BTU_IT': 'British thermal unit (IT)',
               'SEC': 'second', 'FT-PER-MIN': 'foot per minute', 'KiloW-HR': 'kilowatt hour'}
UNIT_QUANTITY = {'DEG_F': 'temperature', 'FT3-PER-MIN': 'air_flow', 'PERCENT': 'ratio', 'PPM': 'concentration',
                 'IN_H2O': 'pressure', 'PSI': 'pressure', 'PERCENT_RH': 'humidity', 'GAL_US-PER-MIN': 'water_flow'}

# Equipment-like container names (decoded Niagara segments). Industry abbreviations, not building facts; every match is a
# candidate. (regex, Brick class, tag template, alternatives)
EQUIPMENT_PATTERNS = [
    (r'(?:[A-Za-z0-9]+_)?AHU_?0*(\d+)(?:_TBs|_Logic|_Ctrlr\d+)?', 'AHU', 'AHU-{0}', []),
    (r'TB_(\d+)_(\d+[A-Z]?)', 'Terminal_Unit', 'TB-{0}-{1}', ['Variable_Air_Volume_Box']),
    (r'FCU_?(\d+[A-Z]?)', 'Fan_Coil_Unit', 'FCU-{0}', []),
    (r'(AV)_(\d+)_(\w+)', 'Terminal_Unit', '{0}-{1}-{2}', []),
    (r'(EV)_(\d+)_(\w+)', 'Terminal_Unit', '{0}-{1}-{2}', ['Fume_Hood']),
    (r'(?:Lockout)?EF(\d+[A-Z]?)(?:_System|_Logic)?', 'Exhaust_Fan', 'EF-{0}', []),
    (r'(?:[A-Za-z0-9]+_)?HWS', 'Hot_Water_System', 'HWS', []),
    (r'CHWS(?:_Ctrlr\d+)?', 'Chilled_Water_System', 'CHWS', []),
    (r'HRS_Cntrl\d+', 'Equipment', 'HRS', []),
    (r'(?:[A-Za-z0-9]+_)?LabExhaust\w*', 'Air_System', 'LAB-EXHAUST', []),
]
MULTI_EF = re.compile(r'EF(\d+[A-Z])_(\d+[A-Z])_Cntrl\d+')
LOGIC = re.compile(r'.*(Logic|Clc|Pid|Ctl|Cntrl\d*|LeadLag|Global|Staging|Dampers?|OSS|RIBs|Aircuity)$', re.I)
INFRA = re.compile(r'(Drivers|\w*Network|NdioBoard\d+|points|Services|Config)', re.I)
DRAWING_TAG = re.compile(r'([A-Z]{1,4})-?(\d+[A-Z]?)')

# Naming-cue point classes: exact whole-name match only (separators and case ignored). Candidates, never accepted.
POINT_LEXICON = {
    'dat': 'Discharge_Air_Temperature_Sensor', 'dischrgairtemp': 'Discharge_Air_Temperature_Sensor',
    'oat': 'Outside_Air_Temperature_Sensor', 'outtmp': 'Outside_Air_Temperature_Sensor',
    'mat': 'Mixed_Air_Temperature_Sensor', 'rat': 'Return_Air_Temperature_Sensor',
    'rmt': 'Zone_Air_Temperature_Sensor', 'avgspacetemp': 'Zone_Air_Temperature_Sensor',
    'datsetpt': 'Discharge_Air_Temperature_Setpoint', 'boxflow': 'Air_Flow_Sensor', 'vlvflowfdbk': 'Air_Flow_Sensor',
    'flowsetpt': 'Air_Flow_Setpoint', 'spcco2': 'CO2_Sensor', 'co2sensorppm': 'CO2_Sensor', 'raco2': 'CO2_Sensor', 'oaco2': 'CO2_Sensor',
    'rmh': 'Zone_Air_Humidity_Sensor', 'oah': 'Outside_Air_Humidity_Sensor', 'rah': 'Return_Air_Humidity_Sensor',
    'sdsp': 'Supply_Air_Static_Pressure_Sensor', 'bsp': 'Building_Air_Static_Pressure_Sensor',
    'chwvlv': 'Valve_Command', 'hwvlv': 'Valve_Command', 'fintubevlv': 'Valve_Command',
    'sfsts': 'Fan_Status', 'sfasts': 'Fan_Status', 'sfbsts': 'Fan_Status', 'rfasts': 'Fan_Status', 'rfbsts': 'Fan_Status',
    'sfcmd': 'Start_Stop_Command', 'sfacmd': 'Start_Stop_Command', 'sfbcmd': 'Start_Stop_Command',
    'rfacmd': 'Start_Stop_Command', 'rfbcmd': 'Start_Stop_Command',
    'sfavfd': 'Fan_Speed_Command', 'sfbvfd': 'Fan_Speed_Command', 'rfavfd': 'Fan_Speed_Command', 'rfbvfd': 'Fan_Speed_Command',
    'hwgpm': 'Water_Flow_Sensor', 'smoke': 'Smoke_Detection_Alarm', 'freeze': 'Freeze_Status',
}
LEXICON_PATTERNS = [(re.compile(r'hwdp\d'), 'Hot_Water_Differential_Pressure_Sensor'),
                    (re.compile(r'p?chwdp\d'), 'Chilled_Water_Differential_Pressure_Sensor')]
CLASS_QUANTITY = {'Supply_Air_Temperature_Sensor': 'temperature', 'Temperature_Sensor': 'temperature', 'Damper_Position_Sensor': 'ratio',
                  'Pressure_Sensor': 'pressure', 'Differential_Pressure_Sensor': 'pressure', 'Discharge_Air_Temperature_Sensor': 'temperature', 'Outside_Air_Temperature_Sensor': 'temperature',
                  'Mixed_Air_Temperature_Sensor': 'temperature', 'Return_Air_Temperature_Sensor': 'temperature',
                  'Zone_Air_Temperature_Sensor': 'temperature', 'Discharge_Air_Temperature_Setpoint': 'temperature',
                  'Air_Flow_Sensor': 'air_flow', 'Air_Flow_Setpoint': 'air_flow', 'CO2_Sensor': 'concentration',
                  'Zone_Air_Humidity_Sensor': 'humidity', 'Outside_Air_Humidity_Sensor': 'humidity', 'Return_Air_Humidity_Sensor': 'humidity',
                  'Supply_Air_Static_Pressure_Sensor': 'pressure', 'Building_Air_Static_Pressure_Sensor': 'pressure',
                  'Hot_Water_Differential_Pressure_Sensor': 'pressure', 'Chilled_Water_Differential_Pressure_Sensor': 'pressure',
                  'Water_Flow_Sensor': 'water_flow', 'Valve_Command': 'ratio', 'Fan_Speed_Command': 'ratio'}
COMPATIBLE = {('humidity', 'ratio'), ('ratio', 'ratio')}

# Point-name cues for physical/thermal hypotheses (review queue only). (cue regex on normalized name, hypothesis key)
THERMAL_CUES = [
    (r'chwvlv|chwvalve', 'chilled_water_coil'), (r'^hwvlv|hcl?at|hwvalve', 'hot_water_coil'),
    (r'decp\d|evap', 'evaporative_cooling'), (r'^hr|heatrecovery|hrpv|hrc', 'heat_recovery'),
    (r'^sf[ab]?(cmd|sts|vfd|alm|fail)', 'supply_fan'), (r'^rf[ab]?(cmd|sts|vfd|alm|fail)', 'return_fan'),
    (r'^(oad|rad|ead|outdpr|retdpr|exhdpr)', 'economizer_dampers'), (r'fintube', 'perimeter_radiation'),
    (r'^hx\d', 'heat_exchanger'), (r'^hps|^hpc', 'steam_source'), (r'^p?chwp\d', 'chilled_water_pumps'),
    (r'^hwp\d', 'hot_water_pumps'), (r'facevelocity|sash', 'fume_hood'),
]
# Equipment classes each hypothesis can apply to (a cue on any other class is ignored).
APPLIES_TO = {
    'chilled_water_coil': {'AHU', 'Fan_Coil_Unit', 'Terminal_Unit'}, 'hot_water_coil': {'AHU', 'Fan_Coil_Unit', 'Terminal_Unit'},
    'evaporative_cooling': {'AHU'}, 'heat_recovery': {'AHU', 'Air_System', 'Equipment'}, 'supply_fan': {'AHU', 'Fan_Coil_Unit'},
    'return_fan': {'AHU'}, 'economizer_dampers': {'AHU'}, 'perimeter_radiation': {'Terminal_Unit', 'Fan_Coil_Unit'},
    'heat_exchanger': {'Hot_Water_System'}, 'steam_source': {'Hot_Water_System'}, 'hot_water_pumps': {'Hot_Water_System'},
    'chilled_water_pumps': {'Chilled_Water_System'}, 'fume_hood': {'Terminal_Unit'},
}
SYSTEM_CLASSES = {'Hot_Water_System', 'Chilled_Water_System', 'Air_System'}
HYPOTHESES = {
    'chilled_water_coil': ('Chilled_Water_Coil', 'Chilled_Water_System', 'feeds',
                           'mechanical schedule or CHW piping diagram identifying the coil and the CHW circuit serving it'),
    'hot_water_coil': ('Hot_Water_Coil', 'Hot_Water_System', 'feeds',
                       'heating-coil schedule or HW piping diagram identifying the coil and its HW circuit'),
    'evaporative_cooling': ('Equipment', None, 'hasPart', 'AHU schedule/section drawing showing the evaporative cooling section'),
    'heat_recovery': ('Equipment', None, 'hasPart', 'heat-recovery (run-around) piping diagram linking exhaust and supply coils'),
    'supply_fan': ('Supply_Fan', None, 'hasPart', 'AHU schedule listing supply fans (count, arrangement)'),
    'return_fan': ('Return_Fan', None, 'hasPart', 'AHU schedule listing return fans'),
    'economizer_dampers': ('Damper', None, 'hasPart', 'AHU section/control drawing showing OA/RA/EA damper arrangement'),
    'perimeter_radiation': ('Equipment', 'Hot_Water_System', 'feeds', 'HW piping plan showing perimeter fin-tube circuits per zone'),
    'heat_exchanger': ('Heat_Exchanger', None, 'hasPart', 'HW flow diagram showing heat exchangers and their primary source'),
    'steam_source': ('Equipment', None, 'feeds', 'utility/steam connection diagram for the HW heat exchangers'),
    'chilled_water_pumps': ('Chilled_Water_Pump', None, 'hasPart', 'CHW flow diagram (primary/secondary arrangement, campus source)'),
    'hot_water_pumps': ('Hot_Water_Pump', None, 'hasPart', 'HW pump schedule and flow diagram'),
    'fume_hood': ('Fume_Hood', 'LAB-EXHAUST', 'feeds', 'lab exhaust riser diagram assigning hood valves to exhaust fans'),
}


def decode(segment):
    """Niagara escapes ($5f -> '_')."""
    return re.sub(r'\$([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), segment)


def norm_name(name):
    return re.sub(r'[\s_\-.]+', '', name).casefold()


def classify_segment(segment):
    """(kind, [(tag, class, alternatives)]) for one decoded container segment."""
    m = MULTI_EF.fullmatch(segment)
    if m:
        return 'equipment', [('EF-' + g, 'Exhaust_Fan', []) for g in m.groups()]
    for rx, cls, template, alts in EQUIPMENT_PATTERNS:
        m = re.fullmatch(rx, segment, re.I)
        if m:
            return 'equipment', [(template.format(*[g.upper() for g in m.groups()]), cls, alts)]
    if INFRA.fullmatch(segment):
        return 'bas_infrastructure', []
    if LOGIC.fullmatch(segment):
        return 'control_logic', []
    return 'unclassified', []


def equipment_family(path, station=''):
    """Tag family (e.g. TB, AV, EV, FCU, AHU, EF, HWS) of the deepest equipment-named BAS container, else of the station."""
    for segment in reversed([decode(s) for s in (path or '').strip('/').split('/') if s]):
        kind, found = classify_segment(segment)
        if found:
            return found[0][0].split('-')[0]
    found = classify_segment(station or '')[1]
    return found[0][0].split('-')[0] if found else ''


def lexicon_class(name):
    key = norm_name(name)
    if key in POINT_LEXICON:
        return POINT_LEXICON[key]
    for rx, cls in LEXICON_PATTERNS:
        if rx.fullmatch(key):
            return cls
    return None


def _sid(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:16]


class BuildingGraph:
    def __init__(self, records, facts, scope, building_label, source_file, source_sha256, drawing_rows=(), drawing_evidence=None,
                 model_results=None, human_decisions=None):
        self.records, self.facts, self.scope = records, facts, scope
        self.label, self.source_file, self.source_sha256 = building_label, source_file, source_sha256
        self.drawing_rows = list(drawing_rows)
        self.drawing_evidence, self.drawing_assessment = drawing_evidence, None
        self.model_results, self.model_outcomes = model_results, {}
        self.extra_records = []           # citable drawing-extraction records (never BAS rows; never auto-accepted)
        self.drawing_evidence_bytes = (json.dumps(drawing_evidence, sort_keys=True, ensure_ascii=False).encode('utf-8')
                                       if drawing_evidence else None)
        self.ns = Namespace('urn:building:' + quote(scope, safe='') + ':')
        self.run_id = uid('phase3a', scope, source_sha256, VERSION)
        self.by_source = {r.source_id: r for r in records}
        self.assertions, self.issues, self.reviews, self.hypotheses = [], [], [], []
        self.review_keys = []
        self.equipment, self.containers, self.point_equipment = {}, {}, {}
        self.connections, self.traced_memberships, self.trace_register, self.diagram_equipment = [], [], [], []
        self.human_decisions = list(human_decisions or [])
        self.annotations = []

    # -- identifiers -------------------------------------------------------------------------------
    def iri(self, entity):
        return self.ns[quote(entity, safe='')]

    def assertion(self, subject, predicate, obj, source_ids, explanation, method, status, medium=None, model_version=None, prompt_version=None):
        a = self.make_assertion(subject, predicate, obj, source_ids, explanation, method, status, medium, model_version, prompt_version)
        self.assertions.append(a)
        return a

    def make_assertion(self, subject, predicate, obj, source_ids, explanation, method, status, medium=None, model_version=None, prompt_version=None):
        decision = eligibility(subject, predicate, obj, source_ids, self.by_source, 'rule' if method != 'direct_document' else method, explanation)
        if status in ACCEPTED and not decision['eligible']:
            status = 'needs_review'
        claim = uid('claim', self.scope, subject, predicate, obj)
        obs = uid('obs', self.run_id, claim, method)
        a = Assertion(assertion_id=obs, observation_uid=obs, claim_uid=claim, subject_entity_uid=uid('entity', self.scope, subject),
                      object_entity_uid=uid('entity', self.scope, obj), original_source_ids=list(source_ids),
                      supporting_source_uids=[self.by_source[s].source_record_uid for s in source_ids if s in self.by_source],
                      eligibility=decision, subject_id=subject, predicate=predicate, object_id=obj, supporting_source_ids=list(source_ids),
                      inference_method=method, explanation=explanation, status=status,
                      validation_results=['accepted graph' if status in ACCEPTED else 'withheld from accepted graph'],
                      run_id=self.run_id, physical_medium=medium, model_version=model_version, prompt_version=prompt_version)
        return a

    def issue(self, asset, category, evidence, method, source_ids, action, priority='review'):
        self.issues.append(Issue(issue_id=uid('issue', self.run_id, asset, method), category=category, point_or_asset_id=asset,
                                 observed_evidence=evidence, rule_or_detection_method=method + '@' + VERSION,
                                 severity_or_priority=priority, source_ids=list(source_ids), recommended_action=action))

    def review(self, key, question, assertions, evidence, reason, impact, priority):
        self.review_keys.append(key)
        self.reviews.append(ReviewItem(review_id=uid('review', self.run_id, key), affected_assertion_ids=[a.assertion_id for a in assertions],
                                       question_or_test=question, supporting_evidence_ids=list(evidence)[:50],
                                       reason_for_uncertainty=reason, estimated_impact=impact, priority=priority))

    # -- build -------------------------------------------------------------------------------------
    def build(self):
        self._points()
        self._containers()
        self._equipment()
        self._relationships()
        self._point_classes()
        if self.model_results:
            self._model_classes()
        self._thermal_hypotheses()
        self._drawing_only_tags()
        if self.drawing_evidence:
            self._apply_drawing_evidence()
            if self.drawing_evidence.get('vector_topology'):
                self._vector_topology()
        if self.human_decisions:
            self._apply_human_decisions()
        return self

    def _apply_drawing_evidence(self):
        """Attach schedule/diagram evidence (local OCR, unreviewed). Support never promotes; a schedule that names a different
        AHU than the BAS folder marks that feeds candidate `contradicted`."""
        from .drawing_evidence import assess
        result = self.drawing_assessment = assess(self, self.drawing_evidence)
        for tag, info in result['equipment'].items():
            if info['schedule_rows'] and self.drawing_evidence_bytes:
                sids = [self._drawing_record(r, tag) for r in info['schedule_rows'][:3]]
                self.assertion('equipment/' + tag, 'type', self.equipment[tag]['class'], sids,
                               'Drawing schedule row (OCR, unconfirmed) carries this tag%s.' % (' via the TB~VAV alias' if info['alias_match'] else ''),
                               'drawing_schedule_ocr', 'needs_review')
            self.equipment[tag]['drawing_tier'] = info['tier']
            self.equipment[tag]['schedule_refs'] = [r['ref'] for r in info['schedule_rows']][:5]
            self.equipment[tag]['diagram_refs'] = [m['ref'] for m in info['diagram_mentions']][:5]
            if info['tier'] != 'bas_only':
                self.equipment[tag]['basis'] = sorted(set(self.equipment[tag]['basis']) | {'drawing_' + info['tier'] + '_ocr'})
        by_id = {a.assertion_id: i for i, a in enumerate(self.assertions)}
        for f in result['feeds']:
            i = by_id[f['assertion_id']]
            a = self.assertions[i]
            if f['verdict'] == 'contradicted':
                new = a.model_copy(update={'status': 'contradicted', 'explanation': a.explanation + ' Drawing schedule (OCR) names %s instead.' % ', '.join(f['schedule_states'])})
                self.issue(a.object_id, 'model_conflict', 'BAS folder suggests %s but the drawing schedule names %s.' % (f['ahu'], ', '.join(f['schedule_states'])),
                           'bas_vs_schedule_conflict', a.supporting_source_ids, 'Confirm the serving AHU from the mechanical schedule and field.')
            elif f['verdict'] == 'documentary_support':
                new = a.model_copy(update={'explanation': a.explanation + ' Drawing schedule (OCR, unreviewed) also names %s.' % f['ahu']})
            else:
                continue
            self.assertions[i] = new
            self.feeds = [new if x.assertion_id == new.assertion_id else x for x in self.feeds]
        for h in self.hypotheses:
            match = next((x for x in result['hypotheses'] if x['hypothesis_id'] == h['hypothesis_id']), None)
            if match:
                h['drawing_verdict'], h['drawing_refs'] = match['verdict'], match['refs']
        self._drawing_equipment(result)
        documented = Counter(v['tier'] for v in result['equipment'].values())
        verdicts = Counter(f['verdict'] for f in result['feeds'])
        if result['drawing_only_scheduled_tags']:
            self.review('drawing-scheduled-not-in-bas', '%d equipment tag(s) appear in drawing schedules (OCR) but match no BAS candidate. Are they '
                        'unmonitored equipment, a different tag format, or OCR errors?' % len(result['drawing_only_scheduled_tags']), [], [],
                        'DRAWING_ONLY_EQUIPMENT', 'equipment completeness', 'medium')
        if documented.get('schedule_documented') or documented.get('diagram_mentioned'):
            self.review('drawing-ocr-confirmation', 'Confirm the OCR readings that link %d equipment candidates to schedule rows and %d to diagram '
                        'sheets (each reference gives PDF hash, page, sheet and box). OCR is unreviewed and cannot verify a claim.'
                        % (documented.get('schedule_documented', 0), documented.get('diagram_mentioned', 0)), [], [],
                        'OCR_UNREVIEWED', 'documentary support for %d candidates' % (documented.get('schedule_documented', 0) + documented.get('diagram_mentioned', 0)), 'high')
        if verdicts.get('contradicted'):
            self.review('feeds-contradicted', '%d BAS-folder AHU assignments disagree with the drawing schedule. Which source is right?'
                        % verdicts['contradicted'], [a for a in self.feeds if a.status == 'contradicted'], [], 'BAS_VS_DRAWING_CONFLICT', 'air-side topology', 'high')

    def _points(self):
        ids = Counter(f['point_id'] for f in self.facts.values() if f['point_id'])
        dup_groups = defaultdict(list)
        for r in self.records:
            f = self.facts[r.source_id]
            if r.parse_status == 'ok':
                self.assertion('point/' + f['point_id'], 'type', 'Point', [r.source_id], 'Source row explicitly represents a BAS point.',
                               'direct_document', 'supported_inference')
                if not f['path']:
                    self.issue(f['point_id'], 'data_quality', 'Point row has no declared BAS container (Fox: Path).', 'missing_container',
                               [r.source_id], 'Confirm the point\'s controller/equipment from the BAS or drawings.')
            elif f['point_id'] and ids[f['point_id']] > 1:
                dup_groups[f['point_id']].append(r)
            else:
                self.issue(f['point_id'] or r.source_id, 'data_quality', '; '.join(r.errors) or 'Source row error', 'source_row_error',
                           [r.source_id], 'Correct the export row; original bytes retained.')
        self.duplicates = {}
        for pid, rows in dup_groups.items():
            identical = len({json.dumps(r.raw_value, ensure_ascii=False) for r in rows}) == 1
            a = self.assertion('point/' + pid, 'type', 'Point', [r.source_id for r in rows],
                               'Identifier appears on %d rows (%s); identity unresolved.' % (len(rows), 'identical content' if identical else 'differing content'),
                               'direct_document', 'needs_review')
            self.duplicates[pid] = {'rows': [r.source_id for r in rows], 'identical': identical, 'assertion': a}
        if dup_groups:
            same = sum(1 for d in self.duplicates.values() if d['identical'])
            self.review('duplicate-identity', 'Which row is authoritative for each of the %d repeated point identifiers (%d groups identical, '
                        '%d differing)? Until decided, these points stay out of the accepted graph.' % (len(dup_groups), same, len(dup_groups) - same),
                        [d['assertion'] for d in self.duplicates.values()], [s for d in self.duplicates.values() for s in d['rows']],
                        'DUPLICATE_IDENTITY', 'identity of %d points' % len(dup_groups), 'high')

    def _containers(self):
        for r in self.records:
            f = self.facts[r.source_id]
            segs = [s for s in f['path'].strip('/').split('/') if s]
            parent = ('station', f['station'])
            for depth in range(1, len(segs) + 1):
                key = (f['station'], '/' + '/'.join(segs[:depth]))
                node = self.containers.setdefault(key, {'station': f['station'], 'path': key[1], 'segment': segs[depth - 1],
                                                        'decoded': decode(segs[depth - 1]), 'parent': parent, 'rows': []})
                parent = key
            if segs:
                self.containers[(f['station'], '/' + '/'.join(segs))]['rows'].append(r.source_id)
        for node in self.containers.values():
            node['kind'], node['equipment'] = classify_segment(node['decoded'])

    def _ancestors(self, key):
        while key and key[0] != 'station':
            yield key, self.containers[key]
            key = self.containers[key]['parent']

    def _equipment(self):
        def add(tag, cls, alts, basis, source_ids, container=None, station=None):
            e = self.equipment.setdefault(tag, {'tag': tag, 'class': cls, 'alternatives': list(alts), 'containers': set(), 'stations': set(),
                                                'source_ids': set(), 'points': set(), 'drawing_mentions': [], 'bases': set()})
            e['bases'].add(basis)
            e['source_ids'].update(source_ids)
            if container:
                e['containers'].add(container)
            if station:
                e['stations'].add(station)
        for key, node in self.containers.items():
            for tag, cls, alts in node['equipment']:
                rows = [s for k, n in self.containers.items() if k == key or self._is_descendant(k, key) for s in n['rows']]
                add(tag, cls, alts, 'bas_container_name', rows[:1] or [], key)
        for station in {f['station'] for f in self.facts.values() if f['station']}:
            kind, found = classify_segment(station)
            for tag, cls, alts in found:
                rows = [s for s, f in self.facts.items() if f['station'] == station]
                add(tag, cls, alts, 'bas_station_name', rows[:1], station=station)
        for row in self.drawing_rows:
            m = DRAWING_TAG.fullmatch((row.get('subject_text') or '').upper().replace(' ', ''))
            if not m:
                continue
            tag = m.group(1) + '-' + m.group(2)
            if tag in self.equipment:
                self.equipment[tag]['drawing_mentions'].append({k: row.get(k) for k in ('candidate_id', 'pdf_sha256', 'page', 'sheet_number', 'extraction_method')})
        for tag, e in sorted(self.equipment.items()):
            corroborated = bool(e['drawing_mentions'])
            basis = sorted(e['bases']) + (['drawing_tag_mention_ocr'] if corroborated else [])
            explanation = ('Equipment candidate from BAS %s naming%s; physical identity unconfirmed.'
                           % ('/'.join(sorted(b.replace('bas_', '').replace('_name', '') for b in e['bases'])),
                              ', corroborated by %d unreviewed OCR drawing mention(s)' % len(e['drawing_mentions']) if corroborated else ''))
            e['assertion'] = self.assertion('equipment/' + tag, 'type', e['class'], sorted(e['source_ids'])[:1], explanation,
                                            'bas_naming_convention', 'needs_review')
            e['basis'] = basis
        fams = defaultdict(list)
        for e in self.equipment.values():
            fams[e['class']].append(e)
        for cls, items in sorted(fams.items()):
            corroborated = sum(1 for e in items if e['drawing_mentions'])
            self.review('equipment-family:' + cls, 'Confirm that the %d BAS container(s) interpreted as %s candidates correspond to physical '
                        'equipment (tags, count, any missing units). %d have an unreviewed drawing mention.' % (len(items), cls, corroborated),
                        [e['assertion'] for e in items], [s for e in items for s in sorted(e['source_ids'])],
                        'NAMING_CONVENTION_ONLY', 'equipment identity for %s' % cls, 'high' if cls in ('AHU', 'Hot_Water_System', 'Chilled_Water_System') else 'medium')

    def _is_descendant(self, key, ancestor):
        return key[0] == ancestor[0] and key[1].startswith(ancestor[1] + '/')

    def _relationships(self):
        """Candidate hasPoint (documented containment under an equipment-named container) and candidate feeds (AHU-named folder
        containing terminal-unit containers). All needs_review."""
        by_tag_points = defaultdict(list)
        # AHU-named folders that hold terminal-unit containers are groupings, not the AHU's own controller.
        self.terminal_folders = {anc for key, n in self.containers.items() if n['equipment'] and n['equipment'][0][1] == 'Terminal_Unit'
                                 for anc, a in list(self._ancestors(key))[1:] if a['equipment'] and a['equipment'][0][1] == 'AHU'}
        for r in self.records:
            if r.parse_status != 'ok':
                continue
            f = self.facts[r.source_id]
            segs = [s for s in f['path'].strip('/').split('/') if s]
            tag, method = None, None
            if segs:
                for key, node in self._ancestors((f['station'], '/' + '/'.join(segs))):
                    if node['equipment'] and key not in self.terminal_folders:
                        tag, method = node['equipment'][0][0], 'bas_containment'
                        break
            if tag is None:
                found = classify_segment(f['station'])[1]
                if found:
                    tag, method = found[0][0], 'bas_station_grouping'
            if tag:
                self.point_equipment[r.source_id] = (tag, method)
                by_tag_points[(tag, method)].append(r.source_id)
                self.equipment[tag]['points'].add(r.source_id)
        self.has_point = []
        for (tag, method), sids in sorted(by_tag_points.items()):
            for sid in sids:
                pid = self.facts[sid]['point_id']
                why = ('Point is contained in a BAS container named like %s (software containment); ownership by the physical '
                       'equipment is not documented.' if method == 'bas_containment' else
                       'Point is hosted by a BAS station named like %s; station hosting is a weak cue for ownership.') % tag
                self.has_point.append(self.assertion('equipment/' + tag, 'hasPoint', 'point/' + pid, [sid], why, method, 'needs_review'))
        self.feeds = []
        for key, node in self.containers.items():
            if not node['equipment'] or node['equipment'][0][1] != 'Terminal_Unit':
                continue
            for anc_key, anc in list(self._ancestors(key))[1:]:
                if anc['equipment'] and anc['equipment'][0][1] == 'AHU':
                    ahu, tu = anc['equipment'][0][0], node['equipment'][0][0]
                    sids = sorted(self.equipment[tu]['points'])[:3] or node['rows'][:1]
                    self.feeds.append(self.assertion('equipment/' + ahu, 'feeds', 'equipment/' + tu, sids,
                                                     'Terminal-unit container %s sits in BAS folder %s named for %s; the BAS grouping '
                                                     'suggests, but does not document, that %s supplies air to %s.' % (tu, anc['decoded'], ahu, ahu, tu),
                                                     'bas_folder_grouping', 'needs_review', medium='air'))
                    break
        self.includes = []
        for key, node in self.containers.items():
            if not node['equipment'] or node['equipment'][0][1] in SYSTEM_CLASSES:
                continue
            for anc_key, anc in list(self._ancestors(key))[1:]:
                if anc['equipment'] and anc['equipment'][0][1] in SYSTEM_CLASSES:
                    system = anc['equipment'][0][0]
                    for member, _, _ in node['equipment']:
                        sids = sorted(self.equipment[member]['points'])[:3] or node['rows'][:1] or sorted(self.equipment[system]['points'])[:1]
                        if not any(a.subject_id == 'equipment/' + system and a.object_id == 'equipment/' + member for a in self.includes):
                            self.includes.append(self.assertion('equipment/' + system, 'includes', 'equipment/' + member, sids,
                                                                'Container of %s is nested in BAS folder %s named for system %s; BAS folder '
                                                                'grouping suggests, but does not document, system membership.' % (member, anc['decoded'], system),
                                                                'bas_folder_grouping', 'needs_review'))
                    break
        if self.includes:
            self.review('system-membership', 'Confirm the %d system memberships suggested by BAS folder nesting (system: members).'
                        % len(self.includes), self.includes, [s for a in self.includes for s in a.supporting_source_ids],
                        'BAS_GROUPING_NOT_DOCUMENTED_TOPOLOGY', 'system composition', 'medium')
        memberless = sorted(t for t, e in self.equipment.items() if e['class'] in SYSTEM_CLASSES
                            and not any(a.subject_id == 'equipment/' + t for a in self.includes))
        if memberless:
            self.review('system-members-missing', '%d system candidate(s) have no documented member equipment (Brick requires a System to '
                        'include at least one member). Which pumps, heat exchangers or coils belong to each?' % len(memberless),
                        [self.equipment[t]['assertion'] for t in memberless], [s for t in memberless for s in sorted(self.equipment[t]['points'])[:2]],
                        'SYSTEM_MEMBERS_UNDOCUMENTED', 'system composition', 'high')
        seen = {}
        for a in self.feeds:
            seen.setdefault(a.subject_id, []).append(a)
        for ahu, rows in sorted(seen.items()):
            self.review('air-serving:' + ahu, 'Confirm that %s supplies the %d terminal units grouped under its BAS folder (and whether any other '
                        'AHU also serves them).' % (ahu.split('/', 1)[1], len({a.object_id for a in rows})), rows,
                        [s for a in rows for s in a.supporting_source_ids], 'BAS_GROUPING_NOT_DOCUMENTED_TOPOLOGY',
                        'air-side topology for %d terminal units' % len(rows), 'high')
        groupless = sorted(t for t, e in self.equipment.items() if e['class'] == 'Terminal_Unit' and not any(a.object_id == 'equipment/' + t for a in self.feeds))
        if groupless:
            stations = Counter(s for t in groupless for s in {self.facts[x]['station'] for x in self.equipment[t]['points']})
            self.review('air-serving:ungrouped', 'Which AHU serves each of the %d terminal units / lab airflow valves that have no AHU-named BAS '
                        'folder (hosted by stations %s)?' % (len(groupless), ', '.join('%s (%d)' % kv for kv in stations.most_common())),
                        [self.equipment[t]['assertion'] for t in groupless], [s for t in groupless for s in sorted(self.equipment[t]['points'])[:1]],
                        'NO_AIR_SIDE_EVIDENCE', 'air-side topology for %d terminal units' % len(groupless), 'high')
        unowned = [r.source_id for r in self.records if r.parse_status == 'ok' and r.source_id not in self.point_equipment]
        if unowned:
            self.review('ownership:unassigned', '%d accepted points have no equipment-named BAS container or station. Which equipment (if any) '
                        'owns them?' % len(unowned), [], unowned, 'OWNERSHIP_NOT_DOCUMENTED', 'equipment ownership of %d points' % len(unowned), 'medium')

    def _point_classes(self):
        self.point_class = {}
        for r in self.records:
            if r.parse_status != 'ok':
                continue
            f = self.facts[r.source_id]
            cls = lexicon_class(f['name'])
            if not cls:
                continue
            unit = QUDT_UNITS.get(f['units'])
            expected, have = CLASS_QUANTITY.get(cls), UNIT_QUANTITY.get(unit)
            conflict = bool(expected and have and expected != have and (expected, have) not in COMPATIBLE)
            status = 'contradicted' if conflict else 'needs_review'
            why = ('Naming cue %r matches the %s lexicon entry%s.' % (f['name'], cls, '; source unit %s conflicts with the class quantity' % f['units'] if conflict else
                   ('; source unit %s is consistent' % f['units'] if have else '; no source unit')))
            self.point_class[r.source_id] = (cls, status)
            a = self.assertion('point/' + f['point_id'], 'type', cls, [r.source_id], why, 'naming_lexicon', status)
            if conflict:
                self.issue(f['point_id'], 'model_conflict', 'Name suggests %s but unit is %s.' % (cls, f['units']), 'unit_class_mismatch',
                           [r.source_id], 'Confirm the point quantity from the BAS or submittals.')
        counts = Counter(c for c, s in self.point_class.values())
        if counts:
            self.review('point-classes:lexicon', 'Accept, correct or reject naming-cue classes for %d points (%d distinct classes; %d unit '
                        'conflicts). The remaining points need model or human classification.' % (len(self.point_class), len(counts),
                        sum(1 for c, s in self.point_class.values() if s == 'contradicted')), [], sorted(self.point_class)[:50],
                        'NAMING_CUE_ONLY', 'point classes for %d points' % len(self.point_class), 'medium')

    def _model_classes(self):
        """Merge hosted-model point classifications (router ledger rows) as candidates. Never accepted; checked against the
        source unit (quantity conflict -> contradicted), the name role, the BAS kind, the naming lexicon and equipment context."""
        run = self.model_results.get('run', {})
        model, prompt = run.get('model'), run.get('prompt_version')
        dup = set(self.duplicates)
        flagged = Counter()
        for row in self.model_results['ledger_rows']:
            sid, cls, outcome = row['source_id'], row['candidate_class'], row['outcome']
            if row.get('route') not in ('llm', 'cache_reuse', 'propagated_signature', 'held_oversized'):
                continue                      # held / deterministic / unrouted rows carry no model output
            r = self.by_source.get(sid)
            if r is None or r.parse_status != 'ok' or self.facts[sid]['point_id'] in dup:
                continue
            f = self.facts[sid]
            self.model_outcomes[sid] = {'class': cls, 'outcome': outcome, 'route': row['route'], 'signature_id': row['signature_id'],
                                        'batch_id': row['batch_id'], 'derived_from': row['derived_from_source_id']}
            alts = row.get('alternatives') or []
            withheld = outcome == 'check_failed' and len(alts) == 1     # router withheld this class after a hard check
            if withheld:
                cls = alts[0]
                self.model_outcomes[sid]['withheld_class'] = cls
            elif outcome != 'resolved' or cls in ('Point', 'unknown'):
                continue
            from .inference import class_checks
            rep = row['derived_from_source_id']
            family = equipment_family(f['path'], f['station'])
            rep_family = equipment_family(self.facts[rep]['path'], self.facts[rep]['station']) if rep in self.facts else family
            if rep and rep_family != family:
                # Under family-aware grouping this row would be its own signature: do not carry the class across families.
                flagged.update(['WITHHELD_PROPAGATION_ACROSS_EQUIPMENT_FAMILY'])
                self.model_outcomes[sid]['withheld'] = 'PROPAGATION_ACROSS_EQUIPMENT_FAMILY'
                continue
            findings = class_checks.check(cls, f['name'], f['units'], f['kind'], family)
            reasons = [code for code, _ in findings] + list(row.get('context_flags') or [])
            if withheld:
                reasons += ['REPRESENTATIVE_FAILED_HARD_CHECK:' + c for c in row.get('validation_reason_codes') or [] if c not in reasons]
            lex = self.point_class.get(sid, (None,))[0]
            if lex and lex != cls and class_checks.class_quantity(lex) and class_checks.class_quantity(cls) and \
                    class_checks.class_quantity(lex) != class_checks.class_quantity(cls):
                reasons.append('DISAGREES_WITH_NAMING_LEXICON')
            flagged.update(reasons)
            status = 'contradicted' if withheld or class_checks.hard(findings) else 'needs_review'
            method = 'llm' if row['route'] in ('llm', 'cache_reuse') else 'llm_signature_propagation'   # cache_reuse: representative on a continued run
            sources = [sid] + ([rep] if rep and rep in self.by_source else [])
            why = ('Hosted model (%s, %s) proposed %s for %s %s; batch %s.%s%s' % (
                model, prompt, cls, 'this representative' if method == 'llm' else 'signature', row['signature_id'], row['batch_id'],
                ' Propagated from %s (same name/units/kind).' % rep if rep else '', (' Flags: ' + ', '.join(reasons) + '.') if reasons else ''))
            self.assertion('point/' + f['point_id'], 'type', cls, sources, why, method, status, model_version=model, prompt_version=prompt)
            if reasons:
                self.issue(f['point_id'] + ':' + cls, 'model_conflict', 'Model class %s flagged: %s.' % (cls, ', '.join(reasons)),
                           'model_class_check', [sid], 'Confirm the point role and quantity from the BAS or submittals.')
        outcomes = Counter((o['outcome'], o['class'] if o['class'] in ('Point', 'unknown') else 'specific') for o in self.model_outcomes.values())
        self.model_summary = {'rows_with_model_outcome': len(self.model_outcomes), 'outcomes': {'%s|%s' % k: v for k, v in sorted(outcomes.items())},
                              'flags': dict(flagged), 'model': model, 'prompt_version': prompt}
        generic = sum(v for (o, c), v in outcomes.items() if c == 'Point')
        if generic:
            vocab = run.get('vocabulary_version')
            why = ('Classify them by hand or from submittals, or extend the vocabulary further.' if vocab else
                   'The allowed class list had no setpoint, command, alarm or mode classes. Extend the accepted vocabulary before broader inference?')
            self.review('model-generic-point', 'The model returned only the generic Point class for %d rows. %s' % (generic, why), [], [],
                        'VOCABULARY_LIMITED' if not vocab else 'MODEL_GENERIC_POINT', 'specific classes for %d rows' % generic, 'high')
        if flagged:
            self.review('model-class-flags', 'Review model classes flagged by deterministic checks (%s).' % ', '.join('%s %d' % kv for kv in flagged.most_common()),
                        [a for a in self.assertions if a.inference_method.startswith('llm') and 'Flags:' in a.explanation][:50], [],
                        'MODEL_CLASS_CHECKS', 'point classes', 'high')
        missing = [sid for sid, o in self.model_outcomes.items() if o['outcome'] in ('no_response', 'resolved_unknown', 'abstained', 'rejected', 'conflict')]
        if missing:
            self.review('model-unresolved', '%d rows received no usable model class (unknown, no valid candidate, or abstention).' % len(missing), [],
                        missing[:50], 'MODEL_UNRESOLVED', 'point classes for %d rows' % len(missing), 'medium')

    def _thermal_hypotheses(self):
        """Physical/thermal hypotheses from point names inside an equipment candidate's BAS containers. Review queue only."""
        for tag, e in sorted(self.equipment.items()):
            cues = defaultdict(list)
            for sid in sorted(e['points']):
                name = norm_name(self.facts[sid]['name'])
                for rx, key in THERMAL_CUES:
                    if e['class'] in APPLIES_TO[key] and re.search(rx, name):
                        cues[key].append(sid)
            for key, sids in sorted(cues.items()):
                component, source, predicate, missing = HYPOTHESES[key]
                h = {'hypothesis_id': uid('hypothesis', self.run_id, tag, key), 'equipment': tag, 'equipment_class': e['class'],
                     'hypothesis': key, 'candidate_component_class': component, 'candidate_source': source, 'predicate': predicate,
                     'status': 'needs_review', 'basis': 'point names within the BAS containers of %s (naming only)' % tag,
                     'cue_point_count': len(sids), 'cue_source_ids': sids[:10], 'missing_evidence': missing,
                     'in_any_graph': False}
                self.hypotheses.append(h)
        grouped = defaultdict(list)
        for h in self.hypotheses:
            grouped[(h['equipment_class'], h['hypothesis'])].append(h)
        for (cls, key), hs in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            component, source, predicate, missing = HYPOTHESES[key]
            relation = ('%s %s the %s of these units' % (source, predicate, component.replace('_', ' ').lower()) if source
                        else '%s %s %s' % (cls, predicate, component.replace('_', ' ').lower()))
            self.review('thermal:%s:%s' % (cls, key), 'Thermal/physical hypothesis for %d %s candidate(s): %s? Needed evidence: %s.'
                        % (len(hs), cls, relation, missing), [], [s for h in hs for s in h['cue_source_ids'][:2]],
                        'NAMING_ONLY_NO_GRAPH_EDGE', '%s for %d units' % (key, len(hs)),
                        'high' if key in ('chilled_water_coil', 'hot_water_coil', 'heat_exchanger', 'chilled_water_pumps') else 'medium')

    def _drawing_only_tags(self):
        tags = defaultdict(list)
        for row in self.drawing_rows:
            m = DRAWING_TAG.fullmatch((row.get('subject_text') or '').upper().replace(' ', ''))
            if m and (m.group(1) + '-' + m.group(2)) not in self.equipment and m.group(1) in ('AHU', 'EF', 'FCU', 'HX'):   # 'P-nnn' is ambiguous with plumbing sheet numbers
                tags[m.group(1) + '-' + m.group(2)].append(row.get('candidate_id'))
        self.drawing_only = dict(sorted(tags.items()))
        if self.drawing_only:
            self.review('drawing-only-tags', '%d equipment-like tag(s) appear in unreviewed drawing OCR but not in the BAS export. Are they real '
                        'equipment without BAS points, OCR errors, or sheet/detail numbers?' % len(self.drawing_only), [],
                        [c for v in self.drawing_only.values() for c in v], 'OCR_UNREVIEWED', 'equipment completeness', 'low')

    # -- graphs ------------------------------------------------------------------------------------
    def _bind(self, graph):
        for prefix, ns in (('brick', BRICK), ('bldg', self.ns), ('ref', REF), ('unit', UNIT), ('skos', SKOS), ('prov', PROV), ('dcterms', DCTERMS), ('qudt', QUDT), ('rec', REC)):
            graph.bind(prefix, ns)
        return graph

    def accepted_graph(self):
        from .pipeline import build_graph
        graph = self._bind(build_graph(self.assertions, str(self.ns)))   # accepted Point types through the existing builder
        building = self.iri('building')
        graph.add((building, RDF.type, BRICK.Building))
        graph.add((building, RDFS.label, Literal(self.label)))
        source = self.iri('source-file/' + self.source_sha256)
        graph.add((source, RDF.type, PROV.Entity))
        graph.add((source, RDFS.label, Literal(self.source_file)))
        graph.add((source, DCTERMS.identifier, Literal('sha256:' + self.source_sha256)))
        graph.add((building, DCTERMS.source, source))
        scheme = self.iri('bas-tree')
        graph.add((scheme, RDF.type, SKOS.ConceptScheme))
        graph.add((scheme, RDFS.label, Literal('BAS software containment as exported (station / Fox path). Not physical topology.')))
        for station in sorted({n['station'] for n in self.containers.values()} | {f['station'] for f in self.facts.values() if f['station']}):
            node = self.iri('bas-station/' + station)
            graph.add((node, RDF.type, SKOS.Concept))
            graph.add((node, SKOS.inScheme, scheme))
            graph.add((node, SKOS.prefLabel, Literal(station)))
            graph.add((node, SKOS.topConceptOf, scheme))
        for key, n in sorted(self.containers.items()):
            node = self.iri('bas-container/' + _sid(*key))
            graph.add((node, RDF.type, SKOS.Concept))
            graph.add((node, SKOS.inScheme, scheme))
            graph.add((node, SKOS.prefLabel, Literal(n['decoded'])))
            graph.add((node, SKOS.notation, Literal(n['path'])))
            parent = n['parent']
            graph.add((node, SKOS.broader, self.iri('bas-station/' + parent[1]) if parent[0] == 'station' else self.iri('bas-container/' + _sid(*parent))))
        typed = {a.subject_id: a.object_id for a in self.assertions
                 if a.predicate == 'type' and a.subject_id.startswith('equipment/') and a.status in ACCEPTED}   # only accepted classes
        for a in self.connections + self.traced_memberships:
            if a.status not in ACCEPTED:          # verified (engineer) or supported_inference (team documentary check of a clean trace)
                continue
            s_, o_ = self.iri(a.subject_id), self.iri(a.object_id)
            graph.add((s_, BRICK.connectedTo if a.predicate == 'connectedTo' else REC.includes, o_))
            generic = ('System' if a.predicate == 'includes' else 'Equipment', 'Equipment')
            for node, ident, fallback in ((s_, a.subject_id, generic[0]), (o_, a.object_id, generic[1])):
                graph.add((node, RDF.type, BRICK[typed.get(ident, fallback)]))
                graph.add((node, RDFS.label, Literal(ident.split('/', 1)[1])))
            for sid in a.supporting_source_ids:
                row = self.iri('source-row/' + self.by_source[sid].source_record_uid)
                graph.add((row, RDF.type, PROV.Entity))
                graph.add((row, DCTERMS.identifier, Literal(sid)))
                graph.add((row, PROV.atLocation, Literal(self.by_source[sid].source_location)))
                graph.add((s_, PROV.wasDerivedFrom, row))
        accepted_points = {a.subject_id for a in self.assertions if a.status in ACCEPTED and a.predicate == 'type'}
        for r in self.records:
            f = self.facts[r.source_id]
            if r.parse_status != 'ok' or 'point/' + f['point_id'] not in accepted_points:
                continue
            point = self.iri('point/' + f['point_id'])
            graph.add((point, RDFS.label, Literal(f['name'])))
            if f['units'] in QUDT_UNITS:
                unit = UNIT[QUDT_UNITS[f['units']]]
                graph.add((point, BRICK.hasUnit, unit))
                graph.add((unit, RDF.type, QUDT.Unit))
                graph.add((unit, RDFS.label, Literal(QUDT_LABELS[QUDT_UNITS[f['units']]])))
            row = self.iri('source-row/' + r.source_record_uid)
            graph.add((row, RDF.type, PROV.Entity))
            graph.add((row, DCTERMS.identifier, Literal(r.source_id)))
            graph.add((row, PROV.atLocation, Literal(r.source_location)))
            graph.add((row, DCTERMS.isPartOf, source))
            graph.add((point, PROV.wasDerivedFrom, row))
            ref = self.iri('bas-object/' + f['point_id'])
            graph.add((point, REF.hasExternalReference, ref))
            graph.add((ref, RDF.type, REF.ExternalReference))
            graph.add((ref, RDFS.label, Literal('BAS object %s' % f['name'])))
            graph.add((ref, DCTERMS.identifier, Literal(f['point_id'])))
            if f['kind']:
                graph.add((ref, DCTERMS.type, Literal(f['kind'])))
            if f['path']:
                graph.add((ref, DCTERMS.isPartOf, self.iri('bas-container/' + _sid(f['station'], '/' + '/'.join(s for s in f['path'].strip('/').split('/') if s)))))
            elif f['station']:
                graph.add((ref, DCTERMS.isPartOf, self.iri('bas-station/' + f['station'])))
        return graph

    def provisional_graph(self):
        graph = self._bind(Graph())
        n = 0

        def candidate(a, triple):
            nonlocal n
            graph.add(triple)
            st = self.iri('candidate/' + a.assertion_id)
            graph.add((st, RDF.type, RDF.Statement))
            graph.add((st, RDF.subject, triple[0]))
            graph.add((st, RDF.predicate, triple[1]))
            graph.add((st, RDF.object, triple[2]))
            graph.add((st, DCTERMS.description, Literal('%s | %s | %s' % (a.status, a.inference_method, a.explanation))))
            for sid in a.supporting_source_ids[:5]:
                graph.add((st, PROV.wasDerivedFrom, self.iri('source-row/' + self.by_source[sid].source_record_uid)))
            n += 1
        for tag, e in sorted(self.equipment.items()):
            subject = self.iri('equipment/' + tag)
            candidate(e['assertion'], (subject, RDF.type, BRICK[e['class']]))
            graph.add((subject, RDFS.label, Literal(tag)))
        for a in self.has_point + self.feeds:
            candidate(a, (self.iri(a.subject_id), BRICK[a.predicate], self.iri(a.object_id)))
        for a in self.includes:
            candidate(a, (self.iri(a.subject_id), REC.includes, self.iri(a.object_id)))
        for a in self.assertions:
            if a.inference_method == 'naming_lexicon':
                candidate(a, (self.iri(a.subject_id), RDF.type, BRICK[a.object_id]))
        for pid, d in sorted(self.duplicates.items()):
            candidate(d['assertion'], (self.iri('point/' + pid), RDF.type, BRICK.Point))
        for d in self.diagram_equipment:
            candidate(d['assertion'], (self.iri('equipment/' + d['tag']), RDF.type, BRICK[d['class']]))
            graph.add((self.iri('equipment/' + d['tag']), RDFS.label, Literal(d['tag'])))
        for a in self.connections:
            if a.status != 'rejected':
                candidate(a, (self.iri(a.subject_id), BRICK.connectedTo, self.iri(a.object_id)))
        for a in self.traced_memberships:
            if a.status != 'rejected':
                candidate(a, (self.iri(a.subject_id), REC.includes, self.iri(a.object_id)))
        for d in getattr(self, 'drawing_equipment', []):
            node = self.iri('equipment/' + d['tag'])
            graph.add((node, RDF.type, BRICK[d['class']]))
            graph.add((node, RDFS.label, Literal(d['tag'])))
            refs = []
            for ref in d['schedule_refs']:
                rnode = self.iri('drawing-ref/%s-p%s-%s' % (ref['pdf_sha256'][:12], ref['page'], _sid(ref['bbox'])))
                graph.add((rnode, RDF.type, PROV.Entity))
                graph.add((rnode, DCTERMS.identifier, Literal('sha256:%s page %s sheet %s bbox %s ocr_min %s' % (
                    ref['pdf_sha256'], ref['page'], ref['sheet_number'], ref['bbox'], ref['ocr_confidence_min']))))
                refs.append(rnode)
            triples = [(node, RDF.type, BRICK[d['class']])] + [(self.iri('equipment/' + m), REC.includes, node) for m in d['candidate_member_of']]
            for i, t in enumerate(triples):
                graph.add(t)
                st = self.iri('candidate/drawing-%s-%d' % (d['tag'], i))
                graph.add((st, RDF.type, RDF.Statement))
                graph.add((st, RDF.subject, t[0]))
                graph.add((st, RDF.predicate, t[1]))
                graph.add((st, RDF.object, t[2]))
                graph.add((st, DCTERMS.description, Literal('needs_review | drawing_schedule_row_ocr | %s' % (
                    'equipment found only in a drawing schedule (OCR)' if i == 0 else 'system membership suggested by the schedule service text (OCR)'))))
                for rnode in refs:
                    graph.add((st, PROV.wasDerivedFrom, rnode))
        return graph

    # -- reports -----------------------------------------------------------------------------------
    def coverage(self, accepted, provisional):
        statuses = Counter((a.predicate if a.predicate != 'type' else 'type:' + ('point' if a.subject_id.startswith('point/') else 'equipment'), a.status)
                           for a in self.assertions)
        ok = [r for r in self.records if r.parse_status == 'ok']
        classified = Counter(s for c, s in self.point_class.values())
        eq_cls = Counter(e['class'] for e in self.equipment.values())
        missing = sorted({(h['hypothesis'], h['missing_evidence']) for h in self.hypotheses})
        return {
            'builder_version': VERSION, 'run_id': self.run_id,
            'source_records': {'total': len(self.records), 'ok': len(ok), 'error': len(self.records) - len(ok),
                               'represented_in_accepted_graph': sum(1 for a in self.assertions if a.status in ACCEPTED and a.predicate == 'type'),
                               'represented_provisionally_duplicate_identity_rows': sum(len(d['rows']) for d in self.duplicates.values()),
                               'duplicate_identifiers': len(self.duplicates),
                               'not_represented_issue_only': len(self.records) - len(ok) - sum(len(d['rows']) for d in self.duplicates.values()),
                               'with_bas_containment': sum(1 for r in ok if self.facts[r.source_id]['path']),
                               'with_qudt_unit': sum(1 for r in ok if self.facts[r.source_id]['units'] in QUDT_UNITS),
                               'with_unit_string_not_mapped': sum(1 for r in ok if self.facts[r.source_id]['units'] and self.facts[r.source_id]['units'] not in QUDT_UNITS)},
            'points': {'accepted_as_generic_brick_Point': sum(1 for a in self.assertions if a.status in ACCEPTED and a.predicate == 'type'),
                       'specific_class_accepted': 0,
                       'specific_class_candidate_needs_review': classified.get('needs_review', 0),
                       'specific_class_candidate_contradicted_by_unit': classified.get('contradicted', 0),
                       'points_without_naming_cue_class': len(ok) - len(self.point_class),
                       'model_specific_class_candidates': sum(1 for a in self.assertions if a.inference_method.startswith('llm') and a.predicate == 'type'),
                       'points_with_any_specific_candidate': len({a.supporting_source_ids[0] for a in self.assertions if a.predicate == 'type'
                                                                  and a.subject_id.startswith('point/') and a.object_id not in ('Point', 'unknown')}),
                       'points_without_any_specific_candidate': len(ok) - len({a.supporting_source_ids[0] for a in self.assertions if a.predicate == 'type'
                                                                  and a.subject_id.startswith('point/') and a.object_id not in ('Point', 'unknown')
                                                                  and self.by_source[a.supporting_source_ids[0]].parse_status == 'ok'}),
                       'candidate_classes': dict(Counter(c for c, s in self.point_class.values()).most_common())},
            'bas_containment': {'stations': len({n['station'] for n in self.containers.values()}), 'containers': len(self.containers),
                                'container_kinds': dict(Counter(n['kind'] for n in self.containers.values()))},
            'equipment_candidates': {'total': len(self.equipment), 'by_class': dict(sorted(eq_cls.items())),
                                     'with_drawing_mention': sum(1 for e in self.equipment.values() if e['drawing_mentions']),
                                     'accepted': 0},
            'relationships': {
                'accepted': {'hasPoint': 0, 'feeds': 0, 'hasPart': 0, 'system_includes': 0, 'thermal': 0},
                'provisional_needs_review': {'hasPoint': len(self.has_point), 'feeds': len(self.feeds), 'hasPart': 0,
                                             'system_includes': len(self.includes)},
                'system_candidates_without_documented_members': sum(1 for t, e in self.equipment.items() if e['class'] in SYSTEM_CLASSES
                                                                   and not any(a.subject_id == 'equipment/' + t for a in self.includes)),
                'points_with_candidate_owner': {'bas_containment': sum(1 for t, m in self.point_equipment.values() if m == 'bas_containment'),
                                                'bas_station_grouping': sum(1 for t, m in self.point_equipment.values() if m == 'bas_station_grouping'),
                                                'none': len(ok) - len(self.point_equipment)},
                'review_queue_only_hypotheses': len(self.hypotheses),
                'hypotheses_by_kind': dict(Counter(h['hypothesis'] for h in self.hypotheses).most_common()),
                'assertion_status_counts': {'%s|%s' % k: v for k, v in sorted(statuses.items())}},
            'missing_evidence_for_physical_relationships': [{'relationship': k, 'needed': v} for k, v in missing] + [
                {'relationship': 'air_side_serving (AHU -> terminal units)', 'needed': 'air-flow riser / duct plans or TAB report listing the AHU serving each terminal unit'},
                {'relationship': 'equipment_identity', 'needed': 'mechanical equipment schedules (M-sheets) or an equipment list with tags'},
                {'relationship': 'time-series behaviour', 'needed': 'trend data; the export has no values, so correlation/physics checks are not evaluable'}],
            'graphs': {'accepted_triples': len(accepted), 'provisional_triples': len(provisional)},
            'drawing_only_equipment_tags': len(self.drawing_only),
            'drawing_evidence': self._drawing_coverage(),
            'model_classifications': getattr(self, 'model_summary', {'status': 'not_supplied'}),
            'equipment_identity': dict(Counter(r['resolution'] for r in self.equipment_identity())),
            'physical_relationships': {'%s|%s' % k: v for k, v in sorted(Counter((r['relationship'], r['category']) for r in self.physical_relationships()).items())},
            'physical_relationship_categories': dict(Counter(r['category'] for r in self.physical_relationships())),
            'review_queue': {'items': len(self.reviews), 'by_priority': dict(Counter(r.priority for r in self.reviews))},
            'issues': len(self.issues), 'verified_count': 0,
        }

    DRAWING_CLASSES = {'P': 'Pump', 'HWP': 'Hot_Water_Pump', 'CHWP': 'Chilled_Water_Pump', 'PCHWP': 'Chilled_Water_Pump',
                       'HX': 'Heat_Exchanger', 'EF': 'Exhaust_Fan', 'SF': 'Supply_Fan', 'VAV': 'Terminal_Unit', 'FTR': 'Radiator'}
    SERVICE_WORDS = [('HEATING HOT WATER', 'HWS'), ('HOT WATER', 'HWS'), ('HHW', 'HWS'), ('HEATING WATER', 'HWS'),
                     ('CHILLED WATER', 'CHWS'), ('CHW', 'CHWS'), ('STEAM', 'steam_primary'), ('GLYCOL', 'heat_recovery'),
                     ('HEAT RECOVERY', 'heat_recovery'), ('RUNAROUND', 'heat_recovery')]

    DRAWING_SOURCE_FILE = 'drawing_evidence.json'

    def _drawing_record(self, row, tag):
        """A citable source record for one OCR schedule row. table_kind 'unknown': the evidence gate cannot treat it as a
        documented equipment table, so nothing citing it can be accepted automatically."""
        ref = row['ref']
        sid = '%s:%s:%s' % (self.DRAWING_SOURCE_FILE, row['schedule_id'], _sid(ref['bbox']))
        if sid in self.by_source:
            return sid
        cells = {str(k): str(v) for k, v in row['cells'].items()}
        record = SourceRecord(source_id=sid, source_record_uid=uid('drawsrc', self.scope, sid), source_file=self.DRAWING_SOURCE_FILE,
                              source_type='ocr_extraction', source_hash=hashlib.sha256(self.drawing_evidence_bytes).hexdigest(),
                              source_location='OCR of PDF sha256 %s page %s sheet %s bbox %s (min confidence %s)' % (
                                  ref['pdf_sha256'], ref['page'], ref['sheet_number'], ref['bbox'], ref['ocr_confidence_min']),
                              table_kind='unknown', raw_value=list(cells.values()), raw_record=cells,
                              normalized_fields={'tag': tag, 'schedule_category': str(row.get('category', '')), 'pdf_sha256': ref['pdf_sha256'],
                                                 'page': str(ref['page']), 'sheet_number': str(ref['sheet_number'] or '')},
                              parse_status='ok', errors=[])
        self.extra_records.append(record)
        self.by_source[sid] = record
        return sid

    SCHEDULE_KEYWORDS = ('PRIMARY', 'SECONDARY', 'CONDENSER', 'HX', 'CHILLED', 'HEATING', 'HOT WATER', 'GLYCOL', 'STEAM', 'DOMESTIC')

    def _drawing_equipment(self, result):
        """Equipment found only in drawing schedules (OCR): provisional candidates cited to drawing references, never to BAS rows.
        The schedule row's own text (service / system columns) can suggest system membership; nothing is accepted."""
        self.drawing_equipment = []
        scheduled = self.drawing_evidence.get('scheduled_tags', {})
        diagram_systems = defaultdict(set)
        for d in self.drawing_evidence.get('diagrams', []):
            for tag in d['tags']:
                diagram_systems[tag].update(x for x in d['systems'] if x != 'unspecified')
        bas_points = defaultdict(list)
        for sid, f in self.facts.items():
            m = re.match(r'^(PCHWP|CHWP|HWP|HX|P)(\d{1,2})(?!\d)', f['name'] or '', re.I)
            if m and self.by_source[sid].parse_status == 'ok':
                bas_points[m.group(1).upper() + '-' + str(int(m.group(2)))].append(sid)
        system_tags = {'HWS': next((t for t, e in self.equipment.items() if e['class'] == 'Hot_Water_System'), None),
                       'CHWS': next((t for t, e in self.equipment.items() if e['class'] == 'Chilled_Water_System'), None)}
        for tag in result['drawing_only_scheduled_tags']:
            family = tag.split('-')[0]
            if family not in self.DRAWING_CLASSES:
                continue
            rows = scheduled.get(tag, [])
            text = ' '.join(str(v) for r in rows for v in r['cells'].values()).upper() + ' '
            row_services = {sys for word, sys in self.SERVICE_WORDS if word in text}
            title_services = {x for r in rows for x in r.get('title_systems', []) if x in ('HWS', 'CHWS', 'steam', 'heat_recovery')}
            services = sorted(row_services | title_services)
            systems = sorted(set(services) | diagram_systems.get(tag, set()))
            member_of = [system_tags[s] for s in ('HWS', 'CHWS') if s in services and system_tags.get(s)]
            self.drawing_equipment.append({'tag': tag, 'class': self.DRAWING_CLASSES[family], 'status': 'needs_review',
                                           'basis': ['drawing_schedule_row_ocr'], 'schedule_refs': [r['ref'] for r in rows][:5],
                                           'schedule_categories': sorted({r['category'] for r in rows}), 'service_from_schedule_text': services,
                                           'diagram_systems': sorted(diagram_systems.get(tag, set())), 'systems': systems,
                                           'candidate_member_of': member_of,
                                           'bas_point_name_matches': len(bas_points.get(tag, [])),
                                           'bas_point_examples': bas_points.get(tag, [])[:5],
                                           'membership_basis': 'schedule_row_text' if row_services else 'schedule_title' if title_services else None,
                                           'schedule_keywords': sorted({k for k in self.SCHEDULE_KEYWORDS if k in text}),
                                           'source_ids': [self._drawing_record(r, tag) for r in rows][:5]})
            d = self.drawing_equipment[-1]
            d['assertion'] = self.assertion('equipment/' + tag, 'type', d['class'], d['source_ids'][:3],
                                            'Equipment found only in a drawing schedule (OCR, unconfirmed); no BAS container carries this tag.',
                                            'drawing_schedule_ocr', 'needs_review')
        if self.drawing_equipment:
            members = [d for d in self.drawing_equipment if d['candidate_member_of']]
            self.review('drawing-equipment', 'Confirm %d equipment items found only in drawing schedules (OCR) as real equipment, and the %d system '
                        'memberships their schedule service text suggests (%s).' % (len(self.drawing_equipment), len(members),
                        ', '.join('%s %d' % kv for kv in Counter(d['class'] for d in self.drawing_equipment).most_common())), [],
                        [], 'DRAWING_ONLY_EQUIPMENT', 'hydronic system composition', 'high')
            matched = [d for d in self.drawing_equipment if d['bas_point_name_matches']]
            if matched:
                self.review('drawing-equipment-bas-points', 'For %d scheduled pumps/heat exchangers, BAS points carry the same tag number in '
                            'their names (%d points). Do those points belong to that equipment? (Name match only; no edge created.)'
                            % (len(matched), sum(d['bas_point_name_matches'] for d in matched)), [], [s for d in matched for s in d['bas_point_examples']],
                            'NAME_MATCH_ONLY', 'point ownership for hydronic equipment', 'medium')

    def _drawing_coverage(self):
        r = self.drawing_assessment
        if not r:
            return {'status': 'not_supplied'}
        return {'status': 'applied', 'method': 'local on-device OCR (unreviewed); support never promotes a claim',
                'equipment_by_tier': dict(Counter(v['tier'] for v in r['equipment'].values())),
                'equipment_alias_matches': sum(1 for v in r['equipment'].values() if v['alias_match'] and v['tier'] != 'bas_only'),
                'feeds_by_verdict': dict(Counter(f['verdict'] for f in r['feeds'])),
                'hypotheses_by_verdict': dict(Counter(h['verdict'] for h in r['hypotheses'])),
                'hypotheses_supported_by_kind': dict(Counter(h['hypothesis'] for h in r['hypotheses'] if h['verdict'] != 'unresolved')),
                'drawing_only_scheduled_tags': len(r['drawing_only_scheduled_tags']),
                'drawing_only_scheduled_families': dict(Counter(t.split('-')[0] for t in r['drawing_only_scheduled_tags'])),
                'drawing_only_equipment_candidates': dict(Counter(d['class'] for d in getattr(self, 'drawing_equipment', []))),
                'drawing_equipment_with_service_text': sum(1 for d in getattr(self, 'drawing_equipment', []) if d['service_from_schedule_text']),
                'drawing_equipment_system_memberships': dict(Counter(m for d in getattr(self, 'drawing_equipment', []) for m in d['candidate_member_of'])),
                'drawing_equipment_with_bas_name_matches': sum(1 for d in getattr(self, 'drawing_equipment', []) if d['bas_point_name_matches'])}

    # -- Phase 5A: traced drawing topology and human decisions -------------------------------------------
    TRACE_SYSTEM_SHEETS = {'HWS': 'Hot_Water_System', 'CHWS': 'Chilled_Water_System', 'exhaust': 'Air_System', 'air_handling': None}
    TRACE_CLASSES = {'P': 'Pump', 'HWP': 'Hot_Water_Pump', 'CHWP': 'Chilled_Water_Pump', 'PCHWP': 'Chilled_Water_Pump', 'HX': 'Heat_Exchanger',
                     'EF': 'Exhaust_Fan', 'SF': 'Supply_Fan', 'RF': 'Return_Fan', 'VAV': 'Terminal_Unit', 'TB': 'Terminal_Unit', 'FTR': 'Radiator',
                     'AHU': 'AHU', 'FCU': 'Fan_Coil_Unit', 'HC': 'Heating_Coil', 'CC': 'Cooling_Coil', 'HRC': 'Coil'}
    TRACE_SOURCE_FILE = 'drawing_topology.json'

    def _decision_map(self):
        latest = {}
        for d in self.human_decisions:
            latest[d['item_key']] = d                      # append-only log: the latest decision on an item wins
        return latest

    @staticmethod
    def _decided_status(human, team_ok=False):
        """Qualified engineering: confirm -> verified, reject -> rejected. Team documentary check (a drawing READING by a named team
        member, with rationale and references): confirm -> supported_inference only when the evidence itself is clean (`team_ok`),
        reject -> rejected. It can never produce `verified`. Annotations and 'unknown' change nothing."""
        if not human:
            return 'needs_review'
        if human.get('authority') == 'qualified_engineering':
            return {'confirm': 'verified', 'reject': 'rejected'}.get(human['decision'], 'needs_review')
        if human.get('authority') == 'team_documentary_check':
            return {'confirm': 'supported_inference' if team_ok else 'needs_review', 'reject': 'rejected'}.get(human['decision'], 'needs_review')
        return 'needs_review'

    @staticmethod
    def _decision_note(human):
        if not human:
            return ''
        if human.get('authority') == 'qualified_engineering':
            return ' Engineering decision %s by %s (%s, %s) on %s: %s' % (human['decision'], human['reviewer'], human.get('qualification'),
                                                                          human.get('organization'), human.get('review_date'), human.get('rationale'))
        if human.get('authority') == 'team_documentary_check':
            return ' Team documentary check (%s) by %s (%s) on %s: %s. Not an engineering verification.' % (
                human['decision'], human['reviewer'], human.get('team_role'), human['recorded_on'], human.get('rationale'))
        return ' Annotation %s by %s on %s (not an engineering verification; status unchanged).' % (human['decision'], human['reviewer'], human['recorded_on'])

    def _trace_record(self, page, c):
        """Citable record for one traced connection (local vector trace + OCR tag boxes). table_kind 'unknown': never auto-accepted."""
        region = next(r for r in c['refs'] if r['role'] == 'path_region')
        sid = '%s:%s:p%s:%s:%s' % (self.TRACE_SOURCE_FILE, page['pdf_sha256'][:12], page['page'], _sid(c['a'], c['b']), _sid(region['bbox']))
        if sid in self.by_source:
            return sid
        fields = {'tag_a': c['a'], 'tag_b': c['b'], 'category': c['category'], 'flags': ','.join(c['flags']), 'path_length': str(c['path_length']),
                  'direction': c['direction'], 'loop_labels': ','.join(c['loop_labels']), 'systems_on_sheet': ','.join(page['systems']),
                  'pdf_sha256': page['pdf_sha256'], 'page': str(page['page']), 'sheet_number': str(page.get('sheet_number') or '')}
        record = SourceRecord(source_id=sid, source_record_uid=uid('tracesrc', self.scope, sid), source_file=self.TRACE_SOURCE_FILE,
                              source_type='drawing_vector_trace', source_hash=self.topology_sha256,
                              source_location='vector trace of PDF sha256 %s page %s sheet %s; path region %s; tag boxes %s' % (
                                  page['pdf_sha256'], page['page'], page.get('sheet_number'), region['bbox'],
                                  [r['bbox'] for r in c['refs'] if r['role'] != 'path_region']),
                              table_kind='unknown', raw_value=list(fields.values()), raw_record=fields, normalized_fields=fields,
                              parse_status='ok', errors=[])
        self.extra_records.append(record)
        self.by_source[sid] = record
        return sid

    def _vector_topology(self):
        """Traced drawn connections become brick:connectedTo candidates; equipment piped into the network on a single-system
        diagram becomes a traced membership candidate. Unspecified-system sheets (often control schematics) stay register
        entries only. Nothing here is accepted; only a recorded human decision verifies."""
        from .drawing_evidence import bas_key
        trace = self.drawing_evidence['vector_topology']
        self.topology_sha256 = hashlib.sha256(json.dumps(trace, sort_keys=True).encode()).hexdigest()
        bas = {}
        for t in sorted(self.equipment):
            k = bas_key(t)
            if k:
                bas.setdefault(k, t)
        drawing = {d['tag'] for d in getattr(self, 'drawing_equipment', [])}
        systems = {cls: next((t for t, e in sorted(self.equipment.items()) if e['class'] == cls), None)
                   for cls in ('Hot_Water_System', 'Chilled_Water_System', 'Air_System')}
        decisions = self._decision_map()
        diagram_only = {}

        def entity(tag, sid):
            if tag in bas:
                return bas[tag]
            if tag in drawing:
                return tag
            if tag not in diagram_only:
                cls = self.TRACE_CLASSES.get(tag.split('-')[0], 'Equipment')
                diagram_only[tag] = {'tag': tag, 'class': cls, 'source_ids': [], 'status': 'needs_review'}
            diagram_only[tag]['source_ids'].append(sid)
            return tag
        pairs, members = defaultdict(list), defaultdict(list)
        for page in trace['pages']:
            named = [x for x in page['systems'] if x != 'unspecified']
            eligible = len(named) == 1 and named[0] in self.TRACE_SYSTEM_SHEETS
            for c in page['connections']:
                sid = self._trace_record(page, c)
                category = c['category'] if eligible else 'ambiguous_unspecified_sheet'
                self.trace_register.append({'tag_a': c['a'], 'tag_b': c['b'], 'category': category, 'flags': c['flags'], 'direction': c['direction'],
                                            'loop_labels': c['loop_labels'], 'path_length': c['path_length'], 'sheet_systems': page['systems'],
                                            'page': page['page'], 'sheet_number': page.get('sheet_number'), 'source_id': sid, 'refs': c['refs'],
                                            'graph_candidate': eligible})
                if not eligible:
                    continue
                ea, eb = entity(c['a'], sid), entity(c['b'], sid)
                if ea != eb:
                    pairs[tuple(sorted((ea, eb)))].append((sid, c, page))
                system_cls = self.TRACE_SYSTEM_SHEETS[named[0]]
                if system_cls and systems.get(system_cls):
                    for e in (ea, eb):
                        members[(systems[system_cls], e)].append((sid, page))
        for tag, d in sorted(diagram_only.items()):
            d['assertion'] = self.assertion('equipment/' + tag, 'type', d['class'], sorted(set(d['source_ids']))[:3],
                                            'Equipment tag read (OCR) on a system diagram whose drawn line work connects it to other equipment '
                                            '(vector trace); no BAS container or schedule row carries this tag.', 'drawing_vector_trace', 'needs_review')
            self.diagram_equipment.append(d)
        for (ea, eb), hits in sorted(pairs.items()):
            sids = sorted({h[0] for h in hits})
            clean = any(h[1]['category'] == 'documented_drawn_connection' for h in hits)
            key = 'connection:%s|%s' % (ea, eb)
            human = decisions.get(key)
            status = self._decided_status(human, team_ok=clean)        # clean = at least one unflagged documented trace
            if human:
                sids = sids + [self._decision_record(human)]
            flags = sorted({f for h in hits for f in h[1]['flags']})
            a = self.make_assertion('equipment/' + ea, 'connectedTo', 'equipment/' + eb, sids,
                                    'Drawn line work connects %s and %s on %d system-diagram sheet(s) without passing another tagged equipment '
                                    '(local vector trace; tags by OCR)%s. Direction: %s.%s' % (
                                        ea, eb, len({(h[2]['pdf_sha256'], h[2]['page']) for h in hits}), '; flags ' + ', '.join(flags) if flags else '',
                                        ', '.join(sorted({h[1]['direction'] for h in hits})),
                                        self._decision_note(human)),
                                    'human_review' if status != 'needs_review' else 'drawing_vector_trace', 'needs_review')
            a = a.model_copy(update={'status': status, 'validation_results': a.validation_results + [
                'trace:' + ('documented_drawn_connection' if clean else 'ambiguous_drawn_connection')] + (
                ['status set by %s decision' % human.get('authority')] if status != 'needs_review' else [])})
            self.connections.append(a)
        for (system, e), hits in sorted(members.items()):
            key = 'membership:%s|%s' % (system, e)
            human = decisions.get(key)
            sids = sorted({h[0] for h in hits})[:5] + ([self._decision_record(human)] if human else [])
            reg = {r['source_id']: r for r in self.trace_register}
            status = self._decided_status(human, team_ok=any(reg.get(h[0], {}).get('category') == 'documented_drawn_connection' for h in hits))
            m = self.make_assertion(
                'equipment/' + system, 'includes', 'equipment/' + e, sids,
                '%s is drawn connected into the piping/duct network of the %s diagram (vector trace, %d traced connection(s)); tags by OCR.%s'
                % (e, system, len(hits), self._decision_note(human)),
                'human_review' if status != 'needs_review' else 'drawing_vector_trace', 'needs_review')
            self.traced_memberships.append(m.model_copy(update={'status': status, 'validation_results': m.validation_results + (
                ['status set by %s decision' % human.get('authority')] if status != 'needs_review' else [])}))
        if self.connections or self.traced_memberships:
            self.review('traced-hydronic-topology', 'Confirm or reject %d drawn equipment connections and %d system memberships traced on the '
                        'system flow diagrams (each cites PDF, page, sheet and the path region; crops provided). These are the first '
                        'candidates eligible for human verification as physical edges.' % (len(self.connections), len(self.traced_memberships)),
                        [], [], 'VECTOR_TRACE_UNCONFIRMED', 'first physical edges', 'high')

    def _decision_record(self, d):
        """A human review decision as a citable source record. For point classes and equipment it carries exactly the fields
        the evidence gate checks, so a confirmed class passes the existing gate without any special case."""
        sid = 'human_review:%s' % d['decision_id']
        if sid in self.by_source:
            return sid
        fields = {'item_key': d['item_key'], 'decision': d['decision'], 'reviewer': d['reviewer'], 'recorded_on': d['recorded_on']}
        kind = 'unknown'
        if d.get('point_id') and d.get('class'):
            kind, fields = 'points', dict(fields, point_id=d['point_id'], point_class=d['class'])
        elif d.get('equipment_id') and d.get('class'):
            kind, fields = 'equipment', dict(fields, equipment_id=d['equipment_id'], equipment_type=d['class'])
        elif d.get('from_equipment') and d.get('to_equipment'):
            kind, fields = 'connections', dict(fields, from_equipment=d['from_equipment'], to_equipment=d['to_equipment'], predicate=d.get('predicate', 'feeds'))
        record = SourceRecord(source_id=sid, source_record_uid=uid('humanrev', self.scope, sid), source_file='review_decisions.jsonl',
                              source_type='human_review', source_hash=hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest(),
                              source_location='human review decision %s (group %s) by %s on %s' % (d['decision_id'], d.get('group_id'), d['reviewer'], d['recorded_on']),
                              table_kind=kind, raw_value=list(map(str, fields.values())), raw_record={k: str(v) for k, v in fields.items()},
                              normalized_fields={k: str(v) for k, v in fields.items()}, parse_status='ok', errors=[])
        self.extra_records.append(record)
        self.by_source[sid] = record
        return sid

    def _apply_human_decisions(self):
        """Point-class and OCR-reading decisions. One decision record per affected row/equipment (each keeps its own source)."""
        from .vocabulary import POINTS
        by_point = {'point/' + f['point_id']: sid for sid, f in self.facts.items()}
        self.annotations = [d for d in self.human_decisions if d.get('authority') not in ('qualified_engineering', 'team_documentary_check')]
        self.documented_feeds = []
        for d in self.human_decisions:
            qualified = d.get('authority') == 'qualified_engineering'
            team = d.get('authority') == 'team_documentary_check'
            if d['item_kind'] == 'documented_connection':
                if qualified:                     # flow direction is engineering interpretation: qualified only
                    rec = self._decision_record(d)
                    a = self.assertion('equipment/' + d['from_equipment'], 'feeds', 'equipment/' + d['to_equipment'], [rec],
                                       'Qualified reviewer %s (%s, %s) read %s>%s on the %s drawings on %s: %s' % (
                                           d['reviewer'], d.get('qualification'), d.get('organization'), d['from_equipment'], d['to_equipment'],
                                           d.get('system'), d.get('review_date'), d.get('rationale')), 'human_review', 'verified',
                                       medium={'HWS': 'hot_water', 'CHWS': 'chilled_water'}.get(d.get('system')))
                    self.documented_feeds.append(a)
                continue
            if team and d['item_kind'] in ('point_class', 'equipment_reading') and d['decision'] in ('set_class', 'confirm'):
                rec = self._decision_record(d)
                subject = ('point/' + d['point_id']) if d['item_kind'] == 'point_class' else ('equipment/' + d['equipment_id'])
                row = by_point.get(subject)
                if d.get('class') and (d['item_kind'] == 'equipment_reading' or d['class'] in POINTS):
                    self.assertion(subject, 'type', d['class'], [rec] + ([row] if row else []),
                                   'Team documentary check by %s (%s) on %s: %s. Not an engineering verification.' % (
                                       d['reviewer'], d.get('team_role'), d['recorded_on'], d.get('rationale')), 'human_review', 'supported_inference')
                continue
            if team and d['item_kind'] == 'point_class' and d['decision'] == 'reject':
                qualified = True                  # rejecting a withheld/contradicted model class is within a team check
            if d['item_kind'] == 'point_class' and d['decision'] in ('set_class', 'confirm') and d.get('class') in POINTS and d.get('point_id'):
                subject = 'point/' + d['point_id']
                row = by_point.get(subject)
                if not qualified:                 # an annotation stays a reviewable suggestion
                    if row:
                        self.assertion(subject, 'type', d['class'], [row], 'Reviewer annotation by %s on %s suggests %s (not an engineering '
                                       'verification).' % (d['reviewer'], d['recorded_on'], d['class']), 'human_annotation', 'needs_review')
                    continue
                rec = self._decision_record(d)
                self.assertion(subject, 'type', d['class'], [rec] + ([row] if row else []),
                               'Qualified reviewer %s (%s, %s) set class %s on %s (decision %s, group %s): %s' % (
                                   d['reviewer'], d.get('qualification'), d.get('organization'), d['class'], d.get('review_date'), d['decision_id'],
                                   d.get('group_id'), d.get('rationale')), 'human_review', 'verified')
            elif not qualified:
                continue
            elif d['item_kind'] == 'point_class' and d['decision'] == 'reject' and d.get('point_id') and d.get('class'):
                subject = 'point/' + d['point_id']
                for i, a in enumerate(self.assertions):
                    if a.subject_id == subject and a.predicate == 'type' and a.object_id == d['class'] and a.inference_method.startswith('llm'):
                        self.assertions[i] = a.model_copy(update={'status': 'rejected', 'explanation': a.explanation + ' Rejected by human reviewer %s.' % d['reviewer']})
            elif d['item_kind'] == 'equipment_reading' and d['decision'] == 'confirm' and d.get('equipment_id') and d.get('class'):
                rec = self._decision_record(d)
                self.assertion('equipment/' + d['equipment_id'], 'type', d['class'], [rec],
                               'Human reviewer %s confirmed the drawing schedule reading for %s (decision %s).' % (d['reviewer'], d['equipment_id'], d['decision_id']),
                               'human_review', 'verified')

    def topology_worksheet(self):
        """Prioritized physical-topology questions with their evidence tier, BAS evidence, drawing references and missing evidence.
        A reading aid for reviewers; decisions are still recorded in the review service."""
        rows = []

        def add(priority, topic, subject, question, tier, bas_ids, refs, missing):
            rows.append({'question_id': 'TOPO-%03d' % (len(rows) + 1), 'priority': priority, 'topic': topic, 'subject': subject,
                         'question': question, 'current_evidence_tier': tier, 'bas_source_count': len(bas_ids),
                         'bas_source_examples': list(bas_ids)[:5], 'drawing_refs': refs[:6], 'missing_evidence': missing,
                         'reviewer_decision': '', 'reviewer': '', 'decision_date': '', 'notes': ''})
        register = {r['source_id']: r for r in self.trace_register}
        for a in self.connections:
            if a.status == 'needs_review':
                refs = [ref for sid in a.supporting_source_ids if sid in register for ref in register[sid]['refs']]
                add('high', 'traced_connection', '%s~%s' % (a.subject_id.split('/', 1)[1], a.object_id.split('/', 1)[1]),
                    'Does drawn piping/duct connect %s and %s with no other equipment between them?' % (a.subject_id.split('/', 1)[1], a.object_id.split('/', 1)[1]),
                    'drawing_vector_trace', [], refs, 'human confirmation on the crop (path region and both tags)')
        hyp = defaultdict(list)
        for h in self.hypotheses:
            hyp[h['equipment']].append(h)
        for d in getattr(self, 'drawing_equipment', []):
            if d['class'] in ('Pump', 'Hot_Water_Pump', 'Chilled_Water_Pump', 'Heat_Exchanger'):
                add('high', 'hydronic_composition', d['tag'], 'Is %s a real %s, and which system does it serve (schedule text suggests %s)?'
                    % (d['tag'], d['class'], ', '.join(d['systems']) or 'none'), 'drawing_schedule_ocr', d['bas_point_examples'],
                    d['schedule_refs'], 'pump/heat-exchanger schedule and HW/CHW flow diagram confirmation')
        for tag, e in sorted(self.equipment.items()):
            if e['class'] in ('Hot_Water_System', 'Chilled_Water_System'):
                members = [d['tag'] for d in getattr(self, 'drawing_equipment', []) if tag in d['candidate_member_of']]
                add('high', 'hydronic_composition', tag, 'Which equipment makes up %s and what is its primary source? Candidate members from '
                    'schedules: %s.' % (tag, ', '.join(members) or 'none'), 'bas_only' if not members else 'drawing_schedule_ocr',
                    sorted(e['points'])[:5], [r for d in getattr(self, 'drawing_equipment', []) if tag in d['candidate_member_of'] for r in d['schedule_refs'][:1]],
                    'HW/CHW flow diagram or riser; utility connection')
        for tag, e in sorted(self.equipment.items()):
            if e['class'] != 'AHU':
                continue
            for h in hyp.get(tag, []):
                add('high' if h['hypothesis'] in ('chilled_water_coil', 'hot_water_coil') else 'medium', 'ahu_internals', tag,
                    '%s: %s?' % (tag, h['hypothesis'].replace('_', ' ')), h.get('drawing_verdict', 'unresolved'), h['cue_source_ids'],
                    h.get('drawing_refs', []), h['missing_evidence'])
            served = [a.object_id.split('/', 1)[1] for a in self.feeds if a.subject_id == 'equipment/' + tag]
            add('high', 'air_serving', tag, 'Does %s supply the %d terminal units grouped under its BAS folder?' % (tag, len(served)),
                'bas_folder_grouping' if served else 'unresolved', [s for a in self.feeds if a.subject_id == 'equipment/' + tag for s in a.supporting_source_ids][:5],
                e.get('schedule_refs', []), 'air-flow riser / duct plans / TAB report naming the AHU per terminal')
        for cls, kind in (('Terminal_Unit', 'hot_water_coil'), ('Fan_Coil_Unit', 'chilled_water_coil'), ('Fan_Coil_Unit', 'hot_water_coil'),
                          ('Terminal_Unit', 'perimeter_radiation')):
            hs = [h for h in self.hypotheses if h['equipment_class'] == cls and h['hypothesis'] == kind]
            if hs:
                verdicts = Counter(h.get('drawing_verdict', 'unresolved') for h in hs)
                add('medium', 'terminal_hydronics', cls, '%s on %d %s candidates? Drawing verdicts: %s.' % (kind.replace('_', ' '), len(hs), cls,
                    ', '.join('%s %d' % kv for kv in verdicts.most_common())), 'mixed', [s for h in hs for s in h['cue_source_ids'][:1]],
                    [r for h in hs for r in h.get('drawing_refs', [])[:1]], HYPOTHESES[kind][3])
        for a in self.includes:
            add('medium', 'exhaust_composition', a.subject_id.split('/', 1)[1], 'Is %s part of %s?' % (a.object_id.split('/', 1)[1], a.subject_id.split('/', 1)[1]),
                'bas_folder_grouping', a.supporting_source_ids, [], 'lab exhaust riser diagram')
        return rows

    def equipment_identity(self):
        """One row per equipment identity across BAS candidates, drawing schedules and diagram mentions."""
        out = []
        assess = self.drawing_assessment or {'equipment': {}}
        for tag, e in sorted(self.equipment.items()):
            info = assess['equipment'].get(tag, {})
            cats = sorted({r['category'] for r in info.get('schedule_rows', [])} - {'unknown'})
            compatible = lambda c: c == e['class'] or (c == 'Fan' and e['class'].endswith('Fan')) or (c == 'Pump' and e['class'].endswith('Pump'))
            agree = 'unknown' if not cats else 'agrees' if any(compatible(c) for c in cats) else 'differs'
            out.append({'identity': tag, 'bas_candidate': True, 'bas_class': e['class'], 'schedule_rows': len(info.get('schedule_rows', [])),
                        'schedule_categories': cats, 'class_agreement': agree, 'diagram_mentions': len(info.get('diagram_mentions', [])),
                        'alias_match': info.get('alias_match', False),
                        'resolution': 'bas_and_schedule' if info.get('schedule_rows') else 'bas_and_diagram' if info.get('diagram_mentions') else 'bas_only'})
        for d in getattr(self, 'drawing_equipment', []):
            out.append({'identity': d['tag'], 'bas_candidate': False, 'bas_class': None, 'schedule_rows': len(d['schedule_refs']),
                        'schedule_categories': [c for c in d['schedule_categories'] if c != 'unknown'], 'class_agreement': 'not_applicable',
                        'diagram_mentions': len(d['diagram_systems']), 'alias_match': False,
                        'resolution': 'schedule_only_with_bas_name_match' if d['bas_point_name_matches'] else 'schedule_only'})
        return out

    def physical_relationships(self):
        """Every candidate physical relationship with an evidence category. Connectivity and flow direction are never inferred
        from names, shared sheets or BAS folders: those give at most a provisional category."""
        rows = []
        for a in self.feeds:
            f = next((x for x in (self.drawing_assessment or {}).get('feeds', []) if x['assertion_id'] == a.assertion_id), None)
            verdict = (f or {}).get('verdict', 'unresolved')
            rows.append({'relationship': 'feeds(air)', 'subject': a.subject_id, 'object': a.object_id,
                         'category': 'contradicted' if a.status == 'contradicted' else 'supported_documentary_ocr_unconfirmed' if verdict == 'documentary_support'
                         else 'provisional_bas_grouping', 'basis': a.inference_method, 'connectivity_established': False})
        for a in self.includes:
            rows.append({'relationship': 'system_membership', 'subject': a.subject_id, 'object': a.object_id, 'category': 'provisional_bas_grouping',
                         'basis': a.inference_method, 'connectivity_established': False})
        for d in getattr(self, 'drawing_equipment', []):
            for m in d['candidate_member_of']:
                rows.append({'relationship': 'system_membership', 'subject': 'equipment/' + m, 'object': 'equipment/' + d['tag'],
                             'category': 'supported_documentary_ocr_unconfirmed', 'basis': d['membership_basis'], 'connectivity_established': False})
        for a in self.connections:
            trace = next((v.split(':', 1)[1] for v in a.validation_results if v.startswith('trace:')), 'ambiguous_drawn_connection')
            rows.append({'relationship': 'connectedTo(drawn)', 'subject': a.subject_id, 'object': a.object_id,
                         'category': 'verified_human' if a.status == 'verified' else 'rejected' if a.status == 'rejected' else
                         'supported_team_documentary_check' if a.status == 'supported_inference' else
                         ('documented_drawn_connection_unconfirmed' if trace == 'documented_drawn_connection' else 'ambiguous_drawn_connection'),
                         'basis': a.inference_method, 'connectivity_established': a.status == 'verified'})
        for a in self.traced_memberships:
            rows.append({'relationship': 'system_membership', 'subject': a.subject_id, 'object': a.object_id,
                         'category': 'verified_human' if a.status == 'verified' else 'rejected' if a.status == 'rejected' else
                         'supported_team_documentary_check' if a.status == 'supported_inference' else 'documented_drawn_membership_unconfirmed',
                         'basis': a.inference_method, 'connectivity_established': a.status == 'verified'})
        for a in getattr(self, 'documented_feeds', []):
            rows.append({'relationship': 'feeds(%s)' % (a.physical_medium or 'documented'), 'subject': a.subject_id, 'object': a.object_id,
                         'category': 'verified_human', 'basis': 'human_review (qualified reading of the flow diagram)', 'connectivity_established': True})
        for r in self.trace_register:
            if not r['graph_candidate']:
                rows.append({'relationship': 'connectedTo(drawn)', 'subject': 'tag/' + r['tag_a'], 'object': 'tag/' + r['tag_b'],
                             'category': 'ambiguous_unspecified_sheet', 'basis': 'drawing_vector_trace', 'connectivity_established': False})
        for h in self.hypotheses:
            v = h.get('drawing_verdict', 'unresolved')
            rows.append({'relationship': h['hypothesis'], 'subject': 'equipment/' + h['equipment'], 'object': h['candidate_component_class'],
                         'category': 'supported_documentary_ocr_unconfirmed' if v == 'documentary_support' else 'unknown_diagram_context_only'
                         if v == 'diagram_mention' else 'unknown', 'basis': 'point names' + (' + ' + v if v != 'unresolved' else ''),
                         'connectivity_established': False})
        return rows

    def missing_evidence_plan(self):
        """The smallest set of drawings / confirmations that would turn provisional or unknown relationships into documented ones."""
        rel = Counter((r['relationship'], r['category']) for r in self.physical_relationships())
        diag = Counter(x for d in (self.drawing_evidence or {}).get('diagrams', []) for x in d['systems'])
        identities = Counter(r['resolution'] for r in self.equipment_identity())
        ocr_confirmations = identities.get('bas_and_schedule', 0) + identities.get('schedule_only', 0) + identities.get('schedule_only_with_bas_name_match', 0)
        return [
            {'need': 'Human confirmation of OCR schedule readings (crops provided)', 'unlocks': '%d equipment identities and %d schedule-supported relationships'
             % (ocr_confirmations, sum(v for (r, c), v in rel.items() if c == 'supported_documentary_ocr_unconfirmed')), 'effort': 'about 1 hour', 'kind': 'confirmation'},
            {'need': 'From the sponsor, any ONE of: (a) test-and-balance (TAB) report listing each terminal unit with its AHU; (b) terminal-unit / VAV '
             'schedule with an "AHU served" or "system" column; (c) mechanical duct floor plans or an airflow riser for AHUs %s. The supplied '
             'drawings contain no traceable AHU-to-terminal duct path (%d air-handling diagram sheets traced: 0 AHU-terminal connections; '
             'terminal schedules carry room/area, not AHU, columns).' % (', '.join(sorted(t for t, e in self.equipment.items() if e['class'] == 'AHU')),
                                                                          sum(1 for p in ((self.drawing_evidence or {}).get('vector_topology') or {}).get('pages', []) if 'air_handling' in p['systems'])),
             'unlocks': '%d AHU->terminal feeds candidates and the 88 lab valves' % sum(v for (r, c), v in rel.items() if r == 'feeds(air)'),
             'effort': 'one document', 'kind': 'document'},
            {'need': 'Human confirmation of %d drawn connections and %d system memberships traced from the HW/CHW/air/exhaust diagrams (crops provided)'
             % (sum(1 for a in self.connections if a.status == 'needs_review'), sum(1 for a in self.traced_memberships if a.status == 'needs_review')),
             'unlocks': 'the first verified physical edges and system memberships', 'effort': 'minutes per sheet', 'kind': 'confirmation'},
            {'need': 'Human tracing of plant-side piping on the hot-water and chilled-water flow diagrams (%d HW and %d CHW sheets): heat exchangers, '
             'pumps and AHU coils are drawn but the automatic trace reaches no other tagged equipment from them, and no consistent drawn flow arrows '
             'were found' % (diag.get('HWS', 0), diag.get('CHWS', 0)),
             'unlocks': 'heat-exchanger / pump / coil sequence (thermodynamic chain) and flow direction', 'effort': '2-3 sheets', 'kind': 'confirmation'},
            {'need': 'Schedule confirmation of coil, fan, evaporative and heat-recovery sections (AHU, FCU and terminal schedules)',
             'unlocks': '%d coil/section hypotheses not yet schedule-supported'
             % sum(v for (r, c), v in rel.items() if r in ('chilled_water_coil', 'hot_water_coil', 'evaporative_cooling', 'heat_recovery', 'supply_fan', 'return_fan', 'economizer_dampers')
                   and c != 'supported_documentary_ocr_unconfirmed'), 'effort': 'one sheet', 'kind': 'confirmation'},
            {'need': 'Lab exhaust riser (hood valves -> exhaust fans)', 'unlocks': 'exhaust-side composition and fume-hood paths', 'effort': 'one sheet', 'kind': 'document'},
            {'need': 'Trend data (any interval)', 'unlocks': 'behavioural checks; still never topology by itself', 'effort': 'export', 'kind': 'data'},
        ]

    def contradicted_layer(self):
        return [a.model_dump() for a in self.assertions if a.status in ('contradicted', 'rejected')]

    def next_questions(self, n=10):
        order = {'high': 0, 'medium': 1, 'review': 2, 'low': 3}
        return [{'priority': r.priority, 'question': r.question_or_test, 'impact': r.estimated_impact}
                for r in sorted(self.reviews, key=lambda r: (order.get(r.priority, 9), -len(r.supporting_evidence_ids), r.review_id))[:n]]

    def equipment_table(self):
        return [{'tag': t, 'class': e['class'], 'alternatives': e['alternatives'], 'status': e['assertion'].status, 'basis': e['basis'],
                 'containers': len(e['containers']), 'stations': sorted(e['stations']), 'candidate_points': len(e['points']),
                 'drawing_mentions': e['drawing_mentions'],
                 'feeds_candidates': sorted(a.object_id for a in self.feeds if a.subject_id == 'equipment/' + t),
                 'fed_by_candidates': sorted(a.subject_id for a in self.feeds if a.object_id == 'equipment/' + t),
                 'includes_candidates': sorted(a.object_id for a in self.includes if a.subject_id == 'equipment/' + t),
                 'hypotheses': sorted(h['hypothesis'] for h in self.hypotheses if h['equipment'] == t),
                 'drawing_tier': e.get('drawing_tier', 'not_assessed'), 'schedule_refs': e.get('schedule_refs', []),
                 'diagram_refs': e.get('diagram_refs', [])}
                for t, e in sorted(self.equipment.items())]
