"""Resumable eval steps: extract, pool, judge. All files are private JSONL under the evals folder."""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from askmike.evals.judge import JUDGE_A, JUDGE_B, judge_batch
from askmike.evals.pool import pool_candidates, pool_stats
from askmike.evals.split import split_of
from askmike.llm.ledger import Ledger
from askmike.retrieval.requirements import extract_requirements

MAX_FAILS = 20
Q_BATCH = 8
DEADLINE = "deadline"


def read_jsonl(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list) -> None:
    path = Path(path)
    part = path.with_name(path.name + ".part")
    part.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    os.replace(part, path)


class Gate:
    """Wraps a runner: raises once the deadline passes or too many calls in a row failed."""

    def __init__(self, runner, max_seconds=None, max_fails=MAX_FAILS):
        self.runner = runner
        self.deadline = None if max_seconds is None else time.monotonic() + max_seconds
        self.max_fails, self.fails, self.stopped = max_fails, 0, False
        self._lock = threading.Lock()

    def __call__(self, prompt, model):
        if self.stopped or (self.deadline is not None and time.monotonic() > self.deadline):
            raise RuntimeError(DEADLINE)
        return self.runner(prompt, model)

    def note(self, ok: bool, error: str = "") -> None:
        if error == DEADLINE:
            return
        with self._lock:
            self.fails = 0 if ok else self.fails + 1
            if self.fails >= self.max_fails:
                self.stopped = True


def _map(fn, items, workers):
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(fn, items))


def _quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


# ---- extract ----
def run_extract(evals: Path, runner, workers=4, max_seconds=None) -> dict:
    evals = Path(evals)
    jds = read_jsonl(evals / "sets" / "jds.jsonl")
    ledger = Ledger(evals / "ledger_extract.jsonl")
    gate = Gate(runner, max_seconds)

    def one(jd):
        reqs, st = extract_requirements(jd["id"], jd["text"], gate, ledger)
        gate.note(not st.error, st.error)
        return jd, reqs, st

    results = _map(one, jds, workers)
    rows, stats = [], []
    for jd, reqs, st in results:
        stats.append({"id": jd["id"], "kept": st.kept, "dropped_invalid": st.dropped_invalid, "error": st.error})
        rows += [{"id": r.id, "jd_id": jd["id"], "cluster": jd["id"], "split": jd["split"], "text": r.text,
                  "required": r.required, "kind": r.kind} for r in reqs]
    write_jsonl(evals / "requirements.jsonl", rows)
    write_jsonl(evals / "extract_stats.jsonl", stats)
    per = [s["kept"] for s in stats if not s["error"]]
    return {"jds": len(jds), "extracted": len(per), "errors": sum(1 for s in stats if s["error"] and s["error"] != DEADLINE),
            "remaining": sum(1 for s in stats if s["error"]), "requirements": len(rows),
            "median": statistics.median(per) if per else 0, "p90": _quantile(per, 0.9),
            "dropped_invalid": sum(s["dropped_invalid"] for s in stats)}


class ExtractIncomplete(RuntimeError):
    """requirements.jsonl is partial: pool and judge refuse it rather than pool a subset silently."""


def extract_problem(evals: Path) -> str:
    """Why the extraction is incomplete, or "" when it is complete. Counts only, never JD text.

    Incomplete: a stats row with an error (the deadline, a failed call, an unparseable reply), or a JD in
    sets/jds.jsonl with no requirement rows and no clean stats row with kept == 0 (a JD that finished with nothing valid is
    complete; kept > 0 with no rows is a hand-cut file)."""
    evals = Path(evals)
    jds = [j["id"] for j in read_jsonl(evals / "sets" / "jds.jsonl")]
    stats = read_jsonl(evals / "extract_stats.jsonl")
    rerun = "; rerun `python -m askmike.evals extract` before pooling or judging"
    stopped = sum(1 for s in stats if s.get("error"))
    if stopped:
        return (f"extract is incomplete: {stopped} of {len(jds)} job descriptions stopped before finishing"
                f" (deadline or error){rerun}")
    covered = {r["jd_id"] for r in read_jsonl(evals / "requirements.jsonl")}
    covered |= {s["id"] for s in stats if not s.get("error") and not s.get("kept")}
    have = sum(1 for j in jds if j in covered)
    if have < len(jds):
        return f"extract is incomplete: requirements cover {have} of {len(jds)} job descriptions{rerun}"
    return ""


def require_complete_extract(evals: Path) -> None:
    problem = extract_problem(evals)
    if problem:
        raise ExtractIncomplete(problem)


# ---- pool ----
def run_pool(evals: Path, conn, embedder, units: list, skills: list) -> dict:
    from askmike.evals.pool import OnceEmbedder, grid_ladders
    from askmike.retrieval.ladder import Ladder, Settings
    evals = Path(evals)
    require_complete_extract(evals)
    ladder = Ladder(conn, OnceEmbedder(embedder), skills, Settings())
    grid = grid_ladders(ladder, skills)
    rows = []
    for r in read_jsonl(evals / "requirements.jsonl"):
        rows.append({"query_id": r["id"], "set": "jdreq", "cluster": r["jd_id"], "split": r["split"],
                     "text": r["text"], "query": r["text"]})
    for q in read_jsonl(evals / "sets" / "questions.jsonl"):
        if q["kind"] == "evidence":
            rows.append({"query_id": q["id"], "set": "questions", "cluster": q["id"], "split": q["split"],
                         "text": q["text"], "query": q["text"]})
    pool_stats.update(queries=0, cap_hits=0)
    out = []
    for r in rows:
        q = r.pop("query")
        out.append(dict(r, pool=pool_candidates(q, ladder, units, skills, grid=grid)))
    write_jsonl(evals / "pools.jsonl", out)
    sizes = [len(r["pool"]) for r in out]
    return {"queries": len(out), "median": statistics.median(sizes) if sizes else 0,
            "max": max(sizes, default=0), "cap_hits": pool_stats["cap_hits"]}


# ---- judge ----
def in_b_sample(jd_id: str) -> bool:
    return hashlib.sha1(("judgeB:" + jd_id).encode("utf-8")).digest()[0] % 10 < 4


def make_batches(pools: list, judge: str, sample: bool) -> list:
    """[(key, [pool rows])]: one batch per JD, then groups of 8 evidence questions."""
    by_jd: dict = {}
    qrows = []
    for r in pools:
        (by_jd.setdefault(r["cluster"], []) if r["set"] == "jdreq" else qrows).append(r)
    batches = []
    for jd in sorted(by_jd):
        if sample and not (split_of(jd) == "report" and in_b_sample(jd)):
            continue
        batches.append((f"judge{judge}:jd:{jd}", by_jd[jd]))
    qrows.sort(key=lambda r: r["query_id"])
    for n, i in enumerate(range(0, len(qrows), Q_BATCH)):
        batches.append((f"judge{judge}:q:{n}", qrows[i:i + Q_BATCH]))
    return batches


def run_judge(evals: Path, judge: str, sample: bool, runner, units: list, workers=4, max_seconds=None) -> dict:
    evals = Path(evals)
    require_complete_extract(evals)
    model = JUDGE_A if judge == "A" else JUDGE_B
    by_id = {u["id"]: u for u in units}
    batches = make_batches(read_jsonl(evals / "pools.jsonl"), judge, sample)
    ledger = Ledger(evals / "ledger_judge.jsonl")
    gate = Gate(runner, max_seconds)

    def one(batch):
        key, rows = batch
        pools = {r["query_id"]: r["pool"] for r in rows}
        ids = sorted({u for p in pools.values() for u in p})
        res = judge_batch(key, [(r["query_id"], r["text"]) for r in rows], [by_id[i] for i in ids if i in by_id],
                          model, gate, ledger, pools)
        gate.note(not res.error, res.error)
        return key, rows, res

    results = _map(one, batches, workers)
    labels, stats = [], []
    for key, rows, res in results:
        stats.append({"judge": judge, "key": key, "model": model, "n_queries": len(rows),
                      "dropped_unknown": res.dropped_unknown, "missing_queries": len(res.missing_queries),
                      "parse_error": res.parse_error, "error": res.error})
        for r in rows:
            if r["query_id"] in res.labels:
                labels.append({"query_id": r["query_id"], "set": r["set"], "cluster": r["cluster"],
                               "split": r["split"], "relevant": res.labels[r["query_id"]], "pool": r["pool"],
                               "judge": judge, "prompt_sha": res.prompt_sha})
    write_jsonl(evals / f"labels_{judge}.jsonl", labels)
    old = [s for s in read_jsonl(evals / "judge_stats.jsonl") if s.get("judge") != judge]
    write_jsonl(evals / "judge_stats.jsonl", old + stats)
    nonempty = [len(r["relevant"]) for r in labels if r["relevant"]]
    return {"batches": len(batches), "parse_errors": sum(1 for s in stats if s["parse_error"]),
            "errors": sum(1 for s in stats if s["error"] and s["error"] not in ("parse", DEADLINE)),
            "remaining": sum(1 for s in stats if s["error"]),
            "dropped": sum(s["dropped_unknown"] for s in stats),
            "missing": sum(s["missing_queries"] for s in stats), "labelled": len(labels),
            "empty": sum(1 for r in labels if not r["relevant"]),
            "median_relevant": statistics.median(nonempty) if nonempty else 0,
            "jd_batches": sum(1 for k, _ in batches if ":jd:" in k)}
