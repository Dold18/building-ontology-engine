"""Durable client-side spend reservation ledger.

A new request may only be sent if its reserved MAXIMUM cost fits in the remaining allowance. A request is durably
marked SENDING before any bytes leave, so a crash can never lead to an automatic second paid request: on recovery a
SENDING row becomes AMBIGUOUS and keeps its full reservation against the cap until the owner resolves it.

Limits (disclosed, not hidden): this is a client-side control. It cannot stop the provider billing for a request whose
response was lost, and it does not replace a provider-side workspace spend limit, which the owner must configure.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from ..errors import EngineError

OPEN = ('RESERVED', 'SENDING', 'AMBIGUOUS')
RESEND_NOTE = 'OWNER_RESEND_AUTHORIZED'      # an ambiguous request counted as charged at its maximum; one resend allowed
BILLING_DISCLOSURE = ('Client-side ledger only: bounds what this program will start, not what the provider bills. '
                      'Lost or ambiguous responses may still have been billed; configure a provider workspace spend limit.')


def _now():
    return datetime.now(timezone.utc).isoformat()


class BudgetLedger:
    def __init__(self, path, cap_usd=None):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(request_key TEXT PRIMARY KEY, model TEXT NOT NULL, state TEXT NOT NULL,
            reserved_usd REAL NOT NULL, actual_usd REAL, usage TEXT, note TEXT, updated_at TEXT NOT NULL);
        ''')
        row = self.db.execute("SELECT value FROM meta WHERE key='cap_usd'").fetchone()
        if row is None:
            if cap_usd is None or cap_usd < 0:
                raise EngineError('BUDGET_CAP_REQUIRED', 'A new ledger needs an explicit non-negative spending cap.')
            with self.db:
                self.db.execute("INSERT INTO meta VALUES('cap_usd', ?)", (repr(float(cap_usd)),))
        elif cap_usd is not None and float(row[0]) != float(cap_usd):
            raise EngineError('BUDGET_CAP_MISMATCH', 'Ledger cap is fixed at creation; changing it needs an explicit owner action.')

    @property
    def cap_usd(self):
        return float(self.db.execute("SELECT value FROM meta WHERE key='cap_usd'").fetchone()[0])

    def committed_usd(self):
        total = 0.0
        for state, reserved, actual in self.db.execute('SELECT state, reserved_usd, actual_usd FROM requests'):
            if state in OPEN:
                total += reserved
            elif state == 'SETTLED':
                total += actual if actual is not None else reserved
        return total

    def remaining_usd(self):
        return self.cap_usd - self.committed_usd()

    def state(self, key):
        row = self.db.execute('SELECT state FROM requests WHERE request_key=?', (key,)).fetchone()
        return row[0] if row else None

    def reserve(self, key, model, max_cost_usd):
        """Atomically reserve a request's maximum cost; refuse duplicates and anything that exceeds the cap."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE') if not self.db.in_transaction else None
            state = self.state(key)
            if state not in (None, 'RELEASED'):
                raise EngineError('DUPLICATE_REQUEST', 'This exact request already has ledger state %s; no second paid request.' % state)
            if max_cost_usd > self.remaining_usd() + 1e-12:
                raise EngineError('BUDGET_EXCEEDED', 'Reserved maximum %.6f USD exceeds remaining allowance %.6f USD; no request sent.'
                                  % (max_cost_usd, self.remaining_usd()))
            self.db.execute('INSERT OR REPLACE INTO requests VALUES(?,?,?,?,NULL,NULL,NULL,?)', (key, model, 'RESERVED', max_cost_usd, _now()))

    def _transition(self, key, allowed, target, **fields):
        with self.db:
            state = self.state(key)
            if state not in allowed:
                raise EngineError('LEDGER_STATE_INVALID', 'Request %s... is %s; cannot become %s.' % (key[:12], state, target))
            sets = ['state=?', 'updated_at=?'] + [k + '=?' for k in fields]
            self.db.execute('UPDATE requests SET ' + ','.join(sets) + ' WHERE request_key=?',
                            (target, _now(), *fields.values(), key))

    def mark_sending(self, key):
        self._transition(key, ('RESERVED',), 'SENDING')

    def settle(self, key, actual_usd, usage):
        self._transition(key, ('SENDING', 'AMBIGUOUS'), 'SETTLED', actual_usd=actual_usd, usage=json.dumps(usage, sort_keys=True))

    def release(self, key, reason):
        """Only for requests that provably never started processing (rejected or never sent)."""
        self._transition(key, ('RESERVED', 'SENDING'), 'RELEASED', note=reason)

    def mark_ambiguous(self, key, reason):
        self._transition(key, ('SENDING',), 'AMBIGUOUS', note=reason)

    def resolve(self, key, billed, actual_usd=None, note='owner resolution'):
        """Explicit owner decision on an ambiguous request."""
        if billed:
            self._transition(key, ('AMBIGUOUS',), 'SETTLED', actual_usd=actual_usd, note=note,
                             usage=json.dumps({'resolved_by_owner': True}))
        else:
            self._transition(key, ('AMBIGUOUS',), 'RELEASED', note=note)

    def resolve_charged_and_resend(self, key, note):
        """Owner decision for a request whose response was lost: count it as charged at its full reserved maximum (the row
        is kept, never released) and allow its body to be sent exactly once more under a derived ledger key."""
        row = self.db.execute('SELECT reserved_usd FROM requests WHERE request_key=?', (key,)).fetchone()
        if row is None:
            raise EngineError('LEDGER_STATE_INVALID', 'Unknown request %s...' % key[:12])
        self._transition(key, ('AMBIGUOUS',), 'SETTLED', actual_usd=row[0], note=RESEND_NOTE + ': ' + note,
                         usage=json.dumps({'resolved_by_owner': True, 'counted_at_reserved_maximum': True}))

    def effective_key(self, key):
        """The ledger key to use for a request body: its own key, or the single resend key the owner authorized."""
        row = self.db.execute('SELECT state, note FROM requests WHERE request_key=?', (key,)).fetchone()
        if row and row[0] == 'SETTLED' and (row[1] or '').startswith(RESEND_NOTE):
            return key + ':resend-1'
        return key

    def recover(self):
        """Crash recovery: anything left SENDING may have been billed. Never auto-resend."""
        with self.db:
            keys = [r[0] for r in self.db.execute("SELECT request_key FROM requests WHERE state='SENDING'")]
            self.db.execute("UPDATE requests SET state='AMBIGUOUS', note='recovered after interruption', updated_at=? WHERE state='SENDING'", (_now(),))
        return keys

    def summary(self):
        counts = {}
        for state, n in self.db.execute('SELECT state, COUNT(*) FROM requests GROUP BY state'):
            counts[state] = n
        return {'cap_usd': self.cap_usd, 'committed_usd': round(self.committed_usd(), 9),
                'remaining_usd': round(self.remaining_usd(), 9), 'requests_by_state': counts,
                'disclosure': BILLING_DISCLOSURE}

    def close(self):
        self.db.close()
