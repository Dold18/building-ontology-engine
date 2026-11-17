"""FR-03/04/08: drawing evidence from local OCR of authorized drawings (schedules and diagram sheets). Deterministic.

Every extraction keeps its drawing reference (PDF SHA-256, page, sheet number, page-normalized bounding box, OCR confidence,
method version). OCR output is an unreviewed machine reading: it can support or contradict a candidate, but it never makes
a claim `verified` or accepted. Spatial proximity on a diagram is recorded as a MENTION on that diagram, never as a
connection or flow direction (no line tracing is attempted).
"""
import re
from collections import Counter, defaultdict
from statistics import median

VERSION = 'drawing-evidence-v1'
FAMILIES = {'AHU': 'AHU', 'FCU': 'Fan_Coil_Unit', 'EF': 'Exhaust_Fan', 'SF': 'Supply_Fan', 'RF': 'Return_Fan', 'HWP': 'Hot_Water_Pump',
            'CHWP': 'Chilled_Water_Pump', 'PCHWP': 'Chilled_Water_Pump', 'P': 'Pump', 'HX': 'Heat_Exchanger', 'VAV': 'Terminal_Unit',
            'TB': 'Terminal_Unit', 'FTR': 'Radiator', 'UH': 'Unit_Heater', 'CUH': 'Unit_Heater', 'ERU': 'Equipment', 'HRU': 'Equipment',
            'HRC': 'Coil', 'HC': 'Heating_Coil', 'CC': 'Cooling_Coil'}
ALIASES = {'TB': 'VAV'}   # BAS 'TB' (terminal box) vs schedule 'VAV': same family, recorded as an alias match
TAG = re.compile(r'(?<![A-Z0-9])(PCHWP|CHWP|HWP|AHU|FCU|VAV|FTR|CUH|ERU|HRU|HRC|EF|SF|RF|HX|TB|UH|HC|CC|P)[\s-]?(\d{1,3}[A-Z]?(?:[-_]\d{1,3}[A-Z]?)?)(?![A-Z0-9])')
SCHEDULE_CATEGORY = [('AIR HANDLING', 'AHU'), ('AHU', 'AHU'), ('FAN COIL', 'Fan_Coil_Unit'), ('EXHAUST FAN', 'Exhaust_Fan'),
                     ('PUMP', 'Pump'), ('HEAT EXCHANGER', 'Heat_Exchanger'), ('VAV', 'Terminal_Unit'), ('TERMINAL', 'Terminal_Unit'),
                     ('AIR VALVE', 'Terminal_Unit'), ('VENTURI', 'Terminal_Unit'), ('FIN TUBE', 'Radiator'), ('RADIATION', 'Radiator'),
                     ('UNIT HEATER', 'Unit_Heater'), ('COIL', 'Coil'), ('FAN', 'Fan'), ('BOILER', 'Boiler'), ('CHILLER', 'Chiller'),
                     ('TANK', 'Tank'), ('SEPARATOR', 'Equipment'), ('HEAT RECOVERY', 'Equipment')]
HEADER_WORDS = ('TAG', 'MARK', 'UNIT', 'NO.', 'SERV', 'LOCATION', 'CFM', 'GPM', 'MBH', 'HP', 'MODEL', 'MANUF', 'TYPE', 'AREA', 'ROOM',
                'SIZE', 'VOLT', 'RPM', 'EAT', 'LAT', 'EWT', 'LWT', 'REMARKS', 'NOTES', 'SYSTEM', 'AHU', 'BTU')
COIL_HEADERS = {'chilled_water_coil': ('COOLING COIL', 'CHW', 'CHILLED'), 'hot_water_coil': ('HEATING COIL', 'HW ', 'HOT WATER', 'REHEAT', 'PREHEAT'),
                'evaporative_cooling': ('EVAP',), 'heat_recovery': ('HEAT RECOVERY', 'ENERGY RECOVERY', 'RUNAROUND', 'GLYCOL'),
                'supply_fan': ('SUPPLY FAN',), 'return_fan': ('RETURN FAN',)}
DIAGRAM_WORDS = ('DIAGRAM', 'RISER', 'SCHEMATIC', 'FLOW')
DIAGRAM_SYSTEMS = [('HEATING HOT WATER', 'HWS'), ('HOT WATER', 'HWS'), ('HEATING WATER', 'HWS'), ('CHILLED WATER', 'CHWS'),
                   ('STEAM', 'steam'), ('EXHAUST', 'exhaust'), ('HEAT RECOVERY', 'heat_recovery'), ('GLYCOL', 'heat_recovery'),
                   ('AIR HANDLING', 'air_handling'), ('AHU', 'air_handling'), ('CONDENSATE', 'steam')]


def norm_tag(family, number):
    """Canonical comparison key: family alias plus integer parts ('FCU-001' == 'FCU-1', 'TB-2-105' ~ 'VAV-2-105')."""
    family = ALIASES.get(family.upper(), family.upper())
    parts = re.split(r'[-_]', number.upper())
    out = []
    for p in parts:
        m = re.fullmatch(r'0*(\d+)([A-Z]?)', p)
        out.append((str(int(m.group(1))) + m.group(2)) if m else p)
    return family + '-' + '-'.join(out)


def tags_in(text):
    return [(m.group(1), norm_tag(m.group(1), m.group(2))) for m in TAG.finditer(text.upper())]


def dedupe(observations):
    """Remove duplicates produced by overlapping OCR tiles (same text, nearby centre)."""
    kept, seen = [], defaultdict(list)
    for o in sorted(observations, key=lambda o: -o['conf']):
        cx, cy = o['x'] + o['w'] / 2, o['y'] + o['h'] / 2
        key = o['text'].strip().upper()
        if any(abs(cx - x) < max(o['w'], 0.004) * 0.6 and abs(cy - y) < max(o['h'], 0.002) * 0.8 for x, y in seen[key]):
            continue
        seen[key].append((cx, cy))
        kept.append(o)
    return kept


def _category(title):
    t = title.upper()
    return next((cat for word, cat in SCHEDULE_CATEGORY if word in t), 'unknown')


def _rows(observations, tol):
    rows = []
    for o in sorted(observations, key=lambda o: -(o['y'] + o['h'] / 2)):
        cy = o['y'] + o['h'] / 2
        if rows and abs(rows[-1]['cy'] - cy) <= tol:
            rows[-1]['obs'].append(o)
        else:
            rows.append({'cy': cy, 'obs': [o]})
    return rows


def _ref(doc, page, box, conf, sheet):
    return {'pdf_file': doc.get('pdf_file'), 'pdf_sha256': doc['pdf_sha256'], 'page': page, 'sheet_number': sheet,
            'bbox': [round(v, 5) for v in box], 'ocr_confidence_min': round(conf, 3), 'method': doc.get('method', 'ocr') + '+' + VERSION}


def _box(obs):
    x0, y0 = min(o['x'] for o in obs), min(o['y'] for o in obs)
    x1, y1 = max(o['x'] + o['w'] for o in obs), max(o['y'] + o['h'] for o in obs)
    return [x0, y0, x1 - x0, y1 - y0]


def schedules_on_page(doc, page, sheet, max_rows=120):
    obs = dedupe(page['observations'])
    if not obs:
        return []
    h_med = median(o['h'] for o in obs)
    titles = [o for o in obs if 'SCHEDULE' in o['text'].upper() and len(o['text']) <= 70]
    out = []
    for title in sorted(titles, key=lambda o: (-o['y'], o['x'])):
        top = title['y']
        # window: below the title, starting near its left edge; width grows with the header row
        below = [o for o in obs if o['y'] + o['h'] <= top + 1e-6 and o['y'] > top - 0.6 and o['x'] + o['w'] > title['x'] - 0.01
                 and o['x'] < title['x'] + 0.45 and o is not title]
        rows = _rows(below, max(h_med * 0.6, 0.0015))
        header_idx = next((i for i, r in enumerate(rows[:6]) if sum(any(w in o['text'].upper() for w in HEADER_WORDS) for o in r['obs']) >= 2), None)
        if header_idx is None:
            continue
        header_rows, body = [rows[header_idx]], []
        for r in rows[header_idx + 1:]:
            if not body and not any(tags_in(o['text']) for o in r['obs']) and len(header_rows) < 3:
                header_rows.append(r)          # multi-line header
                continue
            if body and (body[-1]['cy'] - r['cy']) > max(0.02, 4 * h_med) or any('SCHEDULE' in o['text'].upper() for o in r['obs']):
                break
            body.append(r)
            if len(body) >= max_rows:
                break
        header_obs = [o for r in header_rows for o in r['obs']]
        x_min = min(o['x'] for o in header_obs)
        x_max = max(o['x'] + o['w'] for o in header_obs)
        columns = sorted({round(o['x'] + o['w'] / 2, 3) for o in header_rows[0]['obs'] if x_min - 0.005 <= o['x'] <= x_max})
        if len(columns) < 2:
            continue
        names = {c: [] for c in columns}
        for o in header_obs:
            c = min(columns, key=lambda c: abs(c - (o['x'] + o['w'] / 2)))
            names[c].append(o['text'].strip())
        header = [' '.join(names[c]).upper() for c in columns]
        rows_out = []
        for r in body:
            cells = defaultdict(list)
            inside = [o for o in r['obs'] if x_min - 0.01 <= o['x'] <= x_max + 0.01]
            if not inside:
                continue
            for o in sorted(inside, key=lambda o: o['x']):
                c = min(range(len(columns)), key=lambda i: abs(columns[i] - (o['x'] + o['w'] / 2)))
                cells[c].append(o['text'].strip())
            row = {header[i]: ' '.join(v) for i, v in cells.items()}
            row_tags = [t for o in inside for t in tags_in(o['text'])]
            rows_out.append({'cells': row, 'tags': [t[1] for t in row_tags], 'first_tag': row_tags[0][1] if row_tags else None,
                             'ref': _ref(doc, page['page'], _box(inside), min(o['conf'] for o in inside), sheet)})
        tagged = [r for r in rows_out if r['first_tag']]
        out.append({'schedule_id': '%s:p%d:%d' % (doc['pdf_sha256'][:12], page['page'], len(out)), 'category': _category(title['text']),
                    'title_systems': sorted({sys for word, sys in DIAGRAM_SYSTEMS if word in title['text'].upper()} - {'air_handling'}),
                    'title_ref': _ref(doc, page['page'], _box([title]), title['conf'], sheet), 'header': header,
                    'rows': rows_out, 'tagged_rows': len(tagged),
                    'serves_columns': [h for h in header if 'SERV' in h or 'AREA' in h],
                    'ahu_columns': [h for h in header if 'AHU' in h or 'SERVED BY' in h or 'SOURCE' in h],
                    'coil_features': sorted(k for k, words in COIL_HEADERS.items() if any(w in ' '.join(header) + ' ' for w in words))})
    return out


def diagram_on_page(doc, page, sheet):
    """Diagram/riser/schematic sheet: the system it names and the equipment tags written on it (mentions only)."""
    obs = dedupe(page['observations'])
    if not obs:
        return None
    h_med = median(o['h'] for o in obs)
    big = [o for o in obs if o['h'] >= 1.4 * h_med]
    titles = [o for o in big if any(w in o['text'].upper() for w in DIAGRAM_WORDS)]
    if not titles:
        return None
    systems = sorted({sys for o in titles for word, sys in DIAGRAM_SYSTEMS if word in o['text'].upper()})
    mentions = defaultdict(list)
    for o in obs:
        for fam, tag in tags_in(o['text']):
            if fam == 'P' and re.fullmatch(r'P-\d{3}', tag):
                continue                     # 'P-1xx' style strings are plumbing sheet numbers, not pumps
            mentions[tag].append(_ref(doc, page['page'], _box([o]), o['conf'], sheet))
    return {'diagram_id': '%s:p%d' % (doc['pdf_sha256'][:12], page['page']), 'systems': systems or ['unspecified'],
            'title_refs': [_ref(doc, page['page'], _box([o]), o['conf'], sheet) for o in titles[:5]],
            'tags': {t: refs[:5] for t, refs in sorted(mentions.items())}}


def extract_all(documents, page_index):
    schedules, diagrams = [], []
    for doc in documents:
        for page in doc['pages']:
            sheet = (page_index.get((doc['pdf_sha256'], page['page'])) or {}).get('sheet_number')
            schedules += schedules_on_page(doc, page, sheet)
            d = diagram_on_page(doc, page, sheet)
            if d:
                diagrams.append(d)
    by_tag = defaultdict(list)
    for s in schedules:
        for r in s['rows']:
            if r['first_tag']:
                by_tag[r['first_tag']].append({'schedule_id': s['schedule_id'], 'category': s['category'], 'title_systems': s.get('title_systems', []),
                                               'cells': r['cells'], 'ref': r['ref']})
    summary = {'version': VERSION, 'documents': len(documents), 'pages': sum(len(d['pages']) for d in documents),
               'observations': sum(len(p['observations']) for d in documents for p in d['pages']),
               'schedules_detected': len(schedules), 'schedules_by_category': dict(Counter(s['category'] for s in schedules)),
               'schedule_rows': sum(len(s['rows']) for s in schedules), 'schedule_rows_with_tag': sum(s['tagged_rows'] for s in schedules),
               'schedules_with_title_system': dict(Counter(x for s in schedules for x in s.get('title_systems', []))),
               'distinct_scheduled_tags': len(by_tag), 'scheduled_tag_families': dict(Counter(t.split('-')[0] for t in by_tag)),
               'diagram_sheets': len(diagrams), 'diagram_systems': dict(Counter(sys for d in diagrams for sys in d['systems'])),
               'diagram_tag_mentions': sum(len(d['tags']) for d in diagrams), 'transmitted_anywhere': False}
    return {'version': VERSION, 'summary': summary, 'schedules': schedules, 'diagrams': diagrams, 'scheduled_tags': dict(by_tag)}


# ---- matching to BAS-derived candidates ------------------------------------------------------------------

SYSTEM_DIAGRAMS = {'Hot_Water_System': 'HWS', 'Chilled_Water_System': 'CHWS', 'Air_System': 'exhaust'}
# hypothesis on a system candidate -> (tag families that would be its members, required diagram system or None = the system's own)
SYSTEM_MEMBER_FAMILIES = {'heat_exchanger': (('HX',), None), 'hot_water_pumps': (('HWP', 'P'), None),
                          'chilled_water_pumps': (('CHWP', 'PCHWP', 'P'), None), 'steam_source': ((), 'steam')}


def bas_key(tag):
    """Comparison key for a Phase 3A BAS equipment tag ('AHU-2', 'FCU-001', 'TB-2-105', 'EF-1A')."""
    found = tags_in(tag.replace('_', '-'))
    return found[0][1] if found else None


def assess(bg, evidence):
    """Which candidate equipment and relationships gain documentary (drawing) support, are contradicted, or stay unresolved.

    Returns a dict of per-candidate findings. Nothing here changes a status to accepted; contradictions are reported so the
    graph builder can mark the candidate `contradicted`.
    """
    scheduled = evidence.get('scheduled_tags', {})
    diagram_tags = defaultdict(list)
    for d in evidence.get('diagrams', []):
        for tag, refs in d['tags'].items():
            diagram_tags[tag].append({'diagram_id': d['diagram_id'], 'systems': d['systems'], 'ref': refs[0]})
    equipment = {}
    for tag, e in sorted(bg.equipment.items()):
        key = bas_key(tag)
        rows = scheduled.get(key, []) if key else []
        mentions = diagram_tags.get(key, []) if key else []
        equipment[tag] = {'key': key, 'schedule_rows': rows, 'diagram_mentions': mentions,
                          'tier': 'schedule_documented' if rows else 'diagram_mentioned' if mentions else 'bas_only',
                          'alias_match': bool(key and tag.split('-')[0] in ALIASES)}
    feeds = []
    for a in bg.feeds:
        ahu, tu = a.subject_id.split('/', 1)[1], a.object_id.split('/', 1)[1]
        rows = equipment.get(tu, {}).get('schedule_rows', [])
        stated = sorted({k for r in rows for col, v in r['cells'].items() if 'AHU' in col or 'SERVED BY' in col or 'SOURCE' in col
                         for _, k in tags_in(v) if k.startswith('AHU-')})
        verdict = 'unresolved' if not stated else 'documentary_support' if bas_key(ahu) in stated else 'contradicted'
        feeds.append({'assertion_id': a.assertion_id, 'ahu': ahu, 'terminal': tu, 'verdict': verdict, 'schedule_states': stated,
                      'refs': [r['ref'] for r in rows][:3]})
    hypotheses = []
    for h in bg.hypotheses:
        info = equipment.get(h['equipment'], {})
        support = [r for r in info.get('schedule_rows', [])
                   if any(any(w in col for w in COIL_HEADERS.get(h['hypothesis'], ())) and str(v).strip() not in ('', '-', '--', 'N/A')
                          for col, v in r['cells'].items())]
        on_system_diagram = [m for m in info.get('diagram_mentions', [])
                             if (h['hypothesis'] in ('hot_water_coil', 'heat_exchanger', 'hot_water_pumps', 'perimeter_radiation') and 'HWS' in m['systems'])
                             or (h['hypothesis'] in ('chilled_water_coil', 'chilled_water_pumps') and 'CHWS' in m['systems'])
                             or (h['hypothesis'] == 'heat_recovery' and 'heat_recovery' in m['systems'])]
        system = SYSTEM_DIAGRAMS.get(bg.equipment.get(h['equipment'], {}).get('class'))
        if system and h['hypothesis'] in SYSTEM_MEMBER_FAMILIES:
            fams, want = SYSTEM_MEMBER_FAMILIES[h['hypothesis']]
            on_system_diagram += [{'ref': refs[0], 'systems': d['systems']} for d in evidence.get('diagrams', [])
                                  if (want or system) in d['systems'] for tag, refs in d['tags'].items() if tag.split('-')[0] in fams]
            if h['hypothesis'] == 'steam_source':
                on_system_diagram += [{'ref': d['title_refs'][0], 'systems': d['systems']} for d in evidence.get('diagrams', []) if 'steam' in d['systems']]
        verdict = 'documentary_support' if support else 'diagram_mention' if on_system_diagram else 'unresolved'
        hypotheses.append({'hypothesis_id': h['hypothesis_id'], 'equipment': h['equipment'], 'hypothesis': h['hypothesis'], 'verdict': verdict,
                           'refs': [r['ref'] for r in support][:3] + [m['ref'] for m in on_system_diagram][:3]})
    bas_keys = {v['key'] for v in equipment.values() if v['key']}
    drawing_only = sorted(t for t in scheduled if t not in bas_keys)
    return {'version': VERSION, 'equipment': equipment, 'feeds': feeds, 'hypotheses': hypotheses, 'drawing_only_scheduled_tags': drawing_only}
