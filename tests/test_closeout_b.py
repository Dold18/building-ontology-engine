"""TASK_002-R B: evidence eligibility, canonical publication, audit P5/P7."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from rdflib import Graph, RDF, URIRef
from ontology_engine.pipeline import run
from ontology_engine.evidence import eligibility
from ontology_engine.ingest import ingest
from ontology_engine.publication import verify_package, finalize
from ontology_engine.vocabulary import BRICK
from test_closeout_a import dataset


def mocked(points, equipment=None, relationships=None):
    body = {'equipment': equipment or [], 'points': points, 'relationships': relationships or []}
    response = MagicMock(); response.__enter__.return_value.read.return_value = json.dumps({'model': 'MOCKED',
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(body)}}]}).encode()
    return response


@pytest.mark.parametrize('case', ['empty', 'vague', 'wrong-file', 'wrong-point', 'conflict'])
def test_unsupported_or_irrelevant_classification_never_promoted(tmp_path, monkeypatch, case):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    if case == 'vague':
        (data / 'points.csv').write_text('point_id,description,equipment_id,units\np,Unclear,E,\n')
    p = {'source_id': 'points.csv:2', 'point_class': 'Supply_Air_Temperature_Sensor', 'equipment_id': 'E', 'explanation': 'Unsubstantiated guess'}
    if case == 'empty': p.update(point_class='Pressure_Sensor', explanation='')
    if case == 'wrong-file': p['source_id'] = 'equipment.csv:2'
    if case == 'wrong-point':
        (data / 'points.csv').write_text('point_id,description,equipment_id,units\np,Pressure Reading,E,Pa\nother,Unclear,,\n')
        p['source_id'] = 'points.csv:3'
    with patch('ontology_engine.providers.urlopen', return_value=mocked([p])):
        run(data, tmp_path / 'out', 'openai', 'MOCKED', True)
    graph = Graph().parse(tmp_path / 'out/ontology.ttl')
    assert not list(graph.subjects(RDF.type, BRICK.Supply_Air_Temperature_Sensor))
    if case == 'empty': assert not list(graph.subjects(RDF.type, BRICK.Pressure_Sensor))
    observations = json.loads((tmp_path / 'out/proposal_observations.json').read_text())
    assert observations[0]['proposal'] == p
    assert observations[0]['original_source_ids'] == [p['source_id']]


def test_direction_and_source_authority_are_checked(tmp_path):
    data = dataset(tmp_path / 'input')
    (data / 'connections.csv').write_text('from_equipment,to_equipment\nE,F\n')
    records, _, _ = ingest(data)
    sources = {r.source_id: r for r in records}
    forward = eligibility('equipment/E', 'feeds', 'equipment/F', ['connections.csv:2'], sources, 'direct_document', 'Explicit schedule row')
    reverse = eligibility('equipment/F', 'feeds', 'equipment/E', ['connections.csv:2'], sources, 'direct_document', 'Wrong direction')
    irrelevant = eligibility('equipment/E', 'feeds', 'equipment/F', ['points.csv:2'], sources, 'llm', 'Names look related')
    assert forward['eligible'] and not forward['can_verify']
    assert not reverse['eligible'] and not irrelevant['eligible']
    assert 'DIRECTION_OR_CONNECTION_NOT_DOCUMENTED' in reverse['reason_codes']


def test_original_invalid_citations_are_preserved(tmp_path, monkeypatch):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    relation = {'subject_id': 'equipment/E', 'predicate': 'feeds', 'object_id': 'equipment/GHOST',
                'supporting_source_ids': ['points.csv:2', 'no-such-source'], 'explanation': 'Unsupported hypothesis'}
    with patch('ontology_engine.providers.urlopen', return_value=mocked([], relationships=[relation])):
        run(data, tmp_path / 'out', 'openai', 'MOCKED', True)
    candidate = next(a for a in json.loads((tmp_path / 'out/candidate_assertions.json').read_text()) if a['predicate'] == 'feeds')
    assert candidate['original_source_ids'] == relation['supporting_source_ids']
    assert candidate['supporting_source_ids'] == ['points.csv:2']
    assert not candidate['eligibility']['eligible']


@pytest.mark.parametrize('boundary', ['ontology.ttl', 'classifications.csv', 'relationships.csv', 'issues.csv',
    'review_questions.csv', 'assertions.json', 'validation.json', 'README.md', 'run_manifest.json', 'COMPLETED.json', 'publication'])
def test_export_fault_never_publishes_partial_or_destroys_good_output(tmp_path, boundary):
    data = dataset(tmp_path / 'input')
    run(data, tmp_path / 'good')
    before = (tmp_path / 'good/ontology.ttl').read_bytes()
    target = tmp_path / 'failed'
    original_open, original_write = Path.open, Path.write_text
    original_serialize = Graph.serialize
    fired = [False]
    def fail_once(name):
        if name == boundary and not fired[0]: fired[0] = True; raise OSError('SYNTHETIC write fault')
    def file_open(path, *args, **kwargs):
        if args and 'w' in args[0]: fail_once(path.name)
        return original_open(path, *args, **kwargs)
    def write(path, *args, **kwargs):
        fail_once(path.name)
        return original_write(path, *args, **kwargs)
    def serialize(graph, *args, **kwargs):
        dest = kwargs.get('destination')
        if dest: fail_once(Path(dest).name)
        return original_serialize(graph, *args, **kwargs)
    with patch.object(Path, 'open', file_open), patch.object(Path, 'write_text', write), patch.object(Graph, 'serialize', serialize), patch(
            'ontology_engine.publication.os.rename', side_effect=OSError('SYNTHETIC publication fault')) if boundary == 'publication' else patch(
            'ontology_engine.publication.os.rename', wraps=__import__('os').rename):
        with pytest.raises(OSError): run(data, target)
    assert not target.exists()
    assert (tmp_path / 'good/ontology.ttl').read_bytes() == before
    verify_package(tmp_path / 'good')
    assert list(tmp_path.glob('.failed.staging-*/failure.json'))


def test_full_package_hash_and_exact_graph_ledger_verification(tmp_path):
    data = dataset(tmp_path / 'input'); out = tmp_path / 'out'; run(data, out)
    m = verify_package(out)
    assert m['completion_status'] == 'COMPLETED' and m['schema_version']
    assert m['output_hashes']['ontology.ttl']
    graph = Graph().parse(out / 'ontology.ttl')
    graph.add((URIRef('urn:extra'), RDF.type, BRICK.Pump))
    graph.serialize(destination=str(out / 'ontology.ttl'), format='turtle')
    with pytest.raises(ValueError, match='GRAPH_LEDGER_MISMATCH'): verify_package(out)


def test_canonical_csv_or_extra_file_tampering_detected(tmp_path):
    data = dataset(tmp_path / 'input'); out = tmp_path / 'out'; run(data, out)
    (out / 'unexpected.txt').write_text('extra')
    with pytest.raises(ValueError, match='ARTIFACT_SET_MISMATCH'): verify_package(out)
