"""SQLite transactional lifecycle, immutable stage commits and local redacted events."""
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from .errors import EngineError

VERSION = 'run-state-v1'
TRANSITIONS = {'CREATED': {'RUNNING', 'FAILED', 'BLOCKED'}, 'RUNNING': {'COMPLETED', 'FAILED', 'PARTIAL', 'BLOCKED'},
               'FAILED': {'RUNNING', 'BLOCKED'}, 'PARTIAL': {'RUNNING', 'BLOCKED'}, 'BLOCKED': {'RUNNING'}, 'COMPLETED': set()}


def now(): return datetime.now(timezone.utc).isoformat()

class RunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY,state TEXT NOT NULL,version TEXT NOT NULL,
            fingerprint TEXT NOT NULL,arguments TEXT NOT NULL,started_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stages(run_id TEXT NOT NULL,name TEXT NOT NULL,artifacts TEXT NOT NULL,
            committed_at TEXT NOT NULL,PRIMARY KEY(run_id,name),FOREIGN KEY(run_id) REFERENCES runs(run_id));
        CREATE TABLE IF NOT EXISTS attempts(attempt_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,stage TEXT NOT NULL,
            status TEXT NOT NULL,error TEXT,started_at TEXT NOT NULL,finished_at TEXT,FOREIGN KEY(run_id) REFERENCES runs(run_id));
        CREATE TABLE IF NOT EXISTS artifacts(run_id TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,
            PRIMARY KEY(run_id,path),FOREIGN KEY(run_id) REFERENCES runs(run_id));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,stage TEXT NOT NULL,
            operation TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT,attempt_id TEXT,duration REAL,
            timestamp TEXT NOT NULL,FOREIGN KEY(run_id) REFERENCES runs(run_id));
        ''')

    def create(self, run_id, fingerprint, arguments):
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',
                (run_id, 'CREATED', VERSION, fingerprint, json.dumps(arguments, sort_keys=True), now(), now()))
        self.event(run_id, 'run', 'create', 'CREATED')

    def row(self):
        rows = self.db.execute('SELECT run_id,state,version,fingerprint,arguments,started_at FROM runs').fetchall()
        if len(rows) != 1: raise EngineError('STATE_INVALID', 'Expected exactly one run in isolated store.')
        return dict(zip(('run_id', 'state', 'version', 'fingerprint', 'arguments', 'started_at'), rows[0]))

    def transition(self, run_id, target):
        with self.db:
            state = self.db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0]
            if target not in TRANSITIONS.get(state, set()):
                raise EngineError('INVALID_TRANSITION', 'Illegal lifecycle transition ' + state + ' -> ' + target)
            self.db.execute('UPDATE runs SET state=?,updated_at=? WHERE run_id=?', (target, now(), run_id))
            self._event(run_id, 'run', 'transition', target)

    def _event(self, run_id, stage, operation, status, error_code=None, attempt_id=None, duration=None):
        self.db.execute('INSERT INTO events(run_id,stage,operation,status,error_code,attempt_id,duration,timestamp) VALUES(?,?,?,?,?,?,?,?)',
                        (run_id, stage, operation, status, error_code, attempt_id, duration, now()))

    def event(self, *args, **kwargs):
        with self.db: self._event(*args, **kwargs)

    def commit_stage(self, run_id, name, paths, duration=None):
        artifacts = {str(Path(p).resolve()): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
        with self.db:
            if self.db.execute('SELECT 1 FROM stages WHERE run_id=? AND name=?', (run_id, name)).fetchone():
                raise EngineError('DUPLICATE_STAGE_COMMIT', 'Stage already committed: ' + name)
            self.db.execute('INSERT INTO stages VALUES(?,?,?,?)', (run_id, name, json.dumps(artifacts, sort_keys=True), now()))
            for path, sha in artifacts.items(): self.db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?)', (run_id, path, sha))
            self._event(run_id, name, 'checkpoint', 'COMMITTED', duration=duration)

    def committed(self, run_id, name):
        row = self.db.execute('SELECT artifacts FROM stages WHERE run_id=? AND name=?', (run_id, name)).fetchone()
        if not row: return False
        for path, sha in json.loads(row[0]).items():
            if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha:
                raise EngineError('CHECKPOINT_INVALID', 'Checkpoint artifact missing or changed; unsafe resume refused.')
        return True

    def begin_attempt(self, run_id, attempt_id, stage):
        with self.db:
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)', (attempt_id, run_id, stage, 'STARTED', None, now(), None))
            self._event(run_id, stage, 'attempt', 'STARTED', attempt_id=attempt_id)

    def finish_attempt(self, run_id, attempt_id, status, error=None):
        started = self.db.execute('SELECT started_at FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        duration = (datetime.now(timezone.utc) - datetime.fromisoformat(started[0])).total_seconds() if started else None
        with self.db:
            self.db.execute('UPDATE attempts SET status=?,error=?,finished_at=? WHERE attempt_id=?',
                            (status, json.dumps(error), now(), attempt_id))
            self._event(run_id, 'provider', 'attempt', status, error_code=(error or {}).get('error_code'), attempt_id=attempt_id, duration=duration)

    def attempts(self, run_id):
        return self.db.execute('SELECT attempt_id,status FROM attempts WHERE run_id=?', (run_id,)).fetchall()

    def events(self, run_id):
        columns = ('run_id', 'stage', 'operation', 'status', 'error_code', 'attempt_id', 'duration_seconds', 'timestamp')
        return [dict(zip(columns, row)) for row in self.db.execute(
            'SELECT run_id,stage,operation,status,error_code,attempt_id,duration,timestamp FROM events WHERE run_id=? ORDER BY id', (run_id,))]

    def record_publication(self, run_id, destination):
        with self.db:
            for path in sorted(Path(destination).rglob('*')):
                if path.is_file():
                    self.db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?)',
                        (run_id, str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()))
            self._event(run_id, 'publication', 'publish', 'COMPLETED')

    def close(self): self.db.close()
