import json

import pytest

from askmike.evals import bootstrap, compare, run


def q(qid, cluster, split="report", relevant=None, pool=("a", "b", "c")):
    return {"query_id": qid, "cluster": cluster, "split": split, "text": "t " + qid,
            "relevant": relevant, "pool": list(pool)}


def retrieve(text):
    return ["a", "b", "c"]


def test_evaluate_writes_items_and_ci_and_never_averages_none(tmp_path):
    qs = [q("r1", "j1", relevant=["a"]), q("r2", "j1", relevant=["c"]), q("r3", "j2", relevant=[]),
          q("r4", "j3", relevant=["b"])]
    s = run.evaluate("jdreq", "R1", retrieve, qs, tmp_path)
    items = [json.loads(x) for x in (tmp_path / "jdreq_R1_items.jsonl").read_text().splitlines()]
    by = {i["item_id"]: i for i in items}
    assert set(by) == {"r1", "r2", "r3", "r4"}
    assert by["r3"]["recall_at_10"] is None and by["r3"]["mrr"] is None and by["r3"]["covered_at_10"] is None
    assert by["r1"]["recall_at_10"] == 1.0 and by["r1"]["judged_at_10"] == 1.0
    assert {"item_id", "cluster", "split", "judged_at_10", "recall_at_5", "ndcg_at_10"} <= set(by["r1"])
    rep = s["by_split"]["report"]
    assert rep["recall_at_10"]["mean"] == 1.0 and rep["recall_at_10"]["n_items"] == 3
    assert rep["recall_at_10"]["n_clusters"] == 2
    assert rep["mrr"]["mean"] == pytest.approx((1 + 1 / 3 + 0.5) / 3)
    assert rep["n_unsupported"] == 1 and rep["n_unlabelled"] == 0 and rep["judged_at_10_min"] == 1.0
    assert rep["coverage_at_10"]["n_clusters"] == 2 and rep["coverage_at_10"]["mean"] == 1.0
    assert json.loads((tmp_path / "jdreq_R1.json").read_text()) == s
    assert s["by_split"]["tune"]["recall_at_10"] is None
    assert not list(tmp_path.glob("*.part"))


def test_coverage_is_macro_over_jds(tmp_path):
    qs = [q("r1", "j1", relevant=["a"]), q("r2", "j1", relevant=["z"]), q("r3", "j2", relevant=["a"])]
    s = run.evaluate("jdreq", "R1", retrieve, qs, tmp_path)
    assert s["by_split"]["report"]["coverage_at_10"]["mean"] == pytest.approx((0.5 + 1.0) / 2)


def test_unlabelled_items_excluded_and_counted_separately_from_unsupported(tmp_path):
    qs = [q("r1", "j1", relevant=["a"]), q("r2", "j1", relevant=None), q("r3", "j2", relevant=[]),
          q("r4", "j2", split="tune", relevant=None)]
    s = run.evaluate("jdreq", "R2", retrieve, qs, tmp_path)
    ids = {json.loads(x)["item_id"] for x in (tmp_path / "jdreq_R2_items.jsonl").read_text().splitlines()}
    assert ids == {"r1", "r3"}
    assert s["by_split"]["report"]["n_unlabelled"] == 1 and s["by_split"]["report"]["n_unsupported"] == 1
    assert s["by_split"]["tune"]["n_unlabelled"] == 1 and s["by_split"]["tune"]["n_unsupported"] == 0
    assert s["by_split"]["report"]["recall_at_10"]["n_items"] == 1


def test_judged_at_10_min(tmp_path):
    qs = [q("r1", "j1", relevant=["a"], pool=["a", "b", "c"]), q("r2", "j2", relevant=["a"], pool=["a"])]
    s = run.evaluate("questions", "R1", retrieve, qs, tmp_path)
    assert s["by_split"]["report"]["judged_at_10_min"] == pytest.approx(1 / 3)
    assert "coverage_at_10" not in s["by_split"]["report"]


def item(i, c, v, split="report"):
    return {"item_id": i, "cluster": c, "split": split, "recall_at_10": v}


def test_paired_bootstrap_skips_none_and_refuses_mismatched_items():
    a = {i: item(i, f"c{i}", v) for i, v in zip("wxyz", (0.0, 0.0, None, 0.0))}
    b = {i: item(i, f"c{i}", v) for i, v in zip("wxyz", (1.0, 0.0, None, 1.0))}
    r = compare.paired_bootstrap(a, b, "recall_at_10")
    assert r["n_items"] == 3 and r["delta"] == pytest.approx(2 / 3) and r["lo"] <= r["delta"] <= r["hi"]
    with pytest.raises(ValueError):
        compare.paired_bootstrap(a, {k: v for k, v in b.items() if k != "w"}, "recall_at_10")
    with pytest.raises(ValueError):
        compare.paired_bootstrap(a, {**b, "q": item("q", "cq", 0.5)}, "recall_at_10")


def test_cluster_bootstrap_basic():
    r = bootstrap.cluster_bootstrap({"a": [1.0, 1.0], "b": [0.0]})
    assert r["mean"] == pytest.approx(2 / 3) and r["n_items"] == 3 and r["n_clusters"] == 2
    with pytest.raises(ValueError):
        bootstrap.cluster_bootstrap({})


def test_results_dirs_for_A_and_B_are_separate(tmp_path):
    ev = tmp_path
    (ev / "sets").mkdir()
    reqs = [{"id": "r1", "jd_id": "j1", "cluster": "j1", "split": "report", "text": "x", "required": True, "kind": "k"}]
    (ev / "requirements.jsonl").write_text(json.dumps(reqs[0]) + "\n")
    (ev / "sets" / "questions.jsonl").write_text(json.dumps(
        {"id": "p1", "text": "t", "kind": "personal", "gold": ["a"], "split": "report", "synthetic": True}) + "\n")
    for j, rel in (("A", ["a"]), ("B", ["b"])):
        (ev / f"labels_{j}.jsonl").write_text(json.dumps(
            {"query_id": "r1", "set": "jdreq", "cluster": "j1", "split": "report", "relevant": rel,
             "pool": ["a", "b"], "judge": j, "prompt_sha": "x"}) + "\n")
    rungs = {r: retrieve for r in run.ALL_RUNGS}
    run.run_labels(ev, "A", rungs)
    run.run_labels(ev, "B", rungs)
    ra = json.loads((ev / "results" / "A" / "jdreq_R1.json").read_text())
    rb = json.loads((ev / "results" / "B" / "jdreq_R1.json").read_text())
    assert ra["labels"] == "A" and rb["labels"] == "B"
    assert ra["by_split"]["report"]["recall_at_10"]["mean"] == 1.0
    assert (ev / "results" / "B" / "personal_KW.json").exists()
    cmp = json.loads((ev / "results" / "A" / "cmp_jdreq.json").read_text())
    assert set(cmp["R3"]) == {"vs_R1", "vs_KW"} and set(cmp["R1"]) == {"vs_KW"}
    assert "recall_at_10" in cmp["R3"]["vs_R1"] and "coverage_at_10" in cmp["R3"]["vs_KW"]
