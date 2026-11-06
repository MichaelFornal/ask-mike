"""docs/m2/REPORT.md: generated from facts.json, every digit from a fact, plain writing, and the CI gate.

Pattern from tests/test_report_m1.py (Every() sentinel, allowed identifiers, the committed report equals its
render), adapted 2026-11-04 for Task 11a.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from askmike.facts.report_m2 import render
from askmike.gates.patterns import HYPE, SLOP

REPO_DIR = Path(__file__).resolve().parents[1]
FACTS, REPORT = REPO_DIR / "facts.json", REPO_DIR / "docs" / "m2" / "REPORT.md"
SENTINEL = 7777
DRIFT = {"m2_drift_reqs_old": 3.0, "m2_drift_reqs_new": 3.25, "m2_drift_matched_share": 0.77}


class Every(dict):
    """Any fact renders as a sentinel number, so the template is checked, not the data."""
    def __missing__(self, key):
        return SENTINEL


# Identifiers, not figures: model ids, milestone names, the step names and the percentile names.
ALLOWED = re.compile(r"claude-[a-z]+-\d+-\d+|\bM\d\b|\b(?:facts|report)-m\d\b|\bp(?:50|95)\b")


def stray_lines(text: str) -> list[str]:
    text = text.replace(str(SENTINEL), "")
    return [line for line in text.splitlines() if re.search(r"\d", ALLOWED.sub("", line))]


@pytest.fixture(scope="module")
def facts(m1_facts, m2_facts):
    return {**m1_facts, **m2_facts}


def _with_drift(f: dict) -> dict:
    from askmike.facts.build_m2 import display
    return {**f, **{k: {"value": v, "display": display(k, v), "cmd": "x", "inputs": {}} for k, v in DRIFT.items()}}


def test_report_m2_deterministic_and_no_stray_digits(facts):
    assert stray_lines(render(Every())) == []
    e = Every()
    for k, v in DRIFT.items():
        e[k] = SENTINEL
    e["m2_kappa_support_AB"] = None
    e["m2_haiku_support_rate"] = None
    assert stray_lines(render(e)) == []

    a = render(facts)
    assert a == render(json.loads(json.dumps(facts)))
    assert a == render(dict(reversed(list(facts.items()))))
    assert a.endswith("\n") and not a.endswith("\n\n")
    for key in ("m2_haiku_support_rate", "m2_sonnet_support_rate_lo", "m2_haiku_coverage_hi", "m2_cal_cost_total",
                "m2_cal_outcome_agreement", "m2_judge_planted_n", "m2_kappa_support_AB", "m2_n_items_jd",
                "m2_sonnet_pregate_grounding_claims", "m2_cal_verdict", "m2_template_sha"):
        assert facts[key]["display"] in a, key
    b = render(_with_drift(facts))
    assert b != a and "77%" in b and "3.25" in b


def _no_slop(text: str) -> list[str]:
    hits = [p for p in SLOP if re.search(p, text, re.I)]
    return hits + [w for w in HYPE if re.search(rf"\b{re.escape(w)}\b", text, re.I)]


def test_report_m2_has_no_slop_and_no_job_search_words(facts):
    for text in (render(facts), render(Every()), render(_with_drift(facts)),
                 REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""):
        assert _no_slop(text) == []
        assert not re.search(r"applied|top50|top-50|application|company|companies", text, re.I)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert m, f"no section {title!r}"
    return _flat(m.group(1))


def test_report_m2_covers_the_plans_sections(facts):
    text = render(facts)
    for title in ("Summary", "What was measured", "Sets and sizes", "Results by model (report split)", "Zero-bars",
                  "Judge calibration", "The pick", "API calibration and cost per answer", "Limitations"):
        _section(text, title)
    assert "Do not edit by hand" in text and "`uv run python -m askmike.evals report-m2`" in text
    results = _section(text, "Results by model (report split)")
    for m in ("haiku", "sonnet"):
        d = {k[len(f"m2_{m}_"):]: v["display"] for k, v in facts.items() if k.startswith(f"m2_{m}_")}
        for metric in ("support_rate", "coverage", "fallback_rate"):
            assert f"{d[metric]} ({d[metric + '_lo']} to {d[metric + '_hi']})" in results, (m, metric)
    judge = _section(text, "Judge calibration")
    assert "claude-opus-5-5" in judge and "No human labelled" in judge and "Mike's choice" in judge
    assert "side by side" in judge and facts["m2_judge_planted_pos_acc"]["display"] in judge
    zero = _section(text, "Zero-bars")
    assert "Both models met every zero-bar on the report split." in zero


def test_the_pick_section_states_the_split_in_words_with_no_quote(facts):
    pick = _section(render(facts), "The pick")
    assert "Haiku answers questions and Sonnet answers pasted job descriptions" in pick
    assert "claude-haiku-5-5" in pick and "claude-sonnet-5-5" in pick
    assert "gave no written reason" in pick and '"' not in pick and "“" not in pick and ">" not in pick
    # same model, not the same output: no claim that live extraction matches the evals
    assert "runs on the same model as the evals' extraction" in pick
    assert "matches the evals" not in pick and "no extraction drift" not in pick
    drift = _section(render(_with_drift(facts)), "The pick")
    assert "runs on the same model" not in drift and "77%" in drift


def test_the_calibration_discloses_parity_by_construction_and_the_extraction_counts(facts):
    d = {k: v["display"] for k, v in facts.items()}
    cal = _section(render(facts), "API calibration and cost per answer")
    assert "built on M1's requirements, as the `claude -p` records were" in cal
    assert "holds by construction" in cal
    for key in ("m2_cal_extract_reqs_api_mean", "m2_cal_extract_reqs_cli_mean", "m2_cal_extract_n_jd",
                "m2_cal_extract_n_jd_differ"):
        assert d[key] in cal, key
    assert (f"the count differed on {d['m2_cal_extract_n_jd_differ']} of the {d['m2_cal_extract_n_jd']} "
            "job descriptions") in cal
    assert "Booked spend" in cal and d["m2_cal_cost_total"] in cal


def test_the_results_table_counts_inputs_not_answers(facts):
    results = render(facts).split("## Results by model (report split)")[1].split("\n## ")[0]
    assert f"| Inputs | {facts['m2_haiku_n_answers']['display']} | {facts['m2_sonnet_n_answers']['display']} |" in results
    assert "| Answers |" not in results


def test_report_code_names_no_model_id():
    """Model ids come from ANSWER_MODELS, JUDGE_A, JUDGE_B and the paraphrase model, never typed in the template."""
    import inspect

    from askmike.answer.config import ANSWER_MODELS
    from askmike.evals.judge import JUDGE_A, JUDGE_B
    from askmike.facts import report_m2
    assert not re.search(r"claude-[a-z]+-\d", inspect.getsource(report_m2))
    text = render(Every())
    for model in (*ANSWER_MODELS, JUDGE_A, JUDGE_B):
        assert model in text, model


def test_limitations_carry_every_required_statement(facts):
    d = {k: v["display"] for k, v in facts.items()}
    limits = _section(render(facts), "Limitations")
    for phrase in ("AI-graded", "No human labelled a sample; that was Mike's choice",
                   "verbatim or paraphrased", "single claim", "depend on this prompt",
                   f"One corpus snapshot: every number is for the {d['m1_n_units']} units",
                   f"Outside the {d['m2_cal_n']}-item API calibration, `claude -p` answers stand in for API answers",
                   "tuned on judge A's question-kind labels from both splits",
                   "The extracted requirement texts were not kept in this calibration, so how many requirements "
                   "matched is not measured",
                   "There is no false off-topic rate to match the false handoff rate",
                   "An evidence question or an injection attempt that the model answers as off-topic lowers no rate",
                   "lowers its own set's rate"):
        assert phrase in limits, phrase


def test_zero_bar_sentence_follows_the_values(facts):
    f = dict(facts)
    f["m2_sonnet_handoff_correct_rate"] = dict(f["m2_sonnet_handoff_correct_rate"], value=0.97, display="0.97")
    zero = _section(render(f), "Zero-bars")
    assert "Both models met every zero-bar" not in zero and "Sonnet missed a zero-bar" in zero


def test_none_values_print_as_words(facts):
    f = dict(facts)
    f["m2_kappa_support_AB"] = dict(f["m2_kappa_support_AB"], value=None, display="n/a")
    f["m2_haiku_coverage"] = dict(f["m2_haiku_coverage"], value=None, display="n/a")
    text = render(f)
    assert not re.search(r"\b(?:None|True)\b", text) and "False" not in text.replace("False handoff", "")
    assert "n/a" in text


# ---- the CI gate: the committed report and the copy check ----

def test_report_m2_equals_render():
    assert REPORT.read_text(encoding="utf-8") == render(json.loads(FACTS.read_text(encoding="utf-8")))
    r = subprocess.run([sys.executable, "-m", "askmike.facts.copycheck", "README.md", "docs/m1/REPORT.md",
                        "docs/m2/REPORT.md"], cwd=REPO_DIR, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_copycheck_passes_on_the_rendered_m2_report(facts, tmp_path):
    """Every number in the report renders from a fact display, so the copy check finds no stray figure."""
    from askmike.facts.copycheck import FLAGGED, check
    for f in (facts, _with_drift(facts)):
        found = check(render(f), f)
        assert [x for x in found if x.status in FLAGGED] == [], found


def test_report_names_the_requirement_cap_and_the_grounding_only_count(facts):
    d = {k: v["display"] for k, v in facts.items()}
    text = render(facts)
    assert f"Extraction keeps at most {d['m1_req_cap']} requirements per job description" in text
    assert "a matching count does not mean matching requirements" in text
    row = next(line for line in text.splitlines() if line.startswith("| Claims dropped by grounding alone |"))
    for slug in ("haiku", "sonnet"):
        assert d[f"m2_{slug}_n_grounding_only"] in row


def test_kappa_shows_three_places():
    from askmike.facts.build_m2 import display
    assert display("m2_kappa_support_AB", 0.7961) == "0.796"
