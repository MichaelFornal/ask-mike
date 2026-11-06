"""Extraction drift: what changes when live extraction runs on a model other than M1's extractor.

The evals answer job descriptions on M1's requirements (extracted by EXTRACT_MODEL), while a live posting is
extracted by the picked jd model. When those differ, `run_drift` re-extracts every eval job description with the
jd model through claude -p, with M1's exact call (build_extract_prompt, no schema, no effort), into its own ledger
under work/evals/drift/<slug>/, and compares the two:
- n_requirements_mean old and new, over the job descriptions the new extraction finished;
- matched_share: the share of new requirements whose token Jaccard with some old requirement of the same job
  description is at least MATCH_MIN.

M1's requirements.jsonl and extraction ledger are never touched. When the jd model is EXTRACT_MODEL there is
nothing to measure; the `drift` step says so and reads no private file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from askmike.answer.config import MODEL_SLUGS
from askmike.evals.steps import DEADLINE, Gate, _map, read_jsonl, write_jsonl
from askmike.llm.ledger import Ledger
from askmike.retrieval.requirements import EXTRACT_MODEL, extract_requirements

MATCH_MIN = 0.5


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def compare(old: dict, new: dict) -> dict:
    """old, new: {jd id: [requirement texts]}. Over the job descriptions in `new` (the ones re-extracted)."""
    ids = sorted(new)
    new_reqs = [(j, tokens(t)) for j in ids for t in new[j]]
    matched = sum(1 for j, t in new_reqs if any(jaccard(t, tokens(o)) >= MATCH_MIN for o in old.get(j, ())))
    return {"n_jds": len(ids), "n_requirements_mean_old": _mean([len(old.get(j, ())) for j in ids]),
            "n_requirements_mean_new": _mean([len(new[j]) for j in ids]), "n_new_requirements": len(new_reqs),
            "matched_share": matched / len(new_reqs) if new_reqs else None}


def run_drift(evals: Path, runner, model: str, workers: int = 4, max_seconds=None) -> dict:
    """Re-extract every job description in sets/jds.jsonl with `model`, then compare with M1's requirements.
    Writes requirements.jsonl and summary.json under evals/drift/<slug>/; the summary is returned. A job
    description whose extraction failed is counted in `remaining` and left out of the comparison."""
    if model == EXTRACT_MODEL:
        raise ValueError(f"{model} is M1's EXTRACT_MODEL: there is no drift to measure")
    evals = Path(evals)
    out = evals / "drift" / MODEL_SLUGS.get(model, model)
    out.mkdir(parents=True, exist_ok=True)
    jds = read_jsonl(evals / "sets" / "jds.jsonl")
    ledger = Ledger(out / "ledger_extract.jsonl")
    gate = Gate(runner, max_seconds)

    def one(jd):
        reqs, st = extract_requirements(jd["id"], jd["text"], gate, ledger, model=model)
        gate.note(not st.error, st.error)
        return jd, reqs, st

    results = _map(one, jds, workers)
    rows, new = [], {}
    for jd, reqs, st in results:
        if st.error:
            continue
        new[jd["id"]] = [r.text for r in reqs]
        rows += [{"id": r.id, "jd_id": jd["id"], "text": r.text, "required": r.required, "kind": r.kind}
                 for r in reqs]
    write_jsonl(out / "requirements.jsonl", rows)
    old: dict = {}
    for r in read_jsonl(evals / "requirements.jsonl"):
        old.setdefault(r["jd_id"], []).append(r["text"])
    errors = [st.error for _, _, st in results if st.error]
    s = {"model": model, "n_jds": len(jds), "n_extracted": len(new), "remaining": len(errors),
         "failed": sum(1 for e in errors if e != DEADLINE), "match_min": MATCH_MIN}
    c = compare(old, new)
    s.update({k: c[k] for k in ("n_requirements_mean_old", "n_requirements_mean_new", "n_new_requirements",
                                "matched_share")})
    (out / "summary.json").write_text(json.dumps(s, indent=1, sort_keys=True), encoding="utf-8")
    return s
