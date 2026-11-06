"""Cluster bootstrap, ported from deal-terms-desk `evals/bootstrap.py` (`cluster_bootstrap`) on 2026-11-03.

Same algorithm and seed handling. The 95% interval and the resample count are facts (`m1_ci_pct`, `m1_n_boot`).
"""
from __future__ import annotations

import random

CI_LOW = 0.025
CI_HIGH = 0.975
N_BOOT = 2000


def cluster_bootstrap(values_by_cluster: dict, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    clusters = sorted(c for c, vs in values_by_cluster.items() if vs)
    if not clusters:
        raise ValueError("no values to bootstrap")
    sums = [sum(values_by_cluster[c]) for c in clusters]
    counts = [len(values_by_cluster[c]) for c in clusters]
    mean = sum(sums) / sum(counts)
    rng = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        total = 0.0
        n = 0
        for _ in clusters:
            j = rng.randrange(len(clusters))
            total += sums[j]
            n += counts[j]
        stats.append(total / n)
    stats.sort()
    lo = stats[int(CI_LOW * n_boot)]
    hi = stats[min(n_boot - 1, int(CI_HIGH * n_boot))]
    return {"mean": mean, "lo": lo, "hi": hi, "n_items": sum(counts), "n_clusters": len(clusters)}
