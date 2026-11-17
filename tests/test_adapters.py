"""TASK_002 Phase A: FR-01/03/04/11, synthetic data only (D1/2/5/8 partial)."""
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import pytest
from openpyxl import Workbook
from pydantic import ValidationError
from rdflib import Graph
from ontology_engine.config import InputConfig, TableMapping, load_config
from ontology_engine.ingest import ingest
from ontology_engine.pipeline import run, entity_iri
from ontology_engine.vocabulary import BRICK

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / 'fixtures/tabular_building'
CSV = ROOT / 'fixtures/aliased_building'


def read_csv(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def test_xlsx_raw_types_sheets_cells_hashes_and_errors():
    records, hashes, originals = ingest(XLSX, load_config(ROOT / 'configs/task002_xlsx.json'))
    assert len(records) == 13
    pressure = next(r for r in records if r.normalized_fields.get('point_id') == 'loop-dp')
    assert pressure.source_id == 'assets.xlsx:Telemetry%20A:2'
    assert pressure.sheet_name == 'Telemetry A'
    assert pressure.source_location == 'sheet Telemetry A; rows 2-2'
    assert pressure.cell_locations['4'] == 'Telemetry A!E2'
    assert pressure.raw_value[-1] == 12.5 and pressure.raw_value[-2] == 'kpa'
    assert pressure.normalized_fields['units'] == 'kPa'
    assert pressure.source_hash == hashlib.sha256(originals['assets.xlsx']).hexdigest() == hashes['assets.xlsx']
    dated = next(r for r in records if r.normalized_fields.get('equipment_id') == 'HX-77' and r.table_kind == 'equipment')
    assert dated.raw_value[-1] == '2026-01-01T00:00:00'
    assert dated.raw_record['2']['data_type'] == 'd'
    duplicates = [r for r in records if r.normalized_fields.get('point_id') == 'dup']
    assert len(duplicates) == 2 and all('Duplicate identifier' in r.errors for r in duplicates)
    assert len({r.source_id for r in duplicates}) == 2
    blank = next(r for r in records if r.sheet_name == 'Telemetry B' and r.source_id.endswith(':3'))
    assert 'Empty record' in blank.errors and blank.raw_value == [None] * 5
    assert blank.cell_locations['0'] == 'Telemetry B!A3'
    formula = next(r for r in records if r.normalized_fields.get('point_id') == 'formula')
    assert formula.raw_value[-1] == '=1+1'
    assert formula.parse_status == 'error'
    assert 'Excel error cell' in next(r for r in records if r.normalized_fields.get('point_id') == 'error-cell').errors


@pytest.mark.parametrize('dataset,config,expected_pair', [
    (XLSX, 'task002_xlsx.json', ('P-42', 'HX-77')),
    (CSV, 'task002_csv.json', ('CIRC-31', 'EXCH-8'))])
def test_configured_cli_and_graph_evidence_and_repeat(tmp_path, dataset, config, expected_pair):
    out, repeat = tmp_path / 'out', tmp_path / 'repeat'
    result = subprocess.run([sys.executable, '-m', 'ontology_engine', 'run', '--input', str(dataset),
        '--output', str(out), '--llm-provider', 'fixture', '--config', str(ROOT / 'configs' / config)],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    manifest = run(dataset, repeat, config_path=ROOT / 'configs' / config)
    graph = Graph().parse(out / 'ontology.ttl')
    assert set(graph) == set(Graph().parse(repeat / 'ontology.ttl'))
    feeds = [r for r in read_csv(out / 'relationships.csv') if r['predicate'] == 'feeds' and r['status'] == 'supported_inference']
    assert [(a['subject_id'], a['object_id']) for a in feeds] == [expected_pair]
    source_ids = {r['source_id'] for r in map(json.loads, (out / 'source_records.jsonl').read_text().splitlines())}
    for row in read_csv(out / 'relationships.csv'):
        assert set(json.loads(row['supporting_source_ids'])) <= source_ids
    assert (entity_iri(manifest.settings['namespace'], 'equipment/' + expected_pair[0]), BRICK.feeds,
            entity_iri(manifest.settings['namespace'], 'equipment/' + expected_pair[1])) in graph
    assert (out / 'data_inventory.json').exists() and (out / 'input_config.json').exists()
    if dataset == XLSX:
        assert any(r['rule_or_detection_method'] == 'inconsistent_units' and r['point_or_asset_id'] == 'temp-bad-unit'
                   for r in read_csv(out / 'issues.csv'))
        assert not any('formula' in str(s) or 'dup' in str(s) for s in graph.subjects())
        classes = read_csv(out / 'classifications.csv')
        assert next(r for r in classes if r['source_point_id'] == 'temp-bad-unit')['status'] == 'needs_review'
        assert json.loads((out / 'data_inventory.json').read_text())['files'][1]['worksheets']


def test_unmapped_xlsx_preserves_rows_and_requests_mapping(tmp_path):
    records, _, _ = ingest(XLSX)
    assert records and all(r.parse_status == 'error' for r in records)
    assert any('configure' in e for r in records for e in r.errors)
    assert any(r.raw_value and r.raw_value[0] == 'loop-dp' for r in records)


@pytest.mark.parametrize('xlsx', [False, True])
def test_conflicting_canonical_and_aliased_columns(tmp_path, xlsx):
    file = 'source.xlsx' if xlsx else 'source.csv'
    if xlsx:
        wb = Workbook()
        wb.active.append(['point_id', 'Vendor Tag', 'units'])
        wb.active.append(['one', 'two', 'Pa'])
        wb.save(tmp_path / file)
    else:
        (tmp_path / file).write_text('point_id,Vendor Tag,units\none,two,Pa\n')
    config = InputConfig(tables=[TableMapping(file=file, kind='points', columns={'point_id': 'Vendor Tag'})])
    records, _, _ = ingest(tmp_path, config)
    assert records and all(r.parse_status == 'error' for r in records)
    assert all(any('conflicting' in e for e in r.errors) for r in records)
    assert records[-1].raw_value[:2] == ['one', 'two']


def test_mapped_header_offset_missing_sheet_and_missing_column(tmp_path):
    (tmp_path / 'export.csv').write_text('SYNTHETIC note\nTag,Unit\nabc,Pa\n')
    config = InputConfig(tables=[TableMapping(file='export.csv', kind='points', header_row=2,
                                             columns={'point_id': 'Tag', 'units': 'Unit'})])
    records, _, _ = ingest(tmp_path, config)
    assert records[0].parse_status == 'error' and 'Preamble' in records[0].errors[0]
    assert records[1].source_id == 'export.csv:3' and records[1].normalized_fields['point_id'] == 'abc'
    assert records[1].parse_status == 'ok'
    missing = InputConfig(tables=[TableMapping(file='export.csv', kind='points', columns={'point_id': 'Absent'})])
    assert any('Configured source column absent' in r.errors for r in ingest(tmp_path, missing)[0])
    missing_file = InputConfig(tables=[TableMapping(file='missing.xlsx', kind='points', sheet='Missing')])
    assert any('Configured file/worksheet absent' in e for r in ingest(tmp_path, missing_file)[0] for e in r.errors)


@pytest.mark.parametrize('mapping', [
    {'file': '../escape.csv', 'kind': 'points'},
    {'file': 'source.csv', 'kind': 'points', 'columns': {'invented': 'Tag'}},
    {'file': 'source.csv', 'kind': 'points', 'columns': {'point_id': 'Tag', 'units': 'Tag'}},
])
def test_invalid_config_rejected(mapping):
    with pytest.raises(ValidationError):
        TableMapping.model_validate(mapping)


def test_overlapping_mappings_rejected():
    with pytest.raises(ValidationError):
        InputConfig(tables=[TableMapping(file='book.xlsx', kind='points'),
                            TableMapping(file='book.xlsx', sheet='Sheet', kind='points')])


def test_broken_workbook_unsupported_input_and_missing_units_are_explicit(tmp_path):
    data = tmp_path / 'input'
    data.mkdir()
    (data / 'equipment.xlsx').write_bytes(b'not a workbook')
    (data / 'points.csv').write_text('point_id,description\nx,Temperature\n')
    (data / 'drawing.pdf').write_bytes(b'%PDF-1.7\nSYNTHETIC invalid placeholder; not a real drawing')
    run(data, tmp_path / 'out')
    issues = read_csv(tmp_path / 'out/issues.csv')
    assert any('XLSX parse failure' in r['observed_evidence'] for r in issues)
    assert any('Unsupported input format' in r['observed_evidence'] for r in issues)
    assert any(r['rule_or_detection_method'] == 'missing_units' for r in issues)
    assert (tmp_path / 'out/sources/drawing.pdf').read_bytes() == (data / 'drawing.pdf').read_bytes()


def test_duplicate_ids_across_csv_and_xlsx(tmp_path):
    (tmp_path / 'points.csv').write_text('point_id,units\nx,Pa\n')
    wb = Workbook()
    wb.active.append(['point_id', 'units'])
    wb.active.append(['x', 'Pa'])
    wb.save(tmp_path / 'another.xlsx')
    records, _, _ = ingest(tmp_path)
    assert len(records) == 2 and all(r.parse_status == 'error' for r in records)
    assert all('Duplicate identifier' in r.errors for r in records)


def test_empty_table_has_diagnostic_and_retains_header(tmp_path):
    (tmp_path / 'points.csv').write_text('point_id,description,units\n')
    records, _, _ = ingest(tmp_path)
    assert len(records) == 1 and records[0].parse_status == 'error'
    assert records[0].raw_value == ['point_id', 'description', 'units']
    assert 'Table contains header but no data rows' in records[0].errors


def test_inventory_cli_does_not_modify_input_or_call_provider(tmp_path):
    output = tmp_path / 'inventory.json'
    before = hashlib.sha256((XLSX / 'assets.xlsx').read_bytes()).hexdigest()
    result = subprocess.run([sys.executable, '-m', 'ontology_engine', 'inventory', '--input', str(XLSX),
                             '--output', str(output), '--config', str(ROOT / 'configs/task002_xlsx.json')],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    inventory = json.loads(output.read_text())
    assert inventory['external_processing'] is False
    item = next(f for f in inventory['files'] if f['file'] == 'assets.xlsx')
    assert len(item['worksheets']) == 4 and item['sha256'] == before
    assert 'BAS Tag' in item['columns']
    assert hashlib.sha256((XLSX / 'assets.xlsx').read_bytes()).hexdigest() == before
