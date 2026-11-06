"""The calibration harness against fakes: sampling, the spend cap, the summary and the stop rule."""
import json

import pytest
from answer_fakes import EXTRACTED, JD, MODEL, UNITS, FakeLadder, FakeRunner, claim, make, structured

from askmike.evals import calibrate as cal
from askmike.evals.answer_sets import Item
from askmike.evals.split import split_of
from askmike.llm.ledger import MemoryLedger
from askmike.llm.prices import Prices, cost_usd
from askmike.retrieval.requirements import Requirement

P = Prices("m", 1.0, 5.0, 1.25, 0.1, "src", "2026-11-04")


def item(i, set_, text="Tell me about Mike's SQL work.", **kw):
    return Item(i, set_, split_of(i), i, text, **kw)


def pool():
    out = []
    for set_, n in (("jd", 200), ("evidence", 150), ("gap", 60), ("personal", 60), ("handoff", 60), ("offtopic", 5)):
        out += [item(f"{set_}{k}", set_) for k in range(n)]
    return out


def test_calibration_sample_tune_only_and_stable():
    s = cal.sample(pool(), 40)
    assert len(s) == 40 and all(i.split == "tune" for i in s)
    counts = {k: sum(1 for i in s if i.set == k) for k in cal.QUOTAS}
    assert counts == cal.QUOTAS
    assert [i.item_id for i in cal.sample(list(reversed(pool())), 40)] == [i.item_id for i in s]
    assert len(cal.sample(pool(), 10)) == 10


def api_reply(inp=50, out=9, claims=None):
    r = structured(claims=claims or [claim("Mike moved the nightly reports to SQL.", units=("u3",))])
    r["usage"] = {"input_tokens": inp, "output_tokens": out}
    return r


def test_run_stops_before_exceeding_max_usd():
    extract = {"result": json.dumps(EXTRACTED), "usage": {"input_tokens": 10, "output_tokens": 5}}
    items = [item(f"q{k}", "evidence", f"Tell me about Mike's SQL work, part {k}.") for k in range(5)]
    runner = FakeRunner(answer=api_reply(2000, 100), extract=extract)
    a = make(runner)
    a.model = "m"
    one = cal.worst_case_usd(P, len("Tell me about Mike's SQL work.") + cal.PACK_CHARS, 100)
    r = cal.run(items, a, MemoryLedger(), max_usd=0.022, prices=P, max_tokens=100)
    assert r["stopped"] and 0 < len(r["records"]) < 5 and r["remaining"] == 5 - len(r["records"])
    assert r["spent"] <= 0.022 and one < 0.022 and len(r["records"]) == 3 and len(runner.calls) == len(r["records"])
    rec = r["records"][0]
    assert rec["calls"][0]["usage"]["input_tokens"] == 2000 and rec["cost_usd"] == pytest.approx(cost_usd(P, {"input_tokens": 2000, "output_tokens": 100}))
    # a tiny cap makes no call at all
    runner2 = FakeRunner(answer=api_reply())
    r2 = cal.run(items, make(runner2), MemoryLedger(), max_usd=0.0001, prices=P, max_tokens=100)
    assert r2["records"] == [] and runner2.calls == [] and r2["stopped"]


def test_run_jd_extracts_live_and_rerun_spends_nothing():
    extract = {"result": json.dumps(EXTRACTED), "usage": {"input_tokens": 10, "output_tokens": 5}}
    runner = FakeRunner(answer=api_reply(), extract=extract)
    led = MemoryLedger()
    m1 = (Requirement("input-r01", "Python batch pipelines", True, "skill"),)  # M1's, not the extracted three
    items = [item("jd1", "jd", JD, requirements=m1), item("h1", "handoff", "What salary does Mike want?")]
    a = make(runner)
    a.model = "m"
    r = cal.run(items, a, led, 5.0, P, 100)
    jd, ho = r["records"]
    assert [c["kind"] for c in jd["calls"]] == ["extract", "answer"] and ho["calls"] == [] and len(runner.calls) == 2
    assert jd["ms_total"] == sum(c["ms"] for c in jd["calls"]) and jd["extracted_requirements"] == 3
    assert jd["calls"][0]["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert jd["cost_usd"] == pytest.approx(cost_usd(P, {"input_tokens": 60, "output_tokens": 14}))  # both calls
    # the answer prompt is the one the CLI would send for M1's requirements, whatever the API extracted
    cli_runner = FakeRunner(answer=api_reply())
    cli_rec = make(cli_runner).ask(JD, requirements=list(m1))
    assert jd["prompt_sha"] == cli_rec.prompt_sha and [q["id"] for q in jd["requirements"]] == ["input-r01"]
    spent = r["spent"]
    r2 = cal.run(items, a, led, 5.0, P, 100)
    assert len(runner.calls) == 2 and r2["spent"] == 0 and spent > 0 and r2["records"][0]["cached"]


def rec(iid, outcome="answer", sha="s", calls=None, reqs=(), errors=(), claims=None):
    return {"item_id": iid, "outcome": outcome, "prompt_sha": sha, "requirements": list(reqs), "errors": list(errors),
            "calls": calls if calls is not None else [call()], "claims": claims or []}


def call(inp=100, out=10, ms=500, worst=1.0):
    return {"kind": "answer", "usage": {"input_tokens": inp, "output_tokens": out}, "ms": ms, "worst_case_usd": worst}


def sup_rec(iid, text, **kw):
    r = rec(iid, **kw)
    r["claims"] = [{"raw": {"text": text, "units": ["u1"]}, "verdict": {"claim": {"text": text, "units": ["u1"]},
                                                                           "kept": True, "reasons": []}}]
    return r


def test_summarise_stop_rule_reasons():
    items = [item(f"i{k}", "evidence") for k in range(10)]
    ids = [i.item_id for i in items]
    api = [rec(i) for i in ids]
    cli = [rec(i) for i in ids]
    s = cal.summarise(items, api, cli, {}, {}, P)
    assert s["stop_rule"]["reasons"] == ["support gap unmeasured"]  # nothing judged yet
    assert s["outcome_agreement"] == 1 and s["prompt_parity"] == 1 and s["called"] == 10 and s["worst_case_ok"]
    assert s["cost_total"] == pytest.approx(10 * cost_usd(P, {"input_tokens": 100, "output_tokens": 10}))
    assert s["latency_ms_p50"] == 500 and s["tokens_in_mean"] == 100 and s["tokens_in_p95"] == 100

    # agreement
    bad = [rec(i, outcome="fallback") if k < 3 else rec(i) for k, i in enumerate(ids)]
    s = cal.summarise(items, bad, cli, {}, {}, P)
    assert s["outcome_agreement"] == 0.7 and any("agreement" in r for r in s["stop_rule"]["reasons"])
    # parity covers every called, error-free item; below 1.0 stops
    diff = [rec(i, sha="x") for i in ids]
    s = cal.summarise(items, diff, cli, {}, {}, P)
    assert s["prompt_parity"] == 0 and s["n_parity"] == 10 and any("parity" in r for r in s["stop_rule"]["reasons"])
    one_off = [rec(ids[0], sha="x"), *[rec(i) for i in ids[1:]]]
    assert cal.summarise(items, one_off, cli, {}, {}, P)["prompt_parity"] == 0.9
    uncalled = [rec(ids[0], calls=[]), *[rec(i) for i in ids[1:]]]
    s = cal.summarise(items, uncalled, cli, {}, {}, P)
    assert s["called"] == 9 and s["n_parity"] == 9
    with_err = [rec(ids[0], errors=["truncated: x"]), *[rec(i) for i in ids[1:]]]
    s = cal.summarise(items, with_err, cli, {}, {}, P)
    assert s["n_parity"] == s["called"] - 1 and "support" in s["note"]
    # worst case below an actual cost
    low = [rec(i, calls=[call(worst=1e-9)]) for i in ids]
    s = cal.summarise(items, low, cli, {}, {}, P)
    assert not s["worst_case_ok"] and any("worst-case" in r for r in s["stop_rule"]["reasons"])
    # errors counted
    err = [rec(ids[0], errors=["truncated: x"]), rec(ids[1], errors=["refusal: y"]), *[rec(i) for i in ids[2:]]]
    s = cal.summarise(items, err, cli, {}, {}, P)
    assert (s["errors"], s["truncated"], s["refused"]) == (2, 1, 1)


def test_summarise_support_gap_and_go():
    from askmike.evals import support as sup
    items = [item(f"i{k}", "evidence") for k in range(6)]
    mk = lambda: [sup_rec(i.item_id, "Mike moved the nightly reports to SQL.") for i in items]
    api, cli = mk(), mk()
    text, ids = "Mike moved the nightly reports to SQL.", ("u1",)
    key_sha = sup.claim_sha(text, ids)
    def verdicts(recs, said):
        return {f"{r['item_id']}#0": {"verdict": said(r), "claim_sha": key_sha} for r in recs}
    all_sup = lambda r: "supported"
    s = cal.summarise(items, api, cli, verdicts(api, all_sup), verdicts(cli, all_sup), P)
    assert s["support_gap"] == 0 and s["stop_rule"] == {"verdict": "go", "reasons": []}
    half = lambda r: "supported" if int(r["item_id"][1:]) < 3 else "unsupported"
    s = cal.summarise(items, api, cli, verdicts(api, half), verdicts(cli, all_sup), P)
    assert s["support_gap"] == pytest.approx(-0.5) and s["stop_rule"]["verdict"] == "stop"


def test_summarise_is_incomplete_while_an_api_kept_claim_is_unjudged():
    from askmike.evals import support as sup
    items = [item(f"i{k}", "evidence") for k in range(4)]
    text = "Mike moved the nightly reports to SQL."
    api = [sup_rec(i.item_id, text) for i in items]
    cli = [sup_rec(i.item_id, text) for i in items]
    sha = sup.claim_sha(text, ("u1",))
    rows = lambda recs: {f"{r['item_id']}#0": {"verdict": "supported", "claim_sha": sha} for r in recs}
    s = cal.summarise(items, api, cli, rows(api), rows(cli), P)
    assert s["n_api_unjudged"] == 0 and s["complete"]
    partial = rows(api)
    partial.pop("i0#0")
    s = cal.summarise(items, api, cli, partial, rows(cli), P)
    assert s["n_api_unjudged"] == 1 and not s["complete"]


def test_run_keeps_the_extracted_requirement_texts():
    extract = {"result": json.dumps(EXTRACTED), "usage": {"input_tokens": 10, "output_tokens": 5}}
    m1 = (Requirement("input-r01", "Python batch pipelines", True, "skill"),)
    a = make(FakeRunner(answer=api_reply(), extract=extract))
    a.model = "m"
    r = cal.run([item("jd1", "jd", JD, requirements=m1), item("q1", "evidence")], a, MemoryLedger(), 5.0, P, 100)
    jd, q = r["records"]
    assert jd["extracted_requirements"] == 3
    assert jd["extracted_requirement_texts"] == [x["text"] for x in EXTRACTED]
    assert q["extracted_requirements"] is None and q["extracted_requirement_texts"] is None


def test_summarise_sets_the_api_extraction_against_m1s_by_count():
    m1 = lambda n: tuple(Requirement(f"r{k}", f"need {k}", True, "skill") for k in range(n))
    items = [item("j0", "jd", JD, requirements=m1(3)), item("j1", "jd", JD, requirements=m1(5)),
             item("j2", "jd", JD, requirements=m1(4)), item("q0", "evidence")]
    api = [dict(rec("j0"), extracted_requirements=3), dict(rec("j1"), extracted_requirements=2),
           rec("j2"),  # no count kept: left out, not read as zero
           dict(rec("q0"), extracted_requirements=None)]
    s = cal.summarise(items, api, [rec(i.item_id) for i in items], {}, {}, P)
    assert (s["extract_n_jd"], s["extract_n_jd_differ"]) == (2, 1)
    assert (s["extract_reqs_api_mean"], s["extract_reqs_cli_mean"]) == (2.5, 4.0)
    s = cal.summarise(items[3:], api[3:], [rec("q0")], {}, {}, P)
    assert (s["extract_n_jd"], s["extract_reqs_api_mean"], s["extract_n_jd_differ"]) == (0, None, 0)


def test_cost_total_is_the_booked_spend():
    """A call that may have been billed with no usage was booked at its worst case: cost_total counts what was
    booked, while the per-answer costs price the usage."""
    items = [item(f"i{k}", "evidence") for k in range(3)]
    u = {"input_tokens": 100, "output_tokens": 10}
    booked = [dict(call(), booked_usd=cost_usd(P, u)), dict(call(), booked_usd=cost_usd(P, u)),
              dict(call(), usage={}, booked_usd=0.25)]  # a timeout booked at its worst case
    api = [rec(i.item_id, calls=[c]) for i, c in zip(items, booked)]
    s = cal.summarise(items, api, [rec(i.item_id) for i in items], {}, {}, P)
    assert s["cost_total"] == pytest.approx(2 * cost_usd(P, u) + 0.25)
    assert s["cost_per_answer_mean"] == pytest.approx(2 * cost_usd(P, u) / 3)


def test_timeouts_without_usage_book_worst_case_and_stop_at_the_cap(tmp_path):
    from askmike.llm.api_runner import RunnerError
    items = [item(f"t{k}", "evidence", f"Tell me about Mike's SQL work, take {k}.") for k in range(6)]
    runner = FakeRunner(answer=RunnerError("timeout", "slow"))
    worst = cal.worst_case_usd(P, len(items[0].text) + cal.PACK_CHARS, 100)
    a = make(runner)
    a.model = "m"
    r = cal.run(items, a, MemoryLedger(), max_usd=worst * 1.5, prices=P, max_tokens=100)
    assert r["stopped"] and 1 <= len(runner.calls) < 6  # each timeout books its worst case, so the cap binds
    assert 0 < r["spent"] <= worst * 1.5 and all(c["booked_usd"] > 0 for rec in r["records"] for c in rec["calls"])
    # a bad request carried no billing risk: booked at zero
    r2 = cal.run(items[:2], make(FakeRunner(answer=RunnerError("bad_request", "no"))), MemoryLedger(), worst * 1.5, P, 100)
    assert r2["spent"] == 0
    # a success that reports no usage is booked at worst case too
    empty = structured()
    empty["usage"] = {}
    r3 = cal.run(items[:1], make(FakeRunner(answer=empty)), MemoryLedger(), worst * 1.5, P, 100)
    assert r3["spent"] > 0


def test_second_run_after_a_cap_stop_makes_no_call(tmp_path):
    items = [item(f"s{k}", "evidence", f"Tell me about Mike's SQL work, round {k}.") for k in range(6)]
    spend = tmp_path / "spend.jsonl"
    runner = FakeRunner(answer=api_reply(2000, 100))
    a = make(runner)
    a.model = "m"
    led = MemoryLedger()
    r = cal.run(items, a, led, 0.022, P, 100, spend_path=spend)
    n = len(runner.calls)
    assert r["stopped"] and r["prior"] == 0 and n == 3 and len(spend.read_text().splitlines()) == 3
    # a fresh process: new ledger (a failed call is never cached), same spend file, same cap
    runner2 = FakeRunner(answer=api_reply(2000, 100))
    b = make(runner2)
    b.model = "m"
    r2 = cal.run(items, b, MemoryLedger(), 0.022, P, 100, spend_path=spend)
    assert r2["prior"] == pytest.approx(r["spent"]) and r2["stopped"] and runner2.calls == [] and r2["records"] == []


# ---- the picked setup: each item on its own route's model ----

PQ = Prices("claude-q", 1.0, 5.0, 1.25, 0.1, "src", "2026-11-04")
PJ = Prices("claude-j", 3.0, 15.0, 3.75, 0.3, "src", "2026-11-04")
SPLIT = {"question": "claude-q", "jd": "claude-j"}
SPLIT_PRICES = {"claude-q": PQ, "claude-j": PJ}
M1_REQS = (Requirement("input-r01", "Python batch pipelines", True, "skill"),)
HANDOFF = "What salary does Mike want?"


def picked_items():
    return [item("jd1", "jd", JD, requirements=M1_REQS), item("q1", "evidence"), item("h1", "handoff", HANDOFF)]


def test_item_model_follows_the_route():
    jd, q, h = picked_items()
    assert [cal.item_model(i, SPLIT) for i in (jd, q, h)] == ["claude-j", "claude-q", "claude-q"]
    assert {cal.item_model(i, "m") for i in (jd, q, h)} == {"m"}
    assert cal.models_of(SPLIT) == ("claude-j", "claude-q") and cal.models_of("m") == ("m",)


def test_picked_run_puts_each_item_on_its_routes_model_and_prices_it_so():
    extract = {"result": json.dumps(EXTRACTED), "usage": {"input_tokens": 10, "output_tokens": 5}}
    runner = FakeRunner(answer=api_reply(), extract=extract)
    a = make(runner)
    a.model = dict(SPLIT)
    r = cal.run(picked_items(), a, MemoryLedger(), 5.0, SPLIT_PRICES, 100)
    jd, q, h = r["records"]
    # the jd extracts and answers on the jd model; the question on the question model; the handoff makes no call
    assert [(c["model"], c["schema"] is None) for c in runner.calls] == [
        ("claude-j", True), ("claude-j", False), ("claude-q", False)]
    assert (jd["model"], q["model"], h["model"]) == ("claude-j", "claude-q", "claude-q") and h["calls"] == []
    assert [c["model"] for c in jd["calls"]] == ["claude-j", "claude-j"] and q["calls"][0]["model"] == "claude-q"
    assert jd["cost_usd"] == pytest.approx(cost_usd(PJ, {"input_tokens": 60, "output_tokens": 14}))
    assert q["cost_usd"] == pytest.approx(cost_usd(PQ, {"input_tokens": 50, "output_tokens": 9}))
    assert r["spent"] == pytest.approx(jd["cost_usd"] + q["cost_usd"])
    # the answer prompt is the one the CLI sent for M1's requirements
    assert jd["prompt_sha"] == make(FakeRunner(answer=api_reply())).ask(JD, requirements=list(M1_REQS)).prompt_sha


def test_picked_cap_reserves_each_item_at_its_own_models_prices(tmp_path):
    """A question fits under the cap at the question model's prices; the jd's two calls at the jd model's prices
    do not, so the run stops before the jd and makes no jd call."""
    q, jd = item("q1", "evidence"), item("jd1", "jd", JD, requirements=M1_REQS)
    q_need = cal.worst_case_usd(PQ, len(q.text) + cal.PACK_CHARS, 100)
    jd_need = (cal.worst_case_usd(PJ, len(JD) + cal.PACK_CHARS, 100)
               + cal.worst_case_usd(PJ, len(JD) + cal.TEMPLATE_CHARS, 100))
    cap = q_need + 0.5 * jd_need
    assert cal.worst_case_usd(PQ, len(JD) + cal.PACK_CHARS, 100) * 2 < cap  # at the question prices it would fit
    extract = {"result": json.dumps(EXTRACTED), "usage": {"input_tokens": 10, "output_tokens": 5}}
    runner = FakeRunner(answer=api_reply(), extract=extract)
    a = make(runner)
    a.model = dict(SPLIT)
    spend = tmp_path / "spend.jsonl"
    r = cal.run([q, jd], a, MemoryLedger(), cap, SPLIT_PRICES, 100, spend_path=spend)
    assert r["stopped"] and [x["item_id"] for x in r["records"]] == ["q1"] and r["remaining"] == 1
    assert [c["model"] for c in runner.calls] == ["claude-q"] and len(spend.read_text().splitlines()) == 1


def _cli_rec(iid, sha, outcome="answer", text="Mike moved the nightly reports to SQL."):
    return sup_rec(iid, text, sha=sha, outcome=outcome)


def test_picked_summary_reads_each_items_own_models_cli_record_and_verdicts():
    from askmike.evals import support as sup
    jd, q, h = items = [item("j0", "jd", JD), item("q0", "evidence"), item("h0", "handoff", HANDOFF)]
    text = "Mike moved the nightly reports to SQL."
    api = [dict(_cli_rec("j0", "sj"), model="claude-j", calls=[call(), call()]),
           dict(_cli_rec("q0", "sq"), model="claude-q"),
           dict(rec("h0", outcome="handoff", calls=[]), model="claude-q")]
    # the other model's record for each item disagrees on everything, so reading it would show up
    by_j = [_cli_rec("j0", "sj"), _cli_rec("q0", "WRONG", outcome="fallback"), rec("h0", outcome="answer")]
    by_q = [_cli_rec("j0", "WRONG", outcome="fallback"), _cli_rec("q0", "sq"), rec("h0", outcome="handoff")]
    sha = sup.claim_sha(text, ("u1",))

    def said(verdicts):
        return {k: {"verdict": v, "claim_sha": sha} for k, v in verdicts.items()}
    # keys repeat across the two models' files: q0#0 is supported in the question model's, not in the jd model's
    v_j, v_q = said({"j0#0": "supported", "q0#0": "unsupported"}), said({"j0#0": "unsupported", "q0#0": "supported"})
    cli_recs, cli_sup = cal.cli_side(api, {"claude-j": (by_j, v_j), "claude-q": (by_q, v_q)})
    api_sup = said({"j0#0": "supported", "q0#0": "supported"})
    s = cal.summarise(items, api, cli_recs, api_sup, cli_sup, SPLIT_PRICES, model=SPLIT)
    assert (s["n"], s["n_compared"], s["called"], s["n_to_call"], s["complete"]) == (3, 3, 2, 2, True)
    assert s["outcome_agreement"] == 1 and s["prompt_parity"] == 1 and s["support_gap"] == 0
    assert s["stop_rule"] == {"verdict": "go", "reasons": []}
    u = {"input_tokens": 100, "output_tokens": 10}
    assert s["cost_total"] == pytest.approx(2 * cost_usd(PJ, u) + cost_usd(PQ, u))
    assert s["model"] == SPLIT and [p["model"] for p in s["prices"]] == ["claude-j", "claude-q"]

    # a cap stop leaves an item unanswered: the summary is not complete
    s = cal.summarise(items, api[:2], cli_recs, api_sup, cli_sup, SPLIT_PRICES)
    assert (s["n_compared"], s["complete"]) == (2, False)
    s = cal.summarise(items, [api[0], dict(api[1], calls=[]), api[2]], cli_recs, api_sup, cli_sup, SPLIT_PRICES)
    assert (s["called"], s["n_to_call"], s["complete"]) == (1, 2, False)


def test_summarise_files_records_its_inputs_and_goes_stale(tmp_path):
    from askmike.evals.steps import write_jsonl
    answers = tmp_path / "answers"
    items = [item("q0", "evidence"), item("h0", "handoff", HANDOFF)]
    api = [dict(_cli_rec("q0", "sq"), model="claude-q"), dict(rec("h0", outcome="handoff", calls=[]), model="claude-q")]
    for path, rows in ((answers / "calibration" / "picked" / "records.jsonl", api),
                       (answers / "q" / "records.jsonl", [_cli_rec("q0", "sq"), rec("h0", outcome="handoff")])):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(path, rows)
    s = cal.summarise_files(answers, "picked", SPLIT, items, SPLIT_PRICES, slugs={"claude-q": "q", "claude-j": "j"})
    assert s["n_compared"] == 2 and not s["complete"] and s["n_api_unjudged"] == 1  # nothing judged yet
    assert set(s["inputs"]) == {"calibration/picked/records.jsonl", "calibration/picked/support_A.jsonl",
                                "q/records.jsonl", "q/support_A.jsonl", "j/records.jsonl", "j/support_A.jsonl"}
    assert s["inputs"]["calibration/picked/support_A.jsonl"] is None  # not judged yet
    assert cal.stale_inputs(answers, s) == []
    write_jsonl(answers / "calibration" / "picked" / "support_A.jsonl", [])
    assert cal.stale_inputs(answers, s) == ["calibration/picked/support_A.jsonl"]
    write_jsonl(answers / "q" / "records.jsonl", [_cli_rec("q0", "changed")])
    assert cal.stale_inputs(answers, s) == ["calibration/picked/support_A.jsonl", "q/records.jsonl"]


# ---- the calibrate CLI on the picked setup (fakes for the index, the embedder and the API) ----

def test_calibrate_picked_refuses_without_a_key_before_any_work(monkeypatch, capsys):
    from askmike.evals import __main__ as cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ASK_PRIVATE_DIR", raising=False)
    assert cli.main(["calibrate", "--model", "picked", "--n", "40", "--max-usd", "3.00"]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


class _Conn:
    def close(self):
        pass


class _Embedder:
    name = "fake-embedder"


def _picked_cli(m2_dirs, tmp_path, monkeypatch):
    import shutil
    from answer_fakes import cfg
    from askmike.answer import __main__ as answer_cli
    from askmike.evals import __main__ as cli
    work, _ = m2_dirs
    priv = tmp_path / "private"
    shutil.copytree(work, priv / "work")
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(answer_cli, "gate_config", lambda skills: cfg())
    return cli, priv / "work" / "answers"


def test_calibrate_cli_picked_runs_each_item_on_its_routes_model(m2_dirs, tmp_path, monkeypatch, capsys):
    import shutil
    from askmike.llm import api_runner
    from askmike.retrieval import index, ladder, models
    from askmike.evals.steps import read_jsonl

    cli, answers = _picked_cli(m2_dirs, tmp_path, monkeypatch)
    out = answers / "calibration" / "picked"
    shutil.rmtree(out)
    ids = sorted(cli._corpus()[0])[:4]
    fake_ladder = FakeLadder(ids)
    fake_ladder.conn = _Conn()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-the-cli-guard")
    monkeypatch.setattr(models, "Embedder", _Embedder)
    monkeypatch.setattr(index, "open_index", lambda *a, **k: _Conn())
    monkeypatch.setattr(ladder, "Ladder", lambda *a, **k: fake_ladder)
    extract = {"result": json.dumps(EXTRACTED), "usage": {"input_tokens": 10, "output_tokens": 5}}
    runner = FakeRunner(answer=api_reply(), extract=extract)
    monkeypatch.setattr(api_runner, "make_api_runner", lambda *a, **k: runner)
    assert cli.main(["calibrate", "--model", "picked", "--n", "40", "--max-usd", "3.00"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("calibrate picked: items=") and "max_usd=3.0" in printed
    recs = read_jsonl(out / "records.jsonl")
    by_set = {(r["set"], r["model"]) for r in recs}
    assert by_set == {("jd", "claude-sonnet-5-5"), ("evidence", "claude-haiku-5-5"), ("gap", "claude-haiku-5-5"),
                      ("personal", "claude-haiku-5-5"), ("handoff", "claude-haiku-5-5")}
    for r in recs:  # a jd extracts then answers, a question answers, a handoff makes no call; all on its model
        want = {"jd": ["extract", "answer"], "handoff": []}.get(r["set"], ["answer"])
        assert [c["kind"] for c in r["calls"]] == want and {c["model"] for c in r["calls"]} <= {r["model"]}, r["set"]
    # what reached the API (the fixture's postings share answer prompts, so the ledger answers some of them)
    assert {(c["model"], c["schema"] is None) for c in runner.calls} == {
        ("claude-sonnet-5-5", True), ("claude-sonnet-5-5", False), ("claude-haiku-5-5", False)}
    assert {p.name for p in out.iterdir()} == {"records.jsonl", "ledger.jsonl", "spend.jsonl"}
    assert not (answers / "calibration" / "haiku").exists() and not (answers / "calibration" / "sonnet").exists()


def test_calibrate_cli_picked_summarise_matches_the_library_and_records_inputs(m2_dirs, tmp_path, monkeypatch,
                                                                               capsys):
    cli, answers = _picked_cli(m2_dirs, tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # --summarise needs no key
    path = answers / "calibration" / "picked" / "summary.json"
    want = json.loads(path.read_text())
    path.unlink()
    assert cli.main(["calibrate", "--model", "picked", "--summarise"]) == 0
    got = json.loads(path.read_text())
    assert got == want and got["complete"] and got["stop_rule"]["verdict"] == "go"
    assert set(got["inputs"]) >= {"calibration/picked/records.jsonl", "haiku/support_A.jsonl",
                                  "sonnet/records.jsonl"}
    assert cal.stale_inputs(answers, got) == []
    out = capsys.readouterr().out
    assert "incomplete" not in out
    # after a cap stop the CLI says the run is incomplete
    from askmike.evals.steps import read_jsonl, write_jsonl
    p = answers / "calibration" / "picked" / "records.jsonl"
    write_jsonl(p, read_jsonl(p)[:-1])
    assert cli.main(["calibrate", "--model", "picked", "--summarise"]) == 0
    assert "calibrate picked: incomplete" in capsys.readouterr().out
