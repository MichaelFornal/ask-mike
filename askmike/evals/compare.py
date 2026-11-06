"""Paired cluster bootstrap, adapted from deal-terms-desk `evals/compare.py` (`paired_bootstrap`) on 2026-11-03.

Changes: the cluster key is generic, items whose metric is None (unsupported) are skipped, and `macro=True`
averages the differences within each cluster first (per-JD coverage) so each cluster counts once.
"""
from __future__ import annotations

from collections import defaultdict

from askmike.evals.bootstrap import cluster_bootstrap


def _select(rows: dict, split):
    return {i: r for i, r in rows.items() if split is None or r["split"] == split}


def paired_bootstrap(a_items: dict, b_items: dict, metric: str, cluster_key: str = "cluster",
                     split: str | None = "report", n_boot: int = 2000, seed: int = 0, macro: bool = False) -> dict:
    """b minus a on the same items. Items are {item_id: row}; rows carry the cluster key, split and metric keys."""
    a, b = _select(a_items, split), _select(b_items, split)
    if set(a) != set(b):
        raise ValueError(f"rungs were scored on different items ({len(set(a) - set(b))} only in the first, "
                         f"{len(set(b) - set(a))} only in the second); rerun both on the same labels")
    ids = sorted(i for i in a if a[i].get(metric) is not None and b[i].get(metric) is not None)
    if not ids:
        raise ValueError("no items to compare after the split and None filters")
    diffs = defaultdict(list)
    for i in ids:
        diffs[a[i][cluster_key]].append(b[i][metric] - a[i][metric])
    if macro:
        diffs = {c: [sum(v) / len(v)] for c, v in diffs.items()}
    ci = cluster_bootstrap(diffs, n_boot=n_boot, seed=seed)
    return {"delta": ci["mean"], "lo": ci["lo"], "hi": ci["hi"], "n_items": ci["n_items"],
            "n_clusters": ci["n_clusters"]}
