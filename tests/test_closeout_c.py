"""TASK_002-R C: durable restart, budgets, no charged replay, privacy. SYNTHETIC."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile, ZIP_DEFLATED
from io import BytesIO
import pytest
from openpyxl import Workbook
from ontology_engine.pipeline import run, resume
from ontology_engine.publication import verify_package
from ontology_engine.errors import EngineError, retry_local
from ontology_engine.limits import Limits
from ontology_engine.sources import snapshot_input
from ontology_engine.state import RunStore
from ontology_engine.reference_converter import convert_reference
from test_closeout_a import dataset

ROOT = Path(__file__).resolve().parents[1]
CHILD = '''
import os,sys,json
from unittest.mock import MagicMock,patch
from ontology_engine import runtime
from ontology_engine.pipeline import run
stage=sys.argv[3]
def hook(name):
    if name==stage: os._exit(77)
runtime.checkpoint_hook=hook
if sys.argv[4]=='openai':
    raw={'model':'MOCKED','choices':[{'finish_reason':'stop','message':{'content':json.dumps({'equipment':[],'points':[],'relationships':[]})}}]}
    response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps(raw).encode()
    with patch.dict('os.environ',{'OPENAI_API_KEY':'fake'}),patch('ontology_engine.providers.urlopen',return_value=response):
        run(sys.argv[1],sys.argv[2],'openai','MOCKED',True)
else: run(sys.argv[1],sys.argv[2])
'''


def crash(data, output, stage, provider='fixture'):
    result = subprocess.run([sys.executable, '-c', CHILD, str(data), str(output), stage, provider],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 77, result.stderr


@pytest.mark.parametrize('stage', ['before_snapshot', 'snapshot', 'before_ingest', 'normalized', 'proposals', 'before_publication', 'after_publication'])
def test_process_restart_from_committed_stages(tmp_path, stage):
    data = dataset(tmp_path / 'input'); out = tmp_path / 'out'
    crash(data, out, stage)
    with patch('ontology_engine.providers.urlopen', side_effect=AssertionError('Never call network')):
        recovered = resume(out)
        completed = resume(out)
    assert recovered.run_id == completed.run_id
    verify_package(out)
    observations = json.loads((out / 'assertions.json').read_text())
    assert len(observations) == len({a['observation_uid'] for a in observations})
    assert (out / 'sources/points.csv').read_bytes() == (data / 'points.csv').read_bytes()
    db = sqlite3.connect(tmp_path / '.out.run/state.sqlite')
    assert db.execute('SELECT state FROM runs').fetchone()[0] == 'COMPLETED'
    assert db.execute('SELECT COUNT(*) FROM stages WHERE name="normalized"').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM events WHERE duration > 0').fetchone()[0] > 0
    db.close()


def test_persisted_response_resumes_without_model_request(tmp_path):
    data = dataset(tmp_path / 'input'); out = tmp_path / 'out'
    crash(data, out, 'response', 'openai')
    with patch('ontology_engine.providers.urlopen', side_effect=AssertionError('Do not replay')) as call:
        result = resume(out)
        call.assert_not_called()
    assert result.execution_origin == 'mocked'
    verify_package(out)


def test_inflight_or_timeout_model_request_never_replayed(tmp_path, monkeypatch):
    data = dataset(tmp_path / 'input'); out = tmp_path / 'out'
    crash(data, out, 'request_started', 'openai')
    with patch('ontology_engine.providers.urlopen') as call:
        with pytest.raises(ValueError, match='Provider failed'): resume(out)
        call.assert_not_called()
    failure = json.loads((tmp_path / '.out.run/failure.json').read_text())
    assert failure['error_code'] == 'EXTERNAL_OUTCOME_UNKNOWN'
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    with patch('ontology_engine.providers.urlopen', side_effect=TimeoutError('SYNTHETIC timeout')) as call:
        with pytest.raises(ValueError): run(data, tmp_path / 'timeout', 'openai', 'MOCKED', True)
        assert call.call_count == 1
    with patch('ontology_engine.providers.urlopen') as call:
        with pytest.raises(ValueError): resume(tmp_path / 'timeout')
        call.assert_not_called()


@pytest.mark.parametrize('mutation', ['source', 'checkpoint', 'code', 'model'])
def test_stale_checkpoints_fail_closed(tmp_path, mutation):
    data = dataset(tmp_path / 'input'); out = tmp_path / 'out'
    crash(data, out, 'normalized')
    if mutation == 'source': (data / 'points.csv').write_text('point_id\nchanged\n')
    if mutation == 'checkpoint': (tmp_path / '.out.run/records.json').write_text('[]')
    if mutation == 'model':
        db = sqlite3.connect(tmp_path / '.out.run/state.sqlite')
        args = json.loads(db.execute('SELECT arguments FROM runs').fetchone()[0]); args['model'] = 'different'
        db.execute('UPDATE runs SET arguments=?', (json.dumps(args),)); db.commit(); db.close()
    with patch('ontology_engine.runtime.fingerprint', return_value='changed') if mutation == 'code' else patch(
            'ontology_engine.providers.urlopen', side_effect=AssertionError('Never call')):
        with pytest.raises(EngineError, match='CHECKPOINT_'): resume(out)
    assert not out.exists()


def test_invalid_transitions_and_duplicate_commits_are_rejected(tmp_path):
    store = RunStore(tmp_path / 'state.sqlite'); store.create('r', 'fingerprint', {})
    with pytest.raises(EngineError, match='INVALID_TRANSITION'): store.transition('r', 'COMPLETED')
    store.transition('r', 'RUNNING')
    file = tmp_path / 'artifact'; file.write_text('data')
    store.commit_stage('r', 'stage', [file])
    with pytest.raises(EngineError, match='DUPLICATE_STAGE_COMMIT'): store.commit_stage('r', 'stage', [file])
    store.transition('r', 'COMPLETED')
    with pytest.raises(EngineError, match='INVALID_TRANSITION'): store.transition('r', 'RUNNING')
    store.close()


def test_local_retry_is_bounded_and_has_no_external_fallback():
    calls = []
    def local():
        calls.append(1)
        if len(calls) < 3: raise OSError('transient')
        return 'ok'
    assert retry_local(local) == 'ok' and len(calls) == 3
    with pytest.raises(EngineError) as error: retry_local(lambda: (_ for _ in ()).throw(OSError('persistent')))
    assert error.value.code == 'LOCAL_RETRY_EXHAUSTED' and error.value.attempt_count == 3


@pytest.mark.parametrize('budget', [{'max_files': 1}, {'max_file_bytes': 10}, {'max_total_bytes': 20}, {'max_rows': 1}, {'max_cells': 2}])
def test_input_limits_preserve_failure_state(tmp_path, budget):
    data = dataset(tmp_path / 'input')
    with pytest.raises(EngineError, match='BUDGET_EXCEEDED'): run(data, tmp_path / 'out', limits=Limits(**budget))
    failure = json.loads((tmp_path / '.out.run/failure.json').read_text())
    assert failure['state'] == 'BLOCKED' and failure['error_code'] == 'BUDGET_EXCEEDED'
    assert not (tmp_path / 'out/COMPLETED.json').exists()


def test_declared_workbook_dimensions_rejected_before_openpyxl_iteration(tmp_path):
    data = tmp_path / 'input'; data.mkdir()
    wb = Workbook(); wb.active.append(['point_id']); wb.active.append(['p'])
    buffer = BytesIO(); wb.save(buffer)
    with ZipFile(BytesIO(buffer.getvalue())) as original, ZipFile(data / 'points.xlsx', 'w', ZIP_DEFLATED) as target:
        for name in original.namelist():
            body = original.read(name)
            if name == 'xl/worksheets/sheet1.xml': body = body.replace(b'ref="A1:A2"', b'ref="A1:XFD1048576"')
            target.writestr(name, body)
    with patch('ontology_engine.ingest.load_workbook') as loader:
        with pytest.raises(EngineError, match='dimensions'): run(data, tmp_path / 'out')
        loader.assert_not_called()


@pytest.mark.parametrize('budget,code', [({'max_requests': 0}, 'BUDGET_EXCEEDED'),
    ({'max_context_bytes': 10}, 'BUDGET_EXCEEDED'), ({'max_input_tokens': 10}, 'BUDGET_EXCEEDED'),
    ({'max_cost_usd': 0.01}, 'COST_NOT_EVALUABLE'),
    ({'max_cost_usd': 0.0, 'input_usd_per_million': 1.0, 'output_usd_per_million': 1.0}, 'BUDGET_EXCEEDED')])
def test_request_budgets_refuse_before_transmission(tmp_path, monkeypatch, budget, code):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    with patch('ontology_engine.providers.urlopen') as call:
        with pytest.raises(ValueError): run(data, tmp_path / 'out', 'openai', 'MOCKED', True, limits=Limits(**budget))
        call.assert_not_called()
    assert json.loads((tmp_path / '.out.run/failure.json').read_text())['error_code'] == code


def test_snapshot_approval_is_not_reusable_for_other_sources_or_models(tmp_path):
    from ontology_engine.security import Approval
    from ontology_engine.ingest import ingest
    data = dataset(tmp_path / 'input'); records, hashes, _ = ingest(data)
    approval = Approval('snapshot', 'openai', 'model-A', tuple(hashes.values()))
    assert approval.allows(records, 'model-A')
    assert not approval.allows(records, 'model-B')
    records[0].source_hash = 'different'
    assert not approval.allows(records, 'model-A')


def test_nested_response_credentials_private_origin_and_shareable_exports(tmp_path, monkeypatch):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    raw = {'model': 'MOCKED', 'debug': {'password': 'dummy-password-sentinel'},
           'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'equipment': [], 'points': [], 'relationships': []})}}]}
    response = MagicMock(); response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
    with patch('ontology_engine.providers.urlopen', return_value=response): result = run(data, tmp_path / 'out', 'openai', 'MOCKED', True)
    assert result.execution_origin == 'mocked' and result.data_origin == 'unknown'
    assert 'dummy-password-sentinel' not in (tmp_path / 'out/provider_response.json').read_text()
    assert 'dummy-password-sentinel' in (tmp_path / '.out.run/provider/.restricted/response.txt').read_text()
    for path in (tmp_path / 'out/shareable').rglob('*'):
        if path.is_file(): assert 'dummy-password-sentinel' not in path.read_text()
    assert json.loads((tmp_path / 'out/run_manifest.json').read_text())['execution_origin'] == 'mocked'
    assert os.stat(tmp_path / 'out/sources').st_mode & 0o777 == 0o700
    assert os.stat(tmp_path / 'out/source_records.jsonl').st_mode & 0o777 == 0o600
    verify_package(tmp_path / 'out')


def test_registered_public_source_tampering_refuses_derivation(tmp_path):
    registry = json.loads((ROOT / 'datasets/public_sources.json').read_text())['sources']
    entry = next(s for s in registry if s['kind'] == 'reference_graph')
    tampered = tmp_path / 'bad.ttl'; tampered.write_text('<urn:s> <urn:p> <urn:o> .')
    with pytest.raises(ValueError, match='PUBLIC_SOURCE_HASH_MISMATCH'):
        convert_reference(tampered, tmp_path / 'derived', entry['url'], entry['license'])
    assert not (tmp_path / 'derived').exists()


def test_response_size_limit_records_blocked_attempt(tmp_path, monkeypatch):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    response = MagicMock(); response.__enter__.return_value.read.return_value = b'x' * 100
    with patch('ontology_engine.providers.urlopen', return_value=response) as call:
        with pytest.raises(ValueError): run(data, tmp_path / 'out', 'openai', 'MOCKED', True, limits=Limits(max_response_bytes=20))
        assert call.call_count == 1
    assert json.loads((tmp_path / '.out.run/failure.json').read_text())['error_code'] == 'BUDGET_EXCEEDED'


def test_worksheet_and_expanded_archive_limits(tmp_path):
    data = tmp_path / 'input'; data.mkdir()
    wb = Workbook(); wb.active.append(['point_id']); wb.active.append(['p']); wb.create_sheet('second').append(['point_id'])
    wb.save(data / 'points.xlsx')
    for name, limits in [('sheets', Limits(max_worksheets=1)), ('expanded', Limits(max_expanded_bytes=100))]:
        with pytest.raises(EngineError, match='BUDGET_EXCEEDED'): run(data, tmp_path / name, limits=limits)


def test_malformed_credential_response_is_redacted_and_not_replayed(tmp_path, monkeypatch):
    data = dataset(tmp_path / 'input'); monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    response = MagicMock(); response.__enter__.return_value.read.return_value = b'broken response password=dummy-private-value'
    with patch('ontology_engine.providers.urlopen', return_value=response):
        with pytest.raises(ValueError): run(data, tmp_path / 'out', 'openai', 'MOCKED', True)
    assert 'dummy-private-value' not in (tmp_path / 'out/provider_response.json').read_text()
    with patch('ontology_engine.providers.urlopen') as call:
        with pytest.raises(ValueError): resume(tmp_path / 'out')
        call.assert_not_called()


def test_unresolved_public_license_cannot_be_overridden_by_cli_text(tmp_path, monkeypatch):
    """Exercise the registry's license override offline with invented RDF.

    This is a synthetic regression fixture, not any third-party or sponsor asset.
    """
    import hashlib
    import ontology_engine.reference_converter as converter

    source = tmp_path / 'synthetic_reference.ttl'
    source.write_text(
        '@prefix brick: <https://brickschema.org/schema/Brick#> .\n'
        '<urn:synthetic:ahu> brick:hasPoint <urn:synthetic:temp> .\n',
        encoding='utf-8',
    )
    registry_root = tmp_path / 'synthetic_package'
    (registry_root / 'datasets').mkdir(parents=True)
    (registry_root / 'ontology_engine').mkdir()
    source_url = 'https://example.invalid/synthetic-regression-graph'
    (registry_root / 'datasets' / 'public_sources.json').write_text(json.dumps({
        'sources': [{
            'url': source_url,
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'license': 'unconfirmed synthetic regression status',
        }]
    }), encoding='utf-8')
    # The converter reads a registry next to its module; bind only this test to
    # a temporary, independently authored registry without mutating project files.
    monkeypatch.setattr(converter, '__file__', str(registry_root / 'ontology_engine' / 'reference_converter.py'))
    result = converter.convert_reference(source, tmp_path / 'derived', source_url, 'CC BY 4.0')
    assert not result['redistribution_allowed']
    assert 'unconfirmed' in result['license_note']
