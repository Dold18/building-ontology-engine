"""Offline drawing preparation: page index, candidate mechanical sheets, tag mentions, review tables (FR-03/04/08).

Inputs are LOCAL extraction outputs (PDF text layer and/or on-device OCR). Nothing here reads a PDF or any network.
Everything extracted is a CANDIDATE with exact pdf/page/region provenance and status `needs_review`. Co-occurrence of
equipment tags on a sheet is never a connection. Promotion needs a named human reviewer.
"""
import csv
import re
from collections import Counter
from ..csv_safety import encode_cell

METHOD_VERSION = 'drawing-prep-v1'
SHEET_RE = re.compile(r'^([A-Z]{1,3})[- ]?(\d{2,4}[A-Z]?(?:\.\d+)?)$')
MECH_PREFIXES = {'M', 'MD', 'MH', 'MP', 'HV'}
MECH_KEYWORDS = ('HVAC', 'MECHANICAL', 'PIPING', 'CHILLED WATER', 'HEATING WATER', 'HOT WATER', 'CONDENSER WATER', 'SCHEMATIC',
                 'AIR HANDLING', 'AHU', 'VAV', 'BOILER', 'CHILLER', 'HEAT EXCHANGER', 'PUMP SCHEDULE', 'EQUIPMENT SCHEDULE',
                 'CONTROL', 'DDC', 'FAN COIL', 'DUCT', 'STEAM')
VIEW_KEYWORDS = ('PLAN', 'SCHEDULE', 'SCHEMATIC', 'DETAIL', 'SECTION', 'DIAGRAM', 'RISER', 'LEGEND', 'NOTES', 'POINTS', 'ELEVATION')
TAG_RE = re.compile(r'\b(AHU|RTU|MAU|DOAS|VAV|FCU|CUH|UH|EF|SF|RF|HX|CHWP|HWP|CWP|CT|CH|B|P)[-_ ]?(\d{1,3}[A-Z]?)\b')
REL_RE = re.compile(r'\b(AHU|RTU|MAU|VAV|FCU|CUH|EF|HX|CHWP|HWP|CWP|CT|CH|B|P)[-_ ]?\d{1,3}[A-Z]?\b[^.;]{0,40}?\b(SERVES|FEEDS|SUPPLIES)\b[^.;]{0,40}?'
                    r'\b(AHU|RTU|MAU|VAV|FCU|CUH|EF|HX|CHWP|HWP|CWP|CT|CH|B|P)[-_ ]?\d{1,3}[A-Z]?\b')
ALLOWED_STATUS = {'verified', 'supported_inference', 'needs_review', 'contradicted', 'rejected'}
ALLOWED_PREDICATES = {'feeds', 'hasPart', 'hasPoint', 'isLocatedIn', 'mentions'}
ALLOWED_EVIDENCE = {'text_layer', 'ocr', 'ocr_text_pattern', 'human_entry', 'human_confirmed', 'model_candidate'}
COLUMNS = ['candidate_id', 'kind', 'pdf_file', 'pdf_sha256', 'page', 'sheet_number', 'region_x', 'region_y', 'region_w', 'region_h',
           'subject_text', 'predicate', 'object_text', 'medium', 'direction', 'evidence_kind', 'extraction_method',
           'ocr_confidence_rank', 'status', 'review_required', 'reviewer', 'review_decision', 'review_notes']


def normalize_sheet_id(text):
    t = re.sub(r'\s+', '', (text or '').strip().upper())
    m = SHEET_RE.match(re.sub(r'(?<=[A-Z])-(?=\d)', '-', t))
    return t if m else None


def sheet_prefix(sheet_id):
    return re.match(r'[A-Z]+', sheet_id).group(0)


def titleblock_sheet(observations, region=(0.75, 0.0, 0.25, 0.22)):
    """Sheet-number candidate from the lower-right title block; highest OCR rank, then lowest position."""
    x0, y0, w, h = region
    found = [(o['conf'], -o['y'], normalize_sheet_id(o['text']), o) for o in observations
             if o['x'] >= x0 and o['y'] <= y0 + h and normalize_sheet_id(o['text']) and any(c.isdigit() for c in o['text'])]
    if not found:
        return None
    _, _, sheet, obs = max(found, key=lambda t: (t[0], t[1]))
    return {'sheet_number': sheet, 'region': [obs['x'], obs['y'], obs['w'], obs['h']], 'ocr_rank': obs['conf']}


def view_titles(observations, limit=4):
    out = []
    for o in sorted(observations, key=lambda o: -o['conf']):
        text = o['text'].strip()
        letters = [c for c in text if c.isalpha()]
        if len(text) >= 8 and letters and sum(c.isupper() for c in letters) / len(letters) > 0.8 and any(k in text.upper() for k in VIEW_KEYWORDS):
            out.append({'text': text, 'region': [o['x'], o['y'], o['w'], o['h']], 'ocr_rank': o['conf']})
        if len(out) >= limit:
            break
    return out


def mechanical_assessment(sheet_number, titles):
    """Documented evidence tally, NOT a probability."""
    reasons = []
    if sheet_number and sheet_prefix(sheet_number) in MECH_PREFIXES:
        reasons.append('sheet_prefix:' + sheet_prefix(sheet_number))
    joined = ' '.join(t['text'].upper() for t in titles)
    reasons += ['title_keyword:' + k for k in MECH_KEYWORDS if k in joined][:3]
    level = 'strong' if any(r.startswith('sheet_prefix') for r in reasons) and any(r.startswith('title_keyword') for r in reasons) \
        else 'candidate' if reasons else 'none'
    return {'level': level, 'reasons': reasons, 'basis': 'prefix and keyword tally; unverified'}


def build_page_index(pdf_file, pdf_sha256, text_pages, ocr_pages):
    """One entry per page; text-layer pages and OCR pages are merged by page number. Every row says how it was derived."""
    text_by = {p['page']: p for p in text_pages}
    ocr_by = {p['page']: p for p in ocr_pages}
    index = []
    for number in sorted(set(text_by) | set(ocr_by)):
        t, o = text_by.get(number), ocr_by.get(number)
        observations = o['observations'] if o else []
        sheet = titleblock_sheet(observations) if observations else None
        method = 'ocr' if sheet else None
        if not sheet and t and t.get('text'):
            for line in t['text'].splitlines():
                s = normalize_sheet_id(line)
                if s and any(c.isdigit() for c in line):
                    sheet, method = {'sheet_number': s, 'region': None, 'ocr_rank': None}, 'text_layer'
        titles = view_titles(observations)
        assess = mechanical_assessment(sheet['sheet_number'] if sheet else None, titles)
        index.append({'pdf_file': pdf_file, 'pdf_sha256': pdf_sha256, 'page': number,
                      'text_layer_chars': len((t or {}).get('text', '').strip()), 'ocr_observations': len(observations),
                      'sheet_number': sheet['sheet_number'] if sheet else None, 'sheet_region': sheet['region'] if sheet else None,
                      'sheet_method': method, 'sheet_ocr_rank': sheet['ocr_rank'] if sheet else None,
                      'view_titles': titles, 'mechanical': assess, 'method_version': METHOD_VERSION, 'status': 'needs_review',
                      'page_width_pt': (o or t or {}).get('width_pt') or (t or {}).get('width'),
                      'page_height_pt': (o or t or {}).get('height_pt') or (t or {}).get('height')})
    return index


def summarize_index(index):
    prefixes = Counter(sheet_prefix(p['sheet_number']) for p in index if p['sheet_number'])
    levels = Counter(p['mechanical']['level'] for p in index)
    return {'pages': len(index), 'pages_with_sheet_number': sum(1 for p in index if p['sheet_number']),
            'pages_without_sheet_number': sum(1 for p in index if not p['sheet_number']),
            'sheet_prefix_counts': dict(sorted(prefixes.items())), 'mechanical_levels': dict(levels),
            'sheet_method_counts': dict(Counter(p['sheet_method'] or 'none' for p in index))}


def equipment_tag_mentions(page_entry, observations):
    """Tag mentions with region provenance. A mention is not an equipment assertion and co-occurrence is not a connection."""
    rows = []
    for o in observations:
        for m in TAG_RE.finditer(o['text'].upper()):
            tag = '%s-%s' % (m.group(1), m.group(2))
            rows.append(_row('mention', page_entry, tag, 'mentions', '', 'ocr', 'ocr', o['conf'], o))
    return rows


def relationship_candidates(page_entry, observations):
    """Only explicit text statements 'TAG SERVES/FEEDS/SUPPLIES TAG' in ONE OCR line; direction comes from the words."""
    rows = []
    for o in observations:
        m = REL_RE.search(o['text'].upper())
        if not m:
            continue
        tags = [f'{a}-{b}' for a, b in TAG_RE.findall(o['text'].upper())]
        if len(tags) >= 2:
            rows.append(_row('relationship_candidate', page_entry, tags[0], 'feeds', tags[1], 'ocr_text_pattern', 'ocr_text_pattern', o['conf'], o))
    return rows


def _row(kind, page_entry, subject, predicate, obj, evidence_kind, method, rank, obs):
    return {'candidate_id': '', 'kind': kind, 'pdf_file': page_entry['pdf_file'], 'pdf_sha256': page_entry['pdf_sha256'],
            'page': page_entry['page'], 'sheet_number': page_entry['sheet_number'] or '', 'region_x': obs['x'], 'region_y': obs['y'],
            'region_w': obs['w'], 'region_h': obs['h'], 'subject_text': subject, 'predicate': predicate, 'object_text': obj,
            'medium': '', 'direction': 'as_worded_in_text' if kind == 'relationship_candidate' else '', 'evidence_kind': evidence_kind,
            'extraction_method': method + ':' + METHOD_VERSION, 'ocr_confidence_rank': rank, 'status': 'needs_review',
            'review_required': 'yes', 'reviewer': '', 'review_decision': '', 'review_notes': ''}


def number_candidates(rows):
    for i, r in enumerate(rows, 1):
        r['candidate_id'] = 'DRW-%05d' % i
    return rows


def validate_extraction_table(rows, pdfs):
    """pdfs: {pdf_file: {'sha256':..., 'page_count':...}}. Returns (valid_rows, errors); invalid rows are never silently kept."""
    valid, errors = [], []
    for n, r in enumerate(rows, 1):
        why = []
        for col in ('pdf_file', 'pdf_sha256', 'page', 'evidence_kind', 'extraction_method', 'status', 'subject_text', 'predicate'):
            if str(r.get(col, '')).strip() == '':
                why.append('MISSING_' + col.upper())
        known = pdfs.get(r.get('pdf_file'))
        if not known:
            why.append('UNKNOWN_PDF')
        else:
            if r.get('pdf_sha256') != known['sha256']:
                why.append('PDF_HASH_MISMATCH')
            try:
                page = int(r.get('page'))
                if not 1 <= page <= known['page_count']:
                    why.append('PAGE_OUT_OF_RANGE')
            except (TypeError, ValueError):
                why.append('PAGE_NOT_INTEGER')
        for col in ('region_x', 'region_y', 'region_w', 'region_h'):
            if str(r.get(col, '')).strip() != '':
                try:
                    v = float(r[col])
                    if not 0 <= v <= 1:
                        why.append('REGION_OUT_OF_BOUNDS')
                except ValueError:
                    why.append('REGION_NOT_NUMERIC')
        if r.get('status') not in ALLOWED_STATUS:
            why.append('STATUS_NOT_ALLOWED')
        if r.get('predicate') not in ALLOWED_PREDICATES:
            why.append('PREDICATE_NOT_ALLOWED')
        if r.get('evidence_kind') not in ALLOWED_EVIDENCE:
            why.append('EVIDENCE_KIND_NOT_ALLOWED')
        if r.get('predicate') != 'mentions' and str(r.get('object_text', '')).strip() == '':
            why.append('RELATIONSHIP_NEEDS_OBJECT')
        machine = r.get('evidence_kind') in {'text_layer', 'ocr', 'ocr_text_pattern', 'model_candidate'}
        if machine and r.get('status') != 'needs_review':
            why.append('MACHINE_ROW_CANNOT_EXCEED_NEEDS_REVIEW')
        if r.get('status') == 'verified' and (not str(r.get('reviewer', '')).strip() or r.get('review_decision') != 'confirmed'
                                              or r.get('evidence_kind') not in {'human_entry', 'human_confirmed'}):
            why.append('VERIFIED_NEEDS_NAMED_HUMAN_CONFIRMATION')
        (errors.append({'row': n, 'candidate_id': r.get('candidate_id'), 'reasons': why}) if why else valid.append(r))
    return valid, errors


def write_table(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator='\n')
        writer.writeheader()
        for r in rows:
            writer.writerow({c: encode_cell(r.get(c, '')) for c in COLUMNS})


def read_table(path):
    from ..csv_safety import decode_row
    with open(path, newline='', encoding='utf-8') as handle:
        return [decode_row(r) for r in csv.DictReader(handle)]
