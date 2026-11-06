"""Dense (sqlite-vec) leg, adapted from deal-terms-desk retrieval/dense.py on 2026-11-03."""
from __future__ import annotations

import sqlite3

import sqlite_vec

from askmike.retrieval.bm25 import Hit


def search_dense(conn: sqlite3.Connection, qvec, k: int) -> list[Hit]:
    """k nearest units by cosine distance; score = 1 - distance."""
    sql = ("SELECT u.id, v.distance FROM"
           " (SELECT unit_rowid, distance FROM units_vec WHERE embedding MATCH ? AND k = ?) v"
           " JOIN units u ON u.rowid = v.unit_rowid ORDER BY v.distance, u.rowid")
    return [Hit(uid, 1.0 - dist) for uid, dist in conn.execute(sql, (sqlite_vec.serialize_float32(list(qvec)), k))]
