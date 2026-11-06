"""Tune the shared (depth, rrf_k0) on the tune split, adapted from deal-terms-desk evals/tune.py on 2026-11-03.

Rule + evidence + tuned_on are written to retrieval/settings.json. Only aggregate means are published.
"""
from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass
from pathlib import Path

from askmike.evals.metrics import recall_at_k
from askmike.evals.pool import GRID_DEPTH, GRID_K0

RULE = ("Highest mean recall@10 of R4 on tune-split jdreq items with a non-empty judge-A label "
        "(ties: smaller depth, then larger k0); R3 uses the same setting.")


@dataclass
class TuneResult:
    depth: int
    rrf_k0: int
    rule: str
    evidence: dict
    tuned_on: dict


def tune(items, ladder_factory, labels="A") -> TuneResult:
    """items: query rows {query_id, cluster, split, text, relevant}; ladder_factory(depth, k0) -> ladder."""
    bad = [i["query_id"] for i in items if i["split"] != "tune"]
    if bad:
        raise ValueError(f"tune() was handed {len(bad)} non-tune item(s)")
    used = [i for i in items if i["relevant"]]
    if not used:
        raise ValueError("no labelled supportable tune items")
    evidence = {"R3": {}, "R4": {}}
    for d, k0 in itertools.product(GRID_DEPTH, GRID_K0):
        lad = ladder_factory(d, k0)
        for rung in ("R3", "R4"):
            rs = [recall_at_k([h.unit_id for h in lad.run(rung, i["text"], 10)], set(i["relevant"]), 10) for i in used]
            evidence[rung][f"d{d}_k{k0}"] = round(sum(rs) / len(rs), 4)
    # score from unrounded would differ only below 5e-5; ties are decided on the recorded means
    d, k0 = max(itertools.product(GRID_DEPTH, GRID_K0), key=lambda g: (evidence["R4"][f"d{g[0]}_k{g[1]}"], -g[0], g[1]))
    return TuneResult(d, k0, RULE, evidence,
                      {"split": "tune", "items": len(used), "clusters": len({i["cluster"] for i in used}), "labels": labels})


def write_settings(r: TuneResult, path) -> None:
    path = Path(path)
    doc = {"depth": r.depth, "rrf_k0": r.rrf_k0, "rule": r.rule, "evidence": r.evidence, "tuned_on": r.tuned_on}
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(part, path)
