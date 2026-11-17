"""Evidence corroboration, prioritization and the streamlined engineering review (TASK_006 Phase 5C). Local only.

Independence: evidence is grouped by SOURCE FAMILY, not by observation. Every drawing artifact (diagram traces on several sheets,
schedule rows, OCR) comes from one design document set and counts as ONE family, however many sheets agree. The BAS export
(controls contractor) is a second family. Today the BAS carries no connection data, so a drawn connection can only ever be
single-family; a system membership can be corroborated by BAS points whose classes name the same medium.

Nothing here changes a status. Corroboration ranks work and flags tension; only recorded human decisions change statuses.
"""
from collections import Counter, defaultdict

VERSION = 'corroboration-v1'
SYSTEM_MEDIUM = {'Hot_Water_System': 'HW', 'Chilled_Water_System': 'CHW'}
SHEET_PRIORITY = {'HWS': 0, 'CHWS': 0, 'exhaust': 2, 'air_handling': 3}
PLANT_CLASSES = ('Pump', 'Hot_Water_Pump', 'Chilled_Water_Pump', 'Heat_Exchanger', 'AHU', 'Coil', 'Heating_Coil', 'Cooling_Coil', 'Chiller', 'Boiler')
# Time assumptions (minutes) for the estimate; stated, not measured.
MINUTES = {'sheet_orientation': 3, 'per_relationship_row': 1, 'per_identity_row': 1, 'pathway_question': 10, 'signoff': 5}


def _medium_of_points(bg):
    """Medium named by the candidate classes of each equipment's candidate points (BAS family)."""
    classes = defaultdict(set)
    for a in bg.assertions:
        if a.predicate == 'type' and a.subject_id.startswith('point/') and a.status not in ('rejected', 'contradicted'):
            classes[a.subject_id].add(a.object_id)
    medium = defaultdict(set)
    for sid, (tag, _) in bg.point_equipment.items():
        for c in classes.get('point/' + bg.facts[sid]['point_id'], ()):
            if 'Chilled_Water' in c:
                medium[tag].add('CHW')
            if 'Hot_Water' in c or 'Heating' in c:
                medium[tag].add('HW')
    return medium


def _sheets(bg, a):
    reg = {r['source_id']: r for r in bg.trace_register}
    return sorted({(reg[s]['refs'][0]['pdf_sha256'], reg[s]['page']) for s in a.supporting_source_ids if s in reg})


def corroborate(bg):
    """One row per traced relationship: source families, independent-source count, tension, trace quality, sheets."""
    reg = {r['source_id']: r for r in bg.trace_register}
    medium = _medium_of_points(bg)
    schedule_service = {d['tag']: set(d.get('service_from_schedule_text') or []) for d in getattr(bg, 'drawing_equipment', [])}
    rows = []
    for a in bg.connections + bg.traced_memberships:
        hits = [reg[s] for s in a.supporting_source_ids if s in reg]
        sheets = _sheets(bg, a)
        families, notes = ['drawings'], []
        clean = any(h['category'] == 'documented_drawn_connection' for h in hits)
        if len(sheets) > 1:
            notes.append('drawn on %d sheets (same document set: not independent)' % len(sheets))
        tension = False
        if a.predicate == 'includes':
            system = a.subject_id.split('/', 1)[1]
            member = a.object_id.split('/', 1)[1]
            want = SYSTEM_MEDIUM.get(bg.equipment.get(system, {}).get('class'))
            have = medium.get(member, set())
            if want and want in have:
                families.append('bas')
            elif want and have:
                tension = True
                notes.append('BAS points of %s name only %s' % (member, '/'.join(sorted(have))))
            elif member not in bg.equipment:
                notes.append('not monitored in the BAS')
            if want and {'HW': 'HWS', 'CHW': 'CHWS'}[want] in schedule_service.get(member, set()):
                notes.append('schedule service text agrees (same document set)')
        else:
            notes.append('BAS export has no connection data: drawings are the only possible source')
        rows.append({'relationship': '%s %s %s' % (a.subject_id, a.predicate, a.object_id), 'predicate': a.predicate,
                     'subject': a.subject_id, 'object': a.object_id, 'status': a.status, 'trace': 'documented' if clean else 'ambiguous',
                     'source_families': families, 'independent_sources': len(families),
                     'corroboration': 'tension' if tension else 'two_independent_sources' if len(families) > 1 else 'single_source',
                     'sheets': sheets, 'notes': notes})
    return rows


def streamlined_plan(bg, corroboration):
    """Sheet-level decisions (each relationship on the sheet confirmed/rejected individually) for system-titled sheets, plus one
    plant-pathway question per hydronic system. Hydronic sheets first; air-handling sheets are combined into one decision."""
    reg = {r['source_id']: r for r in bg.trace_register}
    by_rel = {c['relationship']: c for c in corroboration}
    sheet_rel, sheet_systems, sheet_meta = defaultdict(list), {}, {}
    for a in bg.connections + bg.traced_memberships:
        if a.status != 'needs_review':
            continue
        for key in _sheets(bg, a):
            sheet_rel[key].append(a)
        for s in a.supporting_source_ids:
            if s in reg:
                k = (reg[s]['refs'][0]['pdf_sha256'], reg[s]['page'])
                sheet_systems[k] = [x for x in reg[s]['sheet_systems'] if x != 'unspecified']
                sheet_meta[k] = reg[s].get('sheet_number')
    diagram_only = {d['tag']: d['class'] for d in bg.diagram_equipment}
    groups = defaultdict(list)
    for key in sheet_rel:
        system = (sheet_systems.get(key) or ['?'])[0]
        groups['air_handling' if system == 'air_handling' else '%s:%s:%s' % (system, key[0][:12], key[1])].append(key)
    decisions = []
    for gkey, keys in sorted(groups.items(), key=lambda kv: (SHEET_PRIORITY.get(kv[0].split(':')[0], 9), -sum(len(sheet_rel[k]) for k in kv[1]), kv[0])):
        rows, seen = [], set()
        for key in sorted(keys):
            for a in sorted(sheet_rel[key], key=lambda a: (a.predicate, a.subject_id, a.object_id)):
                rel = '%s %s %s' % (a.subject_id, a.predicate, a.object_id)
                if rel in seen:
                    continue
                seen.add(rel)
                c = by_rel[rel]
                kind = 'connection' if a.predicate == 'connectedTo' else 'membership'
                item_key = ('connection:%s|%s' if kind == 'connection' else 'membership:%s|%s') % (a.subject_id.split('/', 1)[1], a.object_id.split('/', 1)[1])
                rows.append({'kind': kind, 'item_key': item_key, 'relationship': rel, 'trace': c['trace'], 'corroboration': c['corroboration'],
                             'notes': c['notes'], 'sheet': {'pdf_sha256': key[0], 'page': key[1], 'sheet_number': sheet_meta.get(key)},
                             'source_ids': [s for s in a.supporting_source_ids if s in reg and (reg[s]['refs'][0]['pdf_sha256'], reg[s]['page']) == key]})
            for tag, cls in sorted(diagram_only.items()):
                if any(tag in (r['relationship']) for r in rows) and ('identity:' + tag) not in seen:
                    seen.add('identity:' + tag)
                    rows.append({'kind': 'identity', 'item_key': 'equipment_reading:' + tag, 'relationship': 'equipment/%s is a %s' % (tag, cls),
                                 'trace': 'n/a', 'corroboration': 'single_source', 'notes': ['tag read on the diagram only (no BAS or schedule row)'],
                                 'sheet': {'pdf_sha256': key[0], 'page': key[1], 'sheet_number': sheet_meta.get(key)}, 'source_ids': [], 'class': cls})
        system = gkey.split(':')[0]
        decisions.append({'decision_id': 'S%02d' % (len(decisions) + 1), 'kind': 'sheet_review', 'system': system,
                          'sheets': [{'pdf_sha256': k[0], 'page': k[1], 'sheet_number': sheet_meta.get(k)} for k in sorted(keys)],
                          'priority': 'core' if system in ('HWS', 'CHWS') else 'optional', 'rows': rows})
    plant = defaultdict(set)
    for a in bg.traced_memberships:
        system = a.subject_id.split('/', 1)[1]
        cls = bg.equipment.get(system, {}).get('class')
        member = a.object_id.split('/', 1)[1]
        mcls = (bg.equipment.get(member) or {}).get('class') or diagram_only.get(member) or next(
            (d['class'] for d in getattr(bg, 'drawing_equipment', []) if d['tag'] == member), 'Equipment')
        if cls in SYSTEM_MEDIUM and mcls in PLANT_CLASSES:
            plant[system].add(member)
    for d in getattr(bg, 'drawing_equipment', []):
        for system in d.get('candidate_member_of', []):
            if d['class'] in PLANT_CLASSES:
                plant[system].add(d['tag'])
    for system, members in sorted(plant.items()):
        sheet_system = {'Hot_Water_System': 'HWS', 'Chilled_Water_System': 'CHWS'}.get(bg.equipment.get(system, {}).get('class'))
        decisions.append({'decision_id': 'S%02d' % (len(decisions) + 1), 'kind': 'pathway', 'system': system, 'sheet_system': sheet_system, 'priority': 'core',
                          'question': 'On the %s flow diagram(s), which documented connections and flow directions link the heat source / heat '
                                      'exchanger, the pumps and the coils? Enter each edge you can read as FROM>TO (e.g. HX-1>HWP-1), using the '
                                      'listed tags or any tag shown on the sheet.' % system,
                          'candidate_equipment': sorted(members), 'rows': []})
    return decisions


def workload(plan, comprehensive_summary=None):
    """Explicit estimate from the MINUTES assumptions; returns minutes for the core and full streamlined plans."""
    def minutes(ds):
        m = MINUTES['signoff'] if ds else 0
        for d in ds:
            if d['kind'] == 'pathway':
                m += MINUTES['pathway_question']
            else:
                m += MINUTES['sheet_orientation'] * len(d['sheets']) + sum(
                    MINUTES['per_identity_row'] if r['kind'] == 'identity' else MINUTES['per_relationship_row'] for r in d['rows'])
        return m
    core = [d for d in plan if d['priority'] == 'core']
    return {'version': VERSION, 'assumptions_minutes': MINUTES,
            'streamlined_decisions': len(plan), 'core_decisions': len(core),
            'relationships_reviewable': sum(1 for d in plan for r in d['rows'] if r['kind'] != 'identity'),
            'relationships_reviewable_core': sum(1 for d in core for r in d['rows'] if r['kind'] != 'identity'),
            'identity_rows': sum(1 for d in plan for r in d['rows'] if r['kind'] == 'identity'),
            'estimated_minutes_core': minutes(core), 'estimated_minutes_all_streamlined': minutes(plan),
            'by_corroboration': dict(Counter(r['corroboration'] for d in plan for r in d['rows'])),
            'comprehensive_reference': comprehensive_summary}
