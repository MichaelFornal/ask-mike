"""M2 facts: the exact key list, the {value, display, cmd, inputs} shape, the refusals, the merge and --check.

Pattern from tests/test_facts.py (M1), adapted 2026-11-04 for Task 11a. Every record and verdict is invented
(tests/m2_fixture.py).
"""
import json
import re
import shutil
from types import SimpleNamespace

import pytest
from answer_fakes import cfg
from m2_fixture import FIX_UNITS, HAIKU, SONNET, items_of, write_picked_calibration

from askmike.answer.evidence import Claim
from askmike.answer.render import render_text
from askmike.evals import calibrate as cal
from askmike.evals.steps import read_jsonl, write_jsonl
from askmike.facts import build_m2 as B

MODELS = ("haiku", "sonnet")
GATES = ("honesty", "slop", "privacy", "voice", "verdict", "grounding")
RATES = ("handoff_correct_rate", "false_handoff_rate", "offtopic_correct_rate", "gap_pivot_rate",
         "personal_correct_rate", "followup_answered_rate")
SETS = ("jd", "evidence", "personal", "handoff", "gap", "injection", "offtopic", "followup")
CAL = ("n", "outcome_agreement", "support_gap", "prompt_parity", "cost_per_answer_mean", "cost_per_answer_p95",
       "cost_total", "tokens_in_mean", "tokens_out_mean", "latency_ms_p50", "latency_ms_p95", "worst_case_ok",
       "verdict",
       # what the cap's ledger booked, every call of every run (a rerun's replaced calls included)
       "spend_total",
       # the final review's extraction comparison: API extraction against M1's requirements, by count
       "extract_reqs_api_mean", "extract_reqs_cli_mean", "extract_n_jd_differ", "extract_n_jd")
DRIFT = ("m2_drift_reqs_old", "m2_drift_reqs_new", "m2_drift_matched_share")
JOB_SEARCH = re.compile(r"applied|top50|application|company", re.I)


def brief_keys() -> set:
    """The plan's exact key list with the controller's one change (the picked model splits by route), spelled
    out here rather than imported from the code under test. No drift keys: the jd model is M1's extractor."""
    keys = set()
    for m in MODELS:
        p = f"m2_{m}_"
        keys |= {p + k for k in ("n_answers", "pregate_claims", "drops_per_answer", "partial_rate", "audit_hits",
                                 "injection_canary_hits", "injection_canary_hits_raw", "grounding_false_drop_rate",
                                 "n_grounding_only")}
        keys |= {f"{p}pregate_{g}_claims" for g in GATES}
        keys |= {f"{p}{r}{s}" for r in ("fallback_rate", "support_rate", "coverage") for s in ("", "_lo", "_hi")}
        keys |= {p + r for r in RATES}
    keys |= {"m2_judge_planted_n", "m2_judge_planted_pos_acc", "m2_judge_planted_neg_acc", "m2_kappa_support_AB",
             "m2_n_pairs_support_AB"}
    keys |= {f"m2_n_items_{s}" for s in SETS}
    keys |= {"m2_picked_model_question", "m2_picked_model_jd", "m2_answer_effort", "m2_template_sha"}
    keys |= {f"m2_cal_{k}" for k in CAL}
    return keys


def test_build_m2_keys_exact(m2_facts):
    assert set(m2_facts) == brief_keys()
    assert "m2_picked_model" not in m2_facts and not any(k.startswith("m2_drift_") for k in m2_facts)


def test_every_fact_has_value_display_cmd_and_inputs(m2_facts):
    for key, f in m2_facts.items():
        assert set(f) == {"value", "display", "cmd", "inputs"}, key
        assert isinstance(f["display"], str) and f["display"], key
        assert f["cmd"].startswith("uv run python -m askmike.evals "), key
        if isinstance(f["value"], float):
            assert round(f["value"], 4) == f["value"], key
        assert f["inputs"], key
        for name, sha in f["inputs"].items():
            assert not name.startswith(("/", "~")) and ".." not in name and "Users" not in name, (key, name)
            assert re.fullmatch(r"[0-9a-f]{64}", sha), (key, name)


def test_no_job_search_facts_m2(m2_facts):
    for key, f in m2_facts.items():
        assert not JOB_SEARCH.search(key), key
        for text in (f["display"], f["cmd"], *f["inputs"]):
            assert not JOB_SEARCH.search(str(text)), (key, text)


def test_values_on_the_fixture(m2_facts, m2_dirs):
    v = {k: f["value"] for k, f in m2_facts.items()}
    d = {k: f["display"] for k, f in m2_facts.items()}
    assert (v["m2_picked_model_question"], v["m2_picked_model_jd"]) == (HAIKU, SONNET)
    assert (d["m2_picked_model_question"], d["m2_picked_model_jd"]) == ("Haiku", "Sonnet")
    assert (v["m2_answer_effort"], v["m2_template_sha"]) == ("medium", "8f2412145427")
    assert {s: v[f"m2_n_items_{s}"] for s in SETS} == {"jd": 12, "evidence": 14, "personal": 8, "handoff": 10,
                                                       "gap": 10, "injection": 4, "offtopic": 8, "followup": 8}
    work, _ = m2_dirs
    report = [i for i in items_of(work) if i.split == "report"]
    for m in MODELS:
        assert v[f"m2_{m}_n_answers"] == len(report)
        assert v[f"m2_{m}_audit_hits"] == 0 and v[f"m2_{m}_injection_canary_hits"] == 0
        assert v[f"m2_{m}_handoff_correct_rate"] == 1.0 and v[f"m2_{m}_false_handoff_rate"] == 0.0
        assert v[f"m2_{m}_personal_correct_rate"] == 1.0 and v[f"m2_{m}_gap_pivot_rate"] == 1.0
        assert v[f"m2_{m}_pregate_claims"] > v[f"m2_{m}_pregate_grounding_claims"] > 0
        assert v[f"m2_{m}_pregate_slop_claims"] == 0
        assert v[f"m2_{m}_grounding_false_drop_rate"] == 1.0  # judge A calls every grounding-only drop supported
        assert 0 < v[f"m2_{m}_support_rate"] < 1 and v[f"m2_{m}_support_rate_lo"] <= v[f"m2_{m}_support_rate"]
        assert v[f"m2_{m}_fallback_rate_lo"] <= v[f"m2_{m}_fallback_rate"] <= v[f"m2_{m}_fallback_rate_hi"]
    assert v["m2_sonnet_fallback_rate"] > v["m2_haiku_fallback_rate"]  # Sonnet falls back on one more posting
    raw = sum(1 for i in report if i.item_id == "inj-0000")
    assert v["m2_haiku_injection_canary_hits_raw"] == raw
    assert (v["m2_judge_planted_n"], v["m2_judge_planted_pos_acc"], v["m2_judge_planted_neg_acc"]) == (120, 1.0, 0.9833)
    assert v["m2_kappa_support_AB"] is not None and v["m2_n_pairs_support_AB"] > 0
    assert (v["m2_cal_verdict"], v["m2_cal_outcome_agreement"], v["m2_cal_prompt_parity"]) == ("go", 1.0, 1.0)
    assert v["m2_cal_support_gap"] == 0.0 and v["m2_cal_worst_case_ok"] is True and d["m2_cal_worst_case_ok"] == "yes"
    assert d["m2_cal_cost_total"].startswith("$") and d["m2_cal_spend_total"].startswith("$")
    assert v["m2_cal_spend_total"] == pytest.approx(v["m2_cal_cost_total"])  # one run, so the two agree
    # the fixture's API extraction found one requirement fewer than M1 on the first sampled posting
    jds = [i for i in cal.sample(items_of(work), 40) if i.set == "jd"]
    assert v["m2_cal_extract_n_jd"] == len(jds) >= 2 and v["m2_cal_extract_n_jd_differ"] == 1
    assert v["m2_cal_extract_reqs_cli_mean"] == 3.0
    assert v["m2_cal_extract_reqs_api_mean"] == round(3.0 - 1 / len(jds), 4)
    assert d["m2_cal_extract_n_jd"] == str(len(jds)) and d["m2_cal_extract_reqs_cli_mean"] == "3.00"


def test_display_formats():
    assert B.display("m2_picked_model_question", HAIKU) == "Haiku"
    assert B.display("m2_picked_model_jd", SONNET) == "Sonnet"
    assert B.display("m2_haiku_n_answers", 1234) == "1,234"
    assert B.display("m2_haiku_support_rate", 0.9866) == "0.99"
    assert B.display("m2_haiku_fallback_rate_lo", 0.0) == "0.00"
    assert B.display("m2_cal_support_gap", -0.0004) == "0.000"
    assert B.display("m2_cal_support_gap", -0.0125) == "-0.013"
    assert B.display("m2_cal_cost_per_answer_mean", 0.01234) == "$0.0123"
    assert B.display("m2_cal_tokens_in_mean", 4321.6) == "4,322"
    assert B.display("m2_cal_latency_ms_p50", 21966) == "21,966"
    assert B.display("m2_cal_worst_case_ok", False) == "no"
    assert B.display("m2_cal_verdict", "go") == "go"
    assert B.display("m2_template_sha", "8f2412145427") == "8f2412145427"
    assert B.display("m2_kappa_support_AB", None) == "n/a"
    assert B.display("m2_drift_matched_share", 0.82) == "82%"
    # a rate held against a bar shows the third place the bar can turn on
    for key in ("m2_cal_outcome_agreement", "m2_cal_prompt_parity", "m2_judge_planted_pos_acc",
                "m2_judge_planted_neg_acc"):
        assert B.display(key, 0.975) == "0.975", key
        assert B.display(key, 1.0) == "1.000", key
    assert B.display("m2_cal_extract_reqs_api_mean", 12.6875) == "12.69"
    assert B.display("m2_cal_extract_n_jd_differ", 6) == "6"


# ---- refusals ---------------------------------------------------------------------

def _copy(m2_dirs, tmp_path):
    work, repo = m2_dirs
    shutil.copytree(work, tmp_path / "work")
    shutil.copytree(repo, tmp_path / "repo")
    return tmp_path / "work", tmp_path / "repo"


def _edit(path, fn):
    rows = read_jsonl(path)
    fn(rows)
    write_jsonl(path, rows)


def _shown(rec, kept):
    """Give a record new kept claims and re-render it, so only the gates can object."""
    rec["kept"] = kept
    rec["rendered"] = render_text(SimpleNamespace(outcome=rec["outcome"], kind=rec["kind"], kept=tuple(
        Claim(c["type"], c["need"], c["req"], c["text"], tuple(c["units"])) for c in kept)), FIX_UNITS)


def _refused(work, repo, match):
    with pytest.raises(B.FactsRefused, match=match):
        B.build_m2(work, repo, cfg=cfg())


def test_facts_m2_refuses_mixed_template_sha_and_audit_hits(m2_dirs, tmp_path):
    work, repo = _copy(m2_dirs, tmp_path)
    path = work / "answers" / "haiku" / "records.jsonl"
    original = path.read_text()
    report_ids = {i.item_id for i in items_of(work) if i.split == "report"}

    def one_old(rows):
        next(r for r in rows if r["item_id"] in report_ids)["template_sha"] = "old000000000"
    _edit(path, one_old)
    _refused(work, repo, r"haiku: 0 missing and 1 stale report record")

    path.write_text(original)
    B.build_m2(work, repo, cfg=cfg())  # restored: no refusal

    def slop_in_tune(rows):  # a tune record: the zero-bars hold on both splits
        r = next(r for r in rows if r["split"] == "tune" and r["set"] == "evidence")
        _shown(r, [dict(r["kept"][0], text="Mike built a seamless queue.")])
    _edit(work / "answers" / "sonnet" / "records.jsonl", slop_in_tune)
    _refused(work, repo, r"sonnet: 1 audit hit")


def _canary(work):
    def go(rows):
        next(r for r in rows if r["set"] == "injection")["canary_hits_rendered"] = ["ZORP"]
    _edit(work / "answers" / "sonnet" / "records.jsonl", go)


def _missing_report_record(work):
    report = {i.item_id for i in items_of(work) if i.split == "report"}
    _edit(work / "answers" / "haiku" / "records.jsonl",
          lambda rows: rows.remove(next(r for r in rows if r["item_id"] in report)))


def _score(**change):
    def go(work):
        p = work / "answers" / "calibration" / "score_A.json"
        p.write_text(json.dumps(dict(json.loads(p.read_text()), **change)))
    return go


def _planted_changed(work):
    with open(work / "answers" / "calibration" / "planted.jsonl", "a") as f:
        f.write(json.dumps({"id": "pl-x", "kind": "positive", "text": "Invented.", "units": ["u1"],
                            "expect": "supported"}) + "\n")


def _summary(fn):
    def go(work):
        p = work / "answers" / "calibration" / "picked" / "summary.json"
        s = json.loads(p.read_text())
        fn(s)
        p.write_text(json.dumps(s))
    return go


def _no_summary(work):
    (work / "answers" / "calibration" / "picked" / "summary.json").unlink()


def _stale_summary(work):
    _edit(work / "answers" / "calibration" / "picked" / "support_A.jsonl", lambda rows: rows.pop())


def _incomplete(work):
    """A cap stop: the last sampled item has no API record, and the summary was rerun on that."""
    from askmike.answer.settings import load_answer_settings
    from askmike.evals import calibrate as cal
    from askmike.llm.prices import load_prices
    model = load_answer_settings().model
    _edit(work / "answers" / "calibration" / "picked" / "records.jsonl", lambda rows: rows.pop())
    chosen = cal.sample(items_of(work), 40)
    s = cal.summarise_files(work / "answers", "picked", model, chosen,
                            {m: load_prices(m) for m in cal.models_of(model)})
    assert s["stop_rule"]["verdict"] == "go" and not s["complete"]
    (work / "answers" / "calibration" / "picked" / "summary.json").write_text(json.dumps(s))


def _wrong_handoff(model, split="report"):
    def go(work):
        chosen = {i.item_id for i in items_of(work) if i.split == split}

        def bad(rows):
            r = next(r for r in rows if r["set"] == "handoff" and r["item_id"] in chosen)
            r["outcome"] = "off_topic"  # a wrong route, re-rendered so only the zero-bar can object
            r["kept"] = []
            _shown(r, [])
        _edit(work / "answers" / model / "records.jsonl", bad)
    return go


def _unjudged(model):
    def go(work):
        report = {i.item_id for i in items_of(work) if i.split == "report"}
        path = work / "answers" / model / "support_A.jsonl"
        write_jsonl(path, [v for v in read_jsonl(path) if v["key"].rsplit("#", 1)[0] not in report])
    return go


@pytest.mark.parametrize("break_it, match", [
    (_wrong_handoff("haiku"), r"haiku: handoff_correct_rate on the report split"),
    (_wrong_handoff("sonnet"), r"sonnet: handoff_correct_rate on the report split"),
    (_wrong_handoff("haiku", "tune"), r"haiku: handoff_correct_rate on the tune split"),
    (_wrong_handoff("sonnet", "tune"), r"sonnet: handoff_correct_rate on the tune split"),
    (_unjudged("haiku"), r"haiku: \d+ kept claim\(s\) .*unjudged"),
    (_unjudged("sonnet"), r"sonnet: \d+ kept claim\(s\) .*unjudged"),
    (_canary, r"sonnet: 1 rendered canary hit"),
    (_missing_report_record, r"haiku: 1 missing and 0 stale report record"),
    (_score(pass_=False), r"judge A did not pass its planted calibration"),
    (_score(template_sha="000000000000"), r"judge A's planted calibration used support template"),
    (_planted_changed, r"planted set changed"),
    (_no_summary, r"calibration summary .* missing"),
    (_stale_summary, r"calibration summary is stale: calibration/picked/support_A.jsonl changed"),
    (_incomplete, r"calibration is incomplete"),
    (_summary(lambda s: s.update(stop_rule={"verdict": "stop", "reasons": ["outcome agreement 0.5 below 0.8"]})),
     r"calibration verdict is stop: outcome agreement"),
    (_summary(lambda s: s.update(model={"question": HAIKU, "jd": HAIKU})), r"calibration ran on .* not the pick"),
    (_summary(lambda s: s.pop("extract_n_jd_differ")), r"calibration summary has no extract_n_jd_differ"),
    (_summary(lambda s: s.update(spend_total=None)), r"no spend ledger"),
    (_summary(lambda s: s.update(spend_total=s["cost_total"] / 2)), r"spend ledger booked .* less than"),
])
def test_facts_m2_refuses(m2_dirs, tmp_path, break_it, match):
    work, repo = _copy(m2_dirs, tmp_path)
    break_it(work)
    _refused(work, repo, match)


# ---- drift: facts only when it ran ------------------------------------------------

def test_drift_facts_when_the_jd_model_is_not_the_extractor(m2_dirs, tmp_path):
    work, repo = _copy(m2_dirs, tmp_path)
    p = repo / "askmike" / "answer" / "settings.json"
    s = json.loads(p.read_text())
    s["model"] = {"question": HAIKU, "jd": HAIKU}
    p.write_text(json.dumps(s))
    write_picked_calibration(work, s["model"])
    _refused(work, repo, r"drift step")
    out = work / "evals" / "drift" / "haiku" / "summary.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"model": HAIKU, "n_jds": 12, "n_extracted": 12, "remaining": 0,
                               "n_requirements_mean_old": 3.0, "n_requirements_mean_new": 3.25,
                               "matched_share": 0.7692}))
    facts = B.build_m2(work, repo, cfg=cfg())
    assert set(facts) == brief_keys() | set(DRIFT)
    assert [facts[k]["value"] for k in DRIFT] == [3.0, 3.25, 0.7692]
    assert facts["m2_drift_matched_share"]["display"] == "77%"
    assert facts["m2_drift_reqs_old"]["cmd"] == "uv run python -m askmike.evals drift"
    assert (facts["m2_picked_model_jd"]["display"], facts["m2_picked_model_question"]["display"]) == ("Haiku", "Haiku")
    out.write_text(json.dumps({"model": HAIKU, "n_jds": 12, "n_extracted": 11, "remaining": 1,
                               "n_requirements_mean_old": 3.0, "n_requirements_mean_new": 3.25,
                               "matched_share": 0.7692}))
    _refused(work, repo, r"drift .* incomplete")


# ---- --check and the CLI ------------------------------------------------------------

def test_check_reports_drift(m2_dirs, tmp_path):
    work, repo = m2_dirs
    path = tmp_path / "facts.json"
    assert B.check(work, repo, path, cfg=cfg()) == sorted(brief_keys())
    facts = B.build_m2(work, repo, cfg=cfg())
    path.write_text(json.dumps({**facts, "m1_other": {"value": 1}}))
    assert B.check(work, repo, path, cfg=cfg()) == []
    facts["m2_haiku_n_answers"] = dict(facts["m2_haiku_n_answers"], value=999)
    path.write_text(json.dumps(facts))
    assert B.check(work, repo, path, cfg=cfg()) == ["m2_haiku_n_answers"]


def test_cli_facts_m2_merges_keeps_m1_and_checks(m2_dirs, tmp_path, monkeypatch, capsys):
    from askmike.answer import __main__ as answer_cli
    from askmike.evals import __main__ as cli
    from askmike.facts.report_m2 import render

    work, repo = _copy(m2_dirs, tmp_path)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(work.parent))
    monkeypatch.setattr(answer_cli, "gate_config", lambda skills: cfg())
    facts_path, report_path = tmp_path / "facts.json", tmp_path / "docs" / "m2" / "REPORT.md"
    monkeypatch.setattr(cli, "REPO", repo)
    monkeypatch.setattr(cli, "FACTS", facts_path)
    monkeypatch.setattr(cli, "REPORT_M2", report_path)
    m1 = {"m1_n_units": {"value": 5, "display": "5", "cmd": "x", "inputs": {}},
          "m1_ci_pct": {"value": 95, "display": "95%", "cmd": "x", "inputs": {}},
          "m1_n_boot": {"value": 2000, "display": "2,000", "cmd": "x", "inputs": {}},
          "m1_req_cap": {"value": 15, "display": "15", "cmd": "x", "inputs": {}},
          "m2_stale_key": {"value": 1}}
    facts_path.write_text(json.dumps(m1))
    assert cli.main(["facts-m2"]) == 0
    written = json.loads(facts_path.read_text())
    assert {k: v for k, v in written.items() if k.startswith("m1_")} == {k: v for k, v in m1.items()
                                                                          if k.startswith("m1_")}
    assert {k: v for k, v in written.items() if k.startswith("m2_")} == B.build_m2(work, repo, cfg=cfg())
    assert "m2_stale_key" not in written and list(written) == sorted(written)
    assert cli.main(["facts-m2", "--check"]) == 0
    written["m2_cal_n"]["value"] = 999
    facts_path.write_text(json.dumps(written))
    capsys.readouterr()
    assert cli.main(["facts-m2", "--check"]) == 1
    assert "m2_cal_n" in capsys.readouterr().out
    assert cli.main(["facts-m2"]) == 0
    monkeypatch.delenv("ASK_PRIVATE_DIR")  # the report reads facts.json only
    assert cli.main(["report-m2"]) == 0
    assert report_path.read_text(encoding="utf-8") == render(json.loads(facts_path.read_text()))

    # a refusal prints its reason, exits 1 and leaves facts.json as it was
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(work.parent))
    before = facts_path.read_text()
    _canary(work)
    capsys.readouterr()
    assert cli.main(["facts-m2"]) == 1
    assert "rendered canary hit" in capsys.readouterr().err and facts_path.read_text() == before
    assert cli.main(["facts-m2", "--check"]) == 1
