"""What the results page shows: rows of facts from facts.json, the evals of record (spec §8, Publishing).

Every figure on the page is a fact's `display` string, so the page can't drift from the evals. A range is printed as
"x (lo to hi)" from the fact's `_lo` and `_hi` keys. A fact the page needs and facts.json lacks raises KeyError when
the page is built, and the app builds it at start, so a release without its facts never serves (push reverts it).
Model names are words from MODEL_SLUGS, and which kind each model answers live comes from the answer settings.
"""
from __future__ import annotations

import json
from pathlib import Path

from askmike.answer.config import ANSWER_MODELS, MODEL_SLUGS
from askmike.answer.settings import load_answer_settings

CODE = "https://github.com/MichaelFornal/ask-mike"
REPORTS = (("How the evidence is found (M1)", f"{CODE}/blob/main/docs/m1/REPORT.md"),
           ("How the answers are checked (M2)", f"{CODE}/blob/main/docs/m2/REPORT.md"),
           ("The code", CODE))
KIND_WORDS = {"question": "questions", "jd": "job descriptions"}

SIZES = (("Evidence units, each reviewed by Mike", "m1_n_units"),
         ("Job descriptions in the evals", "m1_n_jds"),
         ("Requirements read from them", "m1_n_requirements"))
# R4 in docs/m1/REPORT.md: hybrid search (BM25 + embeddings, RRF) plus skill-alias expansion. Plain hybrid is R3.
RETRIEVAL = (("Recall@10, hybrid search with skill aliases", "m1_jdreq_R4_report_recall_at_10"),
             ("Recall@10, keyword-match baseline", "m1_jdreq_KW_report_recall_at_10"),
             ("Coverage@10, hybrid search with skill aliases", "m1_jdreq_R4_report_coverage_at_10"),
             ("Coverage@10, keyword-match baseline", "m1_jdreq_KW_report_coverage_at_10"))
MODEL_ROWS = (("Support rate (judge A)", "support_rate"),
              ("Requirement coverage", "coverage"),
              ("Handoff correct rate", "handoff_correct_rate"),
              ("Audit hits after the gates", "audit_hits"),
              ("Canary hits in what a visitor sees", "injection_canary_hits"),
              ("Fallback rate", "fallback_rate"))
COST = (("Cost per answer, mean", "m2_cal_cost_per_answer_mean", ""),
        ("Cost per answer, p95", "m2_cal_cost_per_answer_p95", ""),
        ("Model time per answer, median", "m2_cal_latency_ms_p50", " ms"),
        ("Model time per answer, p95", "m2_cal_latency_ms_p95", " ms"),
        ("Model spend cap, per month", "m3_model_cap_usd", ""),
        ("All-in per month for both apps on the shared server", "m3_total_usd_month", ""))


def load_facts(path: Path) -> dict:
    """facts.json as a dict of key -> {value, display, cmd, inputs}."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _shown(facts: dict, key: str, unit: str = "") -> tuple[str, str]:
    """(the fact's display string plus unit, its range as "(lo to hi)" or "" when facts.json holds none)."""
    d = facts[key]["display"] + unit
    if f"{key}_lo" in facts and f"{key}_hi" in facts:
        return d, f"({facts[f'{key}_lo']['display']} to {facts[f'{key}_hi']['display']})"
    return d, ""


def results_view(facts: dict) -> dict:
    """The page's sections as rows of (label, (figure, range)). Raises KeyError naming the first fact facts.json lacks."""
    live = load_answer_settings().model  # route kind -> model id
    models = [(m, MODEL_SLUGS[m].capitalize(), [KIND_WORDS[k] for k, v in live.items() if v == m])
              for m in ANSWER_MODELS]
    return {
        "ci_pct": facts["m1_ci_pct"]["display"],
        "sizes": [(label, _shown(facts, key)) for label, key in SIZES],
        "retrieval": [(label, _shown(facts, key)) for label, key in RETRIEVAL],
        "models": [f"{name}, live for {' and '.join(kinds)}" if kinds else name for _, name, kinds in models],
        "model_rows": [(label, [_shown(facts, f"m2_{MODEL_SLUGS[m]}_{row}") for m, _, _ in models])
                       for label, row in MODEL_ROWS],
        "cost": [(label, _shown(facts, key, unit)) for label, key, unit in COST],
        "reports": list(REPORTS),
    }
