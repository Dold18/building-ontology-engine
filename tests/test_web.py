"""FR-01–08/11/12, D1–D5/D7/D8: local UI uses the accepted backend."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import zipfile

import pytest
from rdflib import Graph
from ontology_engine.pipeline import run
from ontology_engine.publication import verify_package
from ontology_engine.review import files_snapshot
from ontology_engine.web import Application, LocalServer, contained, MAX_UPLOAD
from ontology_engine.web_graph import graph_view, MAX_NODES, MAX_EDGES

PASSWORD = 'synthetic-browser-test-password-only'
ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, server):
        self.server = server; self.cookie = ''; self.csrf = ''
    def call(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=90)
        h = {'Origin': self.server.origin, 'Cookie': self.cookie, 'X-CSRF-Token': self.csrf}
        raw = None
        if body is not None: raw = json.dumps(body); h['Content-Type'] = 'application/json'
        h.update(headers or {})
        conn.request('POST' if body is not None else 'GET', path, raw, h)
        res = conn.getresponse(); data = res.read(); response_headers = dict(res.getheaders()); code = res.status
        if 'Set-Cookie' in response_headers: self.cookie = response_headers['Set-Cookie'].split(';')[0]
        conn.close()
        return code, json.loads(data) if response_headers.get('Content-Type') == 'application/json' else data, response_headers
    def login(self):
        code, value, headers = self.call('/api/login', {'password': PASSWORD})
        assert code == 200; self.csrf = value['csrf']; return headers
    def ok(self, path, body=None):
        code, value, _ = self.call(path, body); assert code == 200, value; return value


@pytest.fixture(scope='module')
def parent(tmp_path_factory):
    dest = tmp_path_factory.mktemp('web-parent') / 'parent'
    run(ROOT/'fixtures/review_shared_cause', dest, building_id='SYNTHETIC:web-shared')
    return dest


@pytest.fixture
def app(tmp_path, parent):
    a = Application(tmp_path/'app'); shutil.copytree(parent, a.runs/'parent')
    yield a
    a.close()


@pytest.fixture
def server(app):
    s = LocalServer(app, PASSWORD, 0); thread = threading.Thread(target=s.serve_forever, daemon=True); thread.start()
    yield s
    s.shutdown(); s.server_close(); thread.join()


@pytest.fixture
def client(server):
    c = Client(server); c.login(); return c


def request_for(app):
    service, credential = app.service('parent', create=True)
    w = service.workspace
    g = next(g for g in service.groups()['items'] if g['cause_kind']=='naming_prefix')
    claims = [c for c in g['claims'] if c['predicate']=='type' and c['object_id']=='unknown']
    action = dict(schema_version='review-v2', workspace_id=w.workspace_id, site_scope=w.site_scope,
        parent_fingerprint=w.parent_fingerprint, actor_id=w.reviewer.actor_id, action_id='browser-alias-1',
        correlation_id='browser-investigation', expected_revision=0, target_kind='group', target_id=g['group_id'],
        action='correct', requested_status='needs_review', reason='SYNTHETIC supplied channel schedule documents these exact selected classes.',
        evidence_uids=[s['source_record_uid'] for s in g['sources']], affected_claim_uids=[c['claim_uid'] for c in claims],
        correction=dict(kind='naming_alias', alias='XQ', clarification='SYNTHETIC documentary interpretation; not professional verification.',
            edits=[dict(subject_id=c['subject_id'], predicate='type', object_id='Supply_Air_Temperature_Sensor') for c in claims]))
    return action, service, credential


def wait_job(app, response):
    deadline = time.monotonic()+90
    while time.monotonic()<deadline:
        value = app.job(response['job_id'])
        if value['state'] not in ('queued','running'): return value
        time.sleep(.02)
    raise AssertionError('job timeout')


def test_ui_api_full_workflow_and_actual_cli_semantic_equivalence(client, app, tmp_path):
    before = files_snapshot(app.runs/'parent')
    assert client.ok('/api/runs')[0]['state']=='COMPLETED'
    view = client.ok('/api/view?run=parent')
    assert view['graph']['nodes'] and view['graph']['edges']
    assert client.ok('/api/groups?run=parent')['items']
    c = view['graph']['nodes'][0]['claim_ids'][0]
    evidence = client.ok('/api/inspect?run=parent&kind=claim&id='+c)
    assert evidence['sources'][0]['raw_value'] and evidence['engineering_validation']
    assert evidence['workspace'] is None  # Read-only evidence is available before enrollment.
    public = client.ok('/api/enroll', {'run':'parent'})
    assert 'credential_sha256' not in public and 'parent_path' not in public
    action, service, credential = request_for(app)
    preview = client.ok('/api/preview', {'run':'parent','action':action})
    assert len(preview['changes']['added_triples'])==5 and len(preview['changes']['removed_claims'])==5
    assert preview['outcomes'][0]['outcome']=='applied_supported_inference'
    assert service.history()['revision']==0
    event = client.ok('/api/decide', {'run':'parent','action':action})
    assert event['outcome']=='pending' and event['graph_consequence']=='not_applied'
    assert client.ok('/api/decide', {'run':'parent','action':action})==event
    response = client.ok('/api/export', {'run':'parent','revision':1})
    job = wait_job(app, response); assert job['state']=='completed', job
    name = job['result']['run']; out = app.runs/name
    verify_package(out)
    assert files_snapshot(app.runs/'parent')==before
    after = client.ok('/api/view?run='+name)
    assert after['after_shacl']['full_brick_shacl']=='passed'
    assert any(n['affected'] for n in after['graph']['nodes'])
    assert after['changes']==preview['changes']
    status, archive, _ = client.call('/api/download?run='+name+'&artifact=package.zip')
    assert status==200
    downloaded=tmp_path/'downloaded'
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        assert not any('credential' in n or n.endswith('.sqlite') for n in z.namelist())
        assert all(credential.read_bytes().strip() not in z.read(n) for n in z.namelist())
        z.extractall(downloaded)
    verify_package(downloaded)
    assert (downloaded/'review_changes.json').exists() and (downloaded/'source_records.jsonl').exists()
    cli = tmp_path/'cli-reviewed'
    result = subprocess.run([sys.executable,'-m','ontology_engine','review','export','--workspace',str(service.root),
        '--credential-file',str(credential),'--expected-revision','1','--output',str(cli)],capture_output=True,text=True)
    assert result.returncode==0, result.stderr+result.stdout
    assert set(Graph().parse(out/'ontology.ttl'))==set(Graph().parse(cli/'ontology.ttl'))
    # Initial UI and CLI analysis also have identical accepted RDF on equivalent inputs.
    demo = wait_job(app, client.ok('/api/analyze', {'demo':'shared'}))
    assert demo['state']=='completed'
    assert set(Graph().parse(app.runs/demo['result']['run']/'ontology.ttl'))==set(Graph().parse(app.runs/'parent'/'ontology.ttl'))


def test_preview_withheld_and_explicit_supersession(client, app):
    a,s,_=request_for(app); a['correction']['alias']='UNSUPPORTED'
    preview=client.ok('/api/preview',{'run':'parent','action':a})
    assert preview['outcomes'][0]['outcome']=='pending' and not preview['changes']['added_triples']
    first=client.ok('/api/decide',{'run':'parent','action':a})
    b=dict(a,action_id='replacement',expected_revision=1)
    assert client.call('/api/decide',{'run':'parent','action':b})[0]==400
    b['supersedes_event_id']=first['event_id']; b['correction']=dict(a['correction'],alias='XQ')
    assert client.ok('/api/preview',{'run':'parent','action':b})['changes']['added_triples']
    client.ok('/api/decide',{'run':'parent','action':b})
    assert len(client.ok('/api/history?run=parent')['events'])==2


def test_concurrent_revisions_fail_closed(client, app, server):
    a,s,_=request_for(app)
    def send(action):
        c=Client(server); c.cookie=client.cookie; c.csrf=client.csrf
        return c.call('/api/decide',{'run':'parent','action':action})[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(send,[a,dict(a,action_id='racing-action')]))
    assert sorted(codes)==[200,400] and s.history()['revision']==1
    assert client.call('/api/export',{'run':'parent','revision':0})[0]==200  # asynchronous failure is visible
    j=next(reversed(app.jobs)); assert wait_job(app,{'job_id':j})['state']=='failed'


def test_unauthorized_verification_and_csrf_host_session_guards(client, server, app):
    assert Client(server).call('/api/runs')[0]==403
    assert Client(server).call('/api/decide',{})[0]==403
    assert client.call('/api/upload',{},headers={'X-CSRF-Token':'wrong'})[0]==403
    assert client.call('/api/upload',{},headers={'Origin':'http://evil.test'})[0]==403
    assert client.call('/api/runs',headers={'Host':'evil.test'})[0]==403
    assert client.call('/api/runs',headers={'Sec-Fetch-Site':'cross-site'})[0]==403
    a,s,_=request_for(app); a.update(action='accept',requested_status='verified',correction=None)
    assert client.call('/api/decide',{'run':'parent','action':a})[0]==400
    assert s.history()['revision']==0
    with server.auth_lock:
        for session in server.sessions.values(): session['expires']=0
    assert client.call('/api/runs')[0]==403


def test_login_password_and_cookie_are_not_exposed(server):
    c=Client(server)
    assert c.call('/api/login',{'password':'bad'})[0]==403
    headers=c.login()
    assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    assert server.server_address[0]=='127.0.0.1'
    for path in ('/','/app.js','/style.css','/api/runs'):
        code,value,h=c.call(path); assert code==200
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        assert PASSWORD.encode() not in raw and c.cookie.encode() not in raw
        assert h['Cache-Control']=='no-store' and "frame-ancestors 'none'" in h['Content-Security-Policy']
    c.ok('/api/logout',{})
    assert c.call('/api/runs')[0]==403


@pytest.mark.parametrize('name,data',[
    ('../escape.csv',b'a'),('/tmp/escape.csv',b'a'),('x.exe',b'a'),('x.csv',b'\x00'),
    ('x.xlsx',b'not-a-workbook'),('x.csv',b'x'*(MAX_UPLOAD+1)),('x.csv',b'')],
    ids=['traversal','absolute','extension','binary','bad-workbook','oversize','empty'])
def test_invalid_uploads_refused(client,name,data):
    assert client.call('/api/upload',{'files':[{'name':name,'data':base64.b64encode(data).decode()}]})[0]==400


def test_upload_xlsx_and_mapping_preserved(client, app):
    from openpyxl import Workbook
    wb=Workbook();ws=wb.active;ws.title='points';ws.append(['point_id','description','equipment_id']);ws.append(['p1','SYNTHETIC analog','e1'])
    stream=io.BytesIO();wb.save(stream)
    upload=client.ok('/api/upload',{'files':[{'name':'custom.xlsx','data':base64.b64encode(stream.getvalue()).decode()}],
        'config':{'tables':[{'file':'custom.xlsx','sheet':'points','kind':'points'}]}})
    assert (app.root/'uploads'/upload['input_id']/'custom.xlsx').read_bytes()==stream.getvalue()
    with patch('ontology_engine.web.run',side_effect=AssertionError('Must not run without authorization')):
        assert client.call('/api/analyze',{'input_id':upload['input_id']})[0]==400


def test_malicious_source_text_is_preserved_as_data(client, app):
    evil='<img src=x onerror="window.injected=true"><script>window.injected=true</script>'
    raw=('point_id,description,equipment_id\np1,"'+evil.replace('"','""')+'",e1\n').encode()
    u=client.ok('/api/upload',{'files':[{'name':'points.csv','data':base64.b64encode(raw).decode()},
        {'name':'equipment.csv','data':base64.b64encode(b'equipment_id,equipment_type\ne1,AHU\n').decode()}]})
    result=wait_job(app,client.ok('/api/analyze',{'input_id':u['input_id'],'synthetic':True}));assert result['state']=='completed'
    p=app.runs/result['result']['run']; assert (p/'sources/points.csv').read_bytes()==raw
    records=[json.loads(line) for line in (p/'source_records.jsonl').read_text().splitlines()]
    assert any(evil in r['raw_value'] for r in records)
    js=(ROOT/'ontology_engine/web_assets/app.js').read_text()
    assert 'innerHTML' not in js and 'eval(' not in js and 'localStorage' not in js and 'sessionStorage' not in js


def test_missing_files_and_paths_fail_closed(client, app):
    assert client.call('/api/view?run=missing')[0]==400
    assert client.call('/api/view?run=..%2F..')[0]==400
    link=app.runs/'redirect';link.symlink_to(app.runs/'parent',target_is_directory=True)
    assert client.call('/api/view?run=redirect')[0]==400
    (app.runs/'parent/ontology.ttl').unlink()
    assert client.call('/api/view?run=parent')[0]==400
    assert client.call('/api/download?run=parent&artifact=package.zip')[0]==400


def test_jobs_bounded_and_failure_states(app):
    gate=threading.Event()
    first=app.submit('SYNTHETIC blocking test',lambda:gate.wait(2))
    try:
        with pytest.raises(ValueError,match='JOB_BUSY'): app.submit('other',lambda:None)
    finally:gate.set()
    assert wait_job(app,first)['state']=='completed'
    def fail():raise ValueError('secret provider text must not leak')
    result=wait_job(app,app.submit('SYNTHETIC failure',fail))
    assert result['state']=='failed' and 'secret' not in json.dumps(result)


def test_graph_large_deterministic_bounded_search_focus_and_malformed():
    claims=[]
    for i in range(6000):
        claims.extend([dict(claim_uid=f't{i:05}',subject_id=f'point/{i}',predicate='type',object_id='Temperature_Sensor',status='supported_inference'),
            dict(claim_uid=f'r{i:05}',subject_id='equipment/ahu',predicate='hasPoint',object_id=f'point/{i}',status='needs_review')])
    before=time.perf_counter();g=graph_view(claims);elapsed=time.perf_counter()-before
    assert len(g['nodes'])<=MAX_NODES and len(g['edges'])<=MAX_EDGES and g['next_offset'] is not None
    assert graph_view(list(reversed(claims)))==g
    assert all(not e['accepted'] for e in g['edges']) and elapsed<5
    assert graph_view(claims,query='no-such-label')['nodes']==[]
    assert graph_view(claims,focus='point/42',predicate='hasPoint')['matched_claims']==1
    assert graph_view(claims,status='contradicted')['nodes']==[]
    assert graph_view(claims,offset=g['next_offset'])['offset']==g['next_offset']
    for bad in ([{}],[dict(claim_uid='x',subject_id='<script>',predicate=42,object_id='x',status='verified')],None):
        with pytest.raises(ValueError):graph_view(bad)
    with pytest.raises(ValueError):graph_view(claims*9)


@pytest.mark.parametrize('intent,status', [('accept','supported_inference'),('reject','rejected'),('defer','needs_review')])
def test_v1_pending_actions_share_backend_and_cannot_apply(client, app, intent, status):
    w=client.ok('/api/enroll',{'run':'parent','schema':'review-v1'})
    _,service,_=request_for(app)
    claim=next(c for c in service.parent().claims.values() if c['object_id']=='unknown')
    evidence=client.ok('/api/inspect?run=parent&kind=claim&id='+claim['claim_uid']+'&schema=review-v1')
    a=dict(schema_version='review-v1',workspace_id=w['workspace_id'],site_scope=w['site_scope'],parent_fingerprint=w['parent_fingerprint'],
        actor_id=w['reviewer']['actor_id'],action_id='pending-'+intent,correlation_id='synthetic',expected_revision=0,
        target_kind='claim',target_id=claim['claim_uid'],action=intent,requested_status=status,
        reason='SYNTHETIC pending proposal; engineering authority not established.',
        evidence_uids=[s['source_record_uid'] for s in evidence['sources']])
    original=files_snapshot(app.runs/'parent')
    p=client.ok('/api/preview',{'run':'parent','action':a})
    assert p['graph_changes']=='not_applied' and p['changes']=={}
    event=client.ok('/api/decide',{'run':'parent','action':a})
    assert event['outcome']=='pending' and event['status']=='needs_review'
    assert client.ok('/api/history?run=parent&schema=review-v1')['revision']==1
    assert files_snapshot(app.runs/'parent')==original


def test_body_and_upload_count_limits(client, server):
    assert client.call('/api/upload',{'files':[]})[0]==400
    assert client.call('/api/upload',{'files':[{'name':'a.csv','data':'eA=='}]*11})[0]==400
    assert client.call('/api/upload',{'files':[{'name':'a.csv','data':'not-base64'}]})[0]==400
    assert client.call('/api/upload',{},headers={'Content-Type':'text/plain'})[0]==400
    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
    conn.request('POST','/api/upload',headers={'Content-Type':'application/json','Content-Length':'12000001',
        'Origin':server.origin,'Cookie':client.cookie,'X-CSRF-Token':client.csrf})
    response=conn.getresponse();assert response.status==400;response.read();conn.close()


def test_diagram_download_escapes_labels_and_keeps_candidate_distinction(client):
    from ontology_engine.web_graph import diagram
    from xml.etree.ElementTree import fromstring
    claims=[dict(claim_uid='evil',subject_id='equipment/<script>alert(1)</script>',predicate='feeds',
        object_id='equipment/other',status='contradicted')]
    data=diagram(claims,True);root=fromstring(data)
    assert b'&lt;script&gt;' in data and b'<script>' not in data
    assert root.findall('.//{http://www.w3.org/2000/svg}line')[0].attrib['stroke-dasharray']=='7 6'
    code,raw,headers=client.call('/api/download?run=parent&artifact=graph.svg')
    assert code==200 and headers['Content-Type']=='image/svg+xml'
    assert 'SYNTHETIC / FOR TESTING ONLY' in raw.decode() and fromstring(raw) is not None


def test_graph_first_explorer_is_authenticated_read_only(client, app):
    """UX-B must reuse the existing authenticated evidence service and never write decisions."""
    from ontology_engine.review import files_snapshot
    original = files_snapshot(app.runs/'parent')
    for asset in ('/explorer', '/explorer.js', '/explorer.css'):
        status, payload, _ = client.call(asset)
        assert status == 200
        assert len(payload) > 200
    assert Client(client.server).call('/api/explorer?run=parent')[0] == 403
    graph = client.ok('/api/explorer?run=parent&mode=equipment')
    assert graph['stats']['claims'] > 0
    assert graph['limitations']['graph_is_projection'] is True
    assert all(e['directed'] == (e['predicate'] == 'feeds') for e in graph['edges'])
    assert all(e['status'] != 'verified' or e['accepted'] for e in graph['edges'])
    assert client.ok('/api/explorer?run=parent&mode=points')['stats']['points'] >= 1
    claim_id = next((n['claim_uid'] for n in graph['nodes'] if n['claim_uid']), None)
    assert claim_id
    evidence = client.ok('/api/inspect?run=parent&kind=claim&id=' + claim_id)
    assert evidence['claims']
    assert files_snapshot(app.runs/'parent') == original
    assert client.call('/api/explorer?run=parent&mode=not-a-mode')[0] == 400


def test_api_key_onboarding_is_session_scoped_and_never_echoes_secret(client, server, monkeypatch):
    """Session-only credential, no provider request and no source-data export."""
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    key = 'sk-local-TEST-ONLY-not-a-real-api-key-123456'
    original_client = client
    assert original_client.ok('/api/credentials')['configured'] is False
    status = original_client.ok('/api/credentials', {'provider': 'openai', 'key': key})
    assert status == {'provider': 'openai', 'configured': True, 'source': 'session_memory', 'persistent': False}
    assert key not in json.dumps(status)
    assert key not in json.dumps(original_client.ok('/api/credentials'))
    second = Client(server); second.login()
    assert second.ok('/api/credentials')['source'] == 'none'
    assert original_client.ok('/api/credentials')['source'] == 'session_memory'
    assert original_client.ok('/api/credentials/clear', {})['configured'] is False
    assert original_client.ok('/api/credentials')['source'] == 'none'
    original_client.ok('/api/credentials', {'provider':'openai', 'key':key})
    original_client.ok('/api/logout', {})
    assert original_client.call('/api/credentials')[0] == 403
    assert second.ok('/api/credentials')['source'] == 'none'


def test_api_key_does_not_enable_unauthorized_external_processing(client, app, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    key = 'sk-local-TEST-ONLY-not-a-real-api-key-123456'
    client.ok('/api/credentials', {'provider':'openai', 'key':key})
    uploaded = client.ok('/api/upload', {'files': [
        {'name':'points.csv', 'data':base64.b64encode(b'point_id,description,equipment_id\\np1,temperature,e1\\n').decode()},
        {'name':'equipment.csv', 'data':base64.b64encode(b'equipment_id,equipment_type\\ne1,AHU\\n').decode()}
    ], 'config':{}})
    with patch('ontology_engine.web.run', side_effect=AssertionError('No provider run without consent')):
        assert client.call('/api/analyze', {'input_id':uploaded['input_id'],
            'building_id':'site:demo', 'model':'example-model'})[0] == 400
    assert client.ok('/api/credentials')['configured'] is True


def test_browser_passes_key_to_single_approved_job_without_process_env(client, app, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    key = 'sk-local-TEST-ONLY-not-a-real-api-key-123456'
    client.ok('/api/credentials', {'provider':'openai', 'key':key})
    uploaded = client.ok('/api/upload', {'files': [
        {'name':'points.csv', 'data':base64.b64encode(b'point_id,description,equipment_id\\np1,temperature,e1\\n').decode()},
        {'name':'equipment.csv', 'data':base64.b64encode(b'equipment_id,equipment_type\\ne1,AHU\\n').decode()}
    ], 'config':{}})
    passed = []
    def intercept(*args, **kwargs):
        passed.append(kwargs.get('api_key'))
        assert kwargs.get('allow_external') is True
        assert 'OPENAI_API_KEY' not in __import__('os').environ
        raise ValueError('synthetic no-network interruption')
    with patch('ontology_engine.web.run', side_effect=intercept):
        response = client.ok('/api/analyze', {'input_id':uploaded['input_id'],
            'building_id':'site:demo', 'model':'example-model',
            'authorize_external_processing':True})
        job = wait_job(app, response)
    assert passed == [key]
    assert job['state'] == 'failed'
    assert key not in json.dumps(job)
    assert 'OPENAI_API_KEY' not in __import__('os').environ


def test_guided_preflight_and_execute_require_exact_hash_and_two_approvals(client, app, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    key = 'sk-local-TEST-ONLY-not-a-real-api-key-123456'
    client.ok('/api/credentials', {'provider': 'openai', 'key': key})
    upload = client.ok('/api/upload', {'files': [
        {'name': 'points.csv', 'data': base64.b64encode(b'point_id,description,equipment_id\\np1,temperature,e1\\n').decode()},
        {'name': 'equipment.csv', 'data': base64.b64encode(b'equipment_id,equipment_type\\ne1,AHU\\n').decode()},
    ], 'config': {'version':1, 'inference_mode':'legacy'}})
    request = {'input_id': upload['input_id'], 'provider':'openai', 'model':'test-model',
               'building_id':'site:test-building', 'input_rate':'0.5',
               'output_rate':'5', 'max_cost_usd':'0.25'}
    assert client.call('/api/guided/execute', {'plan_hash':'0'*64,
        'approve_external':True, 'approve_cost':True})[0] == 400
    plan = client.ok('/api/guided/preflight', request)
    assert key not in json.dumps(plan)
    assert plan['settings']['files']
    assert client.call('/api/guided/execute', {'plan_hash':plan['plan_hash'],
        'approve_external':True, 'approve_cost':False})[0] == 400
    # Re-plan because failed consent is not a submission; hash must remain the same.
    plan2 = client.ok('/api/guided/preflight', request)
    assert plan2['plan_hash'] == plan['plan_hash']
    received = []
    def no_network(*args, **kwargs):
        received.append(kwargs)
        assert kwargs['api_key'] == key
        assert kwargs['allow_external'] is True
        assert kwargs['limits'].max_cost_usd == 0.25
        raise ValueError('synthetic provider blocked')
    with patch('ontology_engine.web.run', side_effect=no_network):
        job = client.ok('/api/guided/execute', {'plan_hash':plan['plan_hash'],
            'approve_external':True, 'approve_cost':True})
        assert wait_job(app, job)['state'] == 'failed'
    assert len(received) == 1
    assert client.call('/api/guided/execute', {'plan_hash':plan['plan_hash'],
        'approve_external':True, 'approve_cost':True})[0] == 400
    assert key not in json.dumps(app.jobs)


def test_guided_preflight_session_isolated_and_file_changed_fails(client, app, server, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    upload = client.ok('/api/upload', {'files': [
        {'name': 'points.csv', 'data': base64.b64encode(b'point_id,description,equipment_id\\np1,temperature,e1\\n').decode()},
    ], 'config': {}})
    request = {'input_id': upload['input_id'], 'provider':'openai', 'model':'test-model',
               'building_id':'site:test-building', 'input_rate':'0.5',
               'output_rate':'5', 'max_cost_usd':'0.25'}
    plan = client.ok('/api/guided/preflight', request)
    other = Client(server)
    other.login()
    assert other.call('/api/guided/execute', {'plan_hash':plan['plan_hash'],
        'approve_external':True, 'approve_cost':True})[0] == 400
    (app.root / 'uploads' / upload['input_id'] / 'points.csv').write_text('point_id\\np2\\n')
    client.ok('/api/credentials', {'provider':'openai','key':'sk-local-TEST-ONLY-not-a-real-api-key-123456'})
    with patch('ontology_engine.web.run', side_effect=AssertionError('No network for changed sources')):
        assert client.call('/api/guided/execute', {'plan_hash':plan['plan_hash'],
            'approve_external':True, 'approve_cost':True})[0] == 400


def test_unified_workspace_assets_and_existing_read_only_contract(client, app):
    """UX-E routes use the existing verified-package API without new mutation gates."""
    before = files_snapshot(app.runs/'parent')
    html = client.call('/explorer')[1].decode('utf-8')
    assert '/workspace.js' in html and '/workspace.css' in html
    for asset, text in [
        ('/workspace.js', 'workspaceRoute'),
        ('/workspace.css', '.workspace-hero'),
    ]:
        status, body, _ = client.call(asset)
        assert status == 200
        assert text.encode() in body
    assert 'id="overviewPage"' in html
    assert 'id="qualityPage"' in html
    assert 'id="insightsPage"' in html
    assert 'id="tourPage"' in html
    assert 'id="mappingAssistant"' in html
    assert 'id="commandPalette"' in html

    # The new views consume the established authenticated read-only endpoints.
    assert client.ok('/api/explorer?run=parent&mode=systems&limit=1')['stats']['claims'] > 0
    assert 'issues' in client.ok('/api/view?run=parent')
    assert 'items' in client.ok('/api/groups?run=parent&offset=0')
    assert before == files_snapshot(app.runs/'parent')


def test_unified_workspace_does_not_bypass_review_authorization(client):
    """The frontend must not create an alternate approval or restricted provider run path."""
    status, script, _ = client.call('/workspace.js')
    assert status == 200
    assert b'/api/decide' not in script
    assert b'/api/guided/execute' not in script
    assert b'innerHTML' not in script
    assert b'eval(' not in script
    status, html, _ = client.call('/explorer')
    assert status == 200
    assert b'Separate external-processing authorization required.' in html


def test_unified_workspace_cross_platform_document_and_css(client):
    """Responsive/touch UI is shipped as local assets, never a platform-specific binary."""
    status, html, _ = client.call('/explorer')
    assert status == 200
    assert b'name="viewport"' in html
    assert b'/workspace.css' in html
    assert b'/workspace.js' in html
    status, css, _ = client.call('/workspace.css')
    assert status == 200
    for token in (
        b'@media(max-width:750px)',
        b'@media(max-width:420px)',
        b'@media(hover:none) and (pointer:coarse)',
        b'@media(prefers-reduced-motion:reduce)',
        b'@media(forced-colors:active)',
        b'touch-action:none',
    ):
        assert token in css
    status, script, _ = client.call('/workspace.js')
    assert status == 200
    assert b'commandPalette' in script
    status, onboarding_script, _ = client.call('/onboarding.js')
    assert status == 200 and b'onboardingRender' in onboarding_script
    assert b'os.system' not in script and b'innerHTML' not in script


def test_new_workspace_is_default_and_classic_review_is_explicit_legacy(client):
    """Direct root entry must never land in the old review-first UI."""
    code, new_html, _ = client.call('/')
    assert code == 200
    assert b'id="overviewPage"' in new_html
    assert b'id="commandPalette"' in new_html
    assert b'/workspace.js' in new_html
    assert b'href="/legacy"' in new_html
    assert b'href="/" class="legacy"' not in new_html

    code, explorer_html, _ = client.call('/explorer')
    assert code == 200
    assert explorer_html == new_html

    code, classic_html, _ = client.call('/legacy')
    assert code == 200
    assert b'id="application"' in classic_html
    assert b'href="/"' in classic_html

    # Both entry points still rely on the same protected API.
    assert Client(client.server).call('/api/runs')[0] == 403


def test_native_review_assets_loaded_from_new_default_workspace(client):
    """UX-F is the actual UI, not a redirect to the old review page."""
    status, html, _ = client.call('/')
    assert status == 200
    for mark in (b'/review.js', b'/review.css', b'id="reviewNativeGroups"',
                 b'id="reviewActionForm"', b'id="reviewHistoryList"',
                 b'id="reviewPreviewDetails"', b'id="reviewPublish"'):
        assert mark in html
    status, js, _ = client.call('/review.js')
    assert status == 200
    for endpoint in (b'/api/groups', b'/api/inspect', b'/api/enroll',
                     b'/api/preview', b'/api/decide', b'/api/history', b'/api/export'):
        assert endpoint in js
    assert b'innerHTML' not in js
    assert b'eval(' not in js
    assert b'localStorage' not in js
    assert b'sessionStorage' not in js
    assert b'"verified"' not in js or b'"requested_status":"verified"' not in js
    status, css, _ = client.call('/review.css')
    assert status == 200
    assert b'.review-grid' in css
    assert b'@media(max-width:560px)' in css


def test_native_review_api_remains_audited_documentary_v2(client, app):
    """New UI uses existing v2 endpoints with preview-only and pending effects."""
    from ontology_engine.review import files_snapshot
    before = files_snapshot(app.runs/'parent')
    queue = client.ok('/api/groups?run=parent&offset=0')
    group = next(g for g in queue['items'] if g['cause_kind'] == 'naming_prefix')
    inspected = client.ok('/api/inspect?run=parent&kind=group&id=' + group['group_id'] + '&schema=review-v2')
    assert inspected['sources'] and inspected['claims']
    assert inspected['workspace'] is None
    w = client.ok('/api/enroll', {'run':'parent', 'schema':'review-v2'})
    action, _, _ = request_for(app)
    assert action['workspace_id'] == w['workspace_id']
    assert action['target_kind'] == 'group'
    preview = client.ok('/api/preview', {'run':'parent','action':action})
    assert preview['graph_changes'] == 'preview_only'
    assert preview['changes']['added_triples']
    assert client.ok('/api/history?run=parent&schema=review-v2')['revision'] == 0
    event = client.ok('/api/decide', {'run':'parent','action':action})
    assert event['outcome'] == 'pending'
    assert event['graph_consequence'] == 'not_applied'
    assert client.ok('/api/history?run=parent&schema=review-v2')['revision'] == 1
    assert files_snapshot(app.runs/'parent') == before


def test_input_inspection_is_authenticated_bounded_and_read_only(client, app):
    """UX-G inspects local synthetic records with no upload or model call."""
    source = b"Tag,Description,UOM\np1,Supply Temperature,degF\n"
    before = sorted(p.name for p in (app.root / 'uploads').iterdir())
    body = {'files': [{'name': 'opaque.csv',
                        'data': base64.b64encode(source).decode()}]}
    with patch('ontology_engine.web.run', side_effect=AssertionError('No model invocation')):
        preview = client.ok('/api/input/inspect', body)
    assert preview['file_count'] == 1
    assert preview['files'][0]['tables'][0]['proposed_kind'] == 'points'
    assert preview['files'][0]['tables'][0]['proposed_columns']['point_id'] == 'Tag'
    assert sorted(p.name for p in (app.root / 'uploads').iterdir()) == before
    assert client.call('/api/input/inspect', {'files': []})[0] == 400
    assert Client(client.server).call('/api/input/inspect', body)[0] == 403
    assert client.call('/api/input/inspect', body, headers={'X-CSRF-Token': 'bad'})[0] == 403


def test_onboarding_assets_bound_to_primary_website(client):
    code, html, _ = client.call('/')
    assert code == 200 and b'/onboarding.js' in html and b'/onboarding.css' in html
    assert b'id="mappingInspect"' in html
    for asset, required in [('/onboarding.js', b'/api/input/inspect'),
                            ('/onboarding.css', b'.onboard-sheet')]:
        code, content, _ = client.call(asset)
        assert code == 200 and required in content
        assert b'innerHTML' not in content and b'eval(' not in content


def test_source_trail_assets_are_local_and_do_not_add_review_privileges(client):
    """UX-H is sourced from read-only package endpoints; no drawing-file exposure."""
    status, html, _ = client.call('/')
    assert status == 200
    for expected in (b'/evidence.js', b'/evidence.css',
                     b'id="relationshipRegisterTitle"', b'id="relationshipRows"',
                     b'id="relationshipFilter"', b'id="relationshipStatus"'):
        assert expected in html
    status, script, _ = client.call('/evidence.js')
    assert status == 200
    for expected in (b'evidenceTrace', b'resolved_source_uids',
                     b'evidenceSetRelationships', b'evidenceSourceBox'):
        assert expected in script
    for forbidden in (b'innerHTML', b'eval(', b'fetch("http', b'fetch(\'http',
                      b'/api/decide', b'/api/enroll', b'/api/guided/execute'):
        assert forbidden not in script
    status, css, _ = client.call('/evidence.css')
    assert status == 200
    assert b'.evidence-register' in css and b'@media(max-width:750px)' in css


def test_source_trail_is_grounded_in_parent_claim_observations(client, app):
    """Exactly the source UIDs referenced by observations are displayed as direct support."""
    groups = client.ok('/api/groups?run=parent&offset=0')
    assert groups['items']
    claim = next(c for g in groups['items'] for c in g['claims']
                 if c['predicate'] == 'type')
    identifier = claim['claim_uid']
    result = client.ok('/api/inspect?run=parent&kind=claim&id=' + identifier)
    assert len(result['claims']) == 1
    assert result['claims'][0]['claim_uid'] == identifier
    declared = {uid for observation in result['observations']
                if observation['claim_uid'] == identifier
                for uid in observation['resolved_source_uids']}
    indexed = {source['source_record_uid'] for source in result['sources']}
    assert declared <= indexed
    before = files_snapshot(app.runs/'parent')
    client.ok('/api/inspect?run=parent&kind=claim&id=' + identifier)
    assert files_snapshot(app.runs/'parent') == before


def test_hash_bound_source_viewer_assets_and_strict_csp(client, app):
    """Only the redesigned workspace can embed a matching local blob; no server document route."""
    code, html, headers = client.call('/')
    assert code == 200
    assert b'/source_viewer.js' in html and b'/source_viewer.css' in html
    assert b'id="sourceViewerDialog"' in html and b'id="sourceViewerFile"' in html
    assert b'id="sourceViewerPdf"' in html
    policy = headers['Content-Security-Policy']
    assert "frame-src blob:" in policy
    assert "object-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "connect-src 'self'" in policy
    assert b'sandbox=""' in html

    code, classic, legacy_headers = client.call('/legacy')
    assert code == 200
    assert b'/source_viewer.js' not in classic
    assert "frame-src blob:" not in legacy_headers['Content-Security-Policy']

    code, script, _ = client.call('/source_viewer.js')
    assert code == 200
    for token in (b'crypto.subtle.digest("SHA-256"', b'sourceViewerPDF',
                  b'sourceViewerRelease', b'sourceViewerCanOpen',
                  b'SHA-256 MISMATCH', b'URL.revokeObjectURL'):
        assert token in script
    for forbidden in (b'innerHTML', b'eval(', b'localStorage',
                      b'sessionStorage', b'fetch(', b'XMLHttpRequest',
                      b'/api/upload', b'/api/decide', b'/api/enroll'):
        assert forbidden not in script

    code, css, _ = client.call('/source_viewer.css')
    assert code == 200
    assert b'.source-viewer-dialog' in css
    assert b'@media(max-width:650px)' in css
    original = files_snapshot(app.runs/'parent')
    code, _, _ = client.call('/api/source_pdf?run=parent')
    assert code == 404
    assert files_snapshot(app.runs/'parent') == original
