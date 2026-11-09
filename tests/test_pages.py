"""The pages without the app: the static fallback.html, the answer section against render_text, the NEXT_STEP links,
the stylesheet copy, and the service's site and config-check steps. Every claim and question here is invented."""
import hashlib
from html import unescape
import json
import os
import re
from pathlib import Path

import pytest
from page_html import Page

from askmike.answer.answerer import Outcome
from askmike.answer.config import (
    EMPTY_TEXT, HANDOFF_TEXT, MAX_TOKENS, NEXT_STEP, OFF_TOPIC_TEXT, TOO_LONG_TEXT,
)
from askmike.answer.evidence import Claim
from askmike.answer.gates import BUNDLE, AnswerGateConfig
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.render import load_fallback, render_text
from askmike.answer.settings import load_answer_settings
from askmike.gates.text import (
    always_ok_link, as_skill, first_person, link_tokens, low_depth_hits, privacy_hits, slop_hits, verdict_hits,
)
from askmike.retrieval.requirements import Requirement
from askmike.service import pages
from askmike.service.__main__ import main
from askmike.service.results import load_facts

REPO = Path(__file__).resolve().parents[1]
CORPUS = {u["id"]: u for u in (json.loads(x) for x in (REPO / "corpus" / "units.jsonl")
                                .read_text(encoding="utf-8").splitlines() if x.strip())}
SKILLS = json.loads((REPO / "corpus" / "skills.json").read_text(encoding="utf-8"))
DTD_STYLE_SHA = "36b0c66"  # the Deal Terms Desk commit the stylesheet was copied from
FB = load_fallback()
U0, U1 = FB["units"][:2]
FIXED = {"handoff": HANDOFF_TEXT, "off_topic": OFF_TOPIC_TEXT, "empty": EMPTY_TEXT, "too_long": TOO_LONG_TEXT}
VISIT = "AbCdEfGhIjKlMnOpQrStUv"


def out(outcome="answer", kind="question", kept=(), reqs=(), reason=""):
    return Outcome(outcome, "answered" if outcome == "answer" else "", tuple(kept), (), reason, "",
                   ANSWER_TEMPLATE_SHA, None, tuple(reqs), {}, 3, kind, "")


@pytest.fixture(scope="module")
def fallback_html(tmp_path_factory) -> str:
    d = tmp_path_factory.mktemp("dist")
    assert main(["site", "--out", str(d)]) == 0
    assert sorted(p.name for p in d.iterdir()) == ["fallback.html"]
    return (d / "fallback.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("part", ["public", "private"])
def test_fallback_html_matches_fallback_json_and_passes_every_text_gate(part, fallback_html):
    p = Page(fallback_html)
    assert p.section() == [FB["intro"], *(f"{CORPUS[u]['claim']} [{u}]" for u in FB["units"])]
    assert p.items("ol") == [f"{CORPUS[u]['claim']} [{u}]" for u in FB["units"]]
    assert [h for h, _ in p.links if h.startswith("/evidence/")] == [f"/evidence/{u}" for u in FB["units"]]
    assert NEXT_STEP in p.lines and p.lines.count(pages.SAVED_LINE) == 1
    assert [(f["method"], f["action"]) for f in p.attrs("form")] == [("post", "/ask")]
    assert [d.get("name") for d in p.attrs("textarea")] == ["q"] and p.attrs("input") == []
    assert {h for h, _ in p.links} <= {"#main", "/", "/results", "https://forn.al/", "https://forn.al/resume.pdf",
                                       "mailto:mike@forn.al", *(f"/evidence/{u}" for u in FB["units"])}
    assert p.lines.index(NEXT_STEP) > p.lines.index(p.section()[-1])  # the next step follows the claims

    texts = [p.title, *p.lines, *(d.get("content", "") for d in p.attrs("meta") if d.get("name") == "description")]
    if part == "public":
        ledger = [as_skill(s) for s in SKILLS]
        for t in texts:
            assert slop_hits(t) == [], t
            assert not first_person(t), t
            assert verdict_hits(t) == [], t
            assert low_depth_hits(t, ledger, frozenset()) == [], t
            assert privacy_hits(t, [], []) == [], t  # phone, street, email, job-search counts
            assert all(always_ok_link(tok) for tok in link_tokens(t)), t  # the only links are forn.al's
        return

    private = os.environ.get("ASK_PRIVATE_DIR")
    path = Path(private).expanduser() / BUNDLE if private else None
    if path is None or not path.is_file():
        pytest.skip("privacy with the real gate bundle needs $ASK_PRIVATE_DIR and its deploy/gate_config.json")
    cfg = AnswerGateConfig.from_bundle(path, SKILLS)
    for i, t in enumerate(texts):
        assert privacy_hits(t, cfg.patterns, cfg.companies) == [], f"visible text #{i}"  # never echo a pattern


def test_the_answer_reads_as_render_text():
    """One line per kept claim (need → text for a jd) with its ids; a fixed string alone; the fallback's intro and
    its units' claims; then NEXT_STEP, on every answer page."""
    c1 = Claim("direct", "", "", "Mike wrote an invented importer.", (U0,))
    c2 = Claim("gap", "", "", "Mike hasn't run an invented scheduler; the closest is the importer.", (U0, U1))
    j1 = Claim("match", "invented batch need", "r01", c1.text, c1.units)
    j2 = Claim("match", "", "r02", c2.text, c2.units)  # a jd claim with no need reads as its text
    reqs = (Requirement("input-r01", "Invented batch work", True, "skill"),)
    cases = [out(kept=(c1, c2)), out(kind="jd", kept=(j1, j2), reqs=reqs), out("fallback", reason="parse"),
             out("fallback", kind="jd", reason="extract"), out("answer"), out("no-such-outcome"),
             *(out(k, kind=k) for k in FIXED)]
    for o in cases:
        p = Page(pages.answer_page(o, CORPUS, "Has Mike written an invented importer?", VISIT))
        want = [x for x in render_text(o, CORPUS).splitlines() if x]
        if o.outcome in FIXED:
            assert p.section() == want, o.outcome
        else:
            assert [*p.section(), NEXT_STEP] == [x.removeprefix("- ") for x in want], o.outcome
        assert NEXT_STEP in p.lines and p.lines.index(NEXT_STEP) > p.lines.index(p.section()[-1]), o.outcome


def test_what_was_asked_is_the_question_or_the_requirements_never_the_posting():
    reqs = (Requirement("input-r01", "Invented batch work", True, "skill"),
            Requirement("input-r02", "Invented reporting", False, "experience"))
    q = "Has Mike written an invented importer?"
    p = Page(pages.answer_page(out(kept=(Claim("direct", "", "", "Mike wrote it.", (U0,)),)), CORPUS, q, VISIT))
    assert p.lines[p.lines.index("You asked") + 1] == q and p.items("ul") == []
    posting = "Invented posting about a role.\n" * 30
    for o in (out(kind="jd", kept=(Claim("match", "n", "r01", "Mike wrote it.", (U0,)),), reqs=reqs),
              out("fallback", kind="jd", reqs=reqs, reason="gated_empty")):
        page = pages.answer_page(o, CORPUS, posting, VISIT)
        p = Page(page)
        assert "Requirements read from your job description" in p.lines and "Invented posting" not in page
        assert p.items("ul") == [r.text for r in reqs] and "You asked" not in p.lines
    # an input that never reached the model shows nothing of itself: empty, too long, a jd with no requirements
    for o, text in ((out("empty", kind="empty"), " "), (out("too_long", kind="too_long"), posting * 40),
                    (out("fallback", kind="jd", reason="extract"), posting)):
        page = pages.answer_page(o, CORPUS, text, None)
        p = Page(page)
        assert "Invented posting" not in page and "You asked" not in p.lines and p.items("ul") == []


def test_next_step_and_fixed_emails_are_the_only_links_made_from_text():
    parts = pages.linked(NEXT_STEP)
    assert "".join(t for t, _ in parts) == NEXT_STEP
    assert [(t, h) for t, h in parts if h] == [("mike@forn.al", "mailto:mike@forn.al"),
                                               ("https://forn.al/resume.pdf", "https://forn.al/resume.pdf")]
    assert pages.linked(HANDOFF_TEXT) == [("That one's for Mike: ", ""), ("mike@forn.al", "mailto:mike@forn.al"),
                                          (".", "")]
    # a claim that names a URL or an address shows it as text, never as a link
    url = Claim("direct", "", "", "Mike's notes live at https://forn.al/notes and mike@forn.al.", (U0,))
    p = Page(pages.answer_page(out(kept=(url,)), CORPUS, "Where are the invented notes?", VISIT))
    assert p.items("ol") == [f"{url.text} [{U0}]"]
    assert [h for h, _ in p.links if h not in ("#main", "/", "/results", "https://forn.al/")
            and not h.startswith("/evidence/")
            ] == ["mailto:mike@forn.al", "https://forn.al/resume.pdf"]  # NEXT_STEP's two, nothing from the claim


def test_next_step_leads_with_the_email():
    assert NEXT_STEP == "Email Mike: mike@forn.al · Résumé: https://forn.al/resume.pdf"
    page = pages.answer_page(out(kept=(Claim("direct", "", "", "Mike wrote it.", (U0,)),)), CORPUS,
                             "Has Mike written an invented importer?", VISIT)
    para = page.split('<p class="next">', 1)[1].split("</p>", 1)[0]
    assert [h for h, _ in Page(para).links] == ["mailto:mike@forn.al", "https://forn.al/resume.pdf"]


def test_stylesheet_is_the_dtd_copy_with_provenance_and_a_versioned_link():
    css = pages.STYLE.read_text(encoding="utf-8")
    head = css.split("*/", 1)[0]
    assert "Deal Terms Desk" in head and "site/static/style.css" in head and DTD_STYLE_SHA in head
    assert "--paper:" in css and "--mono:" in css and ".desk-form" in css  # forn.al's tokens, DTD's form
    assert "url(" not in css and "@import" not in css  # nothing loaded from anywhere else
    v = hashlib.sha256(pages.STYLE.read_bytes()).hexdigest()[:12]
    assert pages.style_href() == f"/static/style.css?v={v}"


def test_evidence_page_shows_the_unit():
    u = next(x for x in CORPUS.values() if x["when"] and x["detail"])
    p = Page(pages.evidence_page(u))
    assert p.lines[p.lines.index(u["claim"]) + 1] == u["detail"]
    assert u["when"].replace("..", " to ") in p.lines and p.attrs("form") == []


def test_config_check_prints_the_settings_and_caps(monkeypatch, capsys):
    monkeypatch.setenv("ASK_MONTH_CAP_USD", "15")
    monkeypatch.setenv("ASK_DAY_CAP_USD", "1.5")
    assert main(["config-check"]) == 0
    s = load_answer_settings()
    assert json.loads(capsys.readouterr().out) == {
        "models": s.model, "effort": s.effort, "max_tokens": MAX_TOKENS, "template_sha": ANSWER_TEMPLATE_SHA,
        "month_cap_usd": 15.0, "day_cap_usd": 1.5}


@pytest.mark.parametrize("env", [{}, {"ASK_MONTH_CAP_USD": "fifteen"}, {"ASK_MONTH_CAP_USD": "15",
                                                                         "ASK_DAY_CAP_USD": "-1"}])
def test_config_check_exits_2_on_a_bad_env(env, monkeypatch, capsys):
    for k in ("ASK_MONTH_CAP_USD", "ASK_DAY_CAP_USD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert main(["config-check"]) == 2
    got = capsys.readouterr()
    assert got.out == "" and got.err.startswith("config-check: ") and "ASK_" in got.err


def test_every_page_links_results():
    units = pages.load_units(REPO / "corpus" / "units.jsonl")
    built = {"home": pages.home_page(), "not-found": pages.not_found_page(),
             "answer": pages.answer_page(out("handoff", kind="handoff"), units, "Invented pay question?", None),
             "evidence": pages.evidence_page(CORPUS[U0]), "fallback": pages.fallback_page(units)}
    for name, html in built.items():
        assert "/results" in [href for href, _ in Page(html).links], name


def _every_page() -> dict:
    units = pages.load_units(REPO / "corpus" / "units.jsonl")
    return {"home": pages.home_page(), "not-found": pages.not_found_page(),
            "answer": pages.answer_page(out("handoff", kind="handoff"), units, "Invented pay question?", None),
            "evidence": pages.evidence_page(CORPUS[U0]), "fallback": pages.fallback_page(units),
            "results": pages.results_page(load_facts(REPO / "facts.json"))}


def test_results_nav_link_marks_only_the_results_page_as_current():
    built = _every_page()
    assert '<a href="/results" aria-current="page">Results</a>' in built.pop("results")
    for name, html in built.items():
        assert "aria-current" not in html, name


def test_every_page_links_the_favicon_it_ships():
    for name, html in _every_page().items():
        assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in html, name
    assert (pages.STATIC / "favicon.svg").read_bytes().startswith(b"<svg")
    assert (pages.STATIC / "favicon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"  # the ICO header


QUESTION = "Has Mike written an invented importer?"
POSTING = "Invented posting about a role.\nSecond invented posting line.\nThird invented line."


def _three_outcomes():
    reqs = (Requirement("input-r01", "Invented batch work", True, "skill"),)
    return [(out(kept=(Claim("direct", "", "", "Mike wrote it.", (U0,)),)), QUESTION),
            (out(kind="jd", kept=(Claim("match", "n", "r01", "Mike wrote it.", (U0,)),), reqs=reqs), POSTING),
            (out("handoff", kind="handoff"), "What is his salary?")]


def test_head_and_tail_make_one_valid_page():
    for o, text in _three_outcomes():
        page = pages.answer_page(o, CORPUS, text, VISIT)
        assert page == pages.answer_head(o.kind, text, False) + pages.answer_tail(o, CORPUS, VISIT)
        Page(page)
        assert page.count("<html") == 1 and page.count("</html>") == 1


def test_waiting_head_shows_the_question_and_the_wait_line():
    head = unescape(pages.answer_head("question", QUESTION, True))
    assert QUESTION in head and pages.WAIT_QUESTION in head and 'class="reading"' in head
    assert "</main>" not in head and 'class="answer"' not in head


def test_waiting_head_for_a_jd_never_shows_the_posting():
    head = unescape(pages.answer_head("jd", POSTING, True))
    assert pages.JD_LABEL in head and pages.WAIT_JD in head
    assert not any(line in head for line in POSTING.splitlines())


def _body_text(html: str) -> str:
    body = html[html.index("<body"):]
    return "".join(unescape(re.sub(r"<[^>]*>", " ", body)).split())


def test_waiting_head_holds_enough_text_for_webkit_to_paint():
    for kind, text in (("question", "Hi?"), ("jd", "a short posting of few words")):
        assert len(_body_text(pages.answer_head(kind, text, True))) > 200, kind


def test_wait_note_sits_inside_the_reading_block_and_only_in_a_waiting_head():
    head = unescape(pages.answer_head("question", "Hi?", True))
    reading = head[head.index('<div class="reading">'):]
    assert pages.WAIT_NOTE in reading[:reading.index("</div>")]
    assert pages.WAIT_NOTE not in unescape(pages.answer_head("question", "Hi?", False))


def test_a_streamed_jd_page_shows_its_label_only_inside_the_reading_block():
    o, text = _three_outcomes()[1]
    page = pages.answer_head("jd", text, True) + pages.answer_tail(o, CORPUS, VISIT)
    assert page.count(pages.JD_LABEL) == 1
    reading = page[page.index('<div class="reading">'):]
    assert pages.JD_LABEL in reading[:reading.index("</div>")]
    assert pages.JD_LABEL not in pages.answer_page(o, CORPUS, text, VISIT)


def test_waiting_head_escapes_the_question():
    head = pages.answer_head("question", "<b>x</b> & y", True)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in head and "<b>x</b>" not in head


def test_failed_tail_closes_the_page_with_the_fallback():
    page = pages.answer_head("question", QUESTION, True) + pages.failed_tail(CORPUS)
    text = unescape(page)
    assert FB["intro"] in text and all(t in text for t, _ in pages.linked(NEXT_STEP))
    for u in FB["units"]:
        assert CORPUS[u]["claim"] in text
    assert 'name="prev"' not in page and page.rstrip().endswith("</html>")


def test_stylesheet_hides_the_wait_line_once_the_answer_is_in():
    css = pages.STYLE.read_text(encoding="utf-8")
    assert "main:has(.answer) .reading { display: none; }" in css.split("Ask Mike additions", 1)[1]
