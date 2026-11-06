"""Skill-ledger alias expansion for the BM25 leg (R4)."""
from __future__ import annotations

from askmike.gates.checks import _term_re


def expand_query(query: str, skills: list[dict]) -> str:
    """Append, once each and in ledger order, the term and aliases of every entry the query mentions."""
    extra: list[str] = []
    for s in skills:
        names = [s["term"], *s.get("aliases", [])]
        if any(_term_re(n).search(query) for n in names):
            extra += [n for n in names if n not in extra]
    return " ".join([query, *extra]) if extra else query
