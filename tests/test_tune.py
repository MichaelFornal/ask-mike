import json

import pytest

from askmike.evals import tune as T
from askmike.retrieval.bm25 import Hit
from askmike.retrieval.ladder import load_settings


def q(i, cluster, rel, split="tune"):
    return {"query_id": i, "cluster": cluster, "split": split, "text": i, "relevant": rel, "pool": None}


class Fake:
    """R4 finds the relevant unit only when (depth, k0) is in `good`; R3 never does."""

    def __init__(self, d, k0, good):
        self.d, self.k0, self.good = d, k0, good

    def run(self, rung, query, k=10):
        hit = rung == "R4" and (self.d, self.k0) in self.good
        return [Hit("u1" if hit else "x", 1.0)]


def factory(good):
    return lambda d, k0: Fake(d, k0, good)


ITEMS = [q("a", "j1", ["u1"]), q("b", "j2", ["u1"]), q("c", "j2", []), q("d", "j3", None)]


def test_tune_refuses_report_items():
    with pytest.raises(ValueError):
        T.tune(ITEMS + [q("z", "j9", ["u1"], split="report")], factory({(20, 10)}), "A")


def test_tune_picks_best_with_tiebreak():
    r = T.tune(ITEMS, factory({(20, 10), (20, 60), (50, 10), (50, 60)}), "A")
    assert (r.depth, r.rrf_k0) == (20, 60)  # all tie: smaller depth, larger k0
    r = T.tune(ITEMS, factory({(50, 10)}), "A")
    assert (r.depth, r.rrf_k0) == (50, 10)
    assert r.evidence["R4"]["d50_k10"] == 1.0 and r.evidence["R4"]["d20_k10"] == 0.0
    assert r.evidence["R3"]["d50_k10"] == 0.0


def test_settings_records_tuned_on(tmp_path):
    r = T.tune(ITEMS, factory({(50, 10)}), "A")
    assert r.tuned_on == {"split": "tune", "items": 2, "clusters": 2, "labels": "A"}  # unsupported/unlabelled excluded
    p = tmp_path / "settings.json"
    T.write_settings(r, p)
    d = json.loads(p.read_text())
    assert d["depth"] == 50 and d["rrf_k0"] == 10 and d["tuned_on"] == r.tuned_on and d["rule"]
    assert set(d["evidence"]) == {"R3", "R4"}
    s = load_settings(p)
    assert (s.depth, s.rrf_k0) == (50, 10)
