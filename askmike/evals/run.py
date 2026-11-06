"""Score the retrieval rungs (R1-R4) and the keyword baseline (KW) on the labelled eval sets.

Query rows given to `evaluate` are {query_id, cluster, split, text, relevant, pool}: `relevant` None means the
query has no label row (unlabelled: excluded, counted), [] means a label row with nothing relevant (unsupported:
metrics None, counted). Private outputs go under $ASK_PRIVATE_DIR/work/evals/results/{A|B}/.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from askmike.evals import metrics as M
from askmike.evals.bootstrap import cluster_bootstrap
from askmike.evals.compare import paired_bootstrap
from askmike.evals.steps import read_jsonl, write_jsonl

RUNGS = ("R1", "R2", "R3", "R4")
ALL_RUNGS = RUNGS + ("KW",)
SETS = ("jdreq", "questions", "personal")
METRICS = ("recall_at_5", "recall_at_10", "mrr", "ndcg_at_10")


def write_json(path: Path, obj) -> None:
    path = Path(path)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def _boot(values_by_cluster):
    vals = {c: v for c, v in values_by_cluster.items() if v}
    return cluster_bootstrap(vals) if vals else None


def evaluate(set_name, rung, retrieve, queries, out_dir, k=10, labels=None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    items, unlabelled = [], defaultdict(int)
    per_req = defaultdict(list)  # (split, cluster) -> [(ranked, relevant)]
    for q in queries:
        if q["relevant"] is None:
            unlabelled[q["split"]] += 1
            continue
        ranked = list(retrieve(q["text"]))[:k]
        rel = set(q["relevant"])
        pool = set(q.get("pool") or [])
        judged = 1.0 if q.get("pool") is None else (sum(1 for u in ranked if u in pool) / len(ranked) if ranked else None)
        sup = bool(rel)
        items.append({
            "item_id": q["query_id"], "cluster": q["cluster"], "split": q["split"], "judged_at_10": judged,
            "recall_at_5": M.recall_at_k(ranked, rel, 5), "recall_at_10": M.recall_at_k(ranked, rel, 10),
            "mrr": M.mrr(ranked, rel), "ndcg_at_10": M.ndcg_at_k(ranked, rel),
            "covered_at_10": (1.0 if set(ranked[:10]) & rel else 0.0) if sup else None})
        per_req[(q["split"], q["cluster"])].append((ranked, rel))
    write_jsonl(out_dir / f"{set_name}_{rung}_items.jsonl", items)
    by_split = {}
    for split in ("tune", "report"):
        its = [i for i in items if i["split"] == split]
        d = {m: _boot({c: [i[m] for i in its if i["cluster"] == c and i[m] is not None]
                       for c in {i["cluster"] for i in its}}) for m in METRICS}
        if set_name == "jdreq":
            jd = {c: [M.coverage_at_k(reqs, 10)] for (s, c), reqs in per_req.items() if s == split}
            d["coverage_at_10"] = _boot({c: v for c, v in jd.items() if v[0] is not None})
        d["n_unsupported"] = sum(1 for i in its if i["recall_at_10"] is None)
        d["n_unlabelled"] = unlabelled[split]
        js = [i["judged_at_10"] for i in its if i["judged_at_10"] is not None]
        d["judged_at_10_min"] = min(js) if js else None
        by_split[split] = d
    summary = {"set": set_name, "rung": rung, "labels": labels, "k": k, "by_split": by_split}
    write_json(out_dir / f"{set_name}_{rung}.json", summary)
    return summary


def load_queries(evals: Path, set_name: str, label: str) -> list:
    """Query rows for one set, joined to the A or B label rows (personal: exact gold, no pool)."""
    evals = Path(evals)
    if set_name == "personal":
        return [{"query_id": r["id"], "cluster": r["id"], "split": r["split"], "text": r["text"],
                 "relevant": list(r["gold"]), "pool": None}
                for r in read_jsonl(evals / "sets" / "questions.jsonl") if r["kind"] == "personal"]
    lab = {r["query_id"]: r for r in read_jsonl(evals / f"labels_{label}.jsonl") if r["set"] == set_name}
    if set_name == "jdreq":
        base = [(r["id"], r["jd_id"], r["split"], r["text"]) for r in read_jsonl(evals / "requirements.jsonl")]
    else:
        base = [(r["id"], r["id"], r["split"], r["text"]) for r in read_jsonl(evals / "sets" / "questions.jsonl")
                if r["kind"] == "evidence"]
    return [{"query_id": i, "cluster": c, "split": s, "text": t,
             "relevant": lab[i]["relevant"] if i in lab else None, "pool": lab[i]["pool"] if i in lab else None}
            for i, c, s, t in base]


def run_labels(evals, label, rungs: dict, k=10) -> dict:
    """Run every rung on every set under results/{label}/; write summaries, items and paired comparisons."""
    evals = Path(evals)
    out = evals / "results" / label
    summaries = {}
    for set_name in SETS:
        qs = load_queries(evals, set_name, label)
        items = {}
        for rung in ALL_RUNGS:
            summaries[(set_name, rung)] = evaluate(set_name, rung, rungs[rung], qs, out, k, labels=label)
            items[rung] = {r["item_id"]: r for r in read_jsonl(out / f"{set_name}_{rung}_items.jsonl")}
        metrics = ["recall_at_10"] + (["covered_at_10"] if set_name == "jdreq" else [])
        cmp = {}
        for rung in ALL_RUNGS:
            cmp[rung] = {}
            for base in ("R1", "KW"):
                if base == rung:
                    continue
                cmp[rung][f"vs_{base}"] = {}
                for m in metrics:
                    name = "coverage_at_10" if m == "covered_at_10" else m
                    try:
                        cmp[rung][f"vs_{base}"][name] = paired_bootstrap(
                            items[base], items[rung], m, split="report", macro=(m == "covered_at_10"))
                    except ValueError:
                        cmp[rung][f"vs_{base}"][name] = None
        write_json(out / f"cmp_{set_name}.json", cmp)
    return summaries
