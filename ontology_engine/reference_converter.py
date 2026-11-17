"""Public reference RDF -> derived metadata tables; class/topology targets stay separate."""
import csv
import hashlib
import json
from pathlib import Path
from rdflib import Graph, RDF, RDFS, URIRef
from openpyxl import Workbook
from .vocabulary import BRICK
from .workbooks import save_reproducible_workbook


def convert_reference(source, output, source_url, license_note, max_equipment=5, max_points=20):
    source, output = Path(source), Path(output)
    if max_equipment < 1 or max_points < 1:
        raise ValueError('Positive selection limits required')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Converter output directory must be empty')
    content = source.read_bytes()
    registry_path = Path(__file__).resolve().parents[1] / 'datasets/public_sources.json'
    pinned = next((s for s in json.loads(registry_path.read_text())['sources'] if s['url'] == source_url), None)
    if pinned:
        license_note = pinned['license']
    if pinned and hashlib.sha256(content).hexdigest() != pinned.get('extracted_sha256', pinned['sha256']):
        raise ValueError('PUBLIC_SOURCE_HASH_MISMATCH: derivation refused')
    # Stable public base avoids accidental machine-local file IRIs in legacy relative Turtle.
    graph = Graph().parse(data=content, format='turtle', publicID=source_url)
    equipment = sorted({s for s, o in graph.subject_objects(BRICK.hasPoint) if isinstance(s, URIRef)}, key=str)[:max_equipment]
    points = sorted({p for e in equipment for p in graph.objects(e, BRICK.hasPoint) if isinstance(p, URIRef)}, key=str)[:max_points]
    if not equipment or not points:
        raise ValueError('No hasPoint equipment/point example in source graph')
    output.mkdir(parents=True, exist_ok=True)
    csv_dir, xlsx_dir, reference = [output / name for name in ('input_csv', 'input_xlsx', 'reference')]
    for path in (csv_dir, xlsx_dir, reference):
        path.mkdir()
    provenance = {'label': 'PUBLIC REFERENCE DERIVED / NOT ORIGINAL BMS EXPORT',
                  'source_url': source_url, 'source_sha256': hashlib.sha256(content).hexdigest(),
                  'license_note': license_note, 'redistribution_allowed': license_note.startswith('CC BY 4.0'),
                  'license_status': 'declared_release_license' if license_note.startswith('CC BY 4.0') else 'unresolved_or_local_only', 'reference_topology_in_input': False,
                  'transformation': 'URI identity, literal rdfs:label and explicit hasUnit(s) only; classes and ownership/topology withheld',
                  'selection': {'method': 'lexicographic hasPoint subjects and their points',
                                'max_equipment': max_equipment, 'max_points': max_points}}
    equipment_rows = [{'equipment_id': str(e), 'equipment_type': '', 'origin_entity_uri': str(e),
                       'origin_source_sha256': provenance['source_sha256']} for e in equipment]
    point_rows = []
    for point in points:
        units = sorted(str(u) for predicate in (BRICK.hasUnit, BRICK.hasUnits) for u in graph.objects(point, predicate))
        labels = sorted(str(label) for label in graph.objects(point, RDFS.label))
        point_rows.append({'point_id': str(point), 'description': labels[0] if labels else '',
                           'equipment_id': '', 'units': units[0] if len(set(units)) == 1 else '',
                           'origin_entity_uri': str(point), 'origin_source_sha256': provenance['source_sha256']})
    tables = {'equipment': equipment_rows, 'points': point_rows}
    for name, rows in tables.items():
        with (csv_dir / (name + '.csv')).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in tables.items():
        sheet = workbook.create_sheet(name)
        sheet.append(list(rows[0]))
        for row in rows:
            # Literal text only: do not turn a source label starting '=' into a formula.
            sheet.append(list(row.values()))
            for cell in sheet[sheet.max_row]:
                cell.data_type = 's'
    save_reproducible_workbook(workbook, xlsx_dir / 'tables.xlsx')
    for directory in (csv_dir, xlsx_dir):
        (directory / 'DATA_PROVENANCE.json').write_text(json.dumps(provenance, indent=2) + '\n')
        (directory / 'README.md').write_text('# PUBLIC REFERENCE DERIVED / NOT ORIGINAL BMS EXPORT\n\n'
            'Public development example, never sponsor evidence. No readings invented. '
            'Reference classes/ownership/topology are excluded from these inputs and kept in sibling reference/.\n')
    (reference / 'original.ttl').write_bytes(content)
    selected = set(equipment) | set(points)
    targets = {'entities': [{'id': str(e), 'classes': sorted(str(c) for c in graph.objects(e, RDF.type))}
                            for e in sorted(selected, key=str)],
               'relationships': [{'subject': str(s), 'predicate': str(p), 'object': str(o)}
                                 for s, p, o in sorted(graph, key=lambda triple: tuple(map(str, triple)))
                                 if p in {BRICK.hasPoint, BRICK.feeds, BRICK.hasPart, BRICK.isPointOf, BRICK.isFedBy}
                                 and s in selected and o in selected]}
    (reference / 'targets.json').write_text(json.dumps(targets, indent=2) + '\n')
    provenance['counts'] = {'original_triples': len(graph), 'equipment': len(equipment), 'points': len(points),
                            'reference_relationships': len(targets['relationships'])}
    provenance['output_hashes'] = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'conversion_manifest.json').write_text(json.dumps(provenance, indent=2) + '\n')
    return provenance
