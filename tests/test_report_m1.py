"""docs/m1/REPORT.md: generated from facts.json, every digit from a fact, plain writing, and the CI gate.

Pattern from deal-terms-desk tests/test_report_m2.py (Every() sentinel, allowed identifiers) and tests/test_facts.py
(the committed report equals its render), adapted 2026-11-03.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from askmike.facts.report import render
from askmike.gates.patterns import HYPE, SLOP

REPO_DIR = Path(__file__).resolve().parents[1]
FACTS, REPORT = REPO_DIR / "facts.json", REPO_DIR / "docs" / "m1" / "REPORT.md"
SENTINEL = 7777


class Every(dict):
    """Any fact renders as a sentinel number, so the template is checked, not the data."""
    def __missing__(self, key):
        return SENTINEL


# Identifiers, not figures: model names, the metric names and their cutoffs, rung and milestone names, k0.
ALLOWED = re.compile(r"claude-[a-z]+-\d+-\d+|bge-small-en-v\d+\.\d+|BM25|FTS5|"
                     r"(?i:recall|mrr|ndcg|coverage|judged)@\d+|top-10|\bR\d\b|\bM\d\b|\bk0\b")


def stray_lines(text: str) -> list[str]:
    text = text.replace(str(SENTINEL), "")
    return [line for line in text.splitlines() if re.search(r"\d", ALLOWED.sub("", line))]


def test_report_has_no_stray_digits():
    assert stray_lines(render(Every())) == []
    e = Every()
    e["m1_kappa_AB"] = None
    e["m1_cmp_R4_vs_KW_jdreq_recall_at_10_lo"] = -SENTINEL
    e["m1_cmp_R4_vs_KW_jdreq_recall_at_10_hi"] = -SENTINEL
    assert stray_lines(render(e)) == []


def test_report_render_is_deterministic(m1_facts):
    a = render(m1_facts)
    assert a == render(json.loads(json.dumps(m1_facts)))
    assert a == render(dict(reversed(list(m1_facts.items()))))
    assert a.endswith("\n") and not a.endswith("\n\n")
    for key in ("m1_jdreq_R4_report_recall_at_10", "m1_jdreq_R4_report_recall_at_10_lo", "m1_kappa_AB",
                "m1_tau_rungs_AB", "m1_n_requirements", "m1_jdreq_KW_report_coverage_at_10",
                "m1_cmp_R4_vs_KW_jdreq_recall_at_10_delta", "m1_tuned_rrf_k0", "m1_n_personal_invented"):
        assert m1_facts[key]["display"] in a, key


def _no_slop(text: str) -> list[str]:
    hits = [p for p in SLOP if re.search(p, text, re.I)]
    return hits + [w for w in HYPE if re.search(rf"\b{re.escape(w)}\b", text, re.I)]


def test_report_has_no_slop(m1_facts):
    assert _no_slop(render(m1_facts)) == []
    assert _no_slop(render(Every())) == []
    if REPORT.exists():
        assert _no_slop(REPORT.read_text(encoding="utf-8")) == []


def test_report_carries_the_required_statements(m1_facts):
    text = render(m1_facts)
    for phrase in ("Mike's choice", "claude-opus-5-5", "claude-sonnet-5-5", "recruiter questions",
                   "in a rung order break by rung name, ascending", "invented phrasings",
                   "relative to the judged pool", "more than k relevant units", "steps of",
                   "One corpus snapshot", "per job description", "report split", "Do not edit by hand"):
        assert phrase in text, phrase
    assert f"at most {m1_facts['m1_req_cap']['display']} per job description" in text
    assert "M2 will revisit this" in text


def _flat(text: str) -> str:
    """Markdown joins wrapped lines; so do these checks."""
    return " ".join(text.split())


def test_report_scopes_each_claim_honestly(m1_facts):
    """Review fix 1 and controller ruling R6: say which numbers are report-split only, what judge B checks,
    and why an unsupported requirement is left out."""
    d = {k: v["display"] for k, v in m1_facts.items()}
    text = _flat(render(m1_facts))
    assert "every retrieval metric below is from the report split" in text
    assert "every metric below" not in text
    kappa = text.split("Cohen's κ (agreement corrected for chance)", 1)[1].split(" - ", 1)[0]
    assert "across both splits" in kappa
    summary = text.split("## Summary", 1)[1].split("## ", 1)[0]
    assert f"Between the two AI judges, Cohen's κ across both splits is {d['m1_kappa_AB']}" in summary
    assert (f"Kendall's τ between their rung orders on {d['m1_tau_n_items']} report-split requirements from "
            f"{d['m1_tau_n_jds']} job descriptions is {d['m1_tau_rungs_AB']}") in summary
    assert "Judge A (claude-opus-5-5) graded every AI-graded pool." in text
    assert "graded every pool" not in text and "on everything" not in text
    limits = text.split("## Limitations", 1)[1]
    assert ("Judge B's labels feed only the rung-order check (τ) and the agreement score (κ); every retrieval "
            "metric value comes from judge A's labels, except the personal questions, which use fixed gold "
            "units.") in limits
    assert "Judge B checks the rung order only" not in text
    assert "recall@k cannot reach a perfect score for requirements with more than k relevant units" in limits
    assert "also cannot reach its maximum" not in text
    assert "no pooled unit was judged relevant" in text
    assert "nothing to find" not in text


def test_judged_at_10_reads_as_a_minimum(m1_facts):
    """Final review 4: "is at least 1.00" read oddly for a minimum; name the lowest value instead."""
    text = _flat(render(m1_facts))
    assert (f"The lowest judged@10 of any report-split query, under any rung, is "
            f"{m1_facts['m1_judged_at_10_min']['display']}.") in text
    assert "is at least" not in text


def _with(f: dict, **values) -> dict:
    f = dict(f)
    for k, v in values.items():
        f[k] = dict(f[k], value=v, display=f"{v:,}")
    return f


def test_judge_b_sample_prints_judge_b_sizes_not_proxies(m1_facts):
    """Final review 1: judge B's sample sizes come from judge B's labels, not tau's items or the full set."""
    f = _with(m1_facts, m1_B_n_jds=41, m1_tau_n_jds=39, m1_B_n_questions=52, m1_n_questions=52)
    text = _flat(render(f))
    assert ("Judge B (claude-sonnet-5-5) graded a hash-picked sample on its own: 41 report-split job "
            "descriptions and all 52 recruiter questions.") in text
    f = _with(m1_facts, m1_B_n_jds=41, m1_B_n_questions=50, m1_n_questions=52)
    text = _flat(render(f))
    assert "41 report-split job descriptions and 50 of the 52 recruiter questions." in text
    assert "all 50" not in text


def test_limitations_disclose_short_names_and_the_cap_share(m1_facts):
    """Final review 2 and 3."""
    d = {k: v["display"] for k, v in m1_facts.items()}
    limits = _flat(render(m1_facts)).split("## Limitations", 1)[1]
    assert (f"R4's alias expansion matches skill names of {d['m1_short_name_max_len']} characters or fewer "
            f"(such as SQL or AWS) only when they are capitalised or upper case, as they usually are in the "
            f"extracted requirements; lowercase short names in typed questions are not expanded. "
            f"M2 will revisit this.") in limits
    assert (f"capped at {d['m1_req_cap']} per job description; {d['m1_jds_at_req_cap_share']} of job "
            f"descriptions reached the cap, so any requirements beyond it were not measured.") in limits


def test_kappa_none_prints_undefined(m1_facts):
    f = dict(m1_facts)
    f["m1_kappa_AB"] = dict(f["m1_kappa_AB"], value=None, display="undefined")
    text = render(f)
    assert "Cohen's κ" in text and "undefined" in text
    assert "None" not in text and "True" not in text and "False" not in text


def _cmp(f, lo, hi):
    f = dict(f)
    f["m1_cmp_R4_vs_KW_jdreq_recall_at_10_lo"] = dict(f["m1_cmp_R4_vs_KW_jdreq_recall_at_10_lo"], value=lo)
    f["m1_cmp_R4_vs_KW_jdreq_recall_at_10_hi"] = dict(f["m1_cmp_R4_vs_KW_jdreq_recall_at_10_hi"], value=hi)
    return render(f)


def test_the_r4_versus_keyword_sentence_follows_the_interval(m1_facts):
    assert "the interval is above zero" in _cmp(m1_facts, 0.01, 0.2)
    assert "the interval includes zero" in _cmp(m1_facts, -0.01, 0.2)
    assert "the interval is below zero" in _cmp(m1_facts, -0.2, -0.01)


def test_no_job_search_words_in_the_report(m1_facts):
    for text in (render(m1_facts), REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""):
        assert not re.search(r"applied|top50|top-50|application|company|companies", text, re.I)


# ---- the CI gate: the committed report and README ----

def test_report_equals_render():
    assert REPORT.read_text(encoding="utf-8") == render(json.loads(FACTS.read_text(encoding="utf-8")))


def test_copycheck_passes_on_readme_and_report():
    r = subprocess.run([sys.executable, "-m", "askmike.facts.copycheck", "README.md", "docs/m1/REPORT.md"],
                       cwd=REPO_DIR, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
