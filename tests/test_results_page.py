"""The results page: every number on it is a fact's display string from facts.json, its copy passes the answer
gates' slop and voice checks, and a fact it needs that is missing stops it from being built."""
import json
from pathlib import Path

import pytest
from page_html import Page

from askmike.facts import copycheck
from askmike.gates.text import first_person, slop_hits
from askmike.service import pages
from askmike.service.results import load_facts, results_view

REPO = Path(__file__).resolve().parents[1]
FACTS = load_facts(REPO / "facts.json")
HEADLINE = ("m1_jdreq_R4_report_recall_at_10", "m1_jdreq_KW_report_recall_at_10", "m2_haiku_support_rate",
            "m2_sonnet_support_rate", "m2_cal_cost_per_answer_mean", "m3_model_cap_usd")


def _text() -> str:
    return "\n".join(Page(pages.results_page(FACTS)).lines)


def test_every_number_on_the_results_page_is_a_fact():
    flagged = [x for x in copycheck.check(_text(), FACTS) if x.status in copycheck.FLAGGED]
    assert flagged == []


def test_results_page_prints_the_headline_facts():
    text = _text()
    for key in HEADLINE:
        assert FACTS[key]["display"] in text, key


def test_results_page_copy_passes_the_answer_gates():
    text = _text()
    assert slop_hits(text) == []
    assert first_person(text) is False


def test_results_view_refuses_a_missing_fact():
    facts = {k: v for k, v in FACTS.items() if k != "m2_haiku_support_rate"}
    with pytest.raises(KeyError, match="m2_haiku_support_rate"):
        results_view(facts)


def test_results_page_has_no_script_or_inline_style():
    html = pages.results_page(FACTS)
    assert "<script" not in html and "style=" not in html


def test_load_facts_reads_the_file(tmp_path):
    p = tmp_path / "facts.json"
    p.write_text(json.dumps({"k": {"value": 1, "display": "1"}}), encoding="utf-8")
    assert load_facts(p) == {"k": {"value": 1, "display": "1"}}
