"""Candidate pooling: the union of the top-10 of every system whose top-10 tuning or the report could choose."""
from __future__ import annotations

import itertools

from askmike.evals.keyword import keyword_rank
from askmike.retrieval.ladder import Ladder, Settings

GRID_DEPTH = (20, 50)
GRID_K0 = (10, 60)
pool_stats = {"queries": 0, "cap_hits": 0}


class OnceEmbedder:
    """Embeds each distinct query once (the last one is remembered), so the nine ladder runs share one vector."""

    def __init__(self, inner):
        self.inner, self._q, self._v = inner, None, None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def embed_query(self, q):
        if q != self._q:
            self._q, self._v = q, self.inner.embed_query(q)
        return self._v


def grid_ladders(ladder, skills) -> list:
    return [Ladder(ladder.conn, ladder.embedder, skills, Settings(depth=d, rrf_k0=k0))
            for d, k0 in itertools.product(GRID_DEPTH, GRID_K0)]


def pool_candidates(query, ladder, units, skills, k_each=10, cap=60, grid=None) -> list[str]:
    runs = [ladder.run("R1", query, k_each), ladder.run("R2", query, k_each),
            keyword_rank(query, units, skills, k_each)]
    for lad in (grid if grid is not None else grid_ladders(ladder, skills)):
        runs += [lad.run("R3", query, k_each), lad.run("R4", query, k_each)]
    best: dict = {}
    for run in runs:
        for rank, h in enumerate(run):
            best[h.unit_id] = min(best.get(h.unit_id, rank), rank)
    order = sorted(best, key=lambda u: (best[u], u))
    pool_stats["queries"] += 1
    if len(order) > cap:
        pool_stats["cap_hits"] += 1
    return order[:cap]
