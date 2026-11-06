"""SQLite index over the public corpus: FTS5 + sqlite-vec, built atomically.

connect/sha1/open_cache/fill_cache ported from deal-terms-desk retrieval/vectors.py and the FTS5 schema from
retrieval/index.py on 2026-11-03.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from askmike.retrieval.models import EMBED_DIM

CACHE_SCHEMA = ("CREATE TABLE IF NOT EXISTS emb(model TEXT NOT NULL, sha1 TEXT NOT NULL,"
                " vec BLOB NOT NULL, PRIMARY KEY(model, sha1))")
SCHEMA = f"""
CREATE TABLE units(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, kind TEXT, subject TEXT, text TEXT);
CREATE VIRTUAL TABLE units_fts USING fts5(text, tokenize='porter unicode61');
CREATE VIRTUAL TABLE units_vec USING vec0(unit_rowid INTEGER PRIMARY KEY,
    embedding float[{EMBED_DIM}] distance_metric=cosine);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


STALE = "index is stale: rebuild with `python -m askmike.retrieval index`"
MISSING = "index is missing: build it with `python -m askmike.retrieval index`"


class StaleIndex(RuntimeError):
    """The index was not built from the corpus on disk (meta.corpus_sha256 differs, or there is no index)."""


@dataclass(frozen=True)
class IndexStats:
    units: int
    embedded: int
    cached: int


def default_db() -> Path:
    return Path(__file__).resolve().parents[2] / "build" / "index.db"


def default_cache() -> Path:
    return default_db().with_name("emb_cache.db")


def unit_text(u: dict) -> str:
    parts = [u.get("claim") or "", u.get("detail") or "", u.get("subject") or "",
             ", ".join(s["term"] for s in u.get("skills") or [])]
    return "\n".join(p for p in parts if p)


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def check_fresh(conn: sqlite3.Connection, units_path, model: str | None = None) -> None:
    """Raise StaleIndex unless the index's meta.corpus_sha256 is the sha256 of units_path and, when `model` is
    given, meta.model equals it."""
    want = hashlib.sha256(Path(units_path).read_bytes()).hexdigest()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'corpus_sha256'").fetchone()
    except sqlite3.DatabaseError:
        row = None
    if row is None or row[0] != want:
        raise StaleIndex(STALE)
    if model is not None:
        try:
            got = conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        except sqlite3.DatabaseError:
            got = None
        if got is None or got[0] != model:
            raise StaleIndex(STALE)


def open_index(db_path, units_path, model: str | None = None) -> sqlite3.Connection:
    """connect() for querying: refuses a missing index or one built from another corpus. Every step that
    searches build/index.db (evals run, pool, tune; retrieval search) opens it through here."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise StaleIndex(MISSING)
    conn = connect(db_path)
    try:
        check_fresh(conn, units_path, model)
    except BaseException:
        conn.close()
        raise
    return conn


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def open_cache(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(path)
    cache.execute(CACHE_SCHEMA)
    cache.commit()
    return cache


def fill_cache(cache: sqlite3.Connection, embedder, texts: list[str], batch: int = 32) -> dict:
    """Embed every distinct text not yet cached for this model; commit per batch."""
    have = {r[0] for r in cache.execute("SELECT sha1 FROM emb WHERE model = ?", (embedder.name,))}
    distinct = {sha1(t): t for t in texts}
    todo = [t for h, t in distinct.items() if h not in have]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = embedder.embed_passages(chunk)
        cache.executemany("INSERT OR REPLACE INTO emb VALUES (?, ?, ?)",
                          [(embedder.name, sha1(t), sqlite_vec.serialize_float32(v)) for t, v in zip(chunk, vecs)])
        cache.commit()
    return {"embedded": len(todo), "cached": len(distinct) - len(todo)}


def build_index(corpus_dir: Path, db_path: Path, embedder, cache_path: Path | None = None) -> IndexStats:
    corpus_dir, db_path = Path(corpus_dir), Path(db_path)
    raw = (corpus_dir / "units.jsonl").read_bytes()
    units = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    texts = [unit_text(u) for u in units]
    cache = open_cache(cache_path or default_cache())
    try:
        got = fill_cache(cache, embedder, texts)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = db_path.with_name(db_path.name + ".tmp")
        for p in (tmp, tmp.with_name(tmp.name + "-journal")):
            p.unlink(missing_ok=True)
        conn = connect(tmp)
        try:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN")
            for rid, (u, text) in enumerate(zip(units, texts), start=1):
                vec = cache.execute("SELECT vec FROM emb WHERE model = ? AND sha1 = ?",
                                    (embedder.name, sha1(text))).fetchone()[0]
                conn.execute("INSERT INTO units(rowid, id, kind, subject, text) VALUES (?, ?, ?, ?, ?)",
                             (rid, u["id"], u.get("kind"), u.get("subject"), text))
                conn.execute("INSERT INTO units_fts(rowid, text) VALUES (?, ?)", (rid, text))
                conn.execute("INSERT INTO units_vec(unit_rowid, embedding) VALUES (?, ?)", (rid, vec))
            conn.executemany("INSERT INTO meta VALUES (?, ?)",
                             [("corpus_sha256", hashlib.sha256(raw).hexdigest()), ("model", embedder.name)])
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, db_path)
    finally:
        cache.close()
    return IndexStats(len(units), got["embedded"], got["cached"])
