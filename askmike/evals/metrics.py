"""Binary-relevance retrieval metrics. Every metric is None when the query has no relevant unit (unsupported)."""
from __future__ import annotations

import math


def recall_at_k(ranked, relevant, k):
    if not relevant:
        return None
    return len(set(ranked[:k]) & set(relevant)) / len(relevant)


def mrr(ranked, relevant, k=10):
    if not relevant:
        return None
    for i, u in enumerate(ranked[:k], 1):
        if u in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked, relevant, k=10):
    if not relevant:
        return None
    dcg = sum(1.0 / math.log2(i + 1) for i, u in enumerate(ranked[:k], 1) if u in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal


def coverage_at_k(per_req, k):
    """Share of supportable requirements (non-empty relevant) with at least one relevant unit in the top k."""
    sup = [(r, rel) for r, rel in per_req if rel]
    if not sup:
        return None
    return sum(1 for r, rel in sup if set(r[:k]) & set(rel)) / len(sup)


def cohen_kappa(a, b):
    """Kappa over the (query, unit) pairs both judges decided. None when chance agreement is 1 (undefined)."""
    keys = sorted(set(a) & set(b))
    if not keys:
        raise ValueError("no shared (query, unit) pairs")
    n = len(keys)
    po = sum(1 for key in keys if a[key] == b[key]) / n
    pa = sum(1 for key in keys if a[key]) / n
    pb = sum(1 for key in keys if b[key]) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def kendall_tau(order_a, order_b):
    """Kendall tau-a between two orderings of the same items."""
    if set(order_a) != set(order_b) or len(order_a) != len(set(order_a)):
        raise ValueError("orders must rank the same distinct items")
    pos = {x: i for i, x in enumerate(order_b)}
    n = len(order_a)
    if n < 2:
        raise ValueError("need at least two items")
    s = 0
    for i in range(n):
        for j in range(i + 1, n):
            s += 1 if pos[order_a[i]] < pos[order_a[j]] else -1
    return s / (n * (n - 1) / 2)
