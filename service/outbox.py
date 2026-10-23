import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    uid: str
    payload: dict
    snapshot: bytes | None
    attempts: int


class Outbox:
    """Persistent queue of events awaiting delivery to Tryton."""

    def __init__(self, path, max_pending=2000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('''CREATE TABLE IF NOT EXISTS events (
            uid TEXT PRIMARY KEY, payload TEXT NOT NULL, snapshot BLOB,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            rejections INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL,
            error TEXT, created REAL NOT NULL)''')

    def add(self, uid, payload, snapshot, now=None):
        now = time.time() if now is None else now
        with self._lock:
            (pending,) = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE status = 'pending'").fetchone()
            if pending >= self._max_pending:
                self._db.execute("""DELETE FROM events WHERE uid = (SELECT uid FROM events
                    WHERE status = 'pending' ORDER BY created LIMIT 1)""")
                log.warning('Cola de eventos llena (%d): se descarta el más antiguo', pending)
            self._db.execute(
                'INSERT INTO events (uid, payload, snapshot, next_try, created) VALUES (?, ?, ?, ?, ?)',
                (uid, json.dumps(payload), snapshot, now, now))

    def claim_due(self, now):
        with self._lock:
            row = self._db.execute("""SELECT uid, payload, snapshot, attempts FROM events
                WHERE status = 'pending' AND next_try <= ? ORDER BY created LIMIT 1""",
                (now,)).fetchone()
        return Item(row[0], json.loads(row[1]), row[2], row[3]) if row else None

    def done(self, uid):
        with self._lock:
            self._db.execute('DELETE FROM events WHERE uid = ?', (uid,))

    def defer(self, uid, delay, now):
        """Transient failure (Tryton unavailable): retry later."""
        with self._lock:
            self._db.execute(
                'UPDATE events SET attempts = attempts + 1, next_try = ? WHERE uid = ?',
                (now + delay, uid))

    def reject(self, uid, error, now, limit, delay):
        """Tryton rejected the event; after `limit` rejections, quarantine it as failed."""
        with self._lock:
            self._db.execute("""UPDATE events SET attempts = attempts + 1,
                rejections = rejections + 1, error = ?, next_try = ?,
                status = CASE WHEN rejections + 1 >= ? THEN 'failed' ELSE status END
                WHERE uid = ?""", (error, now + delay, limit, uid))

    def count(self, status='pending'):
        with self._lock:
            return self._db.execute(
                'SELECT COUNT(*) FROM events WHERE status = ?', (status,)).fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()
