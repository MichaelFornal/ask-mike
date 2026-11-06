"""M2 facts: every number docs/m2/REPORT.md prints, each as {value, display, cmd, inputs}, with PREFIX "m2_".

Pattern from askmike/facts/build.py (M1: `build_m1`, `check`, values rounded to 4 places, inputs by sha256), adapted
2026-11-04 for Task 11a:
- per-model facts (`m2_haiku_*`, `m2_sonnet_*`) are recomputed from each model's claude -p records on the report
  split with the functions the steps use (answer_metrics.summarise and audit, support.rates); the fallback rate's
  interval is a cluster bootstrap by item, as every other interval is;
- the pick (`m2_picked_model_question`, `m2_picked_model_jd`) and the prompt come from askmike/answer/settings.json;
- judge facts read judge A's planted score and both judges' verdicts; kappa pools both models' report-split claims;
- calibration facts copy the picked calibration's summary (work/answers/calibration/picked/summary.json),
  including its API extraction set against M1's requirements by count (calibrate.extraction_counts);
- drift facts exist only when the jd model is not M1's EXTRACT_MODEL, and then come from the drift step.

`cmd` is the step whose output a value is copied from (`judge-calib`, `calibrate --summarise`, `drift`), or
`facts-m2` when this module computes it. `inputs` names files relative to $ASK_PRIVATE_DIR/work (evals/...,
answers/...) or to the repo, never an absolute path. No fact counts applied-to jobs or application forms, or names
a company.

build_m2 raises FactsRefused, naming files, counts, ids and gates only, when:
- a report record of either model is missing or carries another template_sha than ANSWER_TEMPLATE_SHA;
- the audit finds any hit, or any rendered answer holds an injection canary, on either split of either model;
- the handoff correct rate is not 1.0 on either split of either model;
- a judge-A verdict was made with another support template than the current one;
- judge A's planted calibration is missing, did not pass, used another support template, or was scored on a
  planted set that has changed since;
- the jd model needs the drift step and it has not run or did not finish;
- the calibration summary is missing, stale (an input changed since), of another setup than the pick, incomplete
  (an item not compared, or not called), its verdict is stop, or it lacks a value a fact copies.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from askmike.answer.config import ANSWER_MODELS, MODEL_SLUGS
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.settings import load_answer_settings
from askmike.evals import calibrate as cal
from askmike.evals import support as sup
from askmike.evals.answer_metrics import SPLITS, audit, summarise
from askmike.evals.answer_sets import FIXTURES, SETS, load_items
from askmike.evals.bootstrap import cluster_bootstrap
from askmike.evals.steps import read_jsonl
from askmike.retrieval.requirements import EXTRACT_MODEL

PREFIX = "m2_"
CMD_FACTS = "uv run python -m askmike.evals facts-m2"
CMD_JUDGE_CALIB = "uv run python -m askmike.evals judge-calib --judge A"
CMD_CALIBRATE = f"uv run python -m askmike.evals calibrate --model {cal.PICKED} --summarise"
CMD_DRIFT = "uv run python -m askmike.evals drift"

GATES = ("honesty", "slop", "privacy", "voice", "verdict", "grounding")
RATES = ("handoff_correct_rate", "false_handoff_rate", "offtopic_correct_rate", "gap_pivot_rate",
         "personal_correct_rate", "followup_answered_rate")
CAL = ("n", "outcome_agreement", "support_gap", "prompt_parity", "cost_per_answer_mean", "cost_per_answer_p95",
       "cost_total", "tokens_in_mean", "tokens_out_mean", "latency_ms_p50", "latency_ms_p95", "worst_case_ok",
       "extract_n_jd", "extract_reqs_api_mean", "extract_reqs_cli_mean", "extract_n_jd_differ")

SETTINGS, UNITS, SKILLS = "askmike/answer/settings.json", "corpus/units.jsonl", "corpus/skills.json"
FIXTURE_PREFIX = "tests/fixtures/evals/"
ITEM_FILES = ("evals/sets/jds.jsonl", "evals/sets/questions.jsonl", "evals/sets/handoffs.jsonl",
              "evals/sets/gaps.jsonl", "evals/sets/injections.jsonl", "evals/requirements.jsonl",
              "answers/kinds_A.jsonl", *(FIXTURE_PREFIX + n for n in ("injections.jsonl", "offtopic.jsonl",
                                                                      "followups.jsonl")))
LABELS = "evals/labels_A.jsonl"
SCORE, PLANTED = "answers/calibration/score_A.json", "answers/calibration/planted.jsonl"
CAL_SUMMARY = f"answers/calibration/{cal.PICKED}/summary.json"

PICKED = ("m2_picked_model_question", "m2_picked_model_jd")
SHORT = {m: MODEL_SLUGS[m].capitalize() for m in ANSWER_MODELS}  # "Haiku", "Sonnet"
WHOLE = ("m2_cal_tokens_in_mean", "m2_cal_tokens_out_mean", "m2_cal_latency_ms_p50", "m2_cal_latency_ms_p95")
SHARES = ("m2_drift_matched_share",)  # printed as whole percents
# rates held against a bar (AGREEMENT_MIN, GAP_MAX, PARITY_MIN, POS_BAR, NEG_BAR): three places, so a value the
# bar turns on never rounds onto the other side of it; kappa too, so 0.7955 does not read as 0.80
THREE_PLACES = ("m2_cal_outcome_agreement", "m2_cal_support_gap", "m2_cal_prompt_parity", "m2_judge_planted_pos_acc",
                "m2_judge_planted_neg_acc", "m2_kappa_support_AB")


class FactsRefused(ValueError):
    """A bar failed, a record is stale or the calibration is not complete: no m2 fact is written."""


def display(key: str, value) -> str:
    """How the report prints a fact: model ids as short names, costs in dollars to 4 places, counts, tokens and
    milliseconds whole, rates held against a bar (THREE_PLACES) to 3 places, other rates and means to 2, the
    drift match share as a percent."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key in PICKED:
        return SHORT.get(value, value)
    if isinstance(value, str):
        return value
    if key in SHARES:
        return f"{value * 100:.0f}%"
    if key.startswith("m2_cal_cost"):
        return f"${value:.4f}"
    if isinstance(value, int) or key in WHOLE:
        return f"{value:,.0f}"
    s = f"{value:.3f}" if key in THREE_PLACES else f"{value:.2f}"
    return s[1:] if s.startswith("-") and float(s) == 0 else s  # never "-0.00"


def _round(v):
    if isinstance(v, float):
        v = round(v, 4)
        return 0.0 if v == 0 else v  # no -0.0
    return v


class _Inputs:
    """sha256 of each file read, by its relative name; hashed once."""

    def __init__(self, work: Path, repo: Path):
        self.work, self.repo, self._sha = Path(work), Path(repo), {}

    def path(self, name: str) -> Path:
        if name.startswith(FIXTURE_PREFIX):
            return FIXTURES / name[len(FIXTURE_PREFIX):]
        return (self.work if name.split("/", 1)[0] in ("evals", "answers") else self.repo) / name

    def of(self, *names: str) -> dict:
        out = {}
        for name in sorted(set(names)):
            if name not in self._sha:
                self._sha[name] = hashlib.sha256(self.path(name).read_bytes()).hexdigest()
            out[name] = self._sha[name]
        return out


def _parts(ci) -> tuple:
    """(mean, lo, hi) of a cluster-bootstrap summary; Nones when there was nothing to measure."""
    return (ci["mean"], ci["lo"], ci["hi"]) if ci else (None, None, None)


def fallback_ci(records) -> dict | None:
    """The fallback rate over `records` with a cluster-bootstrap interval (cluster = item)."""
    values = {r["item_id"]: [1.0 if r.get("outcome") == "fallback" else 0.0] for r in records}
    return cluster_bootstrap(values) if values else None


def _verdict_rows(path: Path) -> dict:
    return {r["key"]: r for r in read_jsonl(path)}


def build_m2(work: Path, repo: Path, cfg=None) -> dict[str, dict]:
    """Every m2_ fact from $ASK_PRIVATE_DIR/work and the repo, or FactsRefused. `cfg` is the answer gates'
    config for the audit; by default the private bundle or files, as the audit step reads them."""
    work, repo = Path(work), Path(repo)
    inp = _Inputs(work, repo)
    facts: dict[str, dict] = {}

    def put(key, value, cmd, *names):
        value = _round(value)
        facts[key] = {"value": value, "display": display(key, value), "cmd": cmd, "inputs": inp.of(*names)}

    try:
        settings = load_answer_settings(repo / SETTINGS)
    except ValueError as e:
        raise FactsRefused(str(e)) from None
    units = {u["id"]: u for u in read_jsonl(repo / UNITS)}
    skills = json.loads((repo / SKILLS).read_text(encoding="utf-8"))
    if cfg is None:
        from askmike.answer import __main__ as answer_cli
        cfg = answer_cli.gate_config(skills)
    kinds = {r["id"]: r["kind"] for r in read_jsonl(work / "answers" / "kinds_A.jsonl")}
    items = load_items(work / "evals", kinds)
    labels_A = {row["query_id"]: row["relevant"] for row in read_jsonl(work / LABELS)}
    report = {i.item_id for i in items if i.split == "report"}

    # ---- per model, report split ----
    verdicts = {"A": {}, "B": {}}
    support_files = []
    for model in ANSWER_MODELS:
        m = MODEL_SLUGS[model]
        recs_name, a_name, b_name = (f"answers/{m}/records.jsonl", f"answers/{m}/support_A.jsonl",
                                     f"answers/{m}/support_B.jsonl")
        records = read_jsonl(work / recs_name)
        by_split = summarise(records, items, labels_A)
        rep = by_split["report"]
        if rep["n_missing"] or rep["n_stale"]:
            raise FactsRefused(f"{m}: {rep['n_missing']} missing and {rep['n_stale']} stale report record(s) of "
                               f"{rep['n_items']}; every report record must carry ANSWER_TEMPLATE_SHA "
                               f"{ANSWER_TEMPLATE_SHA} (rerun answers --model {model} --split all)")
        hits = audit(records, units, cfg)
        if hits:
            ids = sorted({h["item_id"] for h in hits})
            raise FactsRefused(f"{m}: {len(hits)} audit hit(s) in {len(ids)} record(s) on either split: "
                               f"{', '.join(ids)}; run audit --model {model}")
        canary = [r["item_id"] for r in records for _ in r.get("canary_hits_rendered") or []]
        if canary:
            raise FactsRefused(f"{m}: {len(canary)} rendered canary hit(s) on either split: "
                               f"{', '.join(sorted(set(canary)))}")
        for split in SPLITS:  # a zero-bar on both splits, as the audit and canary bars are
            rate = by_split[split]["handoff_correct_rate"]
            if rate != 1.0:
                raise FactsRefused(f"{m}: handoff_correct_rate on the {split} split is {rate}, not 1.0; a zero-bar "
                                   f"is missed and no report is published")
        verdicts["A"][m] = va = _verdict_rows(work / a_name)
        verdicts["B"][m] = _verdict_rows(work / b_name)
        old = sum(1 for v in va.values() if v.get("template_sha") != sup.template_sha())
        if old:
            raise FactsRefused(f"{m}: {old} judge-A verdict(s) were made with another support template than "
                               f"{sup.template_sha()}; rerun support --model {model} --judge A")
        support_files += [a_name, b_name]

        recs = [r for r in records if r["item_id"] in report]
        ans_in = (recs_name, LABELS, *ITEM_FILES)
        p = f"m2_{m}_"
        put(p + "n_answers", rep["n_answers"], CMD_FACTS, *ans_in)
        put(p + "pregate_claims", rep["pregate_claims"], CMD_FACTS, *ans_in)
        for g in GATES:
            put(f"{p}pregate_{g}_claims", rep[f"pregate_{g}_claims"], CMD_FACTS, *ans_in)
        put(p + "drops_per_answer", rep["drops_per_answer"], CMD_FACTS, *ans_in)
        _, lo, hi = _parts(fallback_ci(recs))
        for suffix, v in (("", rep["fallback_rate"]), ("_lo", lo), ("_hi", hi)):
            put(f"{p}fallback_rate{suffix}", v, CMD_FACTS, *ans_in)
        for suffix, v in zip(("", "_lo", "_hi"), _parts(rep["coverage"])):
            put(f"{p}coverage{suffix}", v, CMD_FACTS, *ans_in)
        for r in RATES:
            put(p + r, rep[r], CMD_FACTS, *ans_in)
        put(p + "injection_canary_hits", rep["injection_canary_hits"], CMD_FACTS, *ans_in)
        put(p + "injection_canary_hits_raw", rep["injection_canary_hits_raw"], CMD_FACTS, *ans_in)
        put(p + "audit_hits", sum(1 for h in hits if h["item_id"] in report), CMD_FACTS, recs_name, UNITS, SKILLS)
        rates = sup.rates(va, recs)
        if rates["n_kept_unjudged"]:
            raise FactsRefused(f"{m}: {rates['n_kept_unjudged']} kept claim(s) on the report split unjudged by judge "
                               f"A; rerun support --model {model} --judge A")
        sup_in = (recs_name, a_name, *ITEM_FILES)
        for suffix, v in zip(("", "_lo", "_hi"), _parts(rates["support_rate"])):
            put(f"{p}support_rate{suffix}", v, CMD_FACTS, *sup_in)
        put(p + "partial_rate", _parts(rates["partial_rate"])[0], CMD_FACTS, *sup_in)
        put(p + "grounding_false_drop_rate", rates["grounding_false_drop_rate"], CMD_FACTS, *sup_in)
        put(p + "n_grounding_only", rates["n_grounding_only"], CMD_FACTS, *sup_in)

    # ---- the judge: planted calibration and two-judge agreement ----
    if not (work / SCORE).exists():
        raise FactsRefused(f"judge A has no planted calibration ({SCORE}); run judge-calib --judge A")
    score = json.loads((work / SCORE).read_text(encoding="utf-8"))
    if not score.get("pass_"):
        raise FactsRefused(f"judge A did not pass its planted calibration (pos_acc {score.get('pos_acc')}, "
                           f"neg_acc {score.get('neg_acc')}); no support number is published")
    if score.get("template_sha") != sup.template_sha():
        raise FactsRefused(f"judge A's planted calibration used support template {score.get('template_sha')}, not "
                           f"the current {sup.template_sha()}; rerun judge-calib --judge A")
    planted = work / PLANTED
    sha1 = hashlib.sha1(planted.read_bytes()).hexdigest() if planted.exists() else None
    if score.get("planted_sha1") != sha1:
        raise FactsRefused("the planted set changed since judge A's calibration (planted_sha1 differs from "
                           f"{PLANTED}); rerun judge-calib --judge A")
    put("m2_judge_planted_n", score["n"], CMD_JUDGE_CALIB, SCORE, PLANTED)
    put("m2_judge_planted_pos_acc", score["pos_acc"], CMD_JUDGE_CALIB, SCORE, PLANTED)
    put("m2_judge_planted_neg_acc", score["neg_acc"], CMD_JUDGE_CALIB, SCORE, PLANTED)

    def pooled(judge):  # claim keys repeat across the two models, so each is prefixed with its model's slug
        return {f"{m}:{k}": row for m, rows in verdicts[judge].items() for k, row in rows.items()
                if (row or {}).get("split") == "report"}
    k = sup.kappa(pooled("A"), pooled("B"))
    put("m2_kappa_support_AB", k["kappa"], CMD_FACTS, *support_files)
    put("m2_n_pairs_support_AB", k["n_pairs"], CMD_FACTS, *support_files)

    # ---- items, the pick and the prompt ----
    per_set = Counter(i.set for i in items)
    for s in SETS:
        put(f"m2_n_items_{s}", per_set.get(s, 0), CMD_FACTS, *ITEM_FILES)
    put("m2_picked_model_question", settings.model_for("question"), CMD_FACTS, SETTINGS)
    put("m2_picked_model_jd", settings.model_for("jd"), CMD_FACTS, SETTINGS)
    put("m2_answer_effort", settings.effort, CMD_FACTS, SETTINGS)
    put("m2_template_sha", settings.template_sha, CMD_FACTS, SETTINGS)

    # ---- extraction drift, only when the jd model is not M1's extractor ----
    jd_model = settings.model_for("jd")
    if jd_model != EXTRACT_MODEL:
        name = f"evals/drift/{MODEL_SLUGS[jd_model]}/summary.json"
        if not (work / name).exists():
            raise FactsRefused(f"the jd model {jd_model} is not M1's EXTRACT_MODEL and the drift step has not run "
                               f"({name} is missing); run drift")
        d = json.loads((work / name).read_text(encoding="utf-8"))
        if d.get("model") != jd_model or d.get("remaining"):
            raise FactsRefused(f"the drift summary {name} is incomplete or for another model; rerun drift")
        put("m2_drift_reqs_old", d["n_requirements_mean_old"], CMD_DRIFT, name)
        put("m2_drift_reqs_new", d["n_requirements_mean_new"], CMD_DRIFT, name)
        put("m2_drift_matched_share", d["matched_share"], CMD_DRIFT, name)

    # ---- the API calibration of the picked setup ----
    path = work / CAL_SUMMARY
    if not path.exists():
        raise FactsRefused(f"calibration summary {CAL_SUMMARY} is missing; run calibrate --model {cal.PICKED}, "
                           f"support --records work/answers/calibration/{cal.PICKED}/records.jsonl --judge A, then "
                           f"calibrate --model {cal.PICKED} --summarise")
    s = json.loads(path.read_text(encoding="utf-8"))
    stale = cal.stale_inputs(work / "answers", s)
    if stale or not s.get("inputs"):
        raise FactsRefused(f"calibration summary is stale: {', '.join(stale) or 'it records no inputs'} changed "
                           f"since it was written; rerun calibrate --model {cal.PICKED} --summarise")
    if s.get("model") != settings.model:
        raise FactsRefused(f"calibration ran on {s.get('model')}, not the pick {settings.model}; rerun "
                           f"calibrate --model {cal.PICKED}")
    if not s.get("complete"):
        raise FactsRefused(f"calibration is incomplete: compared {s.get('n_compared')} of {s.get('n')} items, called "
                           f"{s.get('called')} of the {s.get('n_to_call')} that make a call; rerun calibrate "
                           f"--model {cal.PICKED} (spend.jsonl keeps the cap), then --summarise")
    verdict = (s.get("stop_rule") or {}).get("verdict")
    if verdict != "go":
        reasons = "; ".join((s.get("stop_rule") or {}).get("reasons") or []) or "no reason recorded"
        raise FactsRefused(f"calibration verdict is {verdict}: {reasons}; bring Mike the reasons and the options")
    missing = [key for key in CAL if key not in s]
    if missing:
        raise FactsRefused(f"calibration summary has no {', '.join(missing)}; rerun calibrate --model {cal.PICKED} "
                           f"--summarise")
    for key in CAL:
        put(f"m2_cal_{key}", s[key], CMD_CALIBRATE, CAL_SUMMARY)
    put("m2_cal_verdict", verdict, CMD_CALIBRATE, CAL_SUMMARY)
    return dict(sorted(facts.items()))


def check(work: Path, repo: Path, facts_path: Path, cfg=None) -> list[str]:
    """M2 keys whose recomputed fact differs from facts.json (missing and extra keys included), sorted."""
    fresh = build_m2(work, repo, cfg=cfg)
    path = Path(facts_path)
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    stored = {k: v for k, v in stored.items() if k.startswith(PREFIX)}
    return sorted(k for k in set(fresh) | set(stored) if fresh.get(k) != stored.get(k))
