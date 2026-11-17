"""TASK_001 acceptance evidence for FR-01/02/03/04/06/11/12, partial D1/2/5/8."""
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError
from rdflib import Graph, RDF, URIRef
from ontology_engine.ingest import ingest
from ontology_engine.models import Assertion, Proposals
from ontology_engine.pipeline import run, validate_graph, entity_iri
from ontology_engine.providers import openai
from ontology_engine.vocabulary import BRICK

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'fixtures/demo_building'
SECOND = ROOT / 'fixtures/second_building'


def rows(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def test_fr01_raw_records_and_hashes():
    records, hashes, originals = ingest(DEMO)
    assert len(records) == 13
    pressure = next(r for r in records if r.normalized_fields.get('point_id') == 'VAV102_DP')
    assert pressure.raw_value[-2:] == ['', '1.3']
    assert pressure.normalized_fields['units'] == ''
    assert pressure.source_id == 'points.csv:8'
    assert pressure.source_location == 'rows 8-8'
    assert pressure.source_hash == hashlib.sha256(originals['points.csv']).hexdigest()
    assert pressure.source_hash == hashes['points.csv']


def test_fr01_malformed_duplicate_blank_and_multiline(tmp_path):
    data = tmp_path / 'input'
    data.mkdir()
    content = 'point_id,description,equipment_id,units\n p1 ,"Two\nlines",eq,degC\np1,duplicate,eq,degC\n,empty,,\np2,wide,,,extra\np3,short\n'
    (data / 'points.csv').write_text(content)
    records, _, _ = ingest(data)
    assert len(records) == 5
    assert records[0].raw_value[0] == ' p1 '
    assert records[0].normalized_fields['point_id'] == 'p1'
    assert records[0].source_location == 'rows 2-3'
    assert all(r.parse_status == 'error' for r in records)
    assert all('Duplicate identifier' in r.errors for r in records[:2])
    manifest = run(data, tmp_path / 'out')
    assert manifest.counts['issues'] >= 5
    assert (tmp_path / 'out/sources/points.csv').read_bytes() == content.encode()
    assert not list(Graph().parse(tmp_path / 'out/ontology.ttl').subjects(RDF.type, BRICK.Supply_Air_Temperature_Sensor))


@pytest.mark.parametrize('content', [b'', b'bad,header\nx,y\n', b'point_id,point_id\na,b\n',
                                     b'point_id,description\nx,"unterminated\n', b'\xff\xfe'])
def test_fr01_bad_headers_encoding_and_csv_are_not_silently_dropped(tmp_path, content):
    (tmp_path / 'points.csv').write_bytes(content)
    records, _, originals = ingest(tmp_path)
    assert records and any(r.parse_status == 'error' for r in records)
    assert originals['points.csv'] == content


def test_fr03_fr04_exports_uncertainty_and_documented_topology(tmp_path):
    out = tmp_path / 'demo'
    manifest = run(DEMO, out)
    graph = Graph().parse(out / 'ontology.ttl', format='turtle')
    ns = manifest.settings['namespace']
    feeds = set(graph.subject_objects(BRICK.feeds))
    iri = lambda x: entity_iri(ns, 'equipment/' + x)
    assert feeds == {(iri('AHU-1'), iri('VAV-101')), (iri('AHU-1'), iri('VAV-102'))}
    assert (iri('AHU-1'), BRICK.hasPoint, entity_iri(ns, 'point/AHU1_SAT')) in graph
    assert len(list(graph.query('SELECT ?s ?o WHERE {?s <https://brickschema.org/schema/Brick#feeds> ?o}'))) == 2
    classifications = rows(out / 'classifications.csv')
    ambiguous = next(c for c in classifications if c['source_point_id'] == 'AMB_1')
    assert ambiguous['point_class'] == 'unknown' and ambiguous['equipment_id'] == ''
    assert ambiguous['status'] == 'needs_review'
    issues = rows(out / 'issues.csv')
    assert any(i['point_or_asset_id'] == 'VAV102_DP' and i['category'] == 'data_quality' for i in issues)
    ledger = rows(out / 'relationships.csv')
    source_ids = {json.loads(line)['source_id'] for line in (out / 'source_records.jsonl').read_text().splitlines()}
    for a in ledger:
        assert set(json.loads(a['supporting_source_ids'])) <= source_ids
        assert a['explanation']
        assert a['status'] != 'verified'  # Schema checks must not promote status.
        if a['predicate'] == 'feeds':
            assert json.loads(a['supporting_source_ids'])[0].startswith('connections.csv:')
    assert rows(out / 'review_questions.csv')
    assert json.loads((out / 'candidate_assertions.json').read_text())
    assert manifest.validation['physics'] == 'not_evaluable'
    assert manifest.validation['full_brick_shacl'] == 'not_evaluable'
    assert 'SYNTHETIC / FOR TESTING ONLY' in (out / 'README.md').read_text()
    for name in ['ontology.ttl', 'classifications.csv', 'relationships.csv', 'issues.csv',
                 'review_questions.csv', 'run_manifest.json', 'source_records.jsonl']:
        assert (out / name).stat().st_size


def test_fr11_stable_triples_and_second_directory(tmp_path):
    a, b, c = [tmp_path / name for name in ['a', 'b', 'c']]
    first, again = run(DEMO, a), run(DEMO, b)
    assert set(Graph().parse(a / 'ontology.ttl')) == set(Graph().parse(b / 'ontology.ttl'))
    assert first.run_id != again.run_id
    second = run(SECOND, c)
    graph = Graph().parse(c / 'ontology.ttl')
    assert len(list(graph.subject_objects(BRICK.feeds))) == 1
    assert any('HYD-9' in str(s) for s in graph.subjects())
    assert not any('AHU-1' in str(s) for s in graph.subjects())
    assert second.settings['namespace'] != first.settings['namespace']
    assert any(a['status'] == 'needs_review' for a in rows(c / 'relationships.csv') if a['predicate'] == 'feeds')


def test_fr03_no_name_based_feeds_and_dangling_connections(tmp_path):
    data = tmp_path / 'input'
    data.mkdir()
    (data / 'equipment.csv').write_text('equipment_id,equipment_type\nEQ-1,AHU\nEQ-2,VAV\n')
    (data / 'points.csv').write_text('point_id,description,equipment_id,units\nEQ-1-SAT,Supply Air Temperature,EQ-1,degC\n')
    (data / 'connections.csv').write_text('from_equipment,to_equipment,predicate\nEQ-1,GHOST,feeds\nEQ-1,EQ-1,feeds\nEQ-1,EQ-2,maliciousPredicate\n')
    out = tmp_path / 'out'
    run(data, out)
    graph = Graph().parse(out / 'ontology.ttl')
    assert not list(graph.subject_objects(BRICK.feeds))
    assert len(rows(out / 'review_questions.csv')) == 3


def test_fr06_invalid_graph_schema_checks():
    graph = Graph()
    graph.add((URIRef('urn:a'), RDF.type, BRICK.Invented_Class))
    graph.add((URIRef('urn:a'), BRICK.feeds, URIRef('urn:b')))
    assert validate_graph(graph)['supported_subset'] == 'failed'
    assert len(validate_graph(graph)['errors']) == 3


def test_fr04_status_enum_and_extra_fields_rejected():
    with pytest.raises(ValidationError):
        Assertion(assertion_id='a', subject_id='b', predicate='feeds', object_id='c',
                  supporting_source_ids=[], inference_method='rule', explanation='x',
                  status='confident', validation_results=[], run_id='r')
    with pytest.raises(ValidationError):
        Proposals.model_validate({'points': [], 'relationships': [], 'rdf': 'arbitrary'})


def fake_response(proposals, **extra):
    return json.dumps({'model': 'mock-snapshot', 'choices': [{'finish_reason': 'stop',
        'message': {'content': json.dumps(proposals)}}], **extra})


def transport(raw):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = raw.encode()
    return response


def test_fr02_live_adapter_mock_request_and_typed_response(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret-never-persist')
    records, _, _ = ingest(DEMO)
    payload = {'points': [{'source_id': 'points.csv:2', 'point_class': 'Supply_Air_Temperature_Sensor',
                          'equipment_id': 'AHU-1', 'explanation': 'Supplied description and equipment ID.'}],
               'relationships': []}
    with patch('ontology_engine.providers.urlopen', return_value=transport(fake_response(payload))) as call:
        result, version, request = openai(records, 'configured-snapshot', True, tmp_path / 'response.json')
    assert result.points[0].source_id == 'points.csv:2'
    assert version == 'mock-snapshot'
    sent = json.loads(call.call_args.args[0].data)
    assert sent['model'] == 'configured-snapshot'
    assert sent['response_format']['json_schema']['strict'] is True
    assert 'test-secret' not in json.dumps(request)
    assert call.call_args.kwargs['timeout'] == 60


def test_fr02_external_opt_in_and_credentials(tmp_path, monkeypatch):
    with patch('ontology_engine.providers.urlopen') as call:
        with pytest.raises(ValueError, match='authorization'):
            openai([], 'model', False, tmp_path / 'response.json')
        monkeypatch.delenv('OPENAI_API_KEY', raising=False)
        with pytest.raises(ValueError, match='OPENAI_API_KEY'):
            openai([], 'model', True, tmp_path / 'response.json')
        call.assert_not_called()


@pytest.mark.parametrize('raw', ['not json', fake_response({'points': [{'rdf': 'injection'}], 'relationships': []}),
                               json.dumps({'choices': [{'finish_reason': 'length', 'message': {}}]})])
def test_fr02_invalid_live_response_preserved_and_fails_closed(tmp_path, monkeypatch, raw):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    with patch('ontology_engine.providers.urlopen', return_value=transport(raw)):
        with pytest.raises(ValueError, match='Provider failed'):
            run(DEMO, tmp_path / 'out', 'openai', 'mock', True)
    assert (tmp_path / 'out/provider_response.json').read_text() == raw
    assert (tmp_path / 'out/failure.json').exists()
    assert not (tmp_path / 'out/ontology.ttl').exists()
    assert (tmp_path / 'out/source_records.jsonl').exists()


def test_fr02_model_cannot_invent_accepted_links_or_classes(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    proposals = {'points': [{'source_id': 'points.csv:9', 'point_class': 'Invented_Class',
                            'equipment_id': 'GHOST', 'explanation': 'Unsupported guess.'},
                           {'source_id': 'nonexistent', 'point_class': 'Point', 'equipment_id': '', 'explanation': 'Bad source'}],
                 'relationships': [{'subject_id': 'equipment/AHU-1', 'predicate': 'feeds',
                                    'object_id': 'equipment/GHOST', 'supporting_source_ids': ['nonexistent'],
                                    'explanation': 'Unsupported topology.'}]}
    with patch('ontology_engine.providers.urlopen', return_value=transport(fake_response(proposals))):
        run(DEMO, tmp_path / 'out', 'openai', 'mock', True)
    graph = Graph().parse(tmp_path / 'out/ontology.ttl')
    assert not any('GHOST' in str(term) or 'Invented_Class' in str(term) for triple in graph for term in triple)
    assert len(list(graph.subject_objects(BRICK.feeds))) == 2
    assert any(r['rule_or_detection_method'] == 'invalid_proposal_source' for r in rows(tmp_path / 'out/issues.csv'))
    assert json.loads((tmp_path / 'out/run_manifest.json').read_text())['model_version'] == 'mock-snapshot'


def test_fr11_preserve_outputs_and_inputs(tmp_path):
    run(DEMO, tmp_path / 'out')
    with pytest.raises(ValueError, match='empty'):
        run(DEMO, tmp_path / 'out')
    with pytest.raises(ValueError, match='non-nested'):
        run(DEMO, DEMO / 'out')
    with pytest.raises(ValueError, match='No supported'):
        run(tmp_path, tmp_path.parent / 'unused-ontology-output')


def test_fr12_cli(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'ontology_engine', 'run', '--input', str(SECOND),
                             '--output', str(tmp_path / 'out'), '--llm-provider', 'fixture'],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'SYNTHETIC / FOR TESTING ONLY' in result.stdout
    invalid = subprocess.run([sys.executable, '-m', 'ontology_engine', 'run', '--input', str(DEMO),
                              '--output', str(tmp_path / 'remote'), '--llm-provider', 'openai'],
                             cwd=ROOT, capture_output=True, text=True)
    assert invalid.returncode == 1 and 'authorization' in invalid.stderr


def test_synthetic_label_survives_production_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    with patch('ontology_engine.providers.urlopen', return_value=transport(fake_response({'points': [], 'relationships': []}))):
        manifest = run(SECOND, tmp_path / 'out', 'openai', 'mock', True)
    assert manifest.synthetic
    assert 'SYNTHETIC.txt' in manifest.input_hashes
    assert 'SYNTHETIC / FOR TESTING ONLY' in (tmp_path / 'out/README.md').read_text()
    ledger = rows(tmp_path / 'out/relationships.csv')
    assert all(a['predicate_or_class'] == a['predicate'] and a['object_or_value'] == a['object_id'] for a in ledger)


def test_fr03_fr04_fr12_relationship_csv_uses_canonical_ids_with_stable_rdf(tmp_path):
    """CSV entity IDs match input IDs; internal scope prefixes belong to RDF mapping."""
    data = tmp_path / 'input'
    data.mkdir()
    upstream, downstream, point = 'Pump / α', 'equipment/terminal-77', 'point/reading-9'
    (data / 'equipment.csv').write_text(
        f'equipment_id,equipment_type\n{upstream},Pump\n{downstream},Heat Exchanger\n', encoding='utf-8')
    (data / 'points.csv').write_text(
        f'point_id,description,equipment_id,units\n{point},Pressure Reading,{upstream},kPa\n', encoding='utf-8')
    (data / 'connections.csv').write_text(
        f'from_equipment,to_equipment,source_description\n{upstream},{downstream},Documented schedule\n', encoding='utf-8')
    out, repeated = tmp_path / 'out', tmp_path / 'repeated'
    manifest = run(data, out)
    run(data, repeated)
    ledger = rows(out / 'relationships.csv')
    feeds = [a for a in ledger if a['predicate'] == 'feeds']
    assert {(a['subject_id'], a['object_id']) for a in feeds} == {(upstream, downstream)}
    feed = feeds[0]
    assert feed['status'] == 'supported_inference'
    assert json.loads(feed['supporting_source_ids']) == ['connections.csv:2']
    assert feed['explanation'] == 'Documented schedule'
    assert feed['object_or_value'] == downstream
    assert feed['subject_entity_id'] == 'equipment/' + upstream
    assert feed['object_entity_id'] == 'equipment/' + downstream
    assert {(a['subject_id'], a['object_id']) for a in ledger if a['predicate'] == 'hasPoint'} == {(upstream, point)}
    assert any(a['subject_id'] == upstream and a['object_id'] == 'Pump' for a in ledger if a['predicate'] == 'type')
    graph = Graph().parse(out / 'ontology.ttl')
    ns = manifest.settings['namespace']
    assert (entity_iri(ns, 'equipment/' + upstream), BRICK.feeds,
            entity_iri(ns, 'equipment/' + downstream)) in graph
    assert (entity_iri(ns, feed['subject_entity_id']), BRICK.feeds,
            entity_iri(ns, feed['object_entity_id'])) in graph
    assert (entity_iri(ns, 'equipment/' + upstream), BRICK.hasPoint,
            entity_iri(ns, 'point/' + point)) in graph
    assert set(graph) == set(Graph().parse(repeated / 'ontology.ttl'))
