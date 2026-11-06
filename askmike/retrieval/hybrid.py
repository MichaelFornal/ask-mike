"""Reciprocal rank fusion, ported from deal-terms-desk retrieval/hybrid.py on 2026-11-03 (keyed on unit_id)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from askmike.retrieval.bm25 import Hit


def rrf(rankings: list[list[Hit]], k0: int = 60, k: int = 10) -> list[Hit]:
    score: dict[str, float] = defaultdict(float)
    first: dict[str, Hit] = {}
    for ranking in rankings:
        for rank, h in enumerate(ranking, start=1):
            score[h.unit_id] += 1.0 / (k0 + rank)
            first.setdefault(h.unit_id, h)
    order = sorted(score, key=lambda uid: (-score[uid], uid))
    return [replace(first[uid], score=score[uid]) for uid in order[:k]]
