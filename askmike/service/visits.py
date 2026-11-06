"""The private visitor log: every question and pasted posting, the answer's outcome and its turn, and no
address, header or user agent. It lives only in /var/lib/ask on the box and under $ASK_PRIVATE_DIR/logs.

The turn of each answer is kept here so a follow-up names it by an unguessable id (`prev`) and the server
looks it up; the client never sends turn text."""
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from askmike.answer.answerer import Outcome
from askmike.answer.evidence import Claim, Turn

COLUMNS = ("id", "ts", "kind", "input", "prev_id", "outcome", "fallback_reason", "model", "served_from",
           "turn_text", "kept_json", "template_sha")
SCHEMA = ("CREATE TABLE IF NOT EXISTS visits(id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, input TEXT,"
          " prev_id TEXT, outcome TEXT, fallback_reason TEXT, model TEXT, served_from TEXT, turn_text TEXT,"
          " kept_json TEXT, template_sha TEXT)")
ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{22}")  # secrets.token_urlsafe(16)


class Visits:
    def __init__(self, db):
        db = Path(db)
        db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(SCHEMA)
        self._conn.execute("CREATE INDEX IF NOT EXISTS visits_ts ON visits(ts)")

    @staticmethod
    def new_id() -> str:
        return secrets.token_urlsafe(16)

    def record(self, visit_id: str, kind: str, text: str, prev_id: str | None, outcome: Outcome,
               served_from: str) -> None:
        turn = outcome.turn(text)
        kept = None if turn is None else json.dumps(
            [{"type": c.type, "need": c.need, "req": c.req, "text": c.text, "units": list(c.units)}
             for c in turn.kept])
        row = (visit_id, datetime.now(timezone.utc).isoformat(), kind, text, prev_id, outcome.outcome,
               outcome.fallback_reason, outcome.model, served_from, None if turn is None else turn.text, kept,
               outcome.template_sha)
        with self._lock:
            self._conn.execute(f"INSERT OR REPLACE INTO visits VALUES ({','.join('?' * len(COLUMNS))})", row)

    def previous(self, prev_id) -> Turn | None:
        """The stored turn for an answer id; None for a malformed or unknown id or a visit with no turn or no kept claims.
        Never raises on bad input."""
        if not isinstance(prev_id, str) or not ID_SHAPE.fullmatch(prev_id):
            return None
        with self._lock:
            row = self._conn.execute("SELECT turn_text, kept_json FROM visits WHERE id = ?",
                                     (prev_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            kept = tuple(Claim(c["type"], c["need"], c["req"], c["text"], tuple(c["units"]))
                         for c in json.loads(row[1] or "[]"))
        except (ValueError, KeyError, TypeError):
            return None
        if not kept:
            return None
        return Turn(row[0], kept)

    def export(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {','.join(COLUMNS)} FROM visits ORDER BY ts, rowid").fetchall()
        return [dict(zip(COLUMNS, r)) for r in rows]
