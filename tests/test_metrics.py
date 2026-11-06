import pytest

from askmike.evals import metrics as m


def test_metrics_on_hand_cases():
    ranked = ["a", "b", "c", "d", "e", "f"]
    assert m.recall_at_k(ranked, {"c"}, 5) == 1.0
    assert m.recall_at_k(ranked, {"c", "z"}, 5) == 0.5
    assert m.recall_at_k(ranked, {"f"}, 5) == 0.0
    assert m.mrr(ranked, {"c"}) == pytest.approx(1 / 3)
    assert m.mrr(ranked, {"z"}) == 0.0
    assert m.ndcg_at_k(["a", "b"], {"a"}) == 1.0
    assert m.ndcg_at_k(["b", "a"], {"a"}) == pytest.approx(1 / 1.5849625007)
    for f in (lambda r: m.recall_at_k(ranked, r, 5), lambda r: m.mrr(ranked, r), lambda r: m.ndcg_at_k(ranked, r)):
        assert f(set()) is None


def test_coverage_excludes_unsupported():
    per_req = [(["a", "b"], {"b"}), (["a", "b"], {"z"}), (["a"], set())]
    assert m.coverage_at_k(per_req, 10) == 0.5
    assert m.coverage_at_k(per_req, 1) == 0.0
    assert m.coverage_at_k([(["a"], set())], 10) is None


def test_cohen_kappa_known_values():
    a = {("q", "u1"): True, ("q", "u2"): True, ("q", "u3"): False, ("q", "u4"): False}
    assert m.cohen_kappa(a, dict(a)) == 1.0
    b = {("q", "u1"): True, ("q", "u2"): False, ("q", "u3"): False, ("q", "u4"): False, ("x", "y"): True}
    # shared 4 pairs: po = 3/4; pa(T)=.5 pb(T)=.25 -> pe = .5*.25 + .5*.75 = .5; kappa = .5
    assert m.cohen_kappa(a, b) == pytest.approx(0.5)
    assert m.cohen_kappa(a, {k: not v for k, v in a.items()}) == -1.0


def test_cohen_kappa_is_undefined_when_chance_agreement_is_one():
    no = {("q", "u1"): False, ("q", "u2"): False}
    assert m.cohen_kappa(no, dict(no)) is None
    yes = {k: True for k in no}
    assert m.cohen_kappa(yes, dict(yes)) is None


def test_kendall_tau_identical_and_reversed():
    o = ["R1", "R2", "R3", "R4", "KW"]
    assert m.kendall_tau(o, o) == 1.0
    assert m.kendall_tau(o, o[::-1]) == -1.0
    assert m.kendall_tau(o, ["R2", "R1", "R3", "R4", "KW"]) == pytest.approx(0.8)
