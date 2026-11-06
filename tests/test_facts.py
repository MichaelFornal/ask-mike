"""M1 facts: the key list, the {value, display, cmd, inputs} shape, kappa/tau per the controller's rulings, --check.

Pattern from deal-terms-desk tests/test_facts.py (build on a fixture, check reports drift), adapted 2026-11-03.
"""
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from askmike.facts import build as B

REPO_DIR = Path(__file__).resolve().parents[1]
FAMILIES = ("ai_ml", "fde_solutions", "software", "gtm", "data", "product", "other")
SETS = ("jdreq", "questions", "personal")
RUNGS = ("R1", "R2", "R3", "R4", "KW")
METRICS = ("recall_at_5", "recall_at_10", "mrr", "ndcg_at_10")


def brief_keys() -> set:
    """The fact keys the task brief names, spelled out here rather than imported from the code under test."""
    keys = {"m1_n_units", "m1_n_jds", "m1_n_requirements", "m1_n_questions", "m1_ci_pct", "m1_n_boot",
            "m1_judged_at_10_min", "m1_n_unlabelled", "m1_supportable_rate", "m1_kappa_AB", "m1_tau_rungs_AB",
            "m1_n_pairs_AB", "m1_tuned_depth", "m1_tuned_rrf_k0"}
    keys |= {f"m1_jd_family_{f}" for f in FAMILIES}
    for suffix in ("", "_lo", "_hi"):
        keys |= {f"m1_{s}_{r}_report_{m}{suffix}" for s in SETS for r in RUNGS for m in METRICS}
        keys |= {f"m1_jdreq_{r}_report_coverage_at_10{suffix}" for r in RUNGS}
    keys |= {f"m1_cmp_{r}_vs_KW_jdreq_recall_at_10_{p}" for r in RUNGS[:4] for p in ("delta", "lo", "hi")}
    return keys


# Numbers the report's required statements print that the brief's list does not hold; every digit in the report
# comes from a fact, so each gets one (the requirement cap, the invented personal phrasings, the tau step, and the
# report-split sizes the intervals are computed over).
REPORT_KEYS = {"m1_req_cap", "m1_n_personal", "m1_n_personal_invented", "m1_n_rungs", "m1_tau_step",
               "m1_jdreq_report_n_items", "m1_jdreq_report_n_jds", "m1_questions_report_n_items",
               "m1_personal_report_n_items", "m1_tau_n_items", "m1_tau_n_jds",
               # final review: judge B's own sample sizes, the short-name rule R4's expansion inherits, and the
               # share of job descriptions whose extraction stopped at the requirement cap
               "m1_B_n_jds", "m1_B_n_questions", "m1_short_name_max_len", "m1_jds_at_req_cap_share"}
JOB_SEARCH = re.compile(r"applied|top50|application|company", re.I)


def test_build_m1_keys_exact(m1_facts):
    assert set(m1_facts) == brief_keys() | REPORT_KEYS


def test_every_fact_has_value_display_cmd_and_inputs(m1_facts):
    for key, f in m1_facts.items():
        assert set(f) == {"value", "display", "cmd", "inputs"}, key
        assert isinstance(f["display"], str) and f["display"], key
        assert f["cmd"].startswith("uv run python "), key
        v = f["value"]
        if isinstance(v, float):
            assert round(v, 4) == v, key
        for name, sha in f["inputs"].items():
            assert not name.startswith(("/", "~")) and ".." not in name and "Users" not in name, (key, name)
            assert re.fullmatch(r"[0-9a-f]{64}", sha), (key, name)


def test_inputs_name_the_files_read_with_their_sha256(m1_facts, m1_dirs):
    evals, repo = m1_dirs
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    jds = m1_facts["m1_n_jds"]["inputs"]
    assert jds["sets/jds.jsonl"] == sha(evals / "sets" / "jds.jsonl")
    assert jds["corpus/units.jsonl"] == sha(repo / "corpus" / "units.jsonl")
    kappa = m1_facts["m1_kappa_AB"]["inputs"]
    assert {"labels_A.jsonl", "labels_B.jsonl", "corpus/units.jsonl"} <= set(kappa)
    tau = set(m1_facts["m1_tau_rungs_AB"]["inputs"])
    assert {f"results/{j}/jdreq_{r}_items.jsonl" for j in "AB" for r in RUNGS} <= tau
    assert "results/A/jdreq_R4.json" in m1_facts["m1_jdreq_R4_report_recall_at_10"]["inputs"]
    assert m1_facts["m1_n_boot"]["inputs"] == {}


def test_no_job_search_facts(m1_facts):
    committed = REPO_DIR / "facts.json"
    sets = [m1_facts] + ([json.loads(committed.read_text(encoding="utf-8"))] if committed.exists() else [])
    for facts in sets:
        for key, f in facts.items():
            assert not JOB_SEARCH.search(key), key
            if isinstance(f, dict):
                for text in (f.get("display", ""), f.get("cmd", ""), *f.get("inputs", {})):
                    assert not JOB_SEARCH.search(str(text)), (key, text)


def test_counts_on_the_fixture(m1_facts):
    v = {k: f["value"] for k, f in m1_facts.items()}
    assert (v["m1_n_units"], v["m1_n_jds"], v["m1_n_requirements"], v["m1_n_questions"]) == (20, 7, 21, 4)
    assert {f: v[f"m1_jd_family_{f}"] for f in FAMILIES} == {
        "ai_ml": 2, "fde_solutions": 0, "software": 2, "gtm": 1, "data": 1, "product": 0, "other": 1}
    assert (v["m1_n_personal"], v["m1_n_personal_invented"]) == (3, 2)
    assert (v["m1_ci_pct"], v["m1_n_boot"], v["m1_req_cap"], v["m1_n_rungs"], v["m1_tau_step"]) == (95, 2000, 15, 5, 0.2)
    assert (v["m1_tuned_depth"], v["m1_tuned_rrf_k0"]) == (20, 60)
    # report JDs b, d, e, f, h: 15 requirements, h-r3 unlabelled, d-r2 and e-r3 labelled with nothing relevant
    assert v["m1_supportable_rate"] == round(12 / 14, 4)
    assert v["m1_n_unlabelled"] == 1
    assert (v["m1_jdreq_report_n_items"], v["m1_jdreq_report_n_jds"]) == (12, 5)
    assert (v["m1_questions_report_n_items"], v["m1_personal_report_n_items"]) == (2, 2)
    # judge B labelled the report JDs b, d, e and every evidence question; every pool holds 8 units
    assert (v["m1_tau_n_items"], v["m1_tau_n_jds"], v["m1_n_pairs_AB"]) == (9, 3, 13 * 8)
    assert (v["m1_B_n_jds"], v["m1_B_n_questions"]) == (3, 4)
    # every fixture JD has 3 requirements, under the cap of 15
    assert v["m1_jds_at_req_cap_share"] == 0.0
    assert v["m1_short_name_max_len"] == 3


def _copy_evals(m1_dirs, tmp_path):
    evals, repo = m1_dirs
    shutil.copytree(evals, tmp_path / "evals")
    return tmp_path / "evals", repo


def _rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_judge_b_sizes_count_judge_b_labels_not_the_tau_items(m1_dirs, tmp_path):
    """Final review 1: the report printed tau's item count and the full question count as judge B's sample."""
    evals, repo = _copy_evals(m1_dirs, tmp_path)
    p = evals / "labels_B.jsonl"
    rows = _rows(p)
    # judge B labels one more report JD that judge A never labelled (so tau cannot use it) and drops a question
    extra = [dict(r, cluster="jd-widget-z", query_id=r["query_id"].replace(r["cluster"], "jd-widget-z"))
             for r in rows if r["cluster"] == "jd-widget-b"]
    kept = [r for r in rows if r["query_id"] != "q-ev-7"]
    _write(p, kept + extra)
    f = B.build_m1(evals, repo)
    assert (f["m1_B_n_jds"]["value"], f["m1_B_n_questions"]["value"]) == (4, 3)
    assert f["m1_tau_n_jds"]["value"] == 3 and f["m1_n_questions"]["value"] == 4
    assert "labels_B.jsonl" in f["m1_B_n_jds"]["inputs"] and "labels_B.jsonl" in f["m1_B_n_questions"]["inputs"]


def test_judge_b_sample_must_be_report_split(m1_dirs, tmp_path):
    """The report calls judge B's job descriptions report-split; a tune-split B row would make that false."""
    evals, repo = _copy_evals(m1_dirs, tmp_path)
    p = evals / "labels_B.jsonl"
    rows = _rows(p)
    rows[0]["split"] = "tune"
    _write(p, rows)
    with pytest.raises(ValueError, match="report split"):
        B.build_m1(evals, repo)


def test_requirement_cap_share_counts_jds_at_the_cap(m1_dirs, tmp_path):
    """Final review 3: the share of job descriptions whose extracted requirement count equals the cap."""
    evals, repo = _copy_evals(m1_dirs, tmp_path)
    p = evals / "requirements.jsonl"
    rows = _rows(p)
    a = [r for r in rows if r["jd_id"] == "jd-widget-a"]
    more = [dict(a[0], id=f"jd-widget-a-r{n}", text=f"Invented requirement {n}.") for n in range(4, 16)]
    _write(p, rows + more)
    f = B.build_m1(evals, repo)["m1_jds_at_req_cap_share"]
    assert f["value"] == round(1 / 7, 4) and f["display"] == "14%"
    assert {"requirements.jsonl", "sets/jds.jsonl"} <= set(f["inputs"])


def test_short_name_max_len_is_measured_from_the_expansion_rule():
    """Final review 2: the longest skill name R4's expansion matches only in Capitalised or ALL-CAPS form."""
    from askmike.retrieval.expand import expand_query
    n = B.short_name_max_len()
    assert n == 3
    skills = [{"term": "x" * n, "aliases": []}, {"term": "y" * (n + 1), "aliases": []}]
    assert expand_query("x" * n, skills) == "x" * n  # lower case: not expanded
    assert expand_query("X" * n, skills) == "X" * n + " " + "x" * n
    assert expand_query("y" * (n + 1), skills) == "y" * (n + 1) + " " + "y" * (n + 1)


def test_headline_facts_copy_the_report_split_of_results_A(m1_facts, m1_dirs):
    evals, _ = m1_dirs
    for s in SETS:
        for r in RUNGS:
            rep = json.loads((evals / "results" / "A" / f"{s}_{r}.json").read_text())["by_split"]["report"]
            for m in METRICS + (("coverage_at_10",) if s == "jdreq" else ()):
                for suffix, part in (("", "mean"), ("_lo", "lo"), ("_hi", "hi")):
                    assert m1_facts[f"m1_{s}_{r}_report_{m}{suffix}"]["value"] == round(rep[m][part], 4)
            judged = rep["judged_at_10_min"]
            assert m1_facts["m1_judged_at_10_min"]["value"] <= judged
    cmp = json.loads((evals / "results" / "A" / "cmp_jdreq.json").read_text())
    got = m1_facts["m1_cmp_R4_vs_KW_jdreq_recall_at_10_delta"]["value"]
    assert got == round(cmp["R4"]["vs_KW"]["recall_at_10"]["delta"], 4)


def test_display_formats():
    assert B.display("m1_n_requirements", 1446) == "1,446"
    assert B.display("m1_jdreq_R4_report_recall_at_10", 0.5394) == "0.54"
    assert B.display("m1_cmp_R4_vs_KW_jdreq_recall_at_10_lo", 0.0564) == "0.056"
    assert B.display("m1_cmp_R2_vs_KW_jdreq_recall_at_10_hi", -0.0004) == "0.000"
    assert B.display("m1_ci_pct", 95) == "95%"
    assert B.display("m1_supportable_rate", 0.8882) == "89%"
    assert B.display("m1_jds_at_req_cap_share", 0.82) == "82%"
    assert B.display("m1_tau_step", 0.2) == "0.2"
    assert B.display("m1_kappa_AB", None) == "undefined"
    assert B.display("m1_questions_R1_report_mrr", None) == "n/a"


def test_kappa_over_pairs_in_both_pools():
    a = [{"query_id": "q1", "pool": ["u1", "u2", "u3", "u4", "u9"], "relevant": ["u1", "u2"]},
         {"query_id": "q2", "pool": ["u1"], "relevant": ["u1"]}]  # q2: judge A only, left out
    b = [{"query_id": "q1", "pool": ["u1", "u2", "u3", "u4"], "relevant": ["u1"]}]  # u9 is in A's pool only
    # shared 4 pairs: po = 3/4, pe = .5 * .25 + .5 * .75 = .5, kappa = .5
    assert B.kappa_ab(a, b) == (0.5, 4)
    none = [{"query_id": "q1", "pool": ["u1", "u2"], "relevant": []}]
    assert B.kappa_ab(none, none) == (None, 2)


def _items(means: dict, ids=("i1", "i2"), split="report", cluster="jd1"):
    return {r: {i: {"item_id": i, "cluster": cluster, "split": split, "recall_at_10": m} for i in ids}
            for r, m in means.items()}


def test_rung_order_breaks_ties_by_name():
    assert B.rung_order({"R2": 0.5, "R1": 0.5, "KW": 0.9, "R4": 0.1, "R3": 0.5}) == ["KW", "R1", "R2", "R3", "R4"]


def test_tau_uses_report_items_both_judges_labelled():
    a = _items({"R1": 0.1, "R2": 0.2, "R3": 0.3, "R4": 0.4, "KW": 0.5})
    b = _items({"R1": 0.1, "R2": 0.2, "R3": 0.3, "R4": 0.5, "KW": 0.4})  # one adjacent swap
    assert B.tau_ab(a, b) == (pytest.approx(0.8), 2, 1)
    # a tune item and an item judge A never labelled would reverse B's order if they counted
    for r, x in _items({"R1": 9, "R2": 8, "R3": 7, "R4": 6, "KW": 5}, ids=("t1",), split="tune").items():
        b[r].update(x)
    for r, x in _items({"R1": 9, "R2": 8, "R3": 7, "R4": 6, "KW": 5}, ids=("b1",)).items():
        b[r].update(x)
    assert B.tau_ab(a, b) == (pytest.approx(0.8), 2, 1)


def test_tau_mean_skips_items_a_judge_found_unsupported():
    a = _items({"R1": 0.1, "R2": 0.2, "R3": 0.3, "R4": 0.4, "KW": 0.5})
    b = _items({"R1": 0.1, "R2": 0.2, "R3": 0.3, "R4": 0.4, "KW": 0.5})
    for r in b:
        b[r]["i2"]["recall_at_10"] = None
    assert B.tau_ab(a, b) == (1.0, 2, 1)


def test_kappa_none_publishes_null_and_undefined(m1_dirs, tmp_path):
    evals, repo = m1_dirs
    shutil.copytree(evals, tmp_path / "evals")
    for name in ("labels_A.jsonl", "labels_B.jsonl"):
        p = tmp_path / "evals" / name
        rows = [dict(json.loads(x), relevant=[]) for x in p.read_text().splitlines() if x.strip()]
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    f = B.build_m1(tmp_path / "evals", repo)["m1_kappa_AB"]
    assert (f["value"], f["display"]) == (None, "undefined")


def test_check_reports_drift(m1_dirs, tmp_path):
    evals, repo = m1_dirs
    path = tmp_path / "facts.json"
    assert B.check(evals, repo, path) == sorted(brief_keys() | REPORT_KEYS)
    facts = B.build_m1(evals, repo)
    path.write_text(json.dumps({**facts, "m0_other": {"value": 1}}))
    assert B.check(evals, repo, path) == []
    facts["m1_n_jds"] = dict(facts["m1_n_jds"], value=999)
    path.write_text(json.dumps(facts))
    assert B.check(evals, repo, path) == ["m1_n_jds"]


def test_cli_facts_check_and_report(m1_dirs, tmp_path, monkeypatch, capsys):
    from askmike.evals import __main__ as cli
    from askmike.facts.report import render
    evals, repo = m1_dirs
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(evals.parent.parent))
    facts_path, report_path = tmp_path / "facts.json", tmp_path / "docs" / "m1" / "REPORT.md"
    monkeypatch.setattr(cli, "REPO", repo)
    monkeypatch.setattr(cli, "FACTS", facts_path)
    monkeypatch.setattr(cli, "REPORT_M1", report_path)
    facts_path.write_text(json.dumps({"m0_other": {"value": 1}}))
    assert cli.main(["facts"]) == 0
    written = json.loads(facts_path.read_text())
    assert written["m0_other"] == {"value": 1}
    assert {k: v for k, v in written.items() if k.startswith("m1_")} == B.build_m1(evals, repo)
    assert cli.main(["facts", "--check"]) == 0
    written["m1_n_jds"]["value"] = 999
    facts_path.write_text(json.dumps(written))
    capsys.readouterr()
    assert cli.main(["facts", "--check"]) == 1
    assert "m1_n_jds" in capsys.readouterr().out
    assert cli.main(["facts"]) == 0
    monkeypatch.delenv("ASK_PRIVATE_DIR")  # the report reads facts.json only
    assert cli.main(["report"]) == 0
    assert report_path.read_text(encoding="utf-8") == render(json.loads(facts_path.read_text()))
