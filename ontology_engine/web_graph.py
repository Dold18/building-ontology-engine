"""Bounded presentation of existing candidate ledgers; never an ontology engine."""
from collections import Counter

ACCEPTED = {'verified', 'supported_inference'}
MAX_NODES = 180
MAX_EDGES = 360
MAX_CLAIMS = 100_000


def graph_view(claims, query='', focus='', predicate='', status='', kind='', offset=0):
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise ValueError('GRAPH_LIMIT')
    if type(offset) is not int or not 0 <= offset <= MAX_CLAIMS:
        raise ValueError('GRAPH_PAGE')
    required = {'claim_uid', 'subject_id', 'predicate', 'object_id', 'status'}
    if any(not isinstance(c, dict) or not required <= c.keys() or
           any(not isinstance(c[k], str) or len(c[k]) > 4096 for k in required) for c in claims):
        raise ValueError('GRAPH_MALFORMED')
    nodes = {}; edges = []
    # Stable sort and fixed columns/rows make initial layout independent of JSON order.
    types = {}
    for c in sorted(claims, key=lambda c: c['claim_uid']):
        if c['predicate'] == 'type':
            types.setdefault(c['subject_id'], []).append(c)
    related = {focus} if focus else set()
    if focus:
        for c in claims:
            if c['predicate'] != 'type' and focus in (c['subject_id'], c['object_id']):
                related.update((c['subject_id'], c['object_id']))
    matched = []
    for c in sorted(claims, key=lambda c: c['claim_uid']):
        endpoints = [c['subject_id']] + ([] if c['predicate'] == 'type' else [c['object_id']])
        if predicate and c['predicate'] != predicate: continue
        if status and c['status'] != status: continue
        if kind and not any(n.startswith(kind + '/') for n in endpoints): continue
        if related and not set(endpoints) <= related: continue
        if query and query.casefold() not in ' '.join(endpoints + [c['object_id']]).casefold(): continue
        matched.append(c)
    consumed = 0
    for c in matched[offset:]:
        endpoints = [c['subject_id']] + ([] if c['predicate'] == 'type' else [c['object_id']])
        if len(set(endpoints) - nodes.keys()) + len(nodes) > MAX_NODES or len(edges) >= MAX_EDGES: break
        for n in endpoints:
            if n not in nodes:
                nodes[n] = {'id': n, 'kind': n.split('/')[0], 'types': types.get(n, []),
                            'claim_ids': [t['claim_uid'] for t in types.get(n, [])]}
        if c['predicate'] != 'type':
            edges.append({**{k: c[k] for k in required}, 'accepted': c['status'] in ACCEPTED})
        consumed += 1
    columns = Counter()
    for n in sorted(nodes):
        col = 1 if n.startswith('point/') else 0
        nodes[n].update(x=180 + 450 * col, y=65 + 65 * columns[col])
        columns[col] += 1
    return {'nodes': [nodes[n] for n in sorted(nodes)], 'edges': edges,
            'total_claims': len(claims), 'matched_claims': len(matched), 'offset': offset,
            'next_offset': offset + consumed if offset + consumed < len(matched) else None,
            'limits': {'nodes': MAX_NODES, 'edges': MAX_EDGES},
            'statuses': dict(Counter(c['status'] for c in claims))}


def diagram(claims, synthetic):
    """Escaped, bounded presentation export; source ledgers remain authoritative."""
    from xml.etree.ElementTree import Element, SubElement, tostring
    view = graph_view(claims)
    height = max([500] + [n['y'] + 150 for n in view['nodes']])
    root = Element('svg', xmlns='http://www.w3.org/2000/svg', viewBox=f'0 0 900 {height}', role='img')
    SubElement(root, 'title').text = 'Mechanical ontology: accepted and candidate-only assertions'
    def label(x, y, value, size='12'):
        SubElement(root, 'text', x=str(x), y=str(y), fill='#18343c', **{'font-size':size}).text = value
    label(20, 25, 'SYNTHETIC / FOR TESTING ONLY' if synthetic else 'PROVISIONAL BUILDING DATA — sensitive evidence', '16')
    label(20, 48, 'Solid teal = accepted supported assertion; dashed orange = candidate only / disputed.')
    label(20, 68, f'Bounded view: {len(view["nodes"])} nodes, {len(view["edges"])} edges; complete claims are in the JSON/Turtle exports.')
    nodes = {n['id']: n for n in view['nodes']}
    for e in view['edges']:
        a, b = nodes[e['subject_id']], nodes[e['object_id']]
        attrs = {'x1':str(a['x']), 'y1':str(a['y']+80), 'x2':str(b['x']), 'y2':str(b['y']+80),
                 'stroke':'#287c7a' if e['accepted'] else '#b45f37', 'stroke-width':'2'}
        if not e['accepted']: attrs['stroke-dasharray'] = '7 6'
        line = SubElement(root, 'line', attrs)
        SubElement(line, 'title').text = f'{e["subject_id"]} → {e["predicate"]} → {e["object_id"]} [{e["status"]}]'
        label((a['x']+b['x'])/2, (a['y']+b['y'])/2+75, ('? ' if not e['accepted'] else '')+e['predicate'])
    for n in view['nodes']:
        box = SubElement(root, 'rect', x=str(n['x']-130), y=str(n['y']+60), width='260', height='40', rx='6', fill='#e1f0ef', stroke='#547b8c')
        SubElement(box, 'title').text = n['id']+'; '+', '.join(t['object_id']+' ['+t['status']+']' for t in n['types'])
        label(n['x']-122, n['y']+84, n['id'][:38])
    return tostring(root, encoding='utf-8', xml_declaration=True)
