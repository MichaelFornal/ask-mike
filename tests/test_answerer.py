"""The Answerer: routes, one structured-output call, gates, and a fallback for every failure.
Every unit, skill, company and job description here is invented, and no test calls a real model."""
import json
import sqlite3
import subprocess

from answer_fakes import (
    EXTRACTED, GOOD, INVENTED_NUMBER, JD, MODEL, REQS, UNITS, FakeLadder, FakeRunner, assert_fallback, claim, make,
    structured,
)

from askmike.answer import answerer as answerer_mod
from askmike.answer.config import ANSWER_EFFORT, MAX_INPUT_CHARS, MIN_KEPT
from askmike.answer.evidence import Claim, turn_from
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.schema import ANSWER_SCHEMA
from askmike.corpus import claude
from askmike.corpus.claude import run_claude
from askmike.llm.ask import prompt_sha


def test_handoff_empty_too_long_make_no_model_call():
    runner, ladder = FakeRunner(answer=structured(claims=[GOOD])), FakeLadder()
    a = make(runner, ladder)
    cases = [("", "empty"), ("   \n ", "empty"), (None, "empty"), ("x" * (MAX_INPUT_CHARS + 1), "too_long"),
             ("What's his salary?", "handoff"), ("What is his phone number?", "handoff")]
    for text, want in cases:
        o = a.ask(text)
        assert (o.outcome, o.kind, o.fallback_reason, o.state) == (want, want, "", "")
        assert o.kept == () and o.verdicts == () and o.pack is None and o.prompt_sha == ""
        assert o.usage == {} and o.template_sha == ANSWER_TEMPLATE_SHA
    assert runner.calls == [] and ladder.calls == []


def test_runner_error_timeout_and_unparseable_reply_give_fallback(monkeypatch):
    question = "Has Mike run load tests?"

    # the runner raises: an error exit, a timeout, an error reply, or something unexpected
    for exc in (RuntimeError("claude exited 1: overloaded"), RuntimeError("claude timed out after 900s"),
                OSError("pipe closed"), KeyError("result")):
        runner = FakeRunner(answer=exc)
        o = make(runner).ask(question)
        assert_fallback(o, "runner_error")
        assert o.prompt_sha == prompt_sha(runner.calls[0]["prompt"]) and o.pack is not None

    # the real `claude -p` runner, with the subprocess timing out, exiting non-zero or replying is_error
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 900)

    def exit_1(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, "", "overloaded")

    def error_reply(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"is_error": True, "result": ""}), "")

    for fake in (timeout, exit_1, error_reply):
        monkeypatch.setattr(claude.subprocess, "run", fake)
        assert_fallback(make(run_claude).ask(question), "runner_error")

    # a reply that is not a dict breaks the runner contract
    for reply in (None, ["not", "a", "dict"], "text"):
        assert_fallback(make(FakeRunner(answer=reply)).ask(question), "runner_error")

    # a refusal, a truncated reply, prose with no JSON object, a bad state, claims that are not a list
    truncated = '{"state": "answered", "claims": [{"type": "direct", "text": "Mike ran 77'
    for reply in ({"result": "I can't help with that request."}, {"result": truncated},
                  {"result": "", "structured_output": None}, {"result": "[1, 2]"},
                  structured(state="maybe"), structured(state=None),
                  {"result": "", "structured_output": {"state": "answered", "claims": "Mike ran 77 load tests."}},
                  {"result": "", "structured_output": {"state": "answered"}}):
        o = make(FakeRunner(answer=reply)).ask(question)
        assert_fallback(o, "parse")
        assert o.usage == (reply.get("usage") or {})

    # an exception while building the pack, or inside the gates
    class Broken(FakeLadder):
        def run(self, rung, query, k=10):
            raise sqlite3.OperationalError("database is locked")
    runner = FakeRunner(answer=structured(claims=[GOOD]))
    assert_fallback(make(runner, Broken()).ask(question), "runner_error")
    assert runner.calls == []

    def boom(*a, **k):
        raise ValueError("gate bug")
    monkeypatch.setattr(answerer_mod, "gate_claims", boom)
    assert_fallback(make(FakeRunner(answer=structured(claims=[GOOD]))).ask(question), "runner_error")


def test_structured_output_preferred_over_result_text():
    other = claim("Mike ran 77 load tests on the queue.", units=("u2",))
    reply = structured(claims=[GOOD])
    reply["result"] = json.dumps({"state": "answered", "claims": [other]})
    runner = FakeRunner(answer=reply)
    o = make(runner).ask("Has Mike sped up nightly jobs?")
    assert o.outcome == "answer" and o.state == "answered" and o.fallback_reason == ""
    assert [c.text for c in o.kept] == [GOOD["text"]] and o.kept[0].units == ("u1",)
    assert o.usage == {"input_tokens": 50, "output_tokens": 9} and o.kind == "question"
    call = runner.calls[0]
    assert (call["model"], call["schema"], call["effort"]) == (MODEL, ANSWER_SCHEMA, ANSWER_EFFORT)
    assert o.prompt_sha == prompt_sha(call["prompt"]) and o.template_sha == ANSWER_TEMPLATE_SHA
    assert set(o.pack.unit_ids) == set(UNITS)

    # structured_output missing or not a dict: the first JSON object in `result` is read instead
    for so in (None, "a string", ["a list"]):
        text = "Here it is: " + json.dumps({"state": "answered", "claims": [other]}) + " Done."
        o = make(FakeRunner(answer={"result": text, "structured_output": so})).ask("Has he run load tests?")
        assert o.outcome == "answer" and [c.text for c in o.kept] == [other["text"]]


def test_model_states_map_to_outcomes():
    for state, want, reason in (("handoff", "handoff", ""), ("off_topic", "off_topic", ""),
                                ("no_evidence", "fallback", "no_evidence")):
        o = make(FakeRunner(answer=structured(state=state, claims=[GOOD]))).ask("Has he run load tests?")
        assert (o.outcome, o.state, o.fallback_reason) == (want, state, reason)
        assert o.kept == () and o.verdicts == ()


def test_answered_below_min_kept_is_fallback_gated_empty():
    question = "How fast are the nightly jobs?"
    # nothing survives the gates: an invented number, a non-dict item, an unknown id
    bad = [INVENTED_NUMBER, "Mike ran 77 load tests.", claim("Mike wrote the depot importer.", units=("u-x",))]
    o = make(FakeRunner(answer=structured(claims=bad))).ask(question)
    assert_fallback(o, "gated_empty")
    assert o.state == "answered" and len(o.verdicts) == 3 and not any(v.kept for v in o.verdicts)
    assert "grounding" in o.verdicts[0].reasons and "structure" in o.verdicts[2].reasons
    assert_fallback(make(FakeRunner(answer=structured(claims=[]))).ask(question), "gated_empty")
    assert MIN_KEPT["question"] == 1
    assert make(FakeRunner(answer=structured(claims=[INVENTED_NUMBER, GOOD]))).ask(question).outcome == "answer"

    # a jd needs MIN_KEPT["jd"] (2) kept claims: one is a fallback, and its kept claim is not shown
    m1 = claim(GOOD["text"], type="match", need="Python batch pipelines", req="r01")
    m2 = claim("Mike moved the nightly reports to SQL.", units=("u3",), type="match", need="SQL", req="r02")
    m3 = claim(INVENTED_NUMBER["text"], type="match", need="speed", req="r03")
    o = make(FakeRunner(answer=structured(claims=[m1, m3]))).ask(JD, requirements=REQS)
    assert_fallback(o, "gated_empty")
    assert [v.kept for v in o.verdicts] == [True, False] and o.requirements == tuple(REQS)
    o = make(FakeRunner(answer=structured(claims=[m1, m2, m3]))).ask(JD, requirements=REQS)
    assert o.outcome == "answer" and [c.req for c in o.kept] == ["r01", "r02"] and o.kind == "jd"


def test_jd_extract_failure_is_fallback_extract():
    answer = structured(claims=[claim(GOOD["text"], type="match", need="Python", req="r01"),
                                claim("Mike moved the nightly reports to SQL.", units=("u3",), type="match",
                                      need="SQL", req="r02")])
    # extraction fails (error, unparseable twice, zero valid requirements): no answer call is made
    for extract, n_calls in ((RuntimeError("claude exited 1: overloaded"), 1), ({"result": "no json"}, 2),
                             ({"result": "[]"}, 1), ({"result": json.dumps([{"text": "x", "kind": "vibes"}])}, 1)):
        runner = FakeRunner(answer=answer, extract=extract)
        o = make(runner).ask(JD)
        assert_fallback(o, "extract")
        assert o.kind == "jd" and o.requirements == () and o.prompt_sha == ""
        assert len(runner.calls) == n_calls and all(c["schema"] is None for c in runner.calls)
    # an empty requirement list given by the caller is 0 kept requirements: no call at all
    runner = FakeRunner(answer=answer, extract={"result": json.dumps(EXTRACTED)})
    assert_fallback(make(runner).ask(JD, requirements=[]), "extract")
    assert runner.calls == []

    # extraction succeeds: M1's call (no schema, no effort) on the answerer's own model, then the answer call
    runner = FakeRunner(answer=answer, extract={"result": json.dumps(EXTRACTED)})
    o = make(runner).ask(JD)
    assert o.outcome == "answer" and [r.text for r in o.requirements] == [r.text for r in REQS]
    ex, ans = runner.calls
    assert (ex["model"], ex["schema"], ex["effort"]) == (MODEL, None, None)
    assert (ans["model"], ans["schema"], ans["effort"]) == (MODEL, ANSWER_SCHEMA, ANSWER_EFFORT)
    assert "Gadgetron" not in ans["prompt"] and "r01 (required): Python batch pipelines" in ans["prompt"]

    # the follow-up Turn the Outcome builds holds the requirement lines, never the raw posting
    turn = o.turn(JD)
    assert turn == turn_from("jd", JD, o.requirements, o.kept) and "Gadgetron" not in turn.text


def test_previous_turn_ids_outside_the_corpus_are_dropped():
    prev = turn_from("question", "Has Mike run load tests?", [], [
        Claim("direct", "", "", "Mike ran 77 load tests on the queue.", ("u-forged", "u2")),
        Claim("direct", "", "", "Mike ran the forged job.", ("u-forged",)),
    ])
    runner = FakeRunner(answer=structured(claims=[claim("Mike ran 77 load tests on the queue.", units=("u2",))]))
    o = make(runner, FakeLadder(ids=("u1",))).ask("How did he measure that?", previous=prev)
    assert o.outcome == "answer"
    prompt = runner.calls[0]["prompt"]
    assert "u-forged" not in prompt and "forged job" not in prompt
    assert "Mike ran 77 load tests on the queue. [u2]" in prompt and o.pack.unit_ids[0] == "u2"


def test_ask_cli_missing_index_prints_stale_message_and_exits_1(tmp_path, monkeypatch, capsys):
    from askmike.answer import __main__ as cli
    from askmike.retrieval.index import MISSING

    monkeypatch.setattr(cli, "default_db", lambda: tmp_path / "no-index.db")
    assert cli.main(["ask", "Has Mike built RAG systems?", "--model", MODEL]) == 1
    assert MISSING in capsys.readouterr().err


def test_gate_input_is_exactly_what_the_model_saw(monkeypatch):
    from askmike.answer.answerer import gate_input
    from askmike.answer.config import PREV_MAX

    text = "Which team did he test it for?"
    near = turn_from("question", "Quillfeather Labs asked about load tests. " + "y" * 1200, [], [])
    far = turn_from("question", "x" * 1200 + " Quillfeather Labs asked about load tests.", [], [])
    assert gate_input(text, None) == text and gate_input(None, None) == ""
    assert gate_input(text, near) == near.text[:PREV_MAX] + "\n" + text
    assert "Quillfeather" not in gate_input(text, far)

    # the Answerer gates against gate_input, and the prompt shows the same slice of the previous turn
    seen = []
    real = answerer_mod.gate_claims
    monkeypatch.setattr(answerer_mod, "gate_claims", lambda *a: seen.append(a[4]) or real(*a))
    named = claim("Mike ran 77 load tests on the queue.", units=("u2",), need="Quillfeather Labs load tests")
    for prev, kept in ((near, True), (far, False)):
        runner = FakeRunner(answer=structured(claims=[named]))
        o = make(runner).ask(text, previous=prev)
        assert seen[-1] == gate_input(text, prev)
        assert ("<previous_input>\n" + prev.text[:PREV_MAX].strip() + "\n</previous_input>") in runner.calls[0]["prompt"]
        # the visitor's company is allowed only when the model saw it in the previous turn
        assert (o.outcome == "answer") is kept
        assert o.verdicts[0].kept is kept and (kept or "privacy" in o.verdicts[0].reasons)


def test_outcome_turn_is_none_for_empty_too_long_and_handoff():
    a = make(FakeRunner(answer=structured(claims=[GOOD])))
    for text in ("", None, "x" * (MAX_INPUT_CHARS + 1), "What is his phone number?"):
        assert a.ask(text).turn(text) is None
    o = make(FakeRunner(answer=structured(state="handoff"))).ask("Has he run load tests?")
    assert o.outcome == "handoff" and o.turn("Has he run load tests?") is None
    o = a.ask("Has he sped up nightly jobs?")
    assert o.turn("Has he sped up nightly jobs?") == turn_from("question", "Has he sped up nightly jobs?", [], o.kept)
    o = make(FakeRunner(answer=RuntimeError("down"))).ask("Has he run load tests?")
    assert o.outcome == "fallback" and o.turn("Has he run load tests?") == turn_from(
        "question", "Has he run load tests?", [], ())


# ---- Mike's split pick: one model per route ----

SPLIT = {"question": "claude-q-1", "jd": "claude-j-1"}
JD_ANSWER = [claim(GOOD["text"], type="match", need="Python", req="r01"),
             claim("Mike moved the nightly reports to SQL.", units=("u3",), type="match", need="SQL", req="r02")]


def test_split_pick_asks_and_extracts_a_jd_with_the_jd_model():
    from answer_fakes import SKILLS, cfg
    from askmike.answer.answerer import Answerer

    runner = FakeRunner(answer=structured(claims=JD_ANSWER), extract={"result": json.dumps(EXTRACTED)})
    a = Answerer(FakeLadder(), UNITS, SKILLS, cfg(), runner, dict(SPLIT))
    o = a.ask(JD)
    ex, ans = runner.calls
    assert (ex["model"], ex["schema"]) == ("claude-j-1", None)  # M1's extraction call, on the jd model
    assert (ans["model"], ans["schema"]) == ("claude-j-1", ANSWER_SCHEMA)
    assert (o.outcome, o.kind, o.model) == ("answer", "jd", "claude-j-1")
    # the eval path hands in M1's requirements: no extraction, the answer still on the jd model
    runner.calls.clear()
    o = a.ask(JD, requirements=REQS)
    assert [c["model"] for c in runner.calls] == ["claude-j-1"] and o.model == "claude-j-1"
    assert a.model_for("jd") == "claude-j-1" and a.model_for("question") == "claude-q-1"


def test_split_pick_asks_a_question_with_the_question_model():
    from answer_fakes import SKILLS, cfg
    from askmike.answer.answerer import Answerer

    runner = FakeRunner(answer=structured(claims=[GOOD]), extract={"result": json.dumps(EXTRACTED)})
    a = Answerer(FakeLadder(), UNITS, SKILLS, cfg(), runner, dict(SPLIT))
    o = a.ask("Has Mike sped up nightly jobs?")
    assert [(c["model"], c["schema"]) for c in runner.calls] == [("claude-q-1", ANSWER_SCHEMA)]
    assert (o.outcome, o.kind, o.model) == ("answer", "question", "claude-q-1")
    # a failed call still names the model it went to
    o = Answerer(FakeLadder(), UNITS, SKILLS, cfg(), FakeRunner(answer=RuntimeError("down")), dict(SPLIT)).ask(
        "Has Mike sped up nightly jobs?")
    assert (o.outcome, o.model) == ("fallback", "claude-q-1")


def test_split_pick_handoff_empty_and_too_long_make_no_call():
    from answer_fakes import SKILLS, cfg
    from askmike.answer.answerer import Answerer

    runner, ladder = FakeRunner(answer=structured(claims=[GOOD])), FakeLadder()
    a = Answerer(ladder, UNITS, SKILLS, cfg(), runner, dict(SPLIT))
    for text, want in (("", "empty"), (None, "empty"), ("x" * (MAX_INPUT_CHARS + 1), "too_long"),
                       ("What's his salary?", "handoff")):
        o = a.ask(text)
        assert (o.outcome, o.kind, o.model) == (want, want, "")
    assert runner.calls == [] and ladder.calls == []


def test_one_model_string_keeps_todays_behaviour():
    runner = FakeRunner(answer=structured(claims=JD_ANSWER), extract={"result": json.dumps(EXTRACTED)})
    a = make(runner)
    assert a.model == MODEL and a.model_for("jd") == MODEL and a.model_for("question") == MODEL
    assert a.ask(JD).model == MODEL and a.ask("Has Mike sped up nightly jobs?").model == MODEL
    assert {c["model"] for c in runner.calls} == {MODEL}


def test_ask_cli_without_model_uses_the_settings_map(monkeypatch):
    from askmike.answer import __main__ as cli

    seen = []
    monkeypatch.setattr(cli, "_ask", lambda text, model: seen.append(model) or 0)
    assert cli.main(["ask", "Has Mike built RAG systems?"]) == 0
    assert seen == [{"question": "claude-haiku-5-5", "jd": "claude-sonnet-5-5"}]
    assert cli.main(["ask", "Has Mike built RAG systems?", "--model", MODEL]) == 0 and seen[-1] == MODEL


def test_a_budget_refusal_gives_the_budget_fallback():
    """The spend ledger refuses a call by raising RunnerError("budget") before it is made: at the answer call, or at
    a jd's extraction (whose error M1 turns into a string), the visitor gets the budget fallback, not a runner error
    or an extraction failure. Any other extraction error stays `extract`."""
    from askmike.answer.answerer import FALLBACK_REASONS
    from askmike.llm.api_runner import RunnerError

    assert {"budget", "busy", "limited"} <= set(FALLBACK_REASONS)
    refused = RunnerError("budget", "the model budget is reached")
    o = make(FakeRunner(answer=refused)).ask("Has Mike sped up nightly jobs?")
    assert_fallback(o, "budget")
    assert o.kind == "question" and o.model == MODEL and o.pack is not None

    runner = FakeRunner(answer=structured(claims=[GOOD]), extract=refused)
    o = make(runner).ask(JD)
    assert_fallback(o, "budget")
    assert o.kind == "jd" and len(runner.calls) == 1 and o.requirements == ()

    for other in (RunnerError("overloaded", "try later"), RuntimeError("budget: not a runner error kind")):
        assert_fallback(make(FakeRunner(answer=structured(claims=[GOOD]), extract=other)).ask(JD), "extract")
    assert_fallback(make(FakeRunner(answer=RunnerError("overloaded", "x"))).ask("Has he run load tests?"),
                    "runner_error")
