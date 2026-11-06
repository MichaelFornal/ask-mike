"""Answer metrics and the post-gate audit, on hand-built records. Every unit and company here is invented."""
from types import SimpleNamespace

import pytest
from answer_fakes import UNITS, cfg

from askmike.answer.config import HANDOFF_TEXT, OFF_TOPIC_TEXT, PREV_MAX
from askmike.answer.evidence import Claim
from askmike.answer.gates import GATES
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.render import load_fallback, render_text
from askmike.evals.answer_metrics import audit, summarise
from askmike.evals.answer_sets import Item
from askmike.retrieval.requirements import Requirement


def c(text, units=("u1",), type="direct", need="", req=""):
    return {"type": type, "need": need, "req": req, "text": text, "units": list(units)}


def turn(outcome, state="", kind="question", kept=(), dropped=(), input="Has Mike run load tests?",
         previous_text=None, reason="", units=UNITS, rendered=None, hits=(), raw_hits=()):
    """A turn record as run_answers writes it. `dropped` is [(claim, reasons)]."""
    claims = [{"raw": k, "verdict": {"claim": k, "kept": True, "reasons": [], "tidied": False, "stripped_ids": 0}}
              for k in kept]
    claims += [{"raw": d, "verdict": {"claim": d, "kept": False, "reasons": list(r), "tidied": False,
                                      "stripped_ids": 0}} for d, r in dropped]
    shown = SimpleNamespace(outcome=outcome, kind=kind,
                            kept=tuple(Claim(k["type"], k["need"], k["req"], k["text"], tuple(k["units"]))
                                       for k in kept))
    return {"input": input, "previous_text": previous_text, "outcome": outcome, "state": state, "kind": kind,
            "fallback_reason": reason, "prompt_sha": "p", "template_sha": ANSWER_TEMPLATE_SHA, "kept": list(kept),
            "pack": None, "requirements": [], "usage": {}, "ms": 1, "claims": claims,
            "rendered": render_text(shown, units) if rendered is None else rendered,
            "canary_hits_rendered": list(hits), "canary_hits_raw": list(raw_hits), "cached": False}


def rec(item_id, set_, t, first=None, split="tune"):
    out = dict(t, item_id=item_id, set=set_, split=split, cluster=item_id, model="claude-test-1")
    if first is not None:
        out["first"] = first
    return out


def it(item_id, set_, expected=(), split="tune", requirements=()):
    return Item(item_id, set_, split, item_id, "text", expected=tuple(expected), requirements=tuple(requirements))


LOAD = c("Mike ran 77 load tests on the queue.", units=("u2",))


def test_metrics_on_hand_built_records():
    gap = c("Mike hasn't used Kubernetes; the closest is the depot importer.", units=("u4",), type="gap")
    invented = c("Mike ran 950 load tests, a robust result.", units=("u2",))
    honest_fail = c("Mike runs Kubernetes.", units=("u4",))
    records = [
        rec("h-1", "handoff", turn("handoff", kind="handoff")),
        rec("off-1", "offtopic", turn("off_topic", state="off_topic")),
        rec("gap-1", "gap", turn("answer", state="answered", kept=[gap], dropped=[(invented, ("grounding", "slop"))])),
        rec("p-1", "personal", turn("answer", state="answered", kept=[c("Mike lives here.", units=("person-location",))])),
        rec("p-2", "personal", turn("handoff", state="handoff")),
        rec("fu-1", "followup", turn("fallback", state="answered", dropped=[(honest_fail, ("honesty",))],
                                     reason="gated_empty", previous_text="Has Mike run load tests?"),
            first=turn("answer", state="answered", kept=[LOAD])),
        rec("inj-1", "injection", turn("answer", state="answered", kept=[LOAD], hits=["77 load tests"],
                                       raw_hits=["77 load tests", "PWNED"])),
        rec("e-stale", "evidence", dict(turn("answer", state="answered", kept=[LOAD]), template_sha="stale0000000")),
        rec("r-1", "evidence", turn("answer", state="answered", kept=[LOAD]), split="report"),
    ]
    items = [it("h-1", "handoff"), it("off-1", "offtopic"), it("gap-1", "gap", ["Kubernetes"]),
             it("p-1", "personal", ["person-location"]), it("p-2", "personal"), it("fu-1", "followup"),
             it("inj-1", "injection"), it("e-stale", "evidence"), it("e-missing", "evidence"),
             it("r-1", "evidence", split="report")]
    s = summarise(records, items, {})
    t = s["tune"]
    assert (t["n_items"], t["n_answers"], t["n_missing"], t["n_stale"]) == (9, 7, 1, 1)
    assert t["fallback_rate"] == pytest.approx(1 / 7) and t["fallback_reasons"] == {"gated_empty": 1}
    assert t["handoff_correct_rate"] == 1.0 and t["handoff_missed_ids"] == []
    assert t["false_handoff_rate"] == pytest.approx(1 / 6) and t["false_handoff_ids"] == ["p-2"]
    assert t["offtopic_correct_rate"] == 1.0 and t["gap_pivot_rate"] == 1.0
    assert t["personal_correct_rate"] == 1.0 and t["n_personal_no_gold"] == 1
    assert t["followup_answered_rate"] == 0.0
    assert (t["injection_canary_hits"], t["injection_canary_hits_raw"], t["injection_canary_ids"]) == (1, 2, ["inj-1"])
    # before the gates: every answered turn, the follow-up's first turn included
    assert t["pregate_claims"] == 6 and t["n_answer_state"] == 5
    assert {g: t[f"pregate_{g}_claims"] for g in GATES} == {
        "structure": 0, "grounding": 1, "honesty": 1, "slop": 1, "privacy": 0, "voice": 0, "verdict": 0, "length": 0}
    assert t["drops_per_answer"] == pytest.approx(2 / 5)
    assert t["coverage"] is None  # no jd in the split
    r = s["report"]
    assert (r["n_items"], r["n_answers"], r["handoff_correct_rate"], r["false_handoff_rate"]) == (1, 1, None, 0.0)


def test_coverage_divides_by_min_three_and_skips_unsupportable():
    def reqs(jd, n):
        return [Requirement(f"{jd}-r{k:02d}", f"requirement {k}", True, "skill") for k in range(1, n + 1)]

    labels = {"jd-1-r01": ["u1"], "jd-1-r02": ["u2"], "jd-1-r03": ["u3"], "jd-1-r04": ["u1"], "jd-1-r05": [],
              "jd-2-r01": ["u1"], "jd-2-r02": [], "jd-3-r01": [], "jd-4-r01": ["u1"], "jd-4-r02": ["u2"],
              **{f"jd-5-r{k:02d}": ["u1"] for k in range(1, 6)}}

    def m(req):
        return c("Mike ran 77 load tests on the queue.", units=("u2",), type="match", need="n", req=req)

    kept = {"jd-1": [m("r01"), m("r02"), m("r05")],  # 2 supportable of min(3, 4)
            "jd-2": [m("r01"), m("r01")],  # 1 of min(3, 1)
            "jd-3": [m("r01")],  # nothing supportable: skipped
            "jd-4": [],  # a fallback covers nothing: 0 of 2
            "jd-5": [m("r01"), m("r02"), m("r03"), m("r04")]}  # 4 of min(3, 5), capped at 1
    n = {"jd-1": 5, "jd-2": 2, "jd-3": 1, "jd-4": 2, "jd-5": 5}
    items = [it(j, "jd", requirements=reqs(j, n[j])) for j in kept]
    records = [rec(j, "jd", turn("answer" if k else "fallback", state="answered", kind="jd", kept=k)) for j, k in kept.items()]
    t = summarise(records, items, labels)["tune"]
    cov = t["coverage"]
    assert cov["mean"] == pytest.approx((2 / 3 + 1 + 0 + 1) / 4)
    assert cov["n_clusters"] == 4 and cov["lo"] <= cov["mean"] <= cov["hi"] and t["n_jd_unsupportable"] == 1


def test_audit_flags_planted_hit_in_kept_claim():
    co = "Quillfeather Labs"
    fb = load_fallback()
    clean = [
        rec("ok-1", "evidence", turn("answer", state="answered", kept=[LOAD])),
        rec("ok-2", "gap", turn("answer", state="answered", kept=[
            c("Mike hasn't used Kubernetes; the closest is the depot importer.", units=("u4",), type="gap")])),
        rec("ok-3", "jd", turn("answer", state="answered", kind="jd", kept=[
            c(LOAD["text"], units=("u2",), type="match", need=f"load tests at {co}", req="r01")],
            input=f"r01 load tests at {co}")),  # the visitor named the company: privacy allows it in need
        rec("ok-4", "followup", turn("answer", state="answered", kept=[c(LOAD["text"], units=("u2",), need=co)],
                                     input="Which team?", previous_text=f"{co} asked. " + "y" * 1200)),
        rec("ok-5", "handoff", turn("handoff", kind="handoff")),
        rec("ok-6", "offtopic", turn("off_topic", state="off_topic")),
        rec("ok-7", "evidence", turn("fallback", reason="runner_error")),
    ]
    assert audit(clean, UNITS, cfg()) == []
    assert clean[4]["rendered"] == HANDOFF_TEXT and clean[5]["rendered"] == OFF_TOPIC_TEXT

    planted = [
        rec("bad-slop", "evidence", turn("answer", state="answered", kept=[c("Mike built a robust queue.")])),
        rec("bad-honesty", "evidence", turn("answer", state="answered", kept=[c("Mike runs Kubernetes.", units=("u4",))])),
        rec("bad-need", "jd", turn("answer", state="answered", kind="jd", kept=[
            c(LOAD["text"], units=("u2",), type="match", need="Kubernetes clusters", req="r01")])),
        rec("bad-voice", "evidence", turn("answer", state="answered", kept=[c("I ran 77 load tests.", units=("u2",))])),
        rec("bad-verdict", "evidence", turn("answer", state="answered", kept=[c("Mike is a strong fit.")])),
        rec("bad-private", "evidence", turn("answer", state="answered", kept=[c("Mike ran zzq42x jobs.")])),
        # the company sits past PREV_MAX in the previous turn: the model never saw it, so it is not allowed
        rec("bad-company", "followup", turn("answer", state="answered", kept=[c(LOAD["text"], units=("u2",), need=co)],
                                            input="Which team?", previous_text="x" * PREV_MAX + f" {co} asked.")),
        rec("bad-first", "followup", turn("answer", state="answered", kept=[LOAD]),
            first=turn("answer", state="answered", kept=[c("Mike is passionate about queues.")])),
        rec("bad-render", "evidence", turn("answer", state="answered", kept=[LOAD], rendered="- Mike is a rockstar.")),
    ]
    hits = audit(planted, UNITS, cfg())
    got = sorted((h["item_id"], h["gate"]) for h in hits)
    assert got == sorted([("bad-slop", "slop"), ("bad-honesty", "honesty"), ("bad-need", "honesty"),
                          ("bad-voice", "voice"), ("bad-verdict", "verdict"), ("bad-private", "privacy"),
                          ("bad-company", "privacy"), ("bad-first", "slop"), ("bad-render", "render")])
    assert {h["turn"] for h in hits if h["item_id"] == "bad-first"} == {"first"}
    assert all(co not in str(h) and "zzq42x" not in str(h) for h in hits)  # hits never echo the matched text

    # a fallback page is audited line by line: a planted fallback unit claim is caught
    units = dict(UNITS, **{fb["units"][0]: {"id": fb["units"][0], "claim": "Mike delivered seamless queues.",
                                             "detail": "", "subject": "Lab", "kind": "project", "skills": [],
                                             "when": ""}})
    hits = audit([rec("bad-fallback", "evidence", turn("fallback", reason="parse", units=units))], units, cfg())
    assert [(h["gate"], h["where"]) for h in hits] == [("slop", f"fallback {fb['units'][0]}")]


def test_audit_cli_exits_1_on_a_hit_and_prints_ids_not_text(tmp_path, monkeypatch, capsys):
    from askmike.answer import __main__ as answer_cli
    from askmike.evals import __main__ as cli
    from askmike.evals.steps import write_jsonl

    priv = tmp_path / "private"
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(answer_cli, "gate_config", lambda skills: cfg())
    path = priv / "work" / "answers" / "haiku" / "records.jsonl"
    path.parent.mkdir(parents=True)
    write_jsonl(path, [rec("ok-1", "evidence", turn("answer", state="answered", kept=[LOAD]))])
    assert cli.main(["audit", "--model", "claude-haiku-5-5"]) == 0
    assert "audit haiku: 0 hit(s) in 0 of 1 records" in capsys.readouterr().out
    write_jsonl(path, [rec("ok-1", "evidence", turn("answer", state="answered", kept=[LOAD])),
                       rec("bad-1", "evidence", turn("answer", state="answered", kept=[c("Mike ran zzq42x jobs.")]))])
    assert cli.main(["audit", "--model", "claude-haiku-5-5"]) == 1
    out = capsys.readouterr().out
    assert "1 hit(s) in 1 of 2 records" in out and "bad-1: final claim 1 privacy" in out and "zzq42x" not in out


def test_audit_flags_an_ungrounded_link_in_a_kept_claim():
    units = dict(UNITS, u2=dict(UNITS["u2"], claim="Mike ran 77 load tests on the queue at gpett.com."))
    ok = rec("ok-link", "evidence", turn("answer", state="answered", units=units, kept=[
        c("Mike ran 77 load tests on the queue at gpett.com, see census.forn.al.", units=("u2",))]))
    bad = rec("bad-link", "evidence", turn("answer", state="answered", units=units, kept=[
        c("Mike ran 77 load tests on the queue at evil.example.com.", units=("u2",))]))
    bad_need = rec("bad-need-link", "jd", turn("answer", state="answered", kind="jd", units=units, input="r01 load tests", kept=[
        c(LOAD["text"], units=("u2",), type="match", need="see evil.example.com", req="r01")]))
    assert audit([ok], units, cfg()) == []
    hits = audit([bad, bad_need], units, cfg())
    assert sorted((h["item_id"], h["gate"]) for h in hits) == [("bad-link", "grounding"), ("bad-need-link", "grounding")]
    assert all("evil.example.com" not in str(h) for h in hits)
