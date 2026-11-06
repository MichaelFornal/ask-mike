"""Answer cache, after Deal Terms Desk's service/cache.py: SQLite in WAL mode behind one lock.

The key is everything that shapes an answer, so a change to the template, corpus, index or gate config misses
on its own and no TTL is needed. Only an `answer` outcome is stored."""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from askmike.answer.config import ANSWER_EFFORT, MAX_TOKENS

SCHEMA = ("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)")


def _collapse(s) -> str:
    return " ".join((s or "").split())


class AnswerCache:
    def __init__(self, db):
        db = Path(db)
        db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(SCHEMA)

    @staticmethod
    def key(kind: str, model: str, text: str, prev_text: str | None, ids: dict) -> str:
        parts = [kind, model, ANSWER_EFFORT, MAX_TOKENS, _collapse(text), _collapse(prev_text),
                 {k: ids[k] for k in ("template_sha", "corpus_sha", "index_sha", "gate_sha")}]
        return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, payload: dict) -> None:
        if payload.get("outcome") != "answer":
            return
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                               (key, json.dumps(payload, sort_keys=True), time.time()))
