"""FTS5 BM25 leg. fts_query and the -bm25 ranking ported from deal-terms-desk retrieval/bm25.py on 2026-11-03
(contract scoping and the second FTS table dropped; Hit keyed on unit_id)."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

TOKEN = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Hit:
    unit_id: str
    score: float


def fts_query(q: str) -> str:
    """Every token quoted and OR-joined, so FTS5 operators in user text are inert."""
    tokens = [t for t in dict.fromkeys(TOKEN.findall(q.lower())) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in tokens)


def search_bm25(conn: sqlite3.Connection, query: str, k: int) -> list[Hit]:
    match = fts_query(query)
    if not match:
        return []
    sql = ("SELECT u.id, s.score FROM"
           " (SELECT rowid AS rid, -bm25(units_fts) AS score FROM units_fts WHERE units_fts MATCH ?"
           "  ORDER BY bm25(units_fts), rowid LIMIT ?) s JOIN units u ON u.rowid = s.rid"
           " ORDER BY s.score DESC, u.rowid")
    return [Hit(*row) for row in conn.execute(sql, (match, k))]
