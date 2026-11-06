"""Extraction drift: not needed when the jd model is M1's extractor; otherwise a claude -p re-extraction compared
with M1's requirements. Fakes only; no test runs a real extraction."""
import json

import pytest

from askmike.evals import drift
from askmike.evals.steps import read_jsonl, write_jsonl


def test_drift_not_needed_when_the_jd_model_is_the_extract_model(monkeypatch, capsys):
    from askmike.corpus import claude
    from askmike.evals import __main__ as cli

    def no_call(*a, **k):
        raise AssertionError("drift made a model call")

    monkeypatch.setattr(claude, "run_claude", no_call)
    monkeypatch.delenv("ASK_PRIVATE_DIR", raising=False)  # the not-needed path reads no private file
    assert cli.main(["drift"]) == 0
    out = capsys.readouterr()
    assert out.out == "drift: not needed (jd model equals EXTRACT_MODEL)\n" and out.err == ""


def test_jaccard_and_compare():
    assert drift.jaccard(drift.tokens("Python batch pipelines"), drift.tokens("batch pipelines in Python")) == 0.75
    assert drift.jaccard(set(), set()) == 0.0
    old = {"jd1": ["Python batch pipelines", "SQL for reporting"], "jd2": ["load testing"], "jd3": ["Go services"]}
    new = {"jd1": ["batch pipelines in Python", "Kubernetes clusters"], "jd2": ["load testing", "load testing tools"]}
    s = drift.compare(old, new)
    # jd3 was not re-extracted, so it is left out of both means; 3 of the 4 new requirements match an old one
    assert s == {"n_jds": 2, "n_requirements_mean_old": 1.5, "n_requirements_mean_new": 2.0, "n_new_requirements": 4,
                 "matched_share": 0.75}
    assert drift.compare(old, {})["matched_share"] is None


def test_run_drift_extracts_with_the_given_model_into_its_own_ledger(tmp_path):
    evals = tmp_path / "evals"
    (evals / "sets").mkdir(parents=True)
    write_jsonl(evals / "sets" / "jds.jsonl", [{"id": "jd-a", "text": "Invented posting A."},
                                               {"id": "jd-b", "text": "Invented posting B."}])
    write_jsonl(evals / "requirements.jsonl", [
        {"id": "jd-a-r01", "jd_id": "jd-a", "text": "Python batch pipelines", "required": True, "kind": "skill"},
        {"id": "jd-b-r01", "jd_id": "jd-b", "text": "SQL for reporting", "required": True, "kind": "skill"}])
    calls = []

    def runner(prompt, model):
        calls.append(model)
        if "Invented posting B." in prompt:
            raise RuntimeError("claude exited 1")
        return {"result": json.dumps([{"text": "batch pipelines in Python", "required": True, "kind": "skill"}])}

    r = drift.run_drift(evals, runner, "claude-haiku-5-5", workers=1)
    assert set(calls) == {"claude-haiku-5-5"}
    out = evals / "drift" / "haiku"
    assert (out / "ledger_extract.jsonl").exists() and not (evals / "ledger_extract.jsonl").exists()
    assert [x["jd_id"] for x in read_jsonl(out / "requirements.jsonl")] == ["jd-a"]
    s = json.loads((out / "summary.json").read_text())
    assert s == r and (s["model"], s["n_jds"], s["n_extracted"], s["remaining"]) == ("claude-haiku-5-5", 2, 1, 1)
    assert s["matched_share"] == 1.0 and s["n_requirements_mean_old"] == 1.0
    assert read_jsonl(evals / "requirements.jsonl")[0]["text"] == "Python batch pipelines"  # M1's are untouched
    with pytest.raises(ValueError, match="EXTRACT_MODEL"):
        drift.run_drift(evals, runner, "claude-sonnet-5-5")
