"""FR-01/04/05/07/11/12: owner-controlled loopback adapter to existing services.

No source code, browser credentials, or reviewer strings are executed. All public
identifiers are contained basenames; credentials remain in private server files.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import InputConfig
from .errors import EngineError
from .pipeline import run
from .review import (ReviewService, create_workspace, package, read_bytes,
                     private_directory, write_new)
from .review_export import export_review, compute
from .review_models import hashed
from .web_graph import graph_view, diagram
from .web_explorer import project as explorer_project
from .web_guided import preflight as guided_preflight, verify_unchanged
from .web_input_preview import inspect_upload as inspect_input_upload

ASSETS = Path(__file__).parent / 'web_assets'
DEMOS = Path(__file__).parent / 'web_demo'
MAX_BODY = 12_000_000
MAX_UPLOAD = 2_000_000
SESSION_SECONDS = 1800
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$')


def contained(root, name):
    if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or name in ('.', '..'):
        raise ValueError('PATH_REFUSED')
    root = Path(root)
    # Check every component, not only the final basename.
    for p in (root, *root.parents):
        if p.is_symlink(): raise ValueError('PATH_REFUSED')
    path = root / name
    if path.is_symlink(): raise ValueError('PATH_REFUSED')
    if path.resolve().parent != root.resolve(): raise ValueError('PATH_REFUSED')
    return path


def public_workspace(workspace):
    value = workspace.model_dump()
    for key in ('credential_sha256', 'parent_path'): value.pop(key, None)
    return value


class Application:
    def __init__(self, root, runs_root=None):
        requested = Path(root).absolute()
        for p in (requested, *requested.parents):
            if p.is_symlink(): raise ValueError('PATH_REFUSED')
        requested.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_directory(requested)
        self.root = requested.resolve()
        for name in ('runs', 'reviews', 'credentials', 'uploads'):
            path = contained(self.root, name)
            path.mkdir(mode=0o700, exist_ok=True); private_directory(path)
        self.runs = Path(runs_root).absolute() if runs_root else self.root / 'runs'
        contained(self.runs, 'preflight')
        if not self.runs.is_dir(): raise ValueError('RUNS_ROOT_MISSING')
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.jobs = {}

    def close(self): self.executor.shutdown(wait=True)

    def path(self, identifier):
        path = contained(self.runs, identifier)
        if not path.is_dir(): raise ValueError('RUN_MISSING')
        return path

    def list_runs(self):
        result = []
        # Do not traverse hidden checkpoints, source directories or recursive roots.
        for p in sorted(self.runs.iterdir()):
            if len(result) >= 200: break
            if p.name.startswith('.') or p.is_symlink() or not p.is_dir(): continue
            if not (p / 'run_manifest.json').exists(): continue
            try:
                contained(self.runs, p.name)
                m = json.loads(read_bytes(p / 'run_manifest.json', 2_000_000))
                result.append({'id': p.name, 'run_id': m['run_id'], 'synthetic': m['synthetic'],
                               'site': m['settings']['building_scope'], 'state': m['completion_status']})
            except (ValueError, OSError, KeyError):
                result.append({'id': p.name, 'state': 'unavailable'})
        return result

    def submit(self, label, fn):
        with self.lock:
            if any(j['state'] in ('queued', 'running') for j in self.jobs.values()):
                raise ValueError('JOB_BUSY')
            if len(self.jobs) >= 50: raise ValueError('JOB_LIMIT_RESTART_APP')
            identifier = secrets.token_hex(12)
            self.jobs[identifier] = {'id': identifier, 'label': label, 'state': 'queued', 'progress': 'Waiting for worker'}
        def work():
            with self.lock: self.jobs[identifier].update(state='running', progress=label)
            try:
                result = fn()
                with self.lock: self.jobs[identifier].update(state='completed', progress='Completed; package integrity passed', result=result)
            except Exception as exc:
                # Never send source text, provider diagnostics, paths, or secrets to browser/logs.
                code = exc.code if isinstance(exc, EngineError) else 'LOCAL_JOB_FAILED'
                with self.lock: self.jobs[identifier].update(state='failed', progress='Failed; original runs preserved', error=code)
        self.executor.submit(work)
        return {'job_id': identifier}

    def job(self, identifier):
        with self.lock:
            if identifier not in self.jobs: raise ValueError('JOB_MISSING')
            return dict(self.jobs[identifier])

    def upload(self, body):
        files = body.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 10: raise ValueError('UPLOAD_LIMIT')
        decoded = {}; total = 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {'name', 'data'}: raise ValueError('UPLOAD_SCHEMA')
            name = item['name']; contained(self.root / 'uploads', name)
            if Path(name).suffix.lower() not in ('.csv', '.xlsx') or name in decoded: raise ValueError('UPLOAD_FORMAT')
            if not isinstance(item['data'], str) or len(item['data']) > 4 * MAX_UPLOAD // 3 + 4: raise ValueError('UPLOAD_LIMIT')
            raw = base64.b64decode(item['data'], validate=True)
            if not raw or len(raw) > MAX_UPLOAD: raise ValueError('UPLOAD_LIMIT')
            total += len(raw)
            if total > 8_000_000: raise ValueError('UPLOAD_LIMIT')
            if name.lower().endswith('.csv'):
                raw.decode('utf-8-sig')
                if b'\x00' in raw: raise ValueError('UPLOAD_BINARY')
            else:
                from .limits import check_workbook, Limits
                try: check_workbook(raw, Limits())
                except (zipfile.BadZipFile, KeyError): raise ValueError('UPLOAD_WORKBOOK')
            decoded[name] = raw
        identifier = 'input-' + secrets.token_hex(10)
        dest = contained(self.root / 'uploads', identifier); dest.mkdir(mode=0o700)
        for name, raw in decoded.items(): write_new(dest / name, raw)
        config = InputConfig.model_validate(body.get('config', {}))
        write_new(dest / '.input-config.json', config.model_dump_json().encode())
        return {'input_id': identifier, 'files': list(decoded), 'bytes': total}

    def guided_preflight(self, body):
        identifier = body.get('input_id')
        directory = contained(self.root / 'uploads', identifier)
        return guided_preflight(self.root / 'uploads', identifier, body)

    def analyze(self, body, api_key=None, limits=None):
        demo = body.get('demo')
        name = 'building-' + secrets.token_hex(8)
        dest = contained(self.runs, name)
        if demo:
            if demo not in ('shared', 'conflict'): raise ValueError('DEMO_UNKNOWN')
            source = DEMOS / demo; provider = 'fixture'; config = None
            building = 'SYNTHETIC:web-' + demo
        else:
            source = contained(self.root / 'uploads', body.get('input_id'))
            if not source.is_dir(): raise ValueError('INPUT_MISSING')
            if body.get('synthetic') is True:
                provider = 'fixture'; building = 'SYNTHETIC:web-upload-' + name
            else:
                if body.get('authorize_external_processing') is not True:
                    raise ValueError('EXTERNAL_AUTHORIZATION_REQUIRED')
                if not isinstance(body.get('model'), str) or not body['model'].strip() or len(body['model']) > 200:
                    raise ValueError('MODEL_REQUIRED')
                provider = 'openai'; building = body.get('building_id')
                if not isinstance(building, str) or not building.strip() or len(building) > 200:
                    raise ValueError('BUILDING_ID_REQUIRED')
            config = source / '.input-config.json'
        # Only the authorized request carries the session's key, never a global env mutation.
        # The generic provider reads this transient key from the invocation, not from disk.
        def execute():
            run(source, dest, provider=provider, model=body.get('model'),
                allow_external=(provider == 'openai'), building_id=building,
                config_path=config, api_key=api_key if provider == 'openai' else None, limits=limits)
            package(dest)
            return {'run': name}
        return self.submit('Analyze synthetic demonstration' if provider == 'fixture' else 'Analyze explicitly authorized inputs', execute)

    def service(self, identifier, create=False, schema='review-v2'):
        if schema not in ('review-v1', 'review-v2'): raise ValueError('REVIEW_SCHEMA')
        parent = self.path(identifier)
        key = hashed({'path': str(parent)})[:32]
        if schema == 'review-v1': key += '-v1'
        workspace = contained(self.root / 'reviews', key)
        credential = contained(self.root / 'credentials', key)
        # Creation is serialized; ReviewService itself supplies transactional concurrency.
        with self.lock:
            if not workspace.exists():
                if not create: raise ValueError('REVIEW_NOT_ENROLLED')
                create_workspace(parent, workspace, 'local-browser-reviewer', credential, schema_version=schema)
        return ReviewService(workspace), credential

    def view(self, identifier, query):
        _, manifest, files, _ = package(self.path(identifier))
        claims = json.loads(files['candidate_claims.json'])['records']
        view = graph_view(claims, **query)
        changed = set()
        diff = json.loads(files['review_changes.json']) if 'review_changes.json' in files else None
        if diff:
            for c in diff['removed_claims']: changed.add(c['claim']['subject_id'])
            for c in diff.get('claim_changes', []):
                for side in ('before', 'after'):
                    value = c.get(side)
                    if isinstance(value, dict) and 'claim' in value: changed.add(value['claim']['subject_id'])
            # Added type triples can change nodes even when old unknown claims were removed.
        for node in view['nodes']: node['affected'] = node['id'] in changed
        return {'manifest': manifest, 'graph': view, 'issues': json.loads(files['issues.json']),
                'validation': json.loads(files['validation.json']), 'changes': diff,
                'outcomes': json.loads(files['replay_inputs.json'])['outcomes'] if 'replay_inputs.json' in files else [],
                'after_shacl': json.loads(files['after_shacl.json']) if 'after_shacl.json' in files else None}

    def explorer(self, identifier, options):
        _, _, files, _ = package(self.path(identifier))
        claims = json.loads(files['candidate_claims.json'])['records']
        return explorer_project(claims, **options)

    def inspect(self, identifier, kind, target, schema='review-v2'):
        _, _, files, _ = package(self.path(identifier))
        from .review import ParentIndex
        from types import SimpleNamespace
        manifest = json.loads(files['run_manifest.json'])
        # Inspecting an original or derived package does not require enrollment.
        result = {'workspace': None, 'revision': None, 'history': [],
                  **ParentIndex(files, SimpleNamespace(site_scope=manifest['settings']['building_scope'])).target(kind, target)}
        key = hashed({'path': str(self.path(identifier))})[:32]
        if schema == 'review-v1': key += '-v1'
        if contained(self.root / 'reviews', key).exists():
            service, _ = self.service(identifier, schema=schema)
            # v1 has no group action; group evidence remains readable without an action.
            if kind != 'group' or schema == 'review-v2': result = service.inspect(kind, target)
            result['workspace'] = public_workspace(service.workspace)
        engineering = json.loads(files['engineering_results.json'])['records']
        # Preserve all findings (bounded by package limits) to avoid omitting an indirect check.
        result['engineering_validation'] = engineering
        result['confidence_limitations'] = 'Scores are uncalibrated; schema/physics checks and local credentials do not establish facts or professional authority.'
        return result

    def groups(self, identifier, offset):
        from .review import ParentIndex, page_bounds
        from .clarification import consolidate
        from types import SimpleNamespace
        page_bounds(offset, 50)
        _, manifest, files, _ = package(self.path(identifier))
        scope = manifest['settings']['building_scope']
        rows = consolidate(ParentIndex(files, SimpleNamespace(site_scope=scope)), scope)
        return {'total': len(rows), 'items': rows[offset:offset+50]}

    def preview(self, identifier, action):
        service, credential = self.service(identifier, schema=action.get('schema_version', 'review-v2'))
        event = service.decide(action, credential, preview=True)
        if service.workspace.schema_version == 'review-v1':
            return {'event': event.model_dump(), 'outcomes': [], 'changes': {}, 'graph_changes': 'not_applied'}
        with service.transaction() as (db, _):
            events = service.checked_events(db, service.parent())
            # Preview retries must not append an already committed event twice.
            if not any(e.event_id == event.event_id for e in events): events.append(event)
            _, _, files, _ = package(service.workspace.parent_path)
            values, graph = compute(files, service.workspace, events)
            from .change_history import diff_contents
            after = dict(files)
            for name, value in values.items(): after[name] = json.dumps(value).encode()
            after['ontology.ttl'] = graph.serialize(format='turtle').encode()
            return {'event': event.model_dump(), 'outcomes': values['replay_inputs.json']['outcomes'],
                    'changes': diff_contents(files, after), 'graph_changes': 'preview_only'}

    def export(self, identifier, revision):
        service, credential = self.service(identifier)
        name = 'reviewed-' + secrets.token_hex(8)
        def execute():
            manifest = export_review(service, contained(self.runs, name), credential, revision)
            package(self.path(name))
            return {'run': name, 'revision': revision, 'run_id': manifest['run_id']}
        return self.submit('Replay frozen decisions, validate pinned Brick/SHACL and publish new version', execute)

    def download(self, identifier, artifact):
        _, manifest, files, _ = package(self.path(identifier))
        if artifact == 'graph.svg':
            return diagram(json.loads(files['candidate_claims.json'])['records'], manifest['synthetic']), 'image/svg+xml'
        if artifact == 'package.zip':
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, raw in sorted(files.items()): archive.writestr(name, raw)
            return stream.getvalue(), 'application/zip'
        allowed = {'ontology.ttl', 'candidate_claims.json', 'candidate_observations.json', 'relationships.csv',
                   'review_basis.json', 'review_changes.json', 'review_questions.csv', 'validation.json',
                   'engineering_results.json', 'source_records.jsonl', 'after_shacl.json'}
        if artifact not in allowed or artifact not in files: raise ValueError('ARTIFACT_MISSING')
        return files[artifact], 'application/octet-stream'


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    def __init__(self, app, password, port=8765):
        if not isinstance(password, str) or not 16 <= len(password) <= 256: raise ValueError('PASSWORD_LENGTH_16_256')
        self.app = app
        self.password_hash = hashlib.sha256(password.encode()).digest()
        self.sessions = {}; self.auth_lock = threading.Lock(); self.failures = []
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False): request.close(); return
        try: super().process_request(request, client_address)
        except BaseException: self.slots.release(); raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.slots.release()

    def handle_error(self, request, client_address): pass  # no secrets/source text in server logs


class Handler(BaseHTTPRequestHandler):
    server_version = 'OntologyLocal/1'
    def setup(self):
        super().setup(); self.connection.settimeout(10)
    def log_message(self, *args): pass

    def reply(self, value, status=200, mime='application/json', extra=None):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        self.send_response(status)
        for k, v in {'Content-Type': mime, 'Content-Length': str(len(raw)), 'Cache-Control': 'no-store',
                     'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                     'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                     'X-Frame-Options': 'DENY', **(extra or {})}.items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(raw)

    def guard_host(self):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc: raise PermissionError('HOST')
        if self.headers.get('Sec-Fetch-Site') in ('cross-site',): raise PermissionError('ORIGIN')

    def session(self, mutate=False):
        cookie = SimpleCookie(); cookie.load(self.headers.get('Cookie', ''))
        token = cookie['ontology_session'].value if 'ontology_session' in cookie else ''
        now = time.monotonic()
        with self.server.auth_lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > now}
            session = self.server.sessions.get(hashlib.sha256(token.encode()).hexdigest())
        if not session: raise PermissionError('SESSION')
        if mutate and (self.headers.get('Origin') != self.server.origin or
                       not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf'])):
            raise PermissionError('CSRF')
        return session

    def body(self):
        if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
            raise ValueError('CONTENT_TYPE')
        length = int(self.headers.get('Content-Length', '-1'))
        if not 0 <= length <= MAX_BODY: raise ValueError('BODY_LIMIT')
        data = self.rfile.read(length)
        if len(data) != length: raise ValueError('BODY_INCOMPLETE')
        value = json.loads(data)
        if not isinstance(value, dict): raise ValueError('BODY_SCHEMA')
        return value

    def login(self, body):
        if self.headers.get('Origin') != self.server.origin: raise PermissionError('ORIGIN')
        password = body.get('password', '')
        if not isinstance(password, str) or len(password) > 256: raise PermissionError('LOGIN')
        with self.server.auth_lock:
            now = time.monotonic()
            self.server.failures = [t for t in self.server.failures if now - t < 60]
            if len(self.server.failures) >= 10: raise PermissionError('RATE_LIMIT')
            if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self.server.password_hash):
                self.server.failures.append(now); raise PermissionError('LOGIN')
            token = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(32)
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > now}
            if len(self.server.sessions) >= 8: raise PermissionError('SESSION_LIMIT')
            self.server.sessions[hashlib.sha256(token.encode()).hexdigest()] = {'csrf': csrf, 'expires': now + SESSION_SECONDS}
        self.reply({'csrf': csrf}, extra={'Set-Cookie': 'ontology_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=1800'})

    def do_GET(self): self.handle_request(False)
    def do_POST(self): self.handle_request(True)

    def handle_request(self, mutate):
        try:
            self.guard_host()
            parsed = urlsplit(self.path)
            if len(self.path) > 8192: raise ValueError('URL_LIMIT')
            if parsed.fragment: raise ValueError('URL_FRAGMENT')
            path = parsed.path
            if not mutate and path in ('/', '/legacy', '/app.js', '/style.css', '/explorer', '/explorer.js', '/explorer.css', '/workspace.js', '/workspace.css', '/review.js', '/review.css', '/onboarding.js', '/onboarding.css', '/evidence.js', '/evidence.css', '/source_viewer.js', '/source_viewer.css'):
                file, mime = {'/': ('explorer.html', 'text/html; charset=utf-8'), '/legacy': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript'), '/style.css': ('style.css', 'text/css'), '/explorer': ('explorer.html', 'text/html; charset=utf-8'), '/explorer.js': ('explorer.js', 'application/javascript'), '/explorer.css': ('explorer.css', 'text/css'), '/workspace.js': ('workspace.js', 'application/javascript'), '/workspace.css': ('workspace.css', 'text/css'), '/review.js': ('review.js', 'application/javascript'), '/review.css': ('review.css', 'text/css'), '/onboarding.js': ('onboarding.js', 'application/javascript'), '/onboarding.css': ('onboarding.css', 'text/css'), '/evidence.js': ('evidence.js', 'application/javascript'), '/evidence.css': ('evidence.css', 'text/css'), '/source_viewer.js': ('source_viewer.js', 'application/javascript'), '/source_viewer.css': ('source_viewer.css', 'text/css')}[path]
                viewer_policy = {"Content-Security-Policy":
                    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                    "connect-src 'self'; frame-src blob:; object-src 'none'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'"} if file == 'explorer.html' else None
                self.reply((ASSETS / file).read_bytes(), mime=mime, extra=viewer_policy); return
            if mutate and path == '/api/login': self.login(self.body()); return
            session = self.session(mutate)
            app = self.server.app
            if not mutate and path == '/api/session': self.reply({'csrf': session['csrf']}); return
            query = {k: v[0] for k, v in parse_qs(parsed.query, max_num_fields=20).items()}
            body = self.body() if mutate else query
            if path == '/api/logout' and mutate:
                with self.server.auth_lock:
                    session.pop('openai_key', None)
                    self.server.sessions = {k: v for k, v in self.server.sessions.items() if v is not session}
                self.reply({}, extra={'Set-Cookie': 'ontology_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'}); return
            if path == '/api/credentials' and not mutate:
                configured = bool(session.get('openai_key'))
                result = {'provider': 'openai',
                          'configured': configured or bool(os.environ.get('OPENAI_API_KEY')),
                          'source': 'session_memory' if configured else
                                    ('server_environment' if os.environ.get('OPENAI_API_KEY') else 'none'),
                          'persistent': False}
            elif path == '/api/credentials' and mutate:
                if set(body) != {'provider', 'key'} or body['provider'] != 'openai':
                    raise ValueError('CREDENTIAL_SCHEMA')
                key = body['key']
                if (not isinstance(key, str) or not 16 <= len(key) <= 512 or key != key.strip()
                        or any(ord(ch) < 33 or ord(ch) > 126 for ch in key)):
                    raise ValueError('CREDENTIAL_FORMAT')
                with self.server.auth_lock:
                    session['openai_key'] = key
                result = {'provider': 'openai', 'configured': True,
                          'source': 'session_memory', 'persistent': False}
            elif path == '/api/credentials/clear' and mutate:
                if body: raise ValueError('CREDENTIAL_SCHEMA')
                with self.server.auth_lock:
                    session.pop('openai_key', None)
                result = {'provider': 'openai',
                          'configured': bool(os.environ.get('OPENAI_API_KEY')),
                          'source': 'server_environment' if os.environ.get('OPENAI_API_KEY') else 'none',
                          'persistent': False}
            elif path == '/api/guided/preflight' and mutate:
                plan = app.guided_preflight(body)
                with self.server.auth_lock:
                    session['guided_plan'] = plan
                result = plan
            elif path == '/api/guided/execute' and mutate:
                if set(body) != {'plan_hash', 'approve_external', 'approve_cost'}:
                    raise ValueError('APPROVAL_SCHEMA')
                if body['approve_external'] is not True or body['approve_cost'] is not True:
                    raise ValueError('APPROVAL_REQUIRED')
                with self.server.auth_lock:
                    plan = session.pop('guided_plan', None)
                    key = session.get('openai_key')
                if not plan or not hmac.compare_digest(str(body['plan_hash']), plan['plan_hash']):
                    raise ValueError('PREFLIGHT_REQUIRED')
                if not key and not os.environ.get('OPENAI_API_KEY'):
                    raise ValueError('CREDENTIAL_REQUIRED')
                limits = verify_unchanged(app.root / 'uploads', plan)
                settings = plan['settings']
                if settings['provider'] != 'openai':
                    raise ValueError('PROVIDER_NOT_SUPPORTED_IN_BROWSER')
                result = app.analyze({'input_id': settings['input_id'],
                                      'model': settings['model'],
                                      'building_id': settings['building_id'],
                                      'authorize_external_processing': True},
                                     api_key=key, limits=limits)
            elif path == '/api/runs' and not mutate: result = app.list_runs()
            elif path == '/api/upload' and mutate: result = app.upload(body)
            elif path == '/api/input/inspect' and mutate: result = inspect_input_upload(body)
            elif path == '/api/analyze' and mutate: result = app.analyze(body, api_key=session.get('openai_key'))
            elif path == '/api/job' and not mutate: result = app.job(body['id'])
            elif path == '/api/enroll' and mutate:
                service, _ = app.service(body['run'], create=True, schema=body.get('schema', 'review-v2')); result = public_workspace(service.workspace)
            elif path == '/api/view' and not mutate:
                options = {k: body[k] for k in ('query', 'focus', 'predicate', 'status', 'kind') if k in body}
                options['offset'] = int(body.get('offset', 0)); result = app.view(body['run'], options)
            elif path == '/api/explorer' and not mutate:
                options = {k: body[k] for k in ('mode', 'query', 'status', 'focus') if k in body}
                options['offset'] = int(body.get('offset', 0))
                options['limit'] = int(body.get('limit', 64))
                result = app.explorer(body['run'], options)
            elif path == '/api/inspect' and not mutate: result = app.inspect(body['run'], body['kind'], body['id'], body.get('schema', 'review-v2'))
            elif path == '/api/groups' and not mutate:
                result = app.groups(body['run'], int(body.get('offset', 0)))
            elif path == '/api/history' and not mutate:
                service, _ = app.service(body['run'], schema=body.get('schema', 'review-v2'))
                result = service.history(int(body.get('offset', 0)), 50)
            elif path == '/api/preview' and mutate: result = app.preview(body['run'], body['action'])
            elif path == '/api/decide' and mutate:
                service, credential = app.service(body['run'], schema=body['action'].get('schema_version', 'review-v2'))
                result = service.decide(body['action'], credential).model_dump()
            elif path == '/api/export' and mutate: result = app.export(body['run'], body['revision'])
            elif path == '/api/download' and not mutate:
                raw, mime = app.download(body['run'], body['artifact'])
                self.reply(raw, mime=mime, extra={'Content-Disposition': 'attachment; filename="' + body['artifact'] + '"'}); return
            else: self.reply({'error': 'NOT_FOUND'}, 404); return
            self.reply(result)
        except PermissionError:
            self.reply({'error': 'ACCESS_REFUSED'}, 403)
        except EngineError as exc:
            self.reply({'error': exc.code, 'next_action': 'Refresh history and inspect evidence, exact claim scope and current revision before retrying.'}, 400)
        except (ValueError, OSError, KeyError, TypeError):
            # Codes from EngineError are safe constants; arbitrary exception text is not.
            self.reply({'error': 'REQUEST_REFUSED', 'next_action': 'Check selected files, session, claim scope, evidence and current revision.'}, 400)
        except Exception:
            self.reply({'error': 'LOCAL_SERVER_ERROR'}, 500)


def serve(root, runs_root=None, port=8765, password_file=None):
    if password_file:
        password = read_bytes(Path(password_file).absolute(), 512, private=True).decode().strip()
    else:
        import getpass
        password = getpass.getpass('Set local browser password (16–256 characters): ')
    app = Application(root, runs_root)
    server = LocalServer(app, password, port)
    print('Local ontology application: ' + server.origin, flush=True)
    print('Enter your local password in the browser. No professional verification authority is granted.', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); app.close()
