"""Deterministic pipe tracing on vector system diagrams (TASK_006 Phase 5A). Local only; nothing is transmitted.

Inputs per diagram page: the painted line segments and small filled polygons from `scripts/pdf_vectors.swift` (page-normalized
display coordinates) and the local OCR observations of the same page (same frame).

Method (all geometric, all explainable):
1. Text strokes are removed: a segment whose two endpoints fall inside one OCR text box is a glyph stroke, not line work.
2. The remaining stroked line work becomes a graph. Endpoints are snapped within a small tolerance, and an endpoint lying on
   another segment's interior (a drawn T-junction) is joined to it. Lines that merely cross share no node, so they stay
   unconnected, as on a drawing.
3. Each equipment tag occurrence gets an anchor: its OCR box padded by a few text heights. Graph nodes inside the anchor
   that belong to substantial line work are where the equipment's symbol meets its pipes.
4. From each anchor, a shortest-path search follows drawn lines. It stops at, and records, the first other equipment
   anchor reached, so a recorded pair is joined by drawn line work that passes through no other tagged equipment.
5. Drawn arrowheads (small filled triangles) on the path give flow direction, if they agree. Supply/return labels near the
   path are recorded.

What this does NOT do: it never connects components because they share a sheet or a name, and it never verifies anything.
Every result is a review candidate citing the PDF hash, page, sheet and the exact region to inspect.
"""
import heapq
import math
import re
from collections import Counter, defaultdict

from .drawing_evidence import dedupe, tags_in

VERSION = 'drawing-topology-v1'
SNAP = 0.0003                  # endpoint snapping tolerance (page-normalized; about 1 pt on an E-size sheet)
CELL = 0.004                   # spatial index cell
ANCHOR_PAD_HEIGHTS = 2.5       # anchor = OCR tag box padded by this many text heights
MIN_COMPONENT_LENGTH = 0.03    # line work shorter than this in total is symbol/glyph clutter, not a pipe network
MAX_PATH = 1.2                 # search horizon (page widths)
LONG_PATH = 0.35
HIGH_FANOUT = 4
ARROW_NEAR = 0.0015
LABEL_NEAR = 0.002             # label centre within this + two label heights of the path
EQUIPMENT_FAMILIES = {'HWP', 'CHWP', 'PCHWP', 'P', 'HX', 'AHU', 'FCU', 'CUH', 'UH', 'HRC', 'HRU', 'ERU', 'HC', 'CC', 'EF', 'SF', 'RF', 'FTR', 'VAV', 'TB'}
LOOP_LABEL = re.compile(r'^\(?(H?HWS|H?HWR|CHWS|CHWR|CWS|CWR|HRS|HRR|GS|GR)\)?$')


def _len(s):
    return math.hypot(s[2] - s[0], s[3] - s[1])


def _point_segment_distance(px, py, x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - x0, py - y0), 0.0
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / L2))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)), t


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _inside(x, y, box, pad=0.0):
    bx, by, bw, bh = box
    return bx - pad <= x <= bx + bw + pad and by - pad <= y <= by + bh + pad


def remove_text_strokes(segments, observations):
    """Keep line work that is not a glyph of an OCR-read text box. Edges of filled shapes outside text (valve bodies, solid
    symbols) are kept as line work, so a pipe drawn through a filled valve stays connected; arrowheads are read separately."""
    grid = defaultdict(list)
    for i, o in enumerate(observations):
        for gx in range(int(o['x'] / CELL), int((o['x'] + o['w']) / CELL) + 1):
            for gy in range(int(o['y'] / CELL), int((o['y'] + o['h']) / CELL) + 1):
                grid[(gx, gy)].append(i)
    kept = []
    for s in segments:
        boxes = grid.get((int(s[0] / CELL), int(s[1] / CELL)), ())
        text = False
        for i in boxes:
            o = observations[i]
            box = (o['x'], o['y'], o['w'], o['h'])
            pad = 0.1 * o['h']
            if _inside(s[0], s[1], box, pad) and _inside(s[2], s[3], box, pad):
                text = True
                break
        if not text:
            kept.append(s)
    return kept


class LineGraph:
    """Nodes are snapped endpoints; edges are drawn segments (plus T-junction splits)."""

    def __init__(self, segments):
        self.pos, self.adj = {}, defaultdict(dict)
        key = lambda x, y: (round(x / SNAP), round(y / SNAP))  # noqa: E731
        seg_nodes = []
        for s in segments:
            a, b = key(s[0], s[1]), key(s[2], s[3])
            self.pos.setdefault(a, (s[0], s[1]))
            self.pos.setdefault(b, (s[2], s[3]))
            if a != b:
                self._edge(a, b)
            seg_nodes.append((a, b))
        # near-miss endpoints in neighbouring snap cells
        for (i, j) in list(self.pos):
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    n = (i + di, j + dj)
                    if (di or dj) and n in self.pos:
                        self._edge((i, j), n)
        # T-junctions: an endpoint on another segment's interior
        index = defaultdict(list)
        for k, s in enumerate(segments):
            for gx in range(int(min(s[0], s[2]) / CELL), int(max(s[0], s[2]) / CELL) + 1):
                for gy in range(int(min(s[1], s[3]) / CELL), int(max(s[1], s[3]) / CELL) + 1):
                    index[(gx, gy)].append(k)
        degree = Counter(n for a, b in seg_nodes for n in (a, b))
        for node, (x, y) in list(self.pos.items()):
            if degree.get(node, 0) != 1:
                continue                     # only free line ends can terminate on another line
            for k in index.get((int(x / CELL), int(y / CELL)), ()):
                s = segments[k]
                a, b = seg_nodes[k]
                if node in (a, b):
                    continue
                d, t = _point_segment_distance(x, y, s[0], s[1], s[2], s[3])
                if d <= SNAP and 0.0 < t < 1.0:
                    self._edge(node, a)
                    self._edge(node, b)
        uf = _UF()
        for a, nbrs in self.adj.items():
            for b in nbrs:
                uf.union(a, b)
        length = Counter()
        for a, nbrs in self.adj.items():
            for b, w in nbrs.items():
                length[uf.find(a)] += w / 2
        self.component = {n: uf.find(n) for n in self.adj}
        self.component_length = length

    def _edge(self, a, b):
        (x0, y0), (x1, y1) = self.pos[a], self.pos[b]
        w = math.hypot(x1 - x0, y1 - y0)
        self.adj[a][b] = w
        self.adj[b][a] = w

    def substantial(self, node):
        return self.component_length.get(self.component.get(node), 0.0) >= MIN_COMPONENT_LENGTH


def _anchor_box(o):
    pad = ANCHOR_PAD_HEIGHTS * o['h']
    return (o['x'] - pad, o['y'] - pad, o['w'] + 2 * pad, o['h'] + 2 * pad)


def equipment_anchors(observations):
    """One anchor per equipment-tag occurrence (tag text read by OCR)."""
    anchors = []
    for o in observations:
        for fam, tag in tags_in(o['text']):
            if fam not in EQUIPMENT_FAMILIES or (fam == 'P' and re.fullmatch(r'P-\d{3}', tag)):
                continue
            anchors.append({'tag': tag, 'family': fam, 'box': _anchor_box(o), 'text_box': (o['x'], o['y'], o['w'], o['h']), 'conf': o['conf']})
    return anchors


def _nodes_in(graph, box, grid):
    out = []
    for gx in range(int(box[0] / CELL), int((box[0] + box[2]) / CELL) + 1):
        for gy in range(int(box[1] / CELL), int((box[1] + box[3]) / CELL) + 1):
            for n in grid.get((gx, gy), ()):
                if _inside(*graph.pos[n], box) and graph.substantial(n):
                    out.append(n)
    return out


def _arrow(poly):
    """(centroid, unit direction) of a filled triangle arrowhead, or None. Apex = vertex opposite the shortest side."""
    pts = [(poly[i], poly[i + 1]) for i in range(0, len(poly), 2)]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    uniq = []
    for p in pts:
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) > 1e-6 for q in uniq):
            uniq.append(p)
    if len(uniq) != 3:
        return None
    sides = [(math.hypot(uniq[(i + 1) % 3][0] - uniq[(i + 2) % 3][0], uniq[(i + 1) % 3][1] - uniq[(i + 2) % 3][1]), i) for i in range(3)]
    sides.sort()
    if sides[0][0] <= 0 or sides[1][0] / sides[0][0] < 1.3:
        return None                          # not elongated enough to have a clear apex
    apex = uniq[sides[0][1]]
    base = [uniq[(sides[0][1] + 1) % 3], uniq[(sides[0][1] + 2) % 3]]
    mx, my = (base[0][0] + base[1][0]) / 2, (base[0][1] + base[1][1]) / 2
    d = math.hypot(apex[0] - mx, apex[1] - my)
    if d == 0 or d > 0.01:
        return None
    cx, cy = (uniq[0][0] + uniq[1][0] + uniq[2][0]) / 3, (uniq[0][1] + uniq[1][1] + uniq[2][1]) / 3
    return (cx, cy), ((apex[0] - mx) / d, (apex[1] - my) / d)


def trace_page(page_vectors, observations, ref_base, systems):
    """Trace one diagram page. Returns {'connections': [...], 'anchors': [...], 'stats': {...}}."""
    obs = dedupe(observations)
    strokes = remove_text_strokes(page_vectors['segments'], obs)
    graph = LineGraph(strokes)
    node_grid = defaultdict(list)
    for n, (x, y) in graph.pos.items():
        node_grid[(int(x / CELL), int(y / CELL))].append(n)
    anchors = equipment_anchors(obs)
    for a in anchors:
        a['nodes'] = _nodes_in(graph, a['box'], node_grid)
    owner = defaultdict(set)                 # node -> anchor indexes covering it
    for i, a in enumerate(anchors):
        for n in a['nodes']:
            owner[n].add(i)
    arrows = [x for x in (_arrow(p) for p in page_vectors.get('polygons', [])) if x]
    labels = [o for o in obs if LOOP_LABEL.match(o['text'].strip().upper())]
    found = {}
    for i, a in enumerate(anchors):
        if not a['nodes']:
            continue
        dist, prev = {}, {}
        heap = [(0.0, n) for n in a['nodes']]
        for _, n in heap:
            dist[n] = 0.0
        heapq.heapify(heap)
        while heap:
            d, n = heapq.heappop(heap)
            if d > dist.get(n, math.inf) or d > MAX_PATH:
                continue
            others = {j for j in owner.get(n, ()) if anchors[j]['tag'] != a['tag']}
            if others:
                for j in others:
                    key = tuple(sorted((a['tag'], anchors[j]['tag'])))
                    path = [n]
                    while path[-1] in prev:
                        path.append(prev[path[-1]])
                    if key not in found or d < found[key]['length']:
                        found[key] = {'length': d, 'path': path[::-1] if a['tag'] == key[0] else path, 'anchors': (i, j) if a['tag'] == key[0] else (j, i)}
                continue                     # never trace through another equipment
            for m, w in graph.adj[n].items():
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m], prev[m] = nd, n
                    heapq.heappush(heap, (nd, m))
    fanout = Counter(t for key in found for t in key)
    connections = []
    for (ta, tb), f in sorted(found.items()):
        pts = [graph.pos[n] for n in f['path']]
        ia, ib = f['anchors']
        forward = backward = 0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            L = math.hypot(x1 - x0, y1 - y0)
            if L == 0:
                continue
            ux, uy = (x1 - x0) / L, (y1 - y0) / L
            for (cx, cy), (dx, dy) in arrows:
                dd, _ = _point_segment_distance(cx, cy, x0, y0, x1, y1)
                if dd <= ARROW_NEAR and abs(dx * ux + dy * uy) > 0.8:
                    if dx * ux + dy * uy > 0:
                        forward += 1
                    else:
                        backward += 1
        near_labels = sorted({o['text'].strip().upper().strip('()') for o in labels
                              if any(_point_segment_distance(o['x'] + o['w'] / 2, o['y'] + o['h'] / 2, x0, y0, x1, y1)[0] <= LABEL_NEAR + 2 * o['h']
                                     for (x0, y0), (x1, y1) in zip(pts, pts[1:]))})
        direction = 'conflicting_arrows' if forward and backward else 'a_to_b' if forward else 'b_to_a' if backward else 'not_drawn'
        flags = []
        if f['length'] > LONG_PATH:
            flags.append('LONG_PATH')
        if fanout[ta] > HIGH_FANOUT or fanout[tb] > HIGH_FANOUT:
            flags.append('HIGH_FANOUT')
        if direction == 'conflicting_arrows':
            flags.append('CONFLICTING_ARROWS')
        if {'S', 'R'} <= {lbl[-1] for lbl in near_labels}:
            flags.append('SUPPLY_AND_RETURN_LABELS_ON_PATH')
        boxes = [anchors[ia]['box'], anchors[ib]['box']]
        xs = [p[0] for p in pts] + [b[0] for b in boxes] + [b[0] + b[2] for b in boxes]
        ys = [p[1] for p in pts] + [b[1] for b in boxes] + [b[1] + b[3] for b in boxes]
        region = [round(min(xs), 5), round(min(ys), 5), round(max(xs) - min(xs), 5), round(max(ys) - min(ys), 5)]
        connections.append({'a': ta, 'b': tb, 'category': 'ambiguous_drawn_connection' if flags else 'documented_drawn_connection',
                             'flags': flags, 'path_length': round(f['length'], 4), 'path_points': len(pts),
                             'arrows_forward': forward, 'arrows_backward': backward, 'direction': direction, 'loop_labels': near_labels,
                             'systems_on_sheet': systems,
                             'refs': [dict(ref_base, bbox=region, role='path_region'),
                                      dict(ref_base, bbox=[round(v, 5) for v in anchors[ia]['text_box']], role='tag_a', ocr_confidence=round(anchors[ia]['conf'], 3)),
                                      dict(ref_base, bbox=[round(v, 5) for v in anchors[ib]['text_box']], role='tag_b', ocr_confidence=round(anchors[ib]['conf'], 3))],
                             'method': VERSION})
    attached = {a['tag'] for a in anchors if a['nodes']}
    stats = {'segments': len(page_vectors['segments']), 'line_segments_after_text_removal': len(strokes), 'graph_nodes': len(graph.pos),
             'arrowheads': len(arrows), 'loop_labels': len(labels), 'tag_occurrences': len(anchors), 'distinct_tags': len({a['tag'] for a in anchors}),
             'distinct_tags_attached_to_line_work': len(attached), 'connections': len(connections),
             'connections_by_category': dict(Counter(c['category'] for c in connections)),
             'connections_with_direction': sum(1 for c in connections if c['direction'] in ('a_to_b', 'b_to_a'))}
    return {'connections': connections, 'tags': sorted({a['tag'] for a in anchors}), 'attached_tags': sorted(attached), 'stats': stats}


def trace_diagrams(vector_docs, ocr_docs, diagrams, page_index):
    """Trace every system diagram page that has vector geometry. `diagrams` come from drawing_evidence (systems per page)."""
    ocr = {(d['pdf_sha256'], p['page']): p['observations'] for d in ocr_docs for p in d['pages']}
    vectors = {(d['pdf_sha256'], p['page']): p for d in vector_docs for p in d['pages']}
    pages = []
    for d in diagrams:
        sha12, page = d['diagram_id'].split(':')
        page = int(page[1:])
        key = next((k for k in vectors if k[0].startswith(sha12) and k[1] == page), None)
        if key is None or key not in ocr:
            continue
        sheet = (page_index.get(key) or {}).get('sheet_number')
        base = {'pdf_sha256': key[0], 'page': page, 'sheet_number': sheet}
        result = trace_page(vectors[key], ocr[key], base, d['systems'])
        result.update(page=page, pdf_sha256=key[0], sheet_number=sheet, systems=d['systems'])
        pages.append(result)
    pairs = defaultdict(list)
    for p in pages:
        for c in p['connections']:
            pairs[(c['a'], c['b'])].append(c)
    summary = {'version': VERSION, 'pages_traced': len(pages), 'connections': sum(len(p['connections']) for p in pages),
               'distinct_pairs': len(pairs), 'pairs_on_multiple_sheets': sum(1 for v in pairs.values() if len(v) > 1),
               'by_category': dict(Counter(c['category'] for p in pages for c in p['connections'])),
               'by_direction': dict(Counter(c['direction'] for p in pages for c in p['connections'])),
               'flags': dict(Counter(f for p in pages for c in p['connections'] for f in c['flags'])),
               'pages': [dict(p['stats'], page=p['page'], systems=p['systems']) for p in pages], 'transmitted_anywhere': False}
    return {'version': VERSION, 'summary': summary, 'pages': pages}
