"""M3 facts: the month budget and the caps the box enforces, from deploy/budget.json. The exact key list, the
{value, display, cmd, inputs} shape, the refusals, the merge that keeps every m1_ and m2_ key, and --check.

Pattern from tests/test_facts_m2.py, adapted 2026-11-05 for M3 Task 7. The step needs no private files.
"""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from askmike.facts import build_m3 as B

REPO = Path(__file__).resolve().parents[1]
BUDGET = REPO / "deploy" / "budget.json"
KEYS = {"m3_total_usd_month", "m3_server_usd_month", "m3_dtd_model_cap_usd_month", "m3_model_cap_usd",
        "m3_day_cap_usd"}


def repo_with(tmp_path, budget) -> Path:
    """A repo holding only deploy/budget.json: the dict as JSON, a str as raw text, None for no file."""
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    if isinstance(budget, dict):
        (repo / "deploy" / "budget.json").write_text(json.dumps(budget), encoding="utf-8")
    elif isinstance(budget, str):
        (repo / "deploy" / "budget.json").write_text(budget, encoding="utf-8")
    return repo


def test_budget_json_is_the_brief_record():
    assert json.loads(BUDGET.read_text(encoding="utf-8")) == {
        "total_usd_month": 25.00, "server_usd_month": 7.09, "dtd_model_cap_usd_month": 2.91, "day_cap_share": 0.1,
        "source": "spec §7 and DTD deploy/hosting.json", "checked": "2026-11-05"}


def test_build_m3_keys_exact_and_cap_is_15():
    facts = B.build_m3(REPO)
    assert set(facts) == KEYS and list(facts) == sorted(facts)
    v = {k: f["value"] for k, f in facts.items()}
    assert v == {"m3_total_usd_month": 25.0, "m3_server_usd_month": 7.09, "m3_dtd_model_cap_usd_month": 2.91,
                 "m3_model_cap_usd": 15.0, "m3_day_cap_usd": 1.5}
    assert facts["m3_model_cap_usd"]["display"] == "$15.00" and facts["m3_day_cap_usd"]["display"] == "$1.50"
    assert facts["m3_total_usd_month"]["display"] == "$25.00"
    sha = hashlib.sha256(BUDGET.read_bytes()).hexdigest()
    for key, f in facts.items():
        assert set(f) == {"value", "display", "cmd", "inputs"}, key
        assert f["cmd"] == "uv run python -m askmike.evals facts-m3", key
        assert f["inputs"] == {"deploy/budget.json": sha}, key
        assert isinstance(f["value"], float) and round(f["value"], 4) == f["value"], key


def test_the_day_cap_is_the_share_of_the_model_cap(tmp_path):
    """The caps follow the record: what is left of the total after the server and DTD's cap, and its day share."""
    repo = repo_with(tmp_path, {"total_usd_month": 30.0, "server_usd_month": 8.0, "dtd_model_cap_usd_month": 2.0,
                                "day_cap_share": 0.25, "source": "x", "checked": "2026-11-05"})
    facts = B.build_m3(repo)
    assert facts["m3_model_cap_usd"]["value"] == 20.0 and facts["m3_day_cap_usd"]["value"] == 5.0


GOOD = {"total_usd_month": 25.0, "server_usd_month": 7.09, "dtd_model_cap_usd_month": 2.91, "day_cap_share": 0.1}


@pytest.mark.parametrize("budget,match", [
    (None, "deploy/budget.json is missing"),
    ("{not json", "not JSON"),
    ("[1, 2]", "not a JSON object"),
    ({k: v for k, v in GOOD.items() if k != "server_usd_month"}, "server_usd_month"),
    ({**GOOD, "total_usd_month": "25"}, "total_usd_month"),
    ({**GOOD, "day_cap_share": True}, "day_cap_share"),
    ({**GOOD, "server_usd_month": -1.0}, "server_usd_month"),
    ({**GOOD, "day_cap_share": 1.5}, "day_cap_share"),
    ({**GOOD, "total_usd_month": 10.0}, "no model budget"),
])
def test_build_m3_refuses_a_bad_budget(tmp_path, budget, match):
    with pytest.raises(B.FactsRefused, match=match):
        B.build_m3(repo_with(tmp_path, budget))


def test_check_reports_drift(tmp_path):
    path = tmp_path / "facts.json"
    assert B.check(REPO, path) == sorted(KEYS)  # no facts.json yet: every key differs
    facts = B.build_m3(REPO)
    path.write_text(json.dumps({**facts, "m2_other": {"value": 1}}))
    assert B.check(REPO, path) == []
    path.write_text(json.dumps({**facts, "m3_stale": {"value": 1},
                                "m3_day_cap_usd": dict(facts["m3_day_cap_usd"], value=2.0)}))
    assert B.check(REPO, path) == ["m3_day_cap_usd", "m3_stale"]


def test_cli_facts_m3_merges_keeps_m1_and_m2_and_checks(tmp_path, monkeypatch, capsys):
    from askmike.evals import __main__ as cli

    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(BUDGET, repo / "deploy" / "budget.json")
    facts_path = tmp_path / "facts.json"
    monkeypatch.setattr(cli, "REPO", repo)
    monkeypatch.setattr(cli, "FACTS", facts_path)
    monkeypatch.delenv("ASK_PRIVATE_DIR", raising=False)  # the step reads the repo only
    older = {"m1_n_units": {"value": 5, "display": "5", "cmd": "x", "inputs": {}},
             "m2_cal_n": {"value": 40, "display": "40", "cmd": "y", "inputs": {}},
             "m3_stale_key": {"value": 1}}
    facts_path.write_text(json.dumps(older))
    assert cli.main(["facts-m3"]) == 0
    assert "facts-m3: 5 m3 facts written" in capsys.readouterr().out
    written = json.loads(facts_path.read_text())
    assert written["m1_n_units"] == older["m1_n_units"] and written["m2_cal_n"] == older["m2_cal_n"]
    assert {k: v for k, v in written.items() if k.startswith("m3_")} == B.build_m3(repo)
    assert "m3_stale_key" not in written and list(written) == sorted(written)
    assert cli.main(["facts-m3", "--check"]) == 0
    written["m3_model_cap_usd"]["value"] = 20.0
    facts_path.write_text(json.dumps(written))
    capsys.readouterr()
    assert cli.main(["facts-m3", "--check"]) == 1
    assert "drift: m3_model_cap_usd" in capsys.readouterr().out
    # a refusal prints its reason, exits 1 and leaves facts.json as it was
    before = facts_path.read_text()
    (repo / "deploy" / "budget.json").write_text(json.dumps({**GOOD, "total_usd_month": 9.0}))
    assert cli.main(["facts-m3"]) == 1
    assert "facts-m3: refused: " in capsys.readouterr().err and facts_path.read_text() == before


def test_committed_facts_hold_the_m3_caps():
    """facts-m3 --check passes on the repo: facts.json holds exactly the caps deploy/budget.json gives."""
    committed = json.loads((REPO / "facts.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in committed.items() if k.startswith("m3_")} == B.build_m3(REPO)
    assert any(k.startswith("m1_") for k in committed) and any(k.startswith("m2_") for k in committed)
