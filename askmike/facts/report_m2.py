"""docs/m2/REPORT.md, rendered from facts.json. Every digit printed comes from a fact's display string.

Pattern from askmike/facts/report.py (M1: one f-string template over the facts, `display` printed, a plain number
also renders so the stray-digit test can pass a sentinel for every key), adapted 2026-11-04 for Task 11a. It reads
the m2_ facts and three m1_ ones (the interval width, the resample count and the corpus size). Model, gate, set
and constant names are words; bars and thresholds are named by their constants, never typed as figures. Model ids
come from ANSWER_MODELS, JUDGE_A, JUDGE_B and PARA_MODEL, never typed here.
"""
from __future__ import annotations

from askmike.answer.config import ANSWER_MODELS, MODEL_SLUGS
from askmike.evals.judge import JUDGE_A, JUDGE_B
from askmike.evals.support_calib import PARA_MODEL
from askmike.retrieval.requirements import EXTRACT_MODEL

MODELS = tuple((MODEL_SLUGS[m], MODEL_SLUGS[m].capitalize()) for m in ANSWER_MODELS)  # (slug, name)
GATE_ROWS = ("grounding", "honesty", "slop", "privacy", "voice", "verdict")
DRIFT = ("m2_drift_reqs_old", "m2_drift_reqs_new", "m2_drift_matched_share")
SET_ROWS = (
    ("jd", "Job descriptions", "real postings, private; answered on M1's extracted requirements"),
    ("evidence", "Recruiter questions about Mike's work", "private"),
    ("personal", "Personal questions", "work authorization, location and start date; scored against fixed gold "
                                       "units"),
    ("handoff", "Handoff questions", "pay, contact details, job-search history and similar; the right answer is "
                                     "the fixed handoff line"),
    ("gap", "Gap questions", "one per skill the public skills ledger lists below working depth"),
    ("injection", "Injection attempts", "invented postings and questions with planted instructions and canary "
                                        "phrases"),
    ("offtopic", "Off-topic asks", "invented"),
    ("followup", "Follow-up pairs", "invented: a question, then a pronoun-only follow-up"),
)


def _d(f: dict, key: str) -> str:
    v = f[key]
    return v["display"] if isinstance(v, dict) else str(v)


def _v(f: dict, key: str):
    v = f[key]
    return v["value"] if isinstance(v, dict) else v


def _ci(f: dict, key: str) -> str:
    if _v(f, key) is None:
        return "n/a"
    return f"{_d(f, key)} ({_d(f, key + '_lo')} to {_d(f, key + '_hi')})"


def _row(f: dict, label: str, suffix: str, ci: bool = False) -> str:
    cells = [(_ci if ci else _d)(f, f"m2_{slug}_{suffix}") for slug, _ in MODELS]
    return f"| {label} | " + " | ".join(cells) + " |"


def _results(f: dict) -> str:
    rows = ["| Measure | " + " | ".join(name for _, name in MODELS) + " |", "|---" * (len(MODELS) + 1) + "|",
            _row(f, "Inputs", "n_answers"),
            _row(f, "Claims the model wrote, before the gates", "pregate_claims")]
    rows += [_row(f, f"Claims flagged by {g}", f"pregate_{g}_claims") for g in GATE_ROWS]
    rows += [_row(f, "Claims dropped per answer", "drops_per_answer"),
             _row(f, "Fallback rate", "fallback_rate", ci=True),
             _row(f, "Support rate (judge A)", "support_rate", ci=True),
             _row(f, "Partial rate (judge A)", "partial_rate"),
             _row(f, "Requirement coverage", "coverage", ci=True),
             _row(f, "Handoff correct rate", "handoff_correct_rate"),
             _row(f, "False handoff rate", "false_handoff_rate"),
             _row(f, "Off-topic correct rate", "offtopic_correct_rate"),
             _row(f, "Gap pivot rate", "gap_pivot_rate"),
             _row(f, "Personal correct rate", "personal_correct_rate"),
             _row(f, "Follow-up answered rate", "followup_answered_rate"),
             _row(f, "Canary hits in raw replies", "injection_canary_hits_raw"),
             _row(f, "Canary hits in what a visitor sees", "injection_canary_hits"),
             _row(f, "Audit hits", "audit_hits"),
             _row(f, "Claims dropped by grounding alone", "n_grounding_only"),
             _row(f, "Grounding false-drop rate (judge A), of those claims", "grounding_false_drop_rate")]
    return "\n".join(rows)


def _met(f: dict, slug: str) -> bool:
    p = f"m2_{slug}_"
    return (_v(f, p + "audit_hits") == 0 and _v(f, p + "injection_canary_hits") == 0
            and _v(f, p + "handoff_correct_rate") == 1.0)


def _zero_sentence(f: dict) -> str:
    missed = [name for slug, name in MODELS if not _met(f, slug)]
    if not missed:
        return "Both models met every zero-bar on the report split."
    return " ".join(f"{name} missed a zero-bar on the report split; the table shows which." for name in missed)


def _zero_table(f: dict) -> str:
    rows = ["| Bar | " + " | ".join(name for _, name in MODELS) + " |", "|---" * (len(MODELS) + 1) + "|",
            _row(f, "Audit hits (the bar: none)", "audit_hits"),
            _row(f, "Handoff correct rate (the bar: every handoff)", "handoff_correct_rate"),
            _row(f, "Canary hits in what a visitor sees (the bar: none)", "injection_canary_hits")]
    return "\n".join(rows)


def _sets(f: dict) -> str:
    rows = ["| Set | Items | What it holds |", "|---|---|---|"]
    rows += [f"| {label} | {_d(f, f'm2_n_items_{key}')} | {what} |" for key, label, what in SET_ROWS]
    return "\n".join(rows)


def _drift(f: dict) -> str:
    if all(k in f for k in DRIFT):
        return (f"The job-description model is not the model M1 extracted requirements with, so the drift step\n"
                f"re-extracted every eval job description with it. M1's extractor found "
                f"{_d(f, 'm2_drift_reqs_old')} requirements per job\n"
                f"description on average and the picked model {_d(f, 'm2_drift_reqs_new')}; "
                f"{_d(f, 'm2_drift_matched_share')} of the new requirements match one of\n"
                f"M1's for the same job description (token overlap of at least `MATCH_MIN` in "
                f"`askmike/evals/drift.py`). Every\nper-model number above uses M1's requirements.")
    if _v(f, "m2_picked_model_jd") == EXTRACT_MODEL:
        return (f"A pasted job description is first split into requirements, on the job-description model. That is\n"
                f"{_d(f, 'm2_picked_model_jd')}, the model M1 extracted the eval requirements with, so live "
                f"extraction runs on the same model as\nthe evals' extraction. The same model does not return the "
                f"same list every time; the API calibration\nbelow sets the two side by side, by count.")
    return "Extraction drift was not measured."


def render(f: dict) -> str:
    q, jd = _d(f, "m2_picked_model_question"), _d(f, "m2_picked_model_jd")
    q_id, jd_id = _v(f, "m2_picked_model_question"), _v(f, "m2_picked_model_jd")
    support = " and ".join(f"{_ci(f, f'm2_{slug}_support_rate')} for {name}" for slug, name in MODELS)
    coverage = " and ".join(f"{_ci(f, f'm2_{slug}_coverage')} for {name}" for slug, name in MODELS)
    return f"""# M2 report: answering with gates

Generated by `uv run python -m askmike.evals report-m2` from `facts.json`. Do not edit by hand.

## Summary

- Mike picked a split setup: {q} answers questions and {jd} answers pasted job descriptions.
- On the report split, the support rate (the share of kept claims judge A calls supported by the units they cite)
  is {support}.
- Requirement coverage on report-split job descriptions is {coverage}.
- {_zero_sentence(f)}
- The API calibration of the picked setup, on {_d(f, 'm2_cal_n')} tune-split items, returned
  {_d(f, 'm2_cal_verdict')}: outcome agreement with `claude -p` of {_d(f, 'm2_cal_outcome_agreement')}, a support
  gap of {_d(f, 'm2_cal_support_gap')}, and a mean cost per answer of {_d(f, 'm2_cal_cost_per_answer_mean')}.

## What was measured

M2 turns retrieval into answers. Ask Mike routes each visitor input first: a handoff topic gets a fixed line, an
empty or over-long input gets a fixed prompt, and a job description or a question goes on. For those it retrieves
evidence units and asks one model for structured claims, each citing the units it rests on. Every claim then
passes the gates (structure, grounding, honesty, slop, length, privacy, voice and verdict). A visitor sees only the
claims the gates keep, a fixed line, or a reviewed fallback page.

Both models, {" and ".join(ANSWER_MODELS)}, answered every eval item through `claude -p` with the same
frozen prompt template (`{_d(f, 'm2_template_sha')}`) at effort {_d(f, 'm2_answer_effort')}. Job descriptions were
answered on M1's extracted requirements, so both models answered the same judged requirements. The invented
postings in the injection set have no M1 requirements, so each model split them into requirements itself, as a
pasted posting would be.

The tune and report splits are M1's. Prompt revisions read tune-split records only; every per-model number below
is from the report split. Each interval is a {_d(f, 'm1_ci_pct')} cluster-bootstrap interval
({_d(f, 'm1_n_boot')} resamples) with the item as the cluster.

## Sets and sizes

Sizes count both splits. A recruiter question's set (evidence, personal or handoff) is judge A's label for it.

{_sets(f)}

## Results by model (report split)

{_results(f)}

- A claim is flagged by a gate when that gate would drop it. Flags count every claim the model wrote in a turn it
  answered, and a claim can fail more than one gate. Claims dropped per answer divides the dropped claims by
  those turns.
- The fallback rate is the share of inputs that got the reviewed fallback page: no evidence, too few claims left
  after the gates, an unreadable reply or a failed call.
- The support rate is the share of kept claims judge A calls supported by the units they cite; the partial rate is
  the share it calls partly supported.
- Requirement coverage takes, for each job description, the distinct supportable requirements (with a relevant
  unit under M1's judge A labels) that a kept claim answers, over the number an answer is asked to cover
  (`COVER_K` in `askmike/evals/answer_metrics.py`, or fewer when fewer are supportable), capped at a full score.
  It is the mean over job descriptions.
- The handoff correct rate is the share of handoff questions that got the fixed handoff line; the false handoff
  rate is the share of every other input that got it.
- The gap pivot rate is the share of gap questions whose answer kept a gap claim citing evidence: what Mike has
  not done, then the closest thing he has. The personal correct rate is the share of personal questions whose
  answer cites the gold unit. The follow-up answered rate is the share of follow-up pairs whose pronoun-only
  second question got an answer.
- Canary hits count the planted phrases from the injection attempts found in the model's raw reply, and in what a
  visitor would see.
- The audit reruns the honesty, slop, privacy, voice and verdict checks on everything a record would show a
  visitor.
- The grounding false-drop rate is the share of claims dropped by the grounding gate alone that judge A calls
  supported. It is the cost of grounding's strict rule that every id, number and name come from the cited units.

## Zero-bars

These bars allow no exception, for both models on both splits: the post-gate audit finds no honesty, slop,
privacy, voice or verdict hit in anything a visitor would see; every handoff question gets the handoff line; and
no injection canary reaches a rendered answer. A failing bar is fixed in a gate, the router or the prompt and the
items are answered again; a bar is never relaxed. `facts-m2` refuses to publish if an audit hit or a rendered
canary hit exists on either split, or if any report record was answered with another prompt template.

{_zero_table(f)}

{_zero_sentence(f)}

## Judge calibration

Support is graded by judge A ({JUDGE_A}), which reads each claim with the units it cites and calls it
supported, partial or unsupported. No human labelled a sample to check it; that was Mike's choice, as in M1.
Mike's own check was reading answers from both models side by side before he picked.

Before any support number is published, judge A must pass a planted calibration: {_d(f, 'm2_judge_planted_n')}
claims built from the public corpus, each with a known answer. A positive is a unit's own claim, verbatim or
paraphrased (by {PARA_MODEL}, then checked against the unit), citing that unit. A negative cites a unit of
another project, triples a number, adds a skill the unit never names, cites a sibling unit of the same project,
or triples a number in a paraphrase. Judge A scored {_d(f, 'm2_judge_planted_pos_acc')} on the positives and
{_d(f, 'm2_judge_planted_neg_acc')} on the negatives; the bars are `POS_BAR` and `NEG_BAR` in
`askmike/evals/support_calib.py`. `facts-m2` refuses to publish unless judge A passes on the current planted set
with the current judge prompt.

Judge B ({JUDGE_B}) judged a hash-picked sample of the same claims. On the report-split claims of both
models that both judges judged, Cohen's κ for supported versus not is {_d(f, 'm2_kappa_support_AB')} over
{_d(f, 'm2_n_pairs_support_AB')} claims.

## The pick

Mike read report-split answers from both models side by side, next to the per-model metrics, and picked a split
setup:
{q} answers questions and {jd} answers pasted job descriptions. The question model is {q_id} and the
job-description model is {jd_id}. `askmike/answer/settings.json` records the pick with the prompt template it was
made on (`{_d(f, 'm2_template_sha')}`). Mike gave no written reason, so none is quoted here.

{_drift(f)}

## API calibration and cost per answer

The evals ran through `claude -p`, whose token counts include Claude Code's own system prompt, so they cannot price
an answer. The calibration sent {_d(f, 'm2_cal_n')} tune-split items through the Messages API on the picked setup:
job descriptions on {jd}, including the extraction call, and questions on {q}; a handoff question makes no call.
Each API answer was compared with the same item's `claude -p` answer from the same model, and judge A graded both.

| Measure | Value |
|---|---|
| Outcome agreement, API versus `claude -p` | {_d(f, 'm2_cal_outcome_agreement')} |
| Support gap, API minus `claude -p` (judge A) | {_d(f, 'm2_cal_support_gap')} |
| Prompt parity (answer prompts byte-identical) | {_d(f, 'm2_cal_prompt_parity')} |
| Mean cost per answer | {_d(f, 'm2_cal_cost_per_answer_mean')} |
| Cost per answer, p95 | {_d(f, 'm2_cal_cost_per_answer_p95')} |
| Booked spend, every calibration call | {_d(f, 'm2_cal_cost_total')} |
| Mean input tokens per answer | {_d(f, 'm2_cal_tokens_in_mean')} |
| Mean output tokens per answer | {_d(f, 'm2_cal_tokens_out_mean')} |
| Latency per answer, p50 (milliseconds) | {_d(f, 'm2_cal_latency_ms_p50')} |
| Latency per answer, p95 (milliseconds) | {_d(f, 'm2_cal_latency_ms_p95')} |
| Every worst-case reservation covered the actual cost | {_d(f, 'm2_cal_worst_case_ok')} |
| Stop-rule verdict | {_d(f, 'm2_cal_verdict')} |

Cost, tokens and latency count every API call an answer made: for a job description, the extraction call and the
answer call. Prices are in `askmike/llm/prices.json`. The booked spend is what the spend cap counted: each call's
usage at those prices, or its worst case when a call may have been billed with no usage reported. The stop rule
(`AGREEMENT_MIN`, `GAP_MAX` and `PARITY_MIN` in `askmike/evals/calibrate.py`) says stop on low outcome agreement, a
large support gap, any answer prompt that differs from its `claude -p` twin, or a worst-case reservation below an
actual cost. `facts-m2` refuses to publish a calibration that stopped, missed an item, or no longer matches its
records.

The API job-description answers were built on M1's requirements, as the `claude -p` records were, so their answer
prompts can be compared byte for byte; for job descriptions the prompt parity above holds by construction. The
extraction call still ran on each sampled job description, on the job-description model, and its requirements
were counted. It found {_d(f, 'm2_cal_extract_reqs_api_mean')} requirements per job description on average,
against {_d(f, 'm2_cal_extract_reqs_cli_mean')} in M1's extraction of the same job descriptions, and the count
differed on {_d(f, 'm2_cal_extract_n_jd_differ')} of the {_d(f, 'm2_cal_extract_n_jd')} job descriptions.
Extraction keeps at most {_d(f, 'm1_req_cap')} requirements per job description, and many job descriptions reach
that cap in both runs, so a matching count does not mean matching requirements.

## Limitations

- Support is AI-graded: judge A ({JUDGE_A}) on every claim, judge B ({JUDGE_B}) on a sample. No
  human labelled a sample; that was Mike's choice.
- The planted set tests the judge on a single claim at a time, each citing one unit: a unit's own claim, verbatim
  or paraphrased by judge B's model, and negatives built from such claims. It does not test claims that merge
  several units.
- Pre-gate numbers (claims flagged before the gates, claims dropped per answer) depend on this prompt template;
  another prompt changes them. After the gates, both models meet the zero-bars by construction.
- One corpus snapshot: every number is for the {_d(f, 'm1_n_units')} units in `corpus/units.jsonl`. A new export
  changes them.
- Outside the {_d(f, 'm2_cal_n')}-item API calibration, `claude -p` answers stand in for API answers: every
  per-model number above comes from `claude -p`.
- The router's handoff rules were tuned on judge A's question-kind labels from both splits, so the report-split
  handoff rates are not held out.
- Job descriptions were answered on M1's extracted requirements, so extraction errors carry into coverage.
- The extracted requirement texts were not kept in this calibration, so how many requirements matched is not
  measured; only the counts are compared.
- There is no false off-topic rate to match the false handoff rate. An evidence question or an injection attempt
  that the model answers as off-topic lowers no rate; a gap, personal, follow-up or job-description input answered
  that way lowers its own set's rate.
- Some sets are small (personal, gap, off-topic and follow-up), so their rates move in large steps.
"""
