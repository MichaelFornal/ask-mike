"""Pool and judge refuse a partial extraction (final review 5). Invented JDs only."""
import json

import pytest

from askmike.evals import steps
from tests.fakes import fake_claude

REPLY = json.dumps([{"text": "Invented requirement.", "required": True, "kind": "skill"}])


def _jds(ev, ids=("jd-x", "jd-y")):
    (ev / "sets").mkdir(parents=True, exist_ok=True)
    steps.write_jsonl(ev / "sets" / "jds.jsonl", [
        {"id": i, "family": "other", "split": "report", "text": f"Invented posting {i}."} for i in ids])


def _stopping_runner(stop_on: str):
    """Answers every prompt except the one for `stop_on`, where it raises as the deadline gate does."""
    ok = fake_claude(REPLY)

    def runner(prompt, model):
        if stop_on in prompt:
            raise RuntimeError(steps.DEADLINE)
        return ok(prompt, model)
    return runner


def test_run_extract_deadline_leaves_a_partial_file_that_pool_and_judge_refuse(tmp_path):
    _jds(tmp_path)
    r = steps.run_extract(tmp_path, _stopping_runner("jd-y"), workers=1)
    assert (r["extracted"], r["remaining"], r["requirements"]) == (1, 1, 1)
    with pytest.raises(steps.ExtractIncomplete, match="1 of 2 job descriptions stopped"):
        steps.run_pool(tmp_path, None, None, [], [])
    with pytest.raises(steps.ExtractIncomplete, match="rerun `python -m askmike.evals extract`"):
        steps.run_judge(tmp_path, "A", False, fake_claude("{}"), [])


def test_an_extraction_error_other_than_the_deadline_also_refuses(tmp_path):
    _jds(tmp_path)
    steps.write_jsonl(tmp_path / "extract_stats.jsonl", [
        {"id": "jd-x", "kept": 1, "dropped_invalid": 0, "error": ""},
        {"id": "jd-y", "kept": 0, "dropped_invalid": 0, "error": "parse"}])
    assert "1 of 2 job descriptions stopped" in steps.extract_problem(tmp_path)


def test_requirements_covering_fewer_jds_than_the_set_refuse(tmp_path):
    """A stale or hand-cut requirements.jsonl with no error rows in the stats is still incomplete."""
    _jds(tmp_path)
    steps.write_jsonl(tmp_path / "requirements.jsonl", [
        {"id": "jd-x-r01", "jd_id": "jd-x", "cluster": "jd-x", "split": "report", "text": "Invented.",
         "required": True, "kind": "skill"}])
    msg = steps.extract_problem(tmp_path)
    assert "requirements cover 1 of 2 job descriptions" in msg
    with pytest.raises(steps.ExtractIncomplete):
        steps.run_pool(tmp_path, None, None, [], [])


def test_complete_extraction_passes_including_a_jd_with_no_valid_requirements(tmp_path):
    _jds(tmp_path)
    runner = fake_claude(REPLY)
    steps.run_extract(tmp_path, runner, workers=1)
    assert steps.extract_problem(tmp_path) == ""
    # jd-y finished cleanly but kept nothing: complete, not missing
    steps.write_jsonl(tmp_path / "requirements.jsonl",
                      [r for r in steps.read_jsonl(tmp_path / "requirements.jsonl") if r["jd_id"] == "jd-x"])
    steps.write_jsonl(tmp_path / "extract_stats.jsonl", [
        {"id": "jd-x", "kept": 1, "dropped_invalid": 0, "error": ""},
        {"id": "jd-y", "kept": 0, "dropped_invalid": 1, "error": ""}])
    assert steps.extract_problem(tmp_path) == ""


@pytest.mark.parametrize("argv", [["pool"], ["judge", "--judge", "A"]])
def test_cli_pool_and_judge_exit_1_with_the_reason(tmp_path, monkeypatch, capsys, argv):
    from askmike.evals import __main__ as cli
    ev = tmp_path / "priv" / "work" / "evals"
    _jds(ev)
    steps.run_extract(ev, _stopping_runner("jd-y"), workers=1)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path / "priv"))
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "extract is incomplete" in err and "Invented" not in err


def test_extract_guard_flags_jd_with_kept_but_no_rows(tmp_path):
    _jds(tmp_path)
    steps.write_jsonl(tmp_path / "requirements.jsonl", [])
    steps.write_jsonl(tmp_path / "extract_stats.jsonl", [
        {"id": "jd-x", "kept": 3, "dropped_invalid": 0, "error": ""},
        {"id": "jd-y", "kept": 0, "dropped_invalid": 1, "error": ""}])
    msg = steps.extract_problem(tmp_path)
    assert "requirements cover 1 of 2 job descriptions" in msg
