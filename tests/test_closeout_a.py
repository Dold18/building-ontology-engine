"""TASK_002-R A: SEC-01/02 and audit P1/P2/P3/P4/P8. Synthetic only."""
import csv
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from openpyxl import Workbook
from ontology_engine.sources import snapshot_input
from ontology_engine.ingest import ingest
from ontology_engine.inventory import inventory
from ontology_engine.pipeline import run, write_csv
from ontology_engine.identity import unique_source_map
from ontology_engine.csv_safety import encode_cell, decode_cell, decode_row
from ontology_engine.models import Model


def dataset(path):
    path.mkdir()
    (path / 'equipment.csv').write_text('equipment_id,equipment_type\nE,Pump\n')
    (path / 'points.csv').write_text('point_id,description,equipment_id,units\np,Pressure Reading,E,Pa\n')
    return path


@pytest.mark.parametrize('kind', ['outside', 'inside', 'broken', 'directory', 'root'])
def test_symlinks_rejected_before_read_or_provider(tmp_path, kind):
    data = dataset(tmp_path / 'input')
    other = tmp_path / 'sentinel.csv'; other.write_text('DO NOT READ: benign outside sentinel')
    target = {'outside': other, 'inside': data / 'points.csv', 'broken': tmp_path / 'absent',
              'directory': tmp_path / 'dir', 'root': data}[kind]
    if kind == 'directory': target.mkdir()
    if kind == 'root':
        link = tmp_path / 'linked'; link.symlink_to(data, target_is_directory=True); data = link
    else:
        (data / 'link').symlink_to(target, target_is_directory=kind == 'directory')
    with patch('ontology_engine.providers.urlopen') as call:
        with pytest.raises(ValueError, match='SYMLINK_REJECTED'): snapshot_input(data)
        with pytest.raises(ValueError, match='SYMLINK_REJECTED'): run(data, tmp_path / 'out')
        call.assert_not_called()
    assert not (tmp_path / 'out').exists()


def test_snapshot_is_single_source_for_inventory_and_parse(tmp_path):
    data = dataset(tmp_path / 'input')
    snap = snapshot_input(data)
    old = snap.files['points.csv']
    (data / 'points.csv').write_text('point_id,units\nSWAPPED,degC\n')
    records, hashes, originals = ingest(data, snapshot=snap)
    listing = inventory(data, records, hashes, snap)
    assert originals['points.csv'] == old
    assert next(r for r in records if r.table_kind == 'points').normalized_fields['point_id'] == 'p'
    assert next(f for f in listing['files'] if f['file'] == 'points.csv')['bytes'] == len(old)
    with pytest.raises(TypeError): snap.files['x'] = b'changed'


def test_source_id_collision_probe_p3_is_disambiguated(tmp_path):
    wb = Workbook(); wb.active.title = 'S.csv'
    wb.active.append(['point_id', 'units']); wb.active.append(['excel', 'Pa'])
    wb.save(tmp_path / 'collision.xlsx')
    (tmp_path / 'collision.xlsx:S.csv').write_text('point_id,units\ncsv,Pa\n')
    records, _, _ = ingest(tmp_path)
    assert len(records) == len({r.source_record_uid for r in records}) == len({r.source_id for r in records}) == 2
    assert all(r.parse_status == 'ok' for r in records)
    records[1].source_id = records[0].source_id
    with pytest.raises(ValueError, match='SOURCE_ID_COLLISION'): unique_source_map(records)


def test_scoped_claims_and_readme_stability_p1_p2(tmp_path):
    data = dataset(tmp_path / 'input')
    first = run(data, tmp_path / 'first')
    (data / 'README.md').write_text('Irrelevant text')
    again = run(data, tmp_path / 'again')
    assert first.settings['namespace'] == again.settings['namespace']
    run(data, tmp_path / 'site-a', building_id='a'); run(data, tmp_path / 'site-b', building_id='b')
    a = json.loads((tmp_path / 'site-a/assertions.json').read_text())
    b = json.loads((tmp_path / 'site-b/assertions.json').read_text())
    assert not {x['claim_uid'] for x in a} & {x['claim_uid'] for x in b}
    assert not {x['subject_entity_uid'] for x in a} & {x['subject_entity_uid'] for x in b}


def test_multiple_observations_are_independent_p8(tmp_path, monkeypatch):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    proposals = {'points': [{'source_id': 'points.csv:2', 'point_class': 'Pressure_Sensor', 'equipment_id': 'E',
                            'explanation': explanation} for explanation in ['First observation', 'Second observation']],
                 'relationships': []}
    response = MagicMock(); response.__enter__.return_value.read.return_value = json.dumps({'model': 'MOCKED',
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(proposals)}}]}).encode()
    with patch('ontology_engine.providers.urlopen', return_value=response): run(data, tmp_path / 'out', 'openai', 'MOCKED', True)
    candidates = [a for a in json.loads((tmp_path / 'out/candidate_assertions.json').read_text()) if a['object_id'] == 'Pressure_Sensor']
    assert len(candidates) == len({a['observation_uid'] for a in candidates}) == 2
    assert len({a['claim_uid'] for a in candidates}) == 1
    reviews = json.loads((tmp_path / 'out/review_questions.json').read_text())
    assert len(reviews) == len({r['review_id'] for r in reviews})


@pytest.mark.parametrize('text', ['=SUM(A1)', '+CMD', '-CMD', '@CMD', '\tCMD', '\rCMD', '\nCMD',
    ' \t=CMD', '\u2003=CMD', "'literal", "''literal", '-12.5', 'ordinary', 'AHU-1'])
def test_csv_text_roundtrip(text):
    encoded = encode_cell(text)
    assert decode_cell(encoded) == text
    if text not in {'ordinary', 'AHU-1'}: assert encoded.startswith("'")
    else: assert encoded == text
    assert encode_cell(-12.5) == -12.5


def test_all_csv_reports_share_one_encoder_and_canonical_json_remains_exact(tmp_path):
    class Row(Model):
        identifier: str
        explanation: str
        number: float
    for filename in ['classifications.csv', 'relationships.csv', 'issues.csv', 'review_questions.csv']:
        write_csv(tmp_path / filename, [Row(identifier='=BAD', explanation=' \r@BAD', number=-12.5)], list(Row.model_fields))
        with (tmp_path / filename).open(newline='') as stream: row = next(csv.DictReader(stream))
        assert row['identifier'] == "'=BAD" and row['explanation'].startswith("'") and row['number'] == '-12.5'
        assert decode_row(row)['identifier'] == '=BAD'
    data = tmp_path / 'input'; data.mkdir()
    with (data / 'equipment.csv').open('w', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['equipment_id', 'equipment_type']); writer.writerow(['=E', 'Pump'])
    (data / 'points.csv').write_text('point_id,description,equipment_id,units\n=P,Pressure Reading,=E,Pa\n')
    run(data, tmp_path / 'out')
    canonical = json.loads((tmp_path / 'out/classifications.json').read_text())[0]
    assert canonical['source_point_id'] == '=P' and canonical['equipment_id'] == '=E'
    with (tmp_path / 'out/classifications.csv').open() as stream: human = decode_row(next(csv.DictReader(stream)))
    assert human['source_point_id'] == canonical['source_point_id']
