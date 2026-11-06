"""Near-duplicate hints: token Jaccard over claim words, within one subject."""
from __future__ import annotations

import re

_STOP = frozenset("a an the and or of to in on at for with by from as is was were be been it its this that "
                  "he his mike".split())


def _tokens(claim: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", claim.lower()) if w not in _STOP)


def near_dupes(units, threshold: float = 0.6) -> dict[str, list[str]]:
    by_subject: dict[str, list] = {}
    for u in units:
        by_subject.setdefault(u.subject, []).append((u.id, _tokens(u.claim)))
    out: dict[str, list[str]] = {}
    for group in by_subject.values():
        for i, (ida, ta) in enumerate(group):
            for idb, tb in group[i + 1:]:
                union = ta | tb
                if union and len(ta & tb) / len(union) >= threshold:
                    out.setdefault(ida, []).append(idb)
                    out.setdefault(idb, []).append(ida)
    return {k: sorted(v) for k, v in sorted(out.items())}
