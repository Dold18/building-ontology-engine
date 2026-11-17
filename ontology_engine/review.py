"""Local review service: immutable parent, append-only transactional pending decisions.

This service owns review data only. It never invokes inference or graph construction
except the existing read-only publication verifier. No Phase B replay is implemented.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from .errors import EngineError
from .identity import uid
from .publication import verify_package
from .state import now
from .review_models import (Workspace, Reviewer, ReviewAction, DecisionEvent,
    EvidenceReference, canonical, hashed, contract_fingerprint)

MAX_FILES = 5000
MAX_BYTES = 256_000_000
MAX_FILE_BYTES = 64_000_000
MAX_EVENTS = 1000
MAX_REPLAY_WORK = 1_000_000
MAX_REQUEST_BYTES = 65536
MAX_EVENT_BYTES = 256000
ZERO = '0' * 64


def fail(code, message):
    raise EngineError(code, message, next_action='Inspect review history and parent evidence; submit an explicit new action if needed.')


def checkpoint_hook(stage):
    """No-op process-crash instrumentation, never controlled by source/reviewer text."""


def checked_root(path):
    path = Path(path).absolute()
    # Resolve the operator-selected root, never a path taken from source rows.
    if path.is_symlink(): fail('REVIEW_PATH_UNSAFE', 'Symbolic link roots are refused.')
    if not path.is_dir(): fail('REVIEW_PATH_UNSAFE', 'Expected an existing directory.')
    return path.resolve()


def files_snapshot(root):
    """Preflight names/types/bytes before publication verifier reads any package content."""
    files = {}; total = 0; count = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            p = Path(directory) / name
            mode = p.lstat().st_mode
            count += 1
            if count > MAX_FILES: fail('REVIEW_LIMIT', 'Too many package entries.')
            if stat.S_ISLNK(mode): fail('REVIEW_PATH_UNSAFE', 'Package symlinks are refused.')
            if stat.S_ISDIR(mode): continue
            if not stat.S_ISREG(mode): fail('REVIEW_PATH_UNSAFE', 'Regular files required.')
            size = p.stat().st_size; total += size
            if size > MAX_FILE_BYTES or total > MAX_BYTES: fail('REVIEW_LIMIT', 'Package byte limit exceeded.')
            files[p.relative_to(root).as_posix()] = read_bytes(p, MAX_FILE_BYTES)
    return files


def read_bytes(path, limit, private=False):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode): fail('REVIEW_PATH_UNSAFE', 'Regular file required.')
        if private and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
            fail('REVIEW_PERMISSION', 'Private owner-only file required.')
        if before.st_size > limit: fail('REVIEW_LIMIT', 'File byte limit exceeded.')
        with os.fdopen(fd, 'rb', closefd=False) as stream: data = stream.read(limit + 1)
        after = os.fstat(fd)
        if len(data) > limit: fail('REVIEW_LIMIT', 'File byte limit exceeded.')
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            fail('REVIEW_STALE_EVIDENCE', 'File changed during read.')
        return data
    finally:
        os.close(fd)


def sha_bytes(data): return hashlib.sha256(data).hexdigest()


def package(path):
    root = checked_root(path)
    files = files_snapshot(root)
    try:
        manifest = json.loads(files['run_manifest.json'])
        for field in ('input_hashes', 'output_hashes'):
            for name in manifest[field]:
                p = Path(name)
                if p.is_absolute() or '..' in p.parts or '\\' in name:
                    fail('REVIEW_PATH_UNSAFE', 'Unsafe manifest path.')
                if field == 'input_hashes' and len(p.parts) != 1:
                    fail('REVIEW_PATH_UNSAFE', 'Source snapshot must be flat.')
        verified = verify_package(root)
        if verified != manifest: fail('REVIEW_STALE_EVIDENCE', 'Manifest changed during verification.')
        after = files_snapshot(root)
        if files != after: fail('REVIEW_STALE_EVIDENCE', 'Package changed during verification.')
    except (KeyError, ValueError) as exc:
        if isinstance(exc, EngineError): raise
        fail('REVIEW_PARENT_INVALID', 'Original package did not pass publication verification.')
    return root, manifest, files, hashed({name: sha_bytes(data) for name, data in files.items()})


def private_directory(root):
    mode = root.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        fail('REVIEW_PERMISSION', 'Workspace must be owner-only (0700).')


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def create_workspace(parent, destination, actor_id, credential_file, role='reviewer', schema_version='review-v1'):
    """Explicit local enrollment; token is issued privately, never returned or logged.

    The operator chooses a principal label. Possession authenticates that enrolled
    principal only; it does not prove a real person's identity/engineering authority.
    """
    reviewer = Reviewer(actor_id=actor_id, role=role)
    if not actor_id.strip(): fail('REVIEW_AUTHORITY', 'Actor identifier must be nonempty.')
    root, manifest, files, fingerprint = package(parent)
    dest = Path(destination).absolute()
    token_path = Path(credential_file).absolute()
    for path in (dest, token_path):
        if path.exists() or path.is_symlink(): fail('REVIEW_EXISTS', 'Destination and credential must be new.')
        if not path.parent.is_dir() or path.parent.is_symlink(): fail('REVIEW_PATH_UNSAFE', 'Select an existing regular parent directory.')
        resolved = path.parent.resolve() / path.name
        # Do not write into the parent package or its durable state, including via ancestors.
        state_root = root.with_name('.' + root.name + '.run')
        if any(protected == resolved or protected in resolved.parents or resolved in protected.parents
               for protected in (root, state_root)):
            fail('REVIEW_IMMUTABLE_PARENT', 'Review artifacts must be separate from original package/state.')
    dest = dest.parent.resolve() / dest.name
    token_path = token_path.parent.resolve() / token_path.name
    if dest == token_path or dest in token_path.parents:
        fail('REVIEW_PATH_UNSAFE', 'Credential must be outside the review artifact.')
    token = secrets.token_urlsafe(32)
    workspace_model, _, _, fingerprint_function = review_contract(schema_version)
    workspace = workspace_model(workspace_id=uid('review-workspace', manifest['settings']['building_scope'],
        manifest['run_id'], fingerprint, secrets.token_hex(16)), site_scope=manifest['settings']['building_scope'],
        parent_run_id=manifest['run_id'], parent_path=str(root), parent_fingerprint=fingerprint,
        source_fingerprint=hashed(manifest['input_hashes']), graph_sha256=sha_bytes(files['ontology.ttl']),
        manifest_sha256=sha_bytes(files['run_manifest.json']), contract_fingerprint=fingerprint_function(),
        reviewer=reviewer, created_at=now(), credential_sha256=sha_bytes(token.encode()))
    # New directory publication is atomic. An interrupted initialization never opens
    # as a complete workspace; retained private staging is diagnostic, not auto-replayed.
    staging = Path(tempfile.mkdtemp(prefix='.' + dest.name + '.review-', dir=dest.parent))
    db_path = staging / 'review.sqlite'
    write_new(db_path, b'')
    db = sqlite3.connect(db_path)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
        CREATE TABLE metadata(id INTEGER PRIMARY KEY CHECK(id=1), manifest TEXT NOT NULL, credential_hash TEXT NOT NULL);
        CREATE TABLE events(revision INTEGER PRIMARY KEY, action_id TEXT NOT NULL UNIQUE,
            event_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, payload TEXT NOT NULL, event_hash TEXT NOT NULL);
        CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only events'); END;
        CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only events'); END;
        CREATE TRIGGER metadata_no_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END;
        CREATE TRIGGER metadata_no_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END;
        PRAGMA user_version=1;
        ''')
        with db:
            db.execute('INSERT INTO metadata VALUES(1,?,?)',
                       (canonical(workspace.model_dump()), sha_bytes(token.encode())))
    finally: db.close()
    write_new(staging / 'review_manifest.json', (canonical(workspace.model_dump()) + '\n').encode())
    write_new(token_path, (token + '\n').encode())
    sync_directory(staging)
    sync_directory(token_path.parent)
    checkpoint_hook('review_initialized')
    if dest.exists(): fail('REVIEW_EXISTS', 'Workspace destination appeared during initialization.')
    os.rename(staging, dest)
    sync_directory(dest.parent)
    return workspace


def review_contract(version):
    if version == 'review-v1': return Workspace, ReviewAction, DecisionEvent, contract_fingerprint
    if version == 'review-v2':
        from .review_b_models import ReviewWorkspace, Action, Event, contract_fingerprint as fingerprint_v2
        return ReviewWorkspace, Action, Event, fingerprint_v2
    fail('REVIEW_VERSION', 'Unsupported workspace version.')


class ReviewService:
    """Backend shared by CLI/future UI. Each method opens its own SQLite transaction."""
    def __init__(self, directory):
        self.root = checked_root(directory)
        private_directory(self.root)
        self.db_path = self.root / 'review.sqlite'
        self.manifest_path = self.root / 'review_manifest.json'
        # Inspect the bounded file before sqlite opens it, refusing symlink redirection.
        read_bytes(self.db_path, MAX_FILE_BYTES, private=True)
        raw = read_bytes(self.manifest_path, MAX_REQUEST_BYTES, private=True)
        self.workspace_model, self.action_model, self.event_model, fingerprint_function = review_contract(json.loads(raw).get('schema_version'))
        self.workspace = self.workspace_model.model_validate_json(raw)
        if self.workspace.contract_fingerprint != fingerprint_function():
            fail('REVIEW_VERSION', 'Review schema/policy changed; explicit migration required.')

    @contextmanager
    def transaction(self, write=False):
        private_directory(self.root)
        for name in ('review.sqlite', 'review.sqlite-journal', 'review.sqlite-wal', 'review.sqlite-shm'):
            path = self.root / name
            if path.is_symlink(): fail('REVIEW_PATH_UNSAFE', 'Redirected SQLite file refused.')
        read_bytes(self.db_path, MAX_FILE_BYTES, private=True)
        if self.workspace_model.model_validate_json(read_bytes(self.manifest_path, MAX_REQUEST_BYTES, private=True)) != self.workspace:
            fail('REVIEW_STORE_INVALID', 'Workspace manifest changed.')
        db = sqlite3.connect(self.db_path.as_uri() + '?mode=' + ('rw' if write else 'ro'), uri=True, timeout=5)
        try:
            db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA trusted_schema=OFF')
            if write:
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                db.execute('PRAGMA max_page_count=' + str(MAX_FILE_BYTES // page_size))
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                fail('REVIEW_VERSION', 'Unsupported review store version.')
            row = db.execute('SELECT manifest,credential_hash FROM metadata WHERE id=1').fetchone()
            if not row or row[0] != canonical(self.workspace.model_dump()) or row[1] != self.workspace.credential_sha256:
                fail('REVIEW_STORE_INVALID', 'Store and manifest disagree.')
            yield db, row[1]
            if write: db.commit()
            else: db.rollback()
        except sqlite3.Error:
            db.rollback()
            fail('REVIEW_STORE_INVALID', 'SQLite transaction failed or store is busy/corrupt.')
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def parent(self):
        root, manifest, files, fingerprint = package(self.workspace.parent_path)
        w = self.workspace
        if (fingerprint != w.parent_fingerprint or manifest['run_id'] != w.parent_run_id
                or manifest['settings']['building_scope'] != w.site_scope
                or hashed(manifest['input_hashes']) != w.source_fingerprint
                or sha_bytes(files['ontology.ttl']) != w.graph_sha256
                or sha_bytes(files['run_manifest.json']) != w.manifest_sha256):
            fail('REVIEW_STALE_EVIDENCE', 'Original package fingerprint changed; create a new workspace.')
        return ParentIndex(files, w)

    def authenticate(self, credential_file, expected_hash, actor_id):
        token = read_bytes(credential_file, 256, private=True).decode('utf-8').strip()
        if not hmac.compare_digest(sha_bytes(token.encode()), expected_hash) or actor_id != self.workspace.reviewer.actor_id:
            fail('REVIEW_UNAUTHORIZED', 'Invalid credential or actor.')
        if self.workspace.reviewer.role != 'reviewer':
            fail('REVIEW_UNAUTHORIZED', 'This principal cannot submit review decisions.')

    def checked_events(self, db, index):
        count = db.execute('SELECT count(*) FROM events').fetchone()[0]
        if count > MAX_EVENTS: fail('REVIEW_LIMIT', 'Decision history exceeds event limit.')
        self.replay_budget(index, count)
        events = []; previous = ZERO
        for row in db.execute('SELECT revision,action_id,event_id,request_hash,payload,event_hash FROM events ORDER BY revision'):
            revision, action_id, event_id, request_hash, payload, event_hash = row
            if len(payload.encode()) > MAX_EVENT_BYTES: fail('REVIEW_LIMIT', 'Event payload exceeds limit.')
            event = self.event_model.model_validate_json(payload)
            expected = self.make_event(event.request, index, events, event.timestamp)
            if (event != expected or canonical(event.model_dump()) != payload or hashed(event.model_dump()) != event_hash
                or revision != len(events) + 1 or event.revision != revision or event.previous_event_sha256 != previous
                or event.request.action_id != action_id or event.event_id != event_id or event.request_sha256 != request_hash):
                fail('REVIEW_STORE_INVALID', 'Decision history failed deterministic verification.')
            events.append(event); previous = event_hash
        return events

    def replay_budget(self, index, count):
        work = count * (len(index.observations) + len(index.sources) + len(index.conflicts) + count)
        if self.workspace.schema_version == 'review-v2':
            work += count * len(index.questions) * (len(index.observations)+len(index.sources)+len(index.conflicts)+len(index.claims))
        if work > MAX_REPLAY_WORK:
            fail('REVIEW_LIMIT', 'Deterministic review replay work limit exceeded.')

    def make_event(self, action, index, events, timestamp):
        if self.workspace.schema_version == 'review-v2':
            from .clarification import make_event
            return make_event(self, action, index, events, timestamp)
        self.replay_budget(index, len(events) + 1)
        w = self.workspace
        if action.workspace_id != w.workspace_id or action.site_scope != w.site_scope:
            fail('REVIEW_SCOPE', 'Action belongs to another workspace/building.')
        if action.parent_fingerprint != w.parent_fingerprint:
            fail('REVIEW_STALE_EVIDENCE', 'Action refers to a different evidence revision.')
        if action.actor_id != w.reviewer.actor_id or w.reviewer.role != 'reviewer':
            fail('REVIEW_UNAUTHORIZED', 'Invalid actor/authority.')
        if action.requested_status == 'verified':
            fail('REVIEW_VERIFICATION_UNAVAILABLE', 'Phase A does not establish engineering verification authority.')
        if action.expected_revision != len(events): fail('REVIEW_STALE_REVISION', 'Workspace revision changed; inspect and resubmit.')
        if len(events) >= MAX_EVENTS: fail('REVIEW_LIMIT', 'Decision event limit reached.')
        related = index.target(action.target_kind, action.target_id)
        if action.target_kind == 'question' and action.action != 'defer' and len(related['claims']) != 1:
            if action.action != 'correct' or action.correction.kind != 'source_clarification':
                fail('REVIEW_GROUP_ACTION', 'Select one claim; a question cannot implicitly decide its members.')
        evidence = {r['source_record_uid']: r for r in related['sources']}
        if not set(action.evidence_uids) <= set(evidence): fail('REVIEW_EVIDENCE', 'Unrelated or unknown source citation.')
        if action.correction: index.validate_correction(action.correction, related)
        last = next((e for e in reversed(events) if (e.request.target_kind, e.request.target_id) ==
                     (action.target_kind, action.target_id) or set(e.claim_uids).intersection(c['claim_uid'] for c in related['claims'])), None)
        if action.supersedes_event_id != (last.event_id if last else None):
            fail('REVIEW_CONFLICT', 'A replacement decision must explicitly supersede the latest target decision.')
        refs = [EvidenceReference(**{k: evidence[u][k] for k in EvidenceReference.model_fields})
                for u in sorted(action.evidence_uids)]
        return DecisionEvent(event_id=uid('review-event', w.workspace_id, action.action_id), revision=len(events)+1,
            timestamp=timestamp, request=action, request_sha256=hashed(action.model_dump()), reviewer=w.reviewer,
            reason_codes=['ENGINEERING_AUTHORITY_NOT_ESTABLISHED', 'GRAPH_APPLICATION_DEFERRED'],
            claim_uids=sorted(c['claim_uid'] for c in related['claims']),
            observation_uids=sorted(o['observation_uid'] for o in related['observations']),
            conflict_uids=sorted(c['conflict_uid'] for c in related['conflicts']), evidence=refs,
            previous_event_sha256=hashed(events[-1].model_dump()) if events else ZERO)

    def decide(self, request, credential_file, preview=False):
        action = self.action_model.model_validate(request)
        if len(canonical(action.model_dump()).encode()) > MAX_REQUEST_BYTES: fail('REVIEW_LIMIT', 'Request exceeds byte limit.')
        with self.transaction(write=not preview) as (db, credential_hash):
            self.authenticate(credential_file, credential_hash, action.actor_id)
            index = self.parent()
            events = self.checked_events(db, index)
            existing = next((e for e in events if e.request.action_id == action.action_id), None)
            if existing:
                if existing.request_sha256 != hashed(action.model_dump()):
                    fail('REVIEW_IDEMPOTENCY_CONFLICT', 'Action ID already has a different request.')
                return existing
            event = self.make_event(action, index, events, now())
            if len(canonical(event.model_dump()).encode()) > MAX_EVENT_BYTES:
                fail('REVIEW_LIMIT', 'Decision event payload exceeds byte limit.')
            if preview: return event
            checkpoint_hook('review_before_insert')
            payload = canonical(event.model_dump())
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (event.revision, action.action_id,
                event.event_id, event.request_sha256, payload, hashed(event.model_dump())))
            checkpoint_hook('review_before_commit')
            # Original package may have changed while a client was preparing this action.
            self.parent()
        checkpoint_hook('review_committed')
        return event

    def history(self, offset=0, limit=50):
        page_bounds(offset, limit)
        with self.transaction() as (db, _):
            events = self.checked_events(db, self.parent())
            return {'workspace_id': self.workspace.workspace_id, 'revision': len(events), 'total': len(events),
                    'offset': offset, 'events': [e.model_dump() for e in events[offset:offset+limit]]}

    def queue(self, offset=0, limit=50, include_accepted=False):
        page_bounds(offset, limit)
        with self.transaction() as (db, _):
            index = self.parent(); events = self.checked_events(db, index)
            rows = [{'target_kind': 'claim', 'target_id': c['claim_uid'], 'original_status': c['status'],
                     'subject_id': c['subject_id'], 'predicate': c['predicate'], 'object_id': c['object_id']}
                    for c in index.claims.values() if include_accepted or c['status'] not in {'supported_inference', 'verified'}]
            rows += [{'target_kind': 'question', 'target_id': q['review_id'], 'original_status': q['review_status'],
                      'question': q['question_or_test']} for q in index.questions.values()]
            rows.sort(key=lambda r: (r['target_kind'], r['target_id']))
            latest = {(e.request.target_kind, e.request.target_id): e for e in events}
            for row in rows:
                event = latest.get((row['target_kind'], row['target_id']))
                row['latest_event_id'] = event.event_id if event else None
                row['decision_state'] = 'pending' if event else 'unreviewed'
            return {'revision': len(events), 'total': len(rows), 'offset': offset, 'items': rows[offset:offset+limit]}

    def inspect(self, target_kind, target_id):
        with self.transaction() as (db, _):
            index = self.parent(); events = self.checked_events(db, index)
            return {'workspace': self.workspace.model_dump(), 'revision': len(events),
                    **index.target(target_kind, target_id),
                    'history': [e.model_dump() for e in events if (e.request.target_kind, e.request.target_id) == (target_kind, target_id)]}

    def groups(self, offset=0, limit=50):
        from .clarification import consolidate
        page_bounds(offset, limit)
        with self.transaction() as (db, _):
            index = self.parent(); events = self.checked_events(db, index)
            groups = consolidate(index, self.workspace.site_scope)
            return {'revision': len(events), 'total': len(groups), 'items': groups[offset:offset+limit]}

    def verify(self):
        with self.transaction() as (db, _):
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
                fail('REVIEW_STORE_INVALID', 'SQLite integrity check failed.')
            events = self.checked_events(db, self.parent())
            return {'schema_version': self.workspace.schema_version, 'workspace_id': self.workspace.workspace_id,
                    'revision': len(events), 'integrity': 'passed', 'parent_integrity': 'passed',
                    'last_event_sha256': hashed(events[-1].model_dump()) if events else ZERO,
                    'graph_changes': 'not_applied', 'verification_authority': 'not_established',
                    'manifest_sha256': sha_bytes(read_bytes(self.manifest_path, MAX_REQUEST_BYTES, private=True))}


def page_bounds(offset, limit):
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= MAX_EVENTS or not 1 <= limit <= 200:
        fail('REVIEW_LIMIT', 'Pagination requires offset 0..1000 and limit 1..200.')


class ParentIndex:
    def __init__(self, files, workspace):
        self.claims = {c['claim_uid']: c for c in json.loads(files['candidate_claims.json'])['records']}
        self.observations = {o['observation_uid']: o for o in json.loads(files['candidate_observations.json'])['records']}
        self.conflicts = json.loads(files['claim_conflicts.json'])['records']
        self.questions = {q['review_id']: q for q in json.loads(files['review_questions.json'])}
        self.sources = {r['source_record_uid']: r for r in map(json.loads, files['source_records.jsonl'].splitlines())}
        self.site_scope = workspace.site_scope
        if any(c['site_scope'] != workspace.site_scope for c in self.claims.values()):
            fail('REVIEW_SCOPE', 'Parent claim has foreign site scope.')

    def target(self, kind, identifier):
        if kind == 'group':
            from .clarification import consolidate
            group = next((g for g in consolidate(self, self.site_scope) if g['group_id']==identifier), None)
            if group is None: fail('REVIEW_SCOPE', 'Unknown root-cause group.')
            return {'question': None, **group}
        question = None
        if kind == 'claim' and identifier in self.claims:
            claims = [self.claims[identifier]]
        elif kind == 'question' and identifier in self.questions:
            question = self.questions[identifier]; affected = set(question['affected_assertion_ids'])
            claim_ids = {o['claim_uid'] for o in self.observations.values() if o['assertion_id'] in affected}
            claims = [self.claims[c] for c in sorted(claim_ids)]
        else: fail('REVIEW_SCOPE', 'Unknown target in this parent package.')
        claim_ids = {c['claim_uid'] for c in claims}
        observations = [o for o in self.observations.values() if o['claim_uid'] in claim_ids]
        conflicts = [c for c in self.conflicts if claim_ids.intersection(c['claim_uids'])]
        sources = {u for o in observations for u in o['resolved_source_uids']}
        locators = {s for c in conflicts for s in c['source_ids']}
        if question: locators.update(question['supporting_evidence_ids'])
        sources.update(u for u, r in self.sources.items() if r['source_id'] in locators)
        return {'question': question, 'claims': sorted(claims, key=lambda c: c['claim_uid']),
                'observations': sorted(observations, key=lambda o: o['observation_uid']),
                'conflicts': sorted(conflicts, key=lambda c: c['conflict_uid']),
                'alternatives': [self.claims[u] for u in sorted({u for c in conflicts for u in c['claim_uids']} - claim_ids)],
                'sources': [self.sources[u] for u in sorted(sources)]}

    def validate_correction(self, correction, related):
        if correction.kind == 'source_clarification': return
        from .vocabulary import POINTS, EQUIPMENT
        if len(related['claims']) != 1 or correction.subject_id != related['claims'][0]['subject_id']:
            fail('REVIEW_CORRECTION', 'Select one claim and preserve its subject identity.')
        entities = {c['subject_id'] for c in self.claims.values()} | {c['object_id'] for c in self.claims.values() if c['predicate'] != 'type'}
        if correction.subject_id not in entities: fail('REVIEW_CORRECTION', 'Unknown correction subject.')
        if correction.predicate == 'type':
            classes = POINTS if correction.subject_id.startswith('point/') else EQUIPMENT
            if correction.object_id not in classes: fail('REVIEW_CORRECTION', 'Use an allowed canonical Brick class.')
        elif correction.object_id not in entities or correction.object_id == correction.subject_id:
            fail('REVIEW_CORRECTION', 'Unknown or self-referential correction endpoint.')
        elif not correction.subject_id.startswith('equipment/') or not correction.object_id.startswith('point/' if correction.predicate == 'hasPoint' else 'equipment/'):
            fail('REVIEW_CORRECTION', 'Correction endpoint types disagree with predicate.')
