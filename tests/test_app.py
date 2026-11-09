"""The web app: every route answers with a page, never an error page, and no page holds JavaScript. Every question,
posting, claim and requirement here is invented. No test calls a real model: the desks are fakes, or build_desk with
a fake runner and the FakeEmbedder over an invented corpus."""
import asyncio
import hashlib
import inspect
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from answer_fakes import GOOD, SKILLS, UNITS, FakeRunner, claim, structured
from fakes import FakeEmbedder
from fastapi.testclient import TestClient
from page_html import Page

from askmike.answer.answerer import Outcome
from askmike.answer.config import EMPTY_TEXT, HANDOFF_TEXT, NEXT_STEP, OFF_TOPIC_TEXT, TOO_LONG_TEXT
from askmike.answer.evidence import Claim, canonical_subject
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.render import load_fallback
from askmike.retrieval.index import build_index
from askmike.retrieval.requirements import Requirement
from askmike.service import desk as desk_mod
from askmike.service import pages
from askmike.service.__main__ import main as service_main
from askmike.service.app import BODY_CAP, create_app
from askmike.service.config import ServiceConfig
from askmike.service.desk import DeskRefused, Pending, Served, build_desk
from askmike.service.limits import client_key
from askmike.service.pages import SAVED_LINE

REPO = Path(__file__).resolve().parents[1]
CORPUS = {u["id"]: u for u in (json.loads(x) for x in (REPO / "corpus" / "units.jsonl")
                                .read_text(encoding="utf-8").splitlines() if x.strip())}
STYLE = REPO / "askmike" / "service" / "static" / "style.css"
FB = load_fallback()
U0, U1, U2 = FB["units"][:3]  # real public unit ids, so the evidence links open real pages
VISIT = "AbCdEfGhIjKlMnOpQrStUv"  # the shape of a visit id
QUESTION = "Has Mike built an invented queue for nightly jobs?"
POSTING = "\n".join(f"Invented posting line {i}: the team wants batch pipelines and reporting." for i in range(8))
C1 = Claim("direct", "", "", "Mike wrote an invented importer for the depot.", (U0,))
C2 = Claim("direct", "", "", "Mike ran invented load tests on the queue.", (U1, U2))
REQS = (Requirement("input-r01", "Invented batch pipelines in Python", True, "skill"),
        Requirement("input-r02", "Invented SQL for reporting", True, "skill"))
J1 = Claim("match", "invented batch need", "r01", C1.text, C1.units)
J2 = Claim("match", "invented reporting need", "r02", C2.text, C2.units)
FIXED = {"handoff": HANDOFF_TEXT, "off_topic": OFF_TOPIC_TEXT, "empty": EMPTY_TEXT, "too_long": TOO_LONG_TEXT}
GATE = {"patterns": ["zzq[0-9]+x"], "companies": ["Quillfeather Labs"], "honesty_allow": [], "built_from": {}}
FORM = {"content-type": "application/x-www-form-urlencoded"}


def out(outcome="answer", kind="question", kept=(), reqs=(), reason=""):
    return Outcome(outcome, "answered" if outcome == "answer" else "", tuple(kept), (), reason, "",
                   ANSWER_TEMPLATE_SHA, None, tuple(reqs), {}, 3, kind, "")


def served(o, status=200, served_from="model", retry_after=0, visit=VISIT):
    return Served(o, visit, status, served_from, retry_after)


class FakeDesk:
    """Serves `reply` (a Served, an exception to raise, or a callable taking the text) and records each ask.
    `prepare` is `ask`: every reply here is decided before a model call, so it never gives a Pending, and the route
    never calls `finish` or `release`."""

    def __init__(self, reply=None, health=None):
        self.reply, self.calls = reply, []
        self._health = health if health is not None else {"ok": True, "git_sha": "g1", "budget": "ok"}

    def ask(self, text, prev_id, client):
        self.calls.append((text, prev_id, client))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply(text) if callable(self.reply) else self.reply

    def prepare(self, text, prev_id, client):
        return self.ask(text, prev_id, client)

    def finish(self, p):
        raise AssertionError("FakeDesk.prepare never gives a Pending")

    def release(self, p):
        raise AssertionError("FakeDesk.prepare never gives a Pending")

    def health(self):
        if isinstance(self._health, BaseException):
            raise self._health
        return self._health


class StreamDesk:
    """Every ask needs the model: `prepare` gives a Pending, and `finish` serves `reply` (a Served) or raises it (an
    exception). Unlike the live desk, `finish` gives no slot back, so `released` holds each `release` call the route
    makes."""

    def __init__(self, reply):
        self.reply, self.released = reply, []

    def prepare(self, text, prev_id, client):
        return Pending(VISIT, "question", text, prev_id, client, None, "invented-cache-key", 0.0)

    def finish(self, p):
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    def release(self, p):
        self.released.append(p)


def asgi_post(app, body: bytes, disconnect_after_first=False) -> list[bytes]:
    """POST `body` to /ask straight through the ASGI app, and return each http.response.body message's bytes, in
    order. TestClient joins them, so it cannot tell a stream from one body. With `disconnect_after_first` the
    visitor leaves once the first body message is out: from then on `receive` gives http.disconnect."""
    chunks: list[bytes] = []

    async def run():
        first_out, read = asyncio.Event(), False

        async def receive():
            nonlocal read
            if not read:
                read = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect_after_first:
                await first_out.wait()
                return {"type": "http.disconnect"}
            await asyncio.Event().wait()  # the visitor stays until the response ends
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                first_out.set()

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/ask", "raw_path": b"/ask", "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"testserver"), (b"content-type", FORM["content-type"].encode()),
                             (b"content-length", str(len(body)).encode())],
                 "client": ("testclient", 50000), "server": ("testserver", 80)}
        await app(scope, receive, send)

    asyncio.run(run())
    return chunks


def where(fn, needle: str) -> str:
    """`tests/test_app.py:<line>` of the line in fn's source that holds needle: where its raise is logged."""
    src, first = inspect.getsourcelines(fn)
    (n,) = [first + i for i, x in enumerate(src) if needle in x]
    return f"tests/test_app.py:{n}"


def client_for(reply=None, **kw):
    desk = FakeDesk(reply)
    return TestClient(create_app(desk=desk), **kw), desk


@pytest.fixture
def real(tmp_path, monkeypatch):
    """build_desk over an invented corpus and its FakeEmbedder index, with a fake runner; desk.CORPUS points at it."""
    def make(runner):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "units.jsonl").write_text("".join(json.dumps(u) + "\n" for u in UNITS.values()), encoding="utf-8")
        (corpus / "skills.json").write_text(json.dumps(SKILLS), encoding="utf-8")
        build_index(corpus, tmp_path / "index.db", FakeEmbedder(), cache_path=tmp_path / "emb.db")
        gate = tmp_path / "gate_config.json"
        gate.write_text(json.dumps(GATE), encoding="utf-8")
        monkeypatch.setattr(desk_mod, "CORPUS", corpus)
        config = ServiceConfig(month_cap_usd=15.0, day_cap_usd=1.5, state_dir=tmp_path / "state",
                               index_path=tmp_path / "index.db", gate_config=gate, ask_per_hour=20,
                               fresh_per_hour=60, slots=2, api_timeout_s=90.0, git_sha="abc123")
        desk = build_desk(config, embedder=FakeEmbedder(), runner=runner)
        return TestClient(create_app(desk=desk), raise_server_exceptions=False), desk
    return make


def answer_reply(claims=(GOOD,)):
    r = structured(claims=claims)
    r["usage"] = {"input_tokens": 1000, "output_tokens": 200}
    return r


def box(p: Page) -> tuple[list[dict], list[str], list[dict]]:
    """The page's forms, its textarea names and its hidden inputs."""
    return p.attrs("form"), [d.get("name") for d in p.attrs("textarea")], p.attrs("input")


# ---- the ten plan tests ------------------------------------------------------------------------------------------

def test_home_has_the_form_posting_q_and_the_saved_line():
    c, desk = client_for()
    r = c.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    p = Page(r.text)
    forms, areas, inputs = box(p)
    assert [(f["method"], f["action"]) for f in forms] == [("post", "/ask")] * 4
    assert areas == ["q"] and [d["name"] for d in inputs] == ["q"] * 3  # no prev on a first question
    assert "Your question, or a job description" in p.lines
    assert [d.get("type") for d in p.attrs("button")] == ["submit"] * 4
    assert SAVED_LINE == "Questions are saved so Mike can read them."  # the global constraints' exact line
    assert p.lines.count(SAVED_LINE) == 1 and r.text.count("Questions are saved") == 1
    assert desk.calls == []


def test_home_explains_both_uses_and_offers_three_examples():
    p = Page(pages.home_page())
    for line in ("An AI that knows Mike's work. Two ways to use it:",
                 "Ask a question and get a direct answer.",
                 "Paste a job description and get the strongest matches between what the role needs and what Mike "
                 "has done.",
                 "It only uses a record of Mike's work that he has reviewed. Every line of the answer links to its "
                 "evidence."):
        assert line in p.lines, line
    forms, areas, inputs = box(p)
    assert areas == ["q"] and len(forms) == 4
    assert [(f["method"], f["action"]) for f in forms] == [("post", "/ask")] * 4
    assert [(d["type"], d["name"], d["value"]) for d in inputs] == [("hidden", "q", q)
                                                                    for q in pages.EXAMPLE_QUESTIONS]
    assert [d for d in inputs if d["name"] == "prev"] == []
    assert len(pages.EXAMPLE_QUESTIONS) == 3
    assert [d.get("type") for d in p.attrs("button")] == ["submit"] * 4
    for q in pages.EXAMPLE_QUESTIONS:
        assert q in p.lines
    assert p.lines.index("Or try one:") < p.lines.index(pages.EXAMPLE_QUESTIONS[0])


def test_the_uses_list_is_serif_with_a_mono_lead_phrase():
    css = STYLE.read_text(encoding="utf-8")
    additions = css[css.index("Ask Mike additions"):]

    def rule(sel):
        m = re.search(r"^" + re.escape(sel) + r" \{(.*?)\}", additions, re.S | re.M)
        assert m, sel
        return m.group(1)

    assert "font-family: var(--serif)" in rule(".uses li")
    assert "font-family: var(--mono)" in rule(".uses b")


def test_the_example_links_beat_the_submit_button_rule():
    css = STYLE.read_text(encoding="utf-8")
    m = re.search(r"^\.examples \.linkish \{(.*?)\}", css[css.index("Ask Mike additions"):], re.S | re.M)
    assert m
    body = m.group(1)
    assert "background: none" in body and "font-family: var(--serif)" in body and "font-weight: 400" in body
    assert "padding: 0" in body


def test_an_example_tap_reaches_the_desk_as_its_question():
    c, desk = client_for(served(out(kept=(C1,))))
    r = c.post("/ask", data={"q": pages.EXAMPLE_QUESTIONS[0]})
    assert r.status_code == 200
    assert [call[0] for call in desk.calls] == [pages.EXAMPLE_QUESTIONS[0]]


def test_not_found_keeps_the_off_topic_line_without_examples():
    p = Page(pages.not_found_page())
    forms, areas, inputs = box(p)
    assert OFF_TOPIC_TEXT in p.lines and "Or try one:" not in p.lines
    assert [f["action"] for f in forms] == ["/ask"] and areas == ["q"] and inputs == []
    assert "Two ways to use it" not in pages.not_found_page()


def test_answer_page_links_each_claim_to_its_evidence_and_shows_next_step():
    c, desk = client_for(served(out(kept=(C1, C2))))
    r = c.post("/ask", data={"q": QUESTION})
    assert r.status_code == 200
    p = Page(r.text)
    assert desk.calls == [(QUESTION, None, client_key("testclient"))]
    assert QUESTION in p.lines  # what was asked
    assert p.items("ol") == [f"{C1.text} [{U0}]", f"{C2.text} [{U1}, {U2}]"]  # as render_text, one line a claim
    assert [x for x in p.links if x[0].startswith("/evidence/")] == [(f"/evidence/{u}", u) for u in (U0, U1, U2)]
    assert NEXT_STEP in p.lines
    assert ("https://forn.al/resume.pdf", "https://forn.al/resume.pdf") in p.links
    assert ("mailto:mike@forn.al", "mike@forn.al") in p.links

    for u in (U0, U1, U2):  # each link opens its unit: subject's display name, when, claim and detail
        e = c.get(f"/evidence/{u}")
        assert e.status_code == 200
        ep, unit = Page(e.text), CORPUS[u]
        assert canonical_subject(unit["subject"]) in ep.lines and unit["claim"] in ep.lines
        assert not unit["detail"] or unit["detail"] in ep.lines
        assert not unit["when"] or unit["when"].replace("..", " to ") in ep.lines

    # a job description: its requirements, never the posting, and each claim as need → text
    c, _ = client_for(served(out(kind="jd", kept=(J1, J2), reqs=REQS)))
    r = c.post("/ask", data={"q": POSTING})
    p = Page(r.text)
    assert "Requirements read from your job description" in p.lines and "Invented posting line" not in r.text
    assert p.items("ul") == [x.text for x in REQS]
    assert p.items("ol") == [f"{J1.need} → {J1.text} [{U0}]", f"{J2.need} → {J2.text} [{U1}, {U2}]"]
    assert NEXT_STEP in p.lines

    # the fallback: its intro, then each of its units' claims, linked; then the next step
    c, _ = client_for(served(out("fallback", reason="runner_error")))
    p = Page(c.post("/ask", data={"q": QUESTION}).text)
    assert p.section()[0] == FB["intro"] and NEXT_STEP in p.lines
    assert p.items("ol") == [f"{CORPUS[u]['claim']} [{u}]" for u in FB["units"]]
    assert [h for h, _ in p.links if h.startswith("/evidence/")] == [f"/evidence/{u}" for u in FB["units"]]

    # a fixed outcome shows its fixed string; the handoff's email is a link
    for kind, text in FIXED.items():
        c, _ = client_for(served(out(kind, kind=kind), served_from="none"))
        p = Page(c.post("/ask", data={"q": QUESTION}).text)
        assert p.section() == [text] and p.items("ol") == [] and NEXT_STEP in p.lines
        if kind == "handoff":
            assert p.links.count(("mailto:mike@forn.al", "mike@forn.al")) == 2  # the handoff line and NEXT_STEP


def test_visitor_and_model_text_are_escaped():
    evil = "<script>alert(1)</script>"
    bold = Claim("direct", "", "", "Mike shipped <b>bold</b> invented queues & more.", (U0,))
    c, _ = client_for(served(out(kept=(bold,))))
    r = c.post("/ask", data={"q": evil})
    assert "<script" not in r.text.lower() and "<b>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text and "&lt;b&gt;bold&lt;/b&gt;" in r.text
    p = Page(r.text)
    assert evil in p.lines and p.items("ol") == [f"{bold.text} [{U0}]"]

    # a jd's need and requirements, and the id in the hidden field, are escaped as well
    need = Claim("match", '<img src=x onerror="alert(1)">', "r01", "Mike wrote <i>it</i>.", (U0,))
    req = Requirement("input-r01", "<a href='javascript:alert(1)'>invented</a>", True, "skill")
    c, _ = client_for(served(out(kind="jd", kept=(need,), reqs=(req,)), visit='"><script>x</script>'))
    r = c.post("/ask", data={"q": POSTING})
    p = Page(r.text)
    assert {t for t, _ in p.tags} & {"img", "i", "script"} == set()
    assert not any("javascript:" in (h or "") for h, _ in p.links)
    assert p.items("ul") == [req.text] and p.items("ol")[0].startswith(need.need)
    assert [d["value"] for d in p.attrs("input")] == ['"><script>x</script>']


def assert_clean(html: str, where: str) -> None:
    """No script, no inline style, no event handler, no javascript: URL; the one stylesheet is the versioned copy."""
    assert not re.search(r"<\s*script", html, re.I) and not re.search(r"<\s*style", html, re.I), where
    p = Page(html)
    for tag, attrs in p.tags:
        assert tag not in ("script", "style", "iframe", "object", "embed"), (where, tag)
        for name, value in attrs.items():
            assert name != "style" and not name.startswith("on"), (where, tag, name)
            assert "javascript:" not in (value or "").lower(), (where, tag, name)
    want = "/static/style.css?v=" + hashlib.sha256(STYLE.read_bytes()).hexdigest()[:12]
    assert [d.get("href") for d in p.attrs("link") if d.get("rel") == "stylesheet"] == [want], where


def test_pages_have_no_script_inline_style_or_handlers(tmp_path):
    replies = {
        "answer": served(out(kept=(C1, C2))),
        "jd": served(out(kind="jd", kept=(J1, J2), reqs=REQS)),
        "fallback": served(out("fallback", reason="parse")),
        "limited": served(out("fallback", reason="limited"), status=429, served_from="none", retry_after=30),
        "busy": served(out("fallback", reason="busy"), status=503, served_from="none"),
        "error": RuntimeError("an invented failure"),
        **{k: served(out(k, kind=k), served_from="none") for k in FIXED},
    }
    for name, reply in replies.items():
        c, _ = client_for(reply)
        assert_clean(c.post("/ask", data={"q": QUESTION}).text, f"POST /ask {name}")
    c, _ = client_for()
    for path in ("/", f"/evidence/{U0}", f"/evidence/{U2}", "/evidence/no-such-unit", "/docs", "/ask",
                 "/static/no-such.css"):
        r = c.get(path)
        assert r.status_code in (200, 404, 405) and r.headers["content-type"].startswith("text/html"), path
        assert_clean(r.text, f"GET {path}")
    assert service_main(["site", "--out", str(tmp_path)]) == 0
    assert_clean((tmp_path / "fallback.html").read_text(encoding="utf-8"), "fallback.html")


def test_followup_form_carries_prev_only_after_an_answer(real):
    for o, want in [(out(kept=(C1,)), [VISIT]), (out(kind="jd", kept=(J1, J2), reqs=REQS), [VISIT]),
                    (out("fallback", reason="no_evidence"), []), (out("fallback", reason="busy"), []),
                    *((out(k, kind=k), []) for k in FIXED)]:
        c, _ = client_for(served(o))
        p = Page(c.post("/ask", data={"q": QUESTION}).text)
        inputs = p.attrs("input")
        assert [(d.get("type"), d.get("name"), d.get("value")) for d in inputs] == [
            ("hidden", "prev", v) for v in want], o.outcome
        assert [d.get("name") for d in p.attrs("textarea")] == ["q"] and p.lines.count(SAVED_LINE) == 1

    c, desk = client_for(served(out(kept=(C1,))))
    c.post("/ask", data={"q": "And how was it measured?", "prev": VISIT})
    c.post("/ask", data={"q": QUESTION})
    assert [x[:2] for x in desk.calls] == [("And how was it measured?", VISIT), (QUESTION, None)]

    # end to end on a real desk: the id the answer page carries brings the previous turn back
    runner = FakeRunner(answer=answer_reply())
    c, _ = real(runner)
    first = "Has Mike sped up invented nightly jobs?"
    p = Page(c.post("/ask", data={"q": first}).text)
    assert p.items("ol") == [f"{GOOD['text']} [u1]"]
    prev = [d["value"] for d in p.attrs("input") if d.get("name") == "prev"]
    assert len(prev) == 1
    c.post("/ask", data={"q": "How did he measure that?", "prev": prev[0]})
    turn = "<previous_input>\n" + first
    assert len(runner.calls) == 2 and turn not in runner.calls[0]["prompt"] and turn in runner.calls[1]["prompt"]


def test_unknown_evidence_id_gets_a_page_not_an_error():
    c, _ = client_for()
    for path in ("/evidence/no-such-unit", "/evidence/%3Cscript%3Ealert(1)%3C%2Fscript%3E", "/evidence/a/b",
                 "/evidence/", "/no-such-page"):
        r = c.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("text/html"), path
        p = Page(r.text)
        forms, areas, inputs = box(p)
        assert OFF_TOPIC_TEXT in p.lines and p.lines.count(SAVED_LINE) == 1, path
        assert [f["action"] for f in forms] == ["/ask"] and areas == ["q"] and inputs == [], path
        assert "<script" not in r.text and "alert(1)" not in r.text, path


def test_fallback_html_matches_the_page_the_app_serves_when_it_fails(tmp_path):
    """The page Caddy serves while the app is down is the page the app serves when a request fails."""
    assert service_main(["site", "--out", str(tmp_path)]) == 0
    c, _ = client_for(RuntimeError("an invented failure"))
    r = c.post("/ask", data={"q": QUESTION})
    assert r.status_code == 200 and r.text == (tmp_path / "fallback.html").read_text(encoding="utf-8")


def test_bad_bodies_get_pages(real):
    runner = FakeRunner(answer=answer_reply())
    c, desk = real(runner)
    cases = [
        (b"q=" + b"a" * BODY_CAP, TOO_LONG_TEXT),  # over the cap
        (("q=" + quote("é" * 15000)).encode(), TOO_LONG_TEXT),  # over the cap, though fewer characters than the limit
        (b"prev=" + b"x" * BODY_CAP, TOO_LONG_TEXT),  # over the cap with no q in what was read
        (b"q=" + b"a" * (BODY_CAP - 2), TOO_LONG_TEXT),  # at the cap: the desk's own limit
        (b"\xff\xfe\xfd\x80\x81", EMPTY_TEXT),  # not UTF-8, and no q
        (b"", EMPTY_TEXT),
        (b"prev=" + VISIT.encode(), EMPTY_TEXT),
        (b"q", EMPTY_TEXT),
        (b"q=+++%20%0A", EMPTY_TEXT),
        (b'{"q": "Has Mike built invented queues?"}', EMPTY_TEXT),  # JSON is not a form
    ]
    for body, want in cases:
        r = c.post("/ask", content=body, headers=FORM)
        assert r.status_code == 200, body[:20]
        p = Page(r.text)
        assert p.section() == [want] and NEXT_STEP in p.lines, body[:20]
    assert runner.calls == []  # none of them reached a model
    rows = desk.visits.export()
    assert [x["kind"] for x in rows] == ["too_long"] * 4 + ["empty"] * 6
    assert rows[0]["input"].startswith("a" * 100) and rows[1]["input"].startswith("é" * 100)

    # q holding bytes that are not UTF-8 is read with replacement characters, and gets a page
    r = c.post("/ask", content=b"q=\xff\xfeHas+Mike+built+invented+queues%3F", headers=FORM)
    assert r.status_code == 200 and NEXT_STEP in Page(r.text).lines
    assert desk.visits.export()[-1]["input"] == "��Has Mike built invented queues?"
    # the first q and the first prev are the ones read
    c.post("/ask", content=b"q=Has+Mike+built+invented+queues%3F&q=second&prev=x&prev=y", headers=FORM)
    assert (desk.visits.export()[-1]["input"], desk.visits.export()[-1]["prev_id"]) == (
        "Has Mike built invented queues?", None)  # "x" is not an id's shape, so the desk drops it
    # a method the route does not take still gets a page
    r = c.get("/ask")
    assert r.status_code == 405 and OFF_TOPIC_TEXT in Page(r.text).lines


def test_docs_and_openapi_are_404():
    c, _ = client_for()
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = c.get(path)
        assert r.status_code == 404 and OFF_TOPIC_TEXT in Page(r.text).lines, path
        assert "swagger" not in r.text.lower() and "openapi" not in r.text.lower(), path
    assert sorted(getattr(rt, "path", "") for rt in c.app.routes) == [
        "/", "/ask", "/evidence/{unit_id}", "/health", "/results", "/static"]


def test_request_log_holds_no_address_and_no_text(capsys):
    secret = "Zephyrine asks: has Mike built an invented queue?"
    address = "2001:db8::7"
    desk = FakeDesk(lambda text: served(out(kept=(C1,))))
    c = TestClient(create_app(desk=desk), client=(address, 50000))
    capsys.readouterr()
    c.get("/")
    c.post("/ask", data={"q": secret, "prev": VISIT})
    c.get(f"/evidence/{U0}")
    c.get("/evidence/zephyrine-secret")
    c.get("/health")
    c.get("/static/style.css")
    c.get("/zephyrine")
    err = capsys.readouterr().err
    recs = [json.loads(x) for x in err.splitlines()]
    assert [(x["route"], x["status"]) for x in recs] == [
        ("home", 200), ("ask", 200), ("evidence", 200), ("evidence", 404), ("health", 200), ("static", 200),
        ("other", 404)]
    assert all(set(x) == {"route", "method", "status", "outcome", "served_from", "ms"} for x in recs)
    assert (recs[1]["outcome"], recs[1]["served_from"]) == ("answer", "model")
    assert all(isinstance(x["ms"], int) and x["ms"] >= 0 for x in recs)
    assert desk.calls == [(secret, VISIT, client_key(address))]
    for needle in ("2001:db8", client_key(address), "Zephyrine", "zephyrine", VISIT, "invented"):
        assert needle not in err, needle

    # a desk that raises: the fallback page with 200, and one line, outcome "error", holding no text
    c = TestClient(create_app(desk=FakeDesk(RuntimeError(f"boom {secret} from {address}"))),
                   client=(address, 50000))
    capsys.readouterr()
    r = c.post("/ask", data={"q": secret})
    assert r.status_code == 200 and Page(r.text).section()[0] == FB["intro"]
    err = capsys.readouterr().err
    (rec,) = [json.loads(x) for x in err.splitlines()]
    assert rec == {"route": "ask", "method": "POST", "status": 200, "outcome": "error", "served_from": "none",
                   "ms": rec["ms"],
                   "error": "RuntimeError", "where": where(FakeDesk.ask, "raise self.reply")}
    assert "Zephyrine" not in err and "2001:db8" not in err and "boom" not in err


def test_a_desk_error_logs_its_class_and_where_never_its_message(capsys):
    """The catch-all's line names the exception's class and the file:line of its innermost frame, and holds
    neither its message nor a traceback, which can carry the visitor's text."""
    c = TestClient(create_app(desk=FakeDesk(ValueError("visitor secret text"))))
    capsys.readouterr()
    r = c.post("/ask", data={"q": "visitor secret text"})
    assert r.status_code == 200 and Page(r.text).section()[0] == FB["intro"]
    err = capsys.readouterr().err
    (rec,) = [json.loads(x) for x in err.splitlines()]
    raised_at = where(FakeDesk.ask, "raise self.reply")
    assert (rec["outcome"], rec["error"], rec["where"]) == ("error", "ValueError", raised_at)
    assert set(rec) == {"route", "method", "status", "outcome", "served_from", "ms", "error", "where"}
    assert "visitor secret text" not in err and "Traceback" not in err



def test_request_log_names_the_method_so_a_head_ping_is_not_a_page_view(capsys):
    c = TestClient(create_app(desk=FakeDesk(lambda text: served(out(kept=(C1,))))))
    capsys.readouterr()
    c.head("/")
    c.get("/")
    c.head("/results")
    recs = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    assert [(x["route"], x["method"], x["status"]) for x in recs] == [
        ("home", "HEAD", 200), ("home", "GET", 200), ("results", "HEAD", 200)]

# ---- status codes, health and startup ----------------------------------------------------------------------------

def test_limited_sets_retry_after_rounded_up_and_busy_is_503():
    c, _ = client_for(served(out("fallback", reason="limited"), status=429, served_from="none", retry_after=37))
    r = c.post("/ask", data={"q": QUESTION})
    p = Page(r.text)
    assert (r.status_code, r.headers["retry-after"]) == (429, "37")
    assert p.section()[0] == FB["intro"] and NEXT_STEP in p.lines and p.attrs("input") == []
    c, _ = client_for(served(out("fallback", reason="limited"), status=429, served_from="none", retry_after=2.2))
    assert c.post("/ask", data={"q": QUESTION}).headers["retry-after"] == "3"
    c, _ = client_for(served(out("fallback", reason="busy"), status=503, served_from="none"))
    r = c.post("/ask", data={"q": QUESTION})
    assert r.status_code == 503 and "retry-after" not in r.headers and Page(r.text).section()[0] == FB["intro"]


def test_health_is_the_desks_health():
    c, desk = client_for()
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == desk.health()
    c = TestClient(create_app(desk=FakeDesk(health=RuntimeError("locked"))))
    r = c.get("/health")
    assert r.status_code == 503 and r.json() == {"ok": False}


def test_create_app_builds_the_desk_from_the_env_and_a_refusal_stops_it(monkeypatch):
    built = []

    def fake_build(config, embedder=None, runner=None):
        built.append(config)
        return FakeDesk()
    monkeypatch.setattr(desk_mod, "build_desk", fake_build)
    monkeypatch.setenv("ASK_MONTH_CAP_USD", "15")
    monkeypatch.delenv("ASK_DAY_CAP_USD", raising=False)
    assert TestClient(create_app()).get("/").status_code == 200
    assert (built[0].month_cap_usd, built[0].day_cap_usd) == (15.0, 1.5)

    def refuse(config, embedder=None, runner=None):
        raise DeskRefused("ASK_GATE_CONFIG is not set")
    monkeypatch.setattr(desk_mod, "build_desk", refuse)
    with pytest.raises(DeskRefused):
        create_app()
    monkeypatch.delenv("ASK_MONTH_CAP_USD")
    with pytest.raises(ValueError, match="ASK_MONTH_CAP_USD"):
        create_app()


def test_any_route_that_raises_gets_the_fallback_page(monkeypatch, capsys):
    """The middleware's last resort: a GET route that raises serves the fallback page with 200, logged as error."""
    def broken(unit):
        raise KeyError("Zephyrine")  # the raise the log names
    monkeypatch.setattr(pages, "evidence_page", broken)
    c, _ = client_for()
    capsys.readouterr()
    r = c.get(f"/evidence/{U0}")
    assert r.status_code == 200 and Page(r.text).section()[0] == FB["intro"]
    err = capsys.readouterr().err
    (rec,) = [json.loads(x) for x in err.splitlines()]
    assert (rec["route"], rec["status"], rec["outcome"]) == ("evidence", 200, "error") and "Zephyrine" not in err
    assert (rec["error"], rec["where"]) == ("KeyError", where(broken, "the raise the log names"))


# ---- the results page and HEAD --------------------------------------------------------------------------------------

def _facts() -> dict:
    return json.loads((REPO / "facts.json").read_text(encoding="utf-8"))


def test_results_page_is_served_from_facts():
    c, _ = client_for()
    r = c.get("/results")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert _facts()["m1_jdreq_R4_report_recall_at_10"]["display"] in "\n".join(Page(r.text).lines)


def test_every_get_route_answers_head():
    c, _ = client_for()
    for path in ("/", "/results", f"/evidence/{U0}", "/health"):
        r = c.head(path)
        assert r.status_code == 200 and r.content == b"", path


def test_results_takes_no_post():
    c, _ = client_for()
    r = c.post("/results")
    assert r.status_code == 405 and OFF_TOPIC_TEXT in Page(r.text).lines


def test_create_app_refuses_facts_missing_a_results_key(tmp_path):
    facts = _facts()
    del facts["m2_haiku_support_rate"]
    p = tmp_path / "facts.json"
    p.write_text(json.dumps(facts), encoding="utf-8")
    with pytest.raises(KeyError, match="m2_haiku_support_rate"):
        create_app(desk=FakeDesk(), facts_path=p)


# ---- the answer in two parts -------------------------------------------------------------------------------------

ASKED = urlencode({"q": QUESTION}).encode()


def app_units() -> dict:
    """The units the app renders answers from: desk.CORPUS's, as create_app reads them."""
    return pages.load_units(desk_mod.CORPUS / "units.jsonl")


def test_a_model_answer_streams_the_question_before_the_answer(capsys):
    o = out(kept=(C1,))
    desk = StreamDesk(served(o))
    app = create_app(desk=desk)
    capsys.readouterr()
    chunks = asgi_post(app, ASKED)
    assert len(chunks) >= 2
    head = unescape(chunks[0].decode())
    assert QUESTION in head and pages.WAIT_QUESTION in head and 'class="answer"' not in head
    html = b"".join(chunks).decode()
    assert html == pages.answer_head("question", QUESTION, True) + pages.answer_tail(o, app_units(), VISIT)
    p = Page(html)
    assert p.items("ol") == [f"{C1.text} [{U0}]"] and NEXT_STEP in p.lines and html.rstrip().endswith("</html>")
    (rec,) = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    assert (rec["route"], rec["status"], rec["outcome"], rec["served_from"]) == ("ask", 200, "answer", "model")
    assert len(desk.released) == 1


def test_a_failure_after_the_first_part_ends_the_page_with_the_fallback(monkeypatch, capsys):
    desk = StreamDesk(RuntimeError("visitor text here"))
    app = create_app(desk=desk)
    want = pages.answer_head("question", QUESTION, True) + pages.failed_tail(app_units())

    def unrendered(units):
        raise AssertionError("the failed tail is rendered when the app starts, never mid-stream")
    monkeypatch.setattr(pages, "failed_tail", unrendered)
    capsys.readouterr()
    chunks = asgi_post(app, ASKED)
    html = b"".join(chunks).decode()
    assert html == want
    p = Page(html)
    assert p.section()[0] == FB["intro"] and NEXT_STEP in p.lines and html.rstrip().endswith("</html>")
    assert p.attrs("input") == []  # no prev after a failure
    err = capsys.readouterr().err
    (rec,) = [json.loads(x) for x in err.splitlines()]
    assert rec == {"route": "ask", "method": "POST", "status": 200, "outcome": "error", "served_from": "none",
                   "ms": rec["ms"], "error": "RuntimeError", "where": where(StreamDesk.finish, "raise self.reply")}
    assert "visitor text here" not in err and "Traceback" not in err
    assert len(desk.released) == 1


def test_a_head_that_fails_to_render_gets_the_whole_fallback_page(monkeypatch, capsys):
    """The head is rendered before the response starts: a failure there is the catch-all's, the whole fallback page
    in one body with 200, and the Pending's slot goes back once."""
    desk = StreamDesk(served(out(kept=(C1,))))
    app = create_app(desk=desk)

    def broken(kind, text, waiting):
        raise ValueError(f"visitor text here: {text}")  # the raise the log names
    monkeypatch.setattr(pages, "answer_head", broken)
    capsys.readouterr()
    chunks = asgi_post(app, ASKED)
    assert chunks == [pages.fallback_page(app_units()).encode()]
    err = capsys.readouterr().err
    (rec,) = [json.loads(x) for x in err.splitlines()]
    assert rec == {"route": "ask", "method": "POST", "status": 200, "outcome": "error", "served_from": "none",
                   "ms": rec["ms"], "error": "ValueError", "where": where(broken, "the raise the log names")}
    assert "visitor text here" not in err and QUESTION not in err and "Traceback" not in err
    assert len(desk.released) == 1


def test_a_stream_closed_early_releases_the_slot():
    desk = StreamDesk(served(out(kept=(C1,))))
    chunks = asgi_post(create_app(desk=desk), ASKED, disconnect_after_first=True)  # nothing raises out of it
    assert pages.WAIT_QUESTION in unescape(chunks[0].decode())
    assert not any(b'class="answer"' in x for x in chunks)  # the visitor left before the answer went out
    assert len(desk.released) == 1


def test_outcomes_decided_before_the_model_are_served_whole():
    cases = [(served(out("handoff", kind="handoff"), served_from="none"), 200, None),
             (served(out("fallback", reason="limited"), status=429, served_from="none", retry_after=37), 429, "37"),
             (served(out("fallback", reason="busy"), status=503, served_from="none"), 503, None)]
    for reply, status, retry_after in cases:
        c, desk = client_for(reply)
        chunks = [x for x in asgi_post(c.app, ASKED) if x]
        assert len(chunks) == 1 and pages.WAIT_QUESTION not in unescape(chunks[0].decode()), status
        r = c.post("/ask", data={"q": QUESTION})
        assert (r.status_code, r.headers.get("retry-after")) == (status, retry_after)
        assert r.content == chunks[0]
        assert [x[0] for x in desk.calls] == [QUESTION, QUESTION]


def test_a_streamed_jd_never_shows_the_posting(real):
    extract = {"result": json.dumps([{"text": x.text, "required": x.required, "kind": x.kind} for x in REQS]),
               "usage": {"input_tokens": 1000, "output_tokens": 200}}
    jd_claims = (claim(GOOD["text"], type="match", need="Python", req="r01"),
                 claim("Mike moved the nightly reports to SQL.", units=("u3",), type="match", need="SQL", req="r02"))
    c, _ = real(FakeRunner(answer=answer_reply(jd_claims), extract=extract))
    chunks = asgi_post(c.app, urlencode({"q": POSTING}).encode())
    assert len(chunks) >= 2 and pages.WAIT_JD in unescape(chunks[0].decode())
    html = b"".join(chunks).decode()
    assert not any(line in html for line in POSTING.splitlines()) and "Invented posting line" not in html
    p = Page(html)
    assert pages.JD_LABEL in p.lines and p.items("ul") == [x.text for x in REQS]


def test_a_repeat_question_is_served_whole_from_the_cache(real):
    runner = FakeRunner(answer=answer_reply())
    c, _ = real(runner)
    first, second = asgi_post(c.app, ASKED), asgi_post(c.app, ASKED)
    assert len(first) >= 2 and len(second) == 1
    assert pages.WAIT_QUESTION in unescape(first[0].decode())
    assert pages.WAIT_QUESTION not in unescape(second[0].decode())
    assert len(runner.calls) == 1
    assert Page(b"".join(first).decode()).items("ol") == Page(second[0].decode()).items("ol") == [
        f"{GOOD['text']} [u1]"]
