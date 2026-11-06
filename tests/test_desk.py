"""The desk: routing, the per-address limit, the server-side previous turn, the answer cache, busy slots, metered
model calls and the visitor log. Every unit, posting, question and client key here is invented, and no test calls
a real model: the runners are fakes and the spend ledger is a temp file."""
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from answer_fakes import EXTRACTED, GOOD, JD, SKILLS, UNITS, FakeLadder, FakeRunner, cfg, claim, structured
from fakes import FakeEmbedder
from pytest import approx

from askmike.answer.answerer import Answerer, Outcome
from askmike.answer.config import MAX_INPUT_CHARS, MAX_TOKENS
from askmike.answer.evidence import Claim
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.settings import load_answer_settings
from askmike.llm.api_runner import RunnerError
from askmike.llm.prices import cost_usd, load_prices, worst_case_usd
from askmike.retrieval.index import build_index
from askmike.retrieval.requirements import build_extract_prompt
from askmike.service import desk as desk_mod
from askmike.service.budget import STALE_S, Budget
from askmike.service.cache import AnswerCache
from askmike.service.config import ServiceConfig
from askmike.service.desk import Desk, DeskRefused, build_desk, metered
from askmike.service.limits import Buckets, Slots
from askmike.service.visits import Visits

SETTINGS = load_answer_settings()
Q_MODEL, JD_MODEL = SETTINGS.model["question"], SETTINGS.model["jd"]
IDS = {"git_sha": "g1", "corpus_sha": "c1", "index_sha": "i1", "template_sha": ANSWER_TEMPLATE_SHA, "gate_sha": "x1"}
QUESTION = "Has Mike sped up nightly jobs?"
OTHER = "Has Mike moved reports to SQL?"
FOLLOW_UP = "How did he measure that?"
HANDOFF = "What is his phone number?"
JD_CLAIMS = [claim(GOOD["text"], type="match", need="Python", req="r01"),
             claim("Mike moved the nightly reports to SQL.", units=("u3",), type="match", need="SQL", req="r02")]
USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
A, B = "client-a", "client-b"  # rate-limit keys, as the app passes client_key(address)
GATE = {"patterns": ["zzq[0-9]+x"], "companies": ["Quillfeather Labs"], "honesty_allow": [], "built_from": {}}


def answer_reply(claims=(GOOD,), usage=None):
    r = structured(claims=claims)
    r["usage"] = dict(usage or USAGE)
    return r


def extract_reply(result=None, usage=None):
    return {"result": json.dumps(EXTRACTED) if result is None else result, "usage": dict(usage or USAGE)}


def make_desk(tmp_path, runner, *, month=15.0, day=1.5, asks=20, fresh=60, slots=2):
    state = tmp_path / "state"
    budget = Budget(state / "budget.db", month, day)
    answerer = Answerer(FakeLadder(), UNITS, SKILLS, cfg(), metered(runner, budget, MAX_TOKENS), SETTINGS.model,
                        SETTINGS.effort)
    return Desk(answerer, budget, AnswerCache(state / "cache.db"), Visits(state / "visits.db"),
                Buckets(asks, 3600.0), Buckets(fresh, 3600.0), Slots(slots), SETTINGS, IDS)


def rows(tmp_path):
    """The spend ledger's rows: (status, worst, usd)."""
    conn = sqlite3.connect(tmp_path / "state" / "budget.db")
    try:
        return conn.execute("SELECT status, worst, usd FROM spend ORDER BY id").fetchall()
    finally:
        conn.close()


def worst(model, prompt):
    return worst_case_usd(load_prices(model), len(prompt), MAX_TOKENS)


# ---- the eight plan tests ----------------------------------------------------------------------------------------

def test_handoff_empty_too_long_make_no_call_and_are_logged(tmp_path):
    """They take no limit and no budget: the ask bucket is closed, no slot is free and both caps are zero."""
    runner = FakeRunner(answer=answer_reply())
    desk = make_desk(tmp_path, runner, month=0.0, day=0.0, asks=0, fresh=0, slots=0)
    cases = [("", "empty"), ("  \n ", "empty"), ("x" * (MAX_INPUT_CHARS + 1), "too_long"), (HANDOFF, "handoff")]
    served = [desk.ask(text, None, A) for text, _ in cases]
    for s, (_, want) in zip(served, cases):
        o = s.outcome
        assert (s.status, s.served_from, s.retry_after, o.outcome, o.kind, o.kept) == (200, "none", 0, want, want, ())
    assert runner.calls == [] and rows(tmp_path) == []
    logged = desk.visits.export()
    assert [(r["id"], r["outcome"], r["served_from"]) for r in logged] == [
        (s.visit_id, want, "none") for s, (_, want) in zip(served, cases)]
    assert desk.ask(QUESTION, None, A).status == 429  # the address's bucket was closed all along

    # and they never use up an address's asks
    desk = make_desk(tmp_path / "open", FakeRunner(answer=answer_reply()), asks=1)
    for _ in range(3):
        desk.ask(HANDOFF, None, A)
    assert desk.ask(QUESTION, None, A).outcome.outcome == "answer"


def test_cap_reached_after_extraction_gives_budget_fallback_and_settles_extraction(tmp_path):
    """Review Focus 3. The cap covers the extraction's worst case and no more: the extraction is settled at its
    cost, the answer call is never reserved or made, and the visit is logged as fallback / budget."""
    runner = FakeRunner(answer=answer_reply(JD_CLAIMS), extract=extract_reply())
    cap = worst(JD_MODEL, build_extract_prompt(JD))
    desk = make_desk(tmp_path, runner, month=cap, day=cap)
    s = desk.ask(JD, None, A)
    o = s.outcome
    assert (s.status, s.served_from, o.outcome, o.fallback_reason, o.kind, o.model, o.kept) == (
        200, "model", "fallback", "budget", "jd", JD_MODEL, ())
    assert [(c["model"], c["schema"]) for c in runner.calls] == [(JD_MODEL, None)]  # the extraction alone
    assert rows(tmp_path) == [("settled", approx(cap), approx(cost_usd(load_prices(JD_MODEL), USAGE)))]
    row = desk.visits.export()[0]
    assert (row["id"], row["kind"], row["outcome"], row["fallback_reason"], row["served_from"]) == (
        s.visit_id, "jd", "fallback", "budget", "model")

    # a cap that cannot cover the extraction either: no call at all, and still the budget fallback
    runner = FakeRunner(answer=answer_reply(JD_CLAIMS), extract=extract_reply())
    s = make_desk(tmp_path / "none", runner, month=cap / 2, day=cap / 2).ask(JD, None, A)
    assert (s.outcome.outcome, s.outcome.fallback_reason) == ("fallback", "budget") and runner.calls == []
    assert rows(tmp_path / "none") == []


def test_timeout_books_worst_case_and_concurrent_asks_never_overshoot_the_cap(tmp_path):
    for kind in ("timeout", "connection"):  # no reply came back, but the call may have been billed
        runner = FakeRunner(answer=RunnerError(kind, "no reply"))
        s = make_desk(tmp_path / kind, runner).ask(QUESTION, None, A)
        assert (s.status, s.outcome.outcome, s.outcome.fallback_reason) == (200, "fallback", "runner_error")
        w = worst(Q_MODEL, runner.calls[0]["prompt"])
        assert rows(tmp_path / kind) == [("settled", approx(w), approx(w))]

    # Every runner call is held open, so reservations pile up at their worst case. Only as many calls as the cap
    # covers may start; every other ask is refused before the model is called.
    w = worst(Q_MODEL, runner.calls[0]["prompt"])  # every ask of QUESTION renders the same prompt
    cap, n = w * 2.5, 12
    gate, cond, state, calls = threading.Event(), threading.Condition(), {"entered": 0, "done": 0}, []

    def held(prompt, model, schema=None, effort=None):
        with cond:
            calls.append(prompt)
            state["entered"] += 1
            cond.notify_all()
        assert gate.wait(30)
        return answer_reply(usage={"input_tokens": 100, "output_tokens": 100})

    desk = make_desk(tmp_path / "many", held, month=cap, day=cap, asks=n, fresh=n, slots=n)
    out = []

    def go():
        got = desk.ask(QUESTION, None, A)
        with cond:
            out.append(got)
            state["done"] += 1
            cond.notify_all()

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    with cond:
        settled = cond.wait_for(lambda: state["entered"] + state["done"] >= n, timeout=30)
    try:
        assert settled, "threads neither reached the runner nor returned"
        assert len(calls) == int(cap // w) == 2
        assert [s.outcome.fallback_reason for s in out] == ["budget"] * (n - 2)
    finally:
        gate.set()
        for t in threads:
            t.join(30)
    assert len(out) == n and desk.budget.spent()["month"] <= cap
    assert sum(s.outcome.outcome == "answer" for s in out) == 2


def test_rate_limited_address_gets_limited_fallback_429(tmp_path):
    """Review Focus 5. Past its asks an address gets the fallback with 429 and a Retry-After, ahead of the cache
    and the model. Another address, and inputs that make no call, are not held back. A limited ask is not logged:
    a burst past the limit never reaches the private log."""
    runner = FakeRunner(answer=answer_reply())
    desk = make_desk(tmp_path, runner, asks=2)
    assert [desk.ask(QUESTION, None, A).served_from for _ in range(2)] == ["model", "cache"]
    s = desk.ask(QUESTION, None, A)
    o = s.outcome
    assert (s.status, s.served_from, o.outcome, o.fallback_reason, o.kind, o.model, o.kept) == (
        429, "none", "fallback", "limited", "question", "", ())
    assert isinstance(s.retry_after, int) and 1 <= s.retry_after <= 3600
    assert len(runner.calls) == 1
    assert desk.ask(QUESTION, None, B).status == 200
    assert desk.ask(HANDOFF, None, A).status == 200
    logged = desk.visits.export()
    assert [r["served_from"] for r in logged] == ["model", "cache", "cache", "none"]
    assert s.visit_id not in {r["id"] for r in logged}


def test_cached_answer_served_when_budget_reached(tmp_path):
    big = {"input_tokens": 200_000, "output_tokens": 20_000}  # this one call costs more than the whole cap
    runner = FakeRunner(answer=answer_reply(usage=big))
    desk = make_desk(tmp_path, runner, month=0.01, day=0.01)
    first = desk.ask(QUESTION, None, A)
    assert (first.outcome.outcome, first.served_from) == ("answer", "model")
    assert desk.health()["budget"] == "reached"

    again = desk.ask("  Has Mike   sped up nightly jobs? ", None, A)
    o = again.outcome
    assert (again.status, again.served_from, o.outcome, o.state, o.kind, o.model) == (
        200, "cache", "answer", "answered", "question", Q_MODEL)
    assert o.kept == first.outcome.kept and o.template_sha == ANSWER_TEMPLATE_SHA and o.fallback_reason == ""

    fresh = desk.ask(OTHER, None, A)
    assert (fresh.status, fresh.served_from, fresh.outcome.outcome, fresh.outcome.fallback_reason) == (
        200, "model", "fallback", "budget")
    assert len(runner.calls) == 1 and len(rows(tmp_path)) == 1
    assert [(r["outcome"], r["served_from"]) for r in desk.visits.export()] == [
        ("answer", "model"), ("answer", "cache"), ("fallback", "model")]


def test_a_cached_jd_answer_keeps_what_the_page_and_a_follow_up_need(tmp_path):
    """The payload holds the outcome, state, kept claims, requirements, kind, model and template sha: the page
    renders a hit from it, and a follow-up to a hit is built from its requirement lines, never the posting."""
    runner = FakeRunner(answer=answer_reply(JD_CLAIMS), extract=extract_reply())
    desk = make_desk(tmp_path, runner)
    first = desk.ask(JD, None, A)
    assert first.outcome.outcome == "answer" and len(runner.calls) == 2
    conn = sqlite3.connect(tmp_path / "state" / "cache.db")
    (stored,) = [json.loads(p) for (p,) in conn.execute("SELECT payload FROM cache")]
    conn.close()
    assert set(stored) == {"outcome", "state", "kept", "requirements", "kind", "model", "template_sha"}
    assert stored["requirements"][0] == {"id": "input-r01", "text": "Python batch pipelines", "required": True,
                                         "kind": "skill"}
    assert stored["kept"][0] == {"type": "match", "need": "Python", "req": "r01", "text": GOOD["text"],
                                 "units": ["u1"]}

    hit = desk.ask(JD, None, B)
    o = hit.outcome
    assert hit.served_from == "cache" and len(runner.calls) == 2
    assert (o.kept, o.requirements, o.kind, o.model, o.state) == (
        first.outcome.kept, first.outcome.requirements, "jd", JD_MODEL, "answered")
    row = desk.visits.export()[-1]
    assert row["turn_text"] == "Python batch pipelines\nSQL for reporting\nload testing" and "Gadgetron" not in (
        row["turn_text"])
    assert desk.visits.previous(hit.visit_id).kept == first.outcome.kept


def test_followup_uses_the_server_side_previous_turn(tmp_path):
    runner = FakeRunner(answer=answer_reply())
    desk = make_desk(tmp_path, runner)
    first = desk.ask(QUESTION, None, A)
    follow = desk.ask(FOLLOW_UP, first.visit_id, A)
    assert follow.outcome.outcome == "answer" and follow.served_from == "model" and len(runner.calls) == 2
    prompt = runner.calls[1]["prompt"]
    assert f"<previous_input>\n{QUESTION}" in prompt and f"{GOOD['text']} [u1]" in prompt
    assert desk.visits.export()[1]["prev_id"] == first.visit_id

    # an unknown id is ignored: the same words are a first turn, with no previous block
    alone = desk.ask(FOLLOW_UP, desk.visits.new_id(), A)
    assert alone.served_from == "model" and len(runner.calls) == 3
    assert "<previous_input>\n" not in runner.calls[2]["prompt"]  # the template names the tag; no block
    # a malformed id reads the same as no id, and is not logged as a previous turn
    junk = desk.ask(FOLLOW_UP, "x' OR 1=1 --", A)
    assert junk.served_from == "cache" and desk.visits.export()[-1]["prev_id"] is None
    # the follow-up again, after the same earlier answer, comes from the cache
    assert desk.ask(FOLLOW_UP, first.visit_id, A).served_from == "cache" and len(runner.calls) == 3

    # the same earlier words with a different earlier answer is a different follow-up: the cache misses
    other = Claim("direct", "", "", "Mike ran 77 load tests on the queue.", ("u2",))
    vid = desk.visits.new_id()
    desk.visits.record(vid, "question", QUESTION, None, Outcome(
        "answer", "answered", (other,), (), "", "p", ANSWER_TEMPLATE_SHA, None, (), {}, 1, "question", Q_MODEL),
        "model")
    assert desk.ask(FOLLOW_UP, vid, A).served_from == "model" and len(runner.calls) == 4
    assert "Mike ran 77 load tests on the queue. [u2]" in runner.calls[3]["prompt"]


def test_unparseable_extraction_gets_one_real_metered_retry(tmp_path):
    """No ledgered_runner sits in front of the live runner, so M1's parse retry is a second real call, reserved
    and settled like the first."""
    runner = FakeRunner(answer=answer_reply(JD_CLAIMS), extract=extract_reply("Here are the requirements."))
    s = make_desk(tmp_path, runner).ask(JD, None, A)
    assert (s.outcome.outcome, s.outcome.fallback_reason, s.outcome.kind) == ("fallback", "extract", "jd")
    assert len(runner.calls) == 2 and all(c["schema"] is None for c in runner.calls)
    assert runner.calls[0]["prompt"] == runner.calls[1]["prompt"]
    w, cost = worst(JD_MODEL, runner.calls[0]["prompt"]), cost_usd(load_prices(JD_MODEL), USAGE)
    assert rows(tmp_path) == [("settled", approx(w), approx(cost))] * 2


def test_health_has_no_amounts(tmp_path):
    desk = make_desk(tmp_path, FakeRunner(answer=answer_reply()))
    desk.ask(QUESTION, None, A)  # some spend on the ledger
    h = desk.health()
    assert set(h) == {"ok", "git_sha", "corpus_sha", "index_sha", "template_sha", "models", "budget", "rss_mb"}
    assert h["ok"] is True and h["budget"] == "ok" and h["models"] == SETTINGS.model and h["rss_mb"] > 0
    assert {k: h[k] for k in ("git_sha", "corpus_sha", "index_sha", "template_sha")} == {
        k: IDS[k] for k in ("git_sha", "corpus_sha", "index_sha", "template_sha")}
    numbers = [v for v in h.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert numbers == [h["rss_mb"]]  # no cap, spend or headroom figure
    text = json.dumps(h).lower()
    assert "usd" not in text and "spent" not in text and "cap" not in text


# ---- the visitor log's own limits ----------------------------------------------------------------------------------

def test_an_address_writes_at_most_sixty_rows_an_hour_and_still_gets_its_pages(tmp_path):
    """Inputs that make no call take no ask limit, so the log has its own per-address bucket: past it the visitor
    still gets the page, but no row is written. Another address still logs."""
    runner = FakeRunner(answer=answer_reply())
    desk = make_desk(tmp_path, runner)
    served = [desk.ask(HANDOFF, None, A) for _ in range(61)]
    assert all((s.status, s.outcome.outcome) == (200, "handoff") for s in served)
    logged = desk.visits.export()
    assert len(logged) == 60 and served[60].visit_id not in {r["id"] for r in logged}
    # the 62nd visit is a real answer: served, and still not logged
    s = desk.ask(QUESTION, None, A)
    assert (s.status, s.outcome.outcome, s.served_from) == (200, "answer", "model") and len(runner.calls) == 1
    assert len(desk.visits.export()) == 60
    b = desk.ask(HANDOFF, None, B)
    assert len(desk.visits.export()) == 61 and desk.visits.export()[-1]["id"] == b.visit_id


def test_a_too_long_row_stores_the_first_two_thousand_characters_and_the_length(tmp_path):
    text = ("Invented posting line. " * 1400)[:30_000]  # over MAX_INPUT_CHARS
    desk = make_desk(tmp_path, FakeRunner(answer=answer_reply()))
    assert desk.ask(text, None, A).outcome.outcome == "too_long"
    stored = desk.visits.export()[0]["input"]
    assert len(stored) <= 2100 and stored.startswith(text[:2000])
    assert stored.endswith("[truncated from 30000 characters]") and stored[2000:].startswith(" …")


# ---- busy slots, a crash mid-call, and the metering rules ---------------------------------------------------------

def test_busy_when_no_slot_or_no_fresh_call_is_left(tmp_path):
    for name, kw in (("slots", {"slots": 0}), ("fresh", {"fresh": 0})):
        runner = FakeRunner(answer=answer_reply())
        desk = make_desk(tmp_path / name, runner, **kw)
        s = desk.ask(QUESTION, None, A)
        o = s.outcome
        assert (s.status, s.served_from, o.outcome, o.fallback_reason, o.kind, o.kept) == (
            503, "none", "fallback", "busy", "question", ())
        assert runner.calls == [] and rows(tmp_path / name) == []
        assert desk.visits.export()[0]["fallback_reason"] == "busy"

    # a refused fresh call gives its slot back; a cached answer needs neither
    desk = make_desk(tmp_path / "back", FakeRunner(answer=answer_reply()), slots=1, fresh=1)
    assert desk.ask(QUESTION, None, A).served_from == "model"
    assert desk.ask(OTHER, None, A).status == 503
    assert desk.slots.try_acquire()
    assert desk.ask(QUESTION, None, A).served_from == "cache"  # while the only slot is held
    desk.slots.release()


def test_a_crash_between_reserve_and_settle_is_booked_at_worst_case_once(tmp_path):
    """A process killed during the model call leaves an open reservation: it counts at worst case at once, and a
    restart after the stale window books it at worst case, once."""
    runner = FakeRunner(answer=SystemExit("killed"))
    desk = make_desk(tmp_path, runner, slots=1)
    with pytest.raises(SystemExit):
        desk.ask(QUESTION, None, A)
    w = worst(Q_MODEL, runner.calls[0]["prompt"])
    assert rows(tmp_path) == [("open", approx(w), 0.0)]
    assert desk.budget.spent()["month"] == approx(w, abs=1e-6)
    assert desk.slots.try_acquire()  # the slot came back
    for _ in range(2):
        Budget(tmp_path / "state" / "budget.db", 15.0, 1.5, clock=lambda: time.time() + STALE_S + 1)
        assert rows(tmp_path) == [("settled", approx(w), approx(w))]


BILLED = {"input_tokens": 1000, "output_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
ZERO = dict.fromkeys(BILLED, 0)
RULES = [
    ("reply", {"result": "{}", "usage": BILLED}, "cost"),
    ("reply-empty-usage", {"result": "{}", "usage": {}}, "worst"),
    ("reply-no-usage", {"result": "{}"}, "worst"),
    ("reply-zero-usage", {"result": "{}", "usage": ZERO}, "worst"),
    ("truncated-with-usage", RunnerError("truncated", "max_tokens", BILLED), "cost"),
    ("refusal-zero-usage", RunnerError("refusal", "declined", ZERO), "worst"),
    ("timeout", RunnerError("timeout", "no reply"), "worst"),
    ("connection", RunnerError("connection", "reset"), "worst"),
    ("overloaded", RunnerError("overloaded", "try later"), "released"),
    ("bad_request", RunnerError("bad_request", "bad"), "released"),
    ("other-error", ValueError("a bug in the runner"), "released"),
]


@pytest.mark.parametrize("result, booked", [r[1:] for r in RULES], ids=[r[0] for r in RULES])
def test_metered_reserves_the_worst_case_and_settles_by_the_rules(tmp_path, result, booked):
    calls = []

    def runner(prompt, model, schema=None, effort=None):
        calls.append((model, schema, effort))
        if isinstance(result, BaseException):
            raise result
        return result

    run = metered(runner, Budget(tmp_path / "state" / "budget.db", 15.0, 1.5), MAX_TOKENS)
    prompt, schema = "p" * 3000, {"type": "object"}
    if isinstance(result, BaseException):
        with pytest.raises(type(result)) as e:
            run(prompt, JD_MODEL, schema=schema, effort="medium")
        assert e.value is result
    else:
        assert run(prompt, JD_MODEL, schema=schema, effort="medium") is result
    assert calls == [(JD_MODEL, schema, "medium")]
    prices = load_prices(JD_MODEL)
    w = worst_case_usd(prices, len(prompt), MAX_TOKENS)
    status, usd = {"cost": ("settled", cost_usd(prices, BILLED)), "worst": ("settled", w),
                   "released": ("released", 0.0)}[booked]
    assert rows(tmp_path) == [(status, approx(w), approx(usd))]


def test_metered_prices_each_model_and_refuses_past_the_cap_without_calling(tmp_path):
    calls = []

    def m1_runner(prompt, model):  # M1's two-argument runner: a call with no schema and no effort reaches it as is
        calls.append(model)
        return {"result": "[]", "usage": BILLED}

    prompt = "p" * 1000
    cap = worst(Q_MODEL, prompt) * 1.5  # one Haiku call fits; a Sonnet call's worst case does not
    run = metered(m1_runner, Budget(tmp_path / "state" / "budget.db", cap, cap), MAX_TOKENS)
    assert run(prompt, Q_MODEL)["usage"] == BILLED
    with pytest.raises(RunnerError) as e:
        run(prompt, JD_MODEL)
    assert e.value.kind == "budget" and e.value.usage == {} and calls == [Q_MODEL]
    assert rows(tmp_path) == [("settled", approx(worst(Q_MODEL, prompt)), approx(cost_usd(load_prices(Q_MODEL),
                                                                                          BILLED)))]


def test_a_locked_ledger_at_settle_leaves_the_reservation_open_and_returns_the_reply(tmp_path):
    budget = Budget(tmp_path / "state" / "budget.db", 15.0, 1.5)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    budget.settle = budget.release = locked
    reply = {"result": "{}", "usage": BILLED}
    assert metered(lambda prompt, model: reply, budget, MAX_TOKENS)("p", Q_MODEL) is reply
    with pytest.raises(RunnerError):
        metered(FakeRunner(answer=RunnerError("overloaded", "x")), budget, MAX_TOKENS)("p", Q_MODEL, schema={})
    assert [r[0] for r in rows(tmp_path)] == ["open", "open"]  # counted at worst case until reconciled


# ---- build_desk: the real wiring ---------------------------------------------------------------------------------

def service_config(tmp_path, monkeypatch) -> ServiceConfig:
    """An invented corpus with its index (FakeEmbedder) and a gate bundle; desk.CORPUS points at the corpus."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "units.jsonl").write_text("".join(json.dumps(u) + "\n" for u in UNITS.values()), encoding="utf-8")
    (corpus / "skills.json").write_text(json.dumps(SKILLS), encoding="utf-8")
    build_index(corpus, tmp_path / "index.db", FakeEmbedder(), cache_path=tmp_path / "emb.db")
    gate = tmp_path / "gate_config.json"
    gate.write_text(json.dumps(GATE), encoding="utf-8")
    monkeypatch.setattr(desk_mod, "CORPUS", corpus)
    return ServiceConfig(month_cap_usd=15.0, day_cap_usd=1.5, state_dir=tmp_path / "state",
                         index_path=tmp_path / "index.db", gate_config=gate, ask_per_hour=20, fresh_per_hour=60,
                         slots=2, api_timeout_s=90.0, git_sha="abc123")


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_desk_wires_the_real_index_and_answers_from_a_request_thread(tmp_path, monkeypatch, capsys):
    config = service_config(tmp_path, monkeypatch)
    runner = FakeRunner(answer=answer_reply())
    desk = build_desk(config, embedder=FakeEmbedder(), runner=runner)
    out = []
    for _ in range(2):  # the app answers on worker threads, never the one that built the desk
        t = threading.Thread(target=lambda: out.append(desk.ask(OTHER if out else QUESTION, None, A)))
        t.start()
        t.join(30)
    assert [(s.outcome.outcome, s.served_from) for s in out] == [("answer", "model")] * 2
    assert [(c["model"], c["effort"]) for c in runner.calls] == [(Q_MODEL, SETTINGS.effort)] * 2
    assert desk.ids == {"git_sha": "abc123", "corpus_sha": sha256(tmp_path / "corpus" / "units.jsonl"),
                        "index_sha": sha256(tmp_path / "index.db"), "template_sha": ANSWER_TEMPLATE_SHA,
                        "gate_sha": sha256(tmp_path / "gate_config.json")}
    assert {"budget.db", "cache.db", "visits.db"} <= {p.name for p in config.state_dir.iterdir()}
    assert [r[0] for r in rows(tmp_path)] == ["settled", "settled"]  # each call went through the ledger
    assert (desk.asks.per_window, desk.fresh.per_window) == (20, 60)
    assert desk.budget.month_cap == 15.0 and desk.budget.day_cap == 1.5
    seen = capsys.readouterr()
    assert "zzq" not in seen.out + seen.err  # the gate config is never printed


def test_build_desk_refuses_without_its_gate_config_or_a_fresh_index(tmp_path, monkeypatch, capsys):
    config = service_config(tmp_path, monkeypatch)
    bad_bundle = tmp_path / "bad.json"
    bad_bundle.write_text(json.dumps(dict(GATE, patterns=["zzq[0-9"])), encoding="utf-8")
    for bad, match in ((replace(config, gate_config=None), "ASK_GATE_CONFIG"),
                       (replace(config, gate_config=tmp_path / "nope.json"), "gate config"),
                       (replace(config, gate_config=bad_bundle), "pattern #0"),
                       (replace(config, index_path=tmp_path / "no-index.db"), "index is missing")):
        with pytest.raises(DeskRefused, match=match) as e:
            build_desk(bad, embedder=FakeEmbedder(), runner=FakeRunner())
        assert "zzq" not in str(e.value)  # never a pattern
    # an index built from another corpus, or with another embedder
    units = tmp_path / "corpus" / "units.jsonl"
    units.write_text(units.read_text(encoding="utf-8") + json.dumps(dict(UNITS["u1"], id="u9")) + "\n",
                     encoding="utf-8")
    with pytest.raises(DeskRefused, match="stale"):
        build_desk(config, embedder=FakeEmbedder(), runner=FakeRunner())
    build_index(tmp_path / "corpus", tmp_path / "index.db", FakeEmbedder(), cache_path=tmp_path / "emb.db")

    class OtherEmbedder(FakeEmbedder):
        name = "other-embedder"
    with pytest.raises(DeskRefused, match="stale"):
        build_desk(config, embedder=OtherEmbedder(), runner=FakeRunner())
    assert not config.state_dir.exists()  # refused before any state was made
    seen = capsys.readouterr()
    assert "zzq" not in seen.out + seen.err
    assert build_desk(config, embedder=FakeEmbedder(), runner=FakeRunner()).ids["git_sha"] == "abc123"
