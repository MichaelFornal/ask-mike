import json

import pytest
from fakes import FakeEmbedder

from askmike.retrieval.bm25 import Hit, fts_query, search_bm25
from askmike.retrieval.dense import search_dense
from askmike.retrieval.expand import expand_query
from askmike.retrieval.hybrid import rrf
from askmike.retrieval.index import build_index, connect
from askmike.retrieval.ladder import Ladder, Settings

UNITS = [
    {"id": "u1", "claim": "Built a billing system with Stripe.", "detail": "Metered usage.", "subject": "Acme",
     "kind": "role", "skills": []},
    {"id": "u2", "claim": "Wrote retrieval evals for search.", "detail": "", "subject": "Lab", "kind": "project",
     "skills": []},
    {"id": "u3", "claim": "Shipped a storefront.", "detail": "Headless checkout.", "subject": "Shop",
     "kind": "role", "skills": []},
    {"id": "u4", "claim": "Ran web scrapers nightly.", "detail": "", "subject": "Data", "kind": "role",
     "skills": []},
]
SKILLS = [{"term": "Scraping", "depth": "deep", "aliases": ["scrapers", "crawler"]},
          {"term": "Go", "depth": "working", "aliases": []}]


@pytest.fixture
def conn(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "units.jsonl").write_text("".join(json.dumps(u) + "\n" for u in UNITS))
    build_index(d, tmp_path / "i.db", FakeEmbedder(), tmp_path / "e.db")
    return connect(tmp_path / "i.db")


def test_fts_query_neutralises_operators():
    q = fts_query('a OR "b" NEAR(c)')
    assert q == '"or" OR "near"'


def test_bm25_ranks_exact_term_first(conn):
    hits = search_bm25(conn, "stripe", 3)
    assert [h.unit_id for h in hits] == ["u1"]
    assert hits[0].score > 0
    assert search_bm25(conn, "!!", 3) == []


def test_dense_returns_k_sorted(conn):
    hits = search_dense(conn, FakeEmbedder().embed_query("retrieval evals for search"), 3)
    assert len(hits) == 3 and hits[0].unit_id == "u2"
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_rrf_merges_and_breaks_ties_deterministically():
    a = [Hit("x", 3), Hit("y", 2)]
    b = [Hit("z", 9), Hit("x", 1)]
    out = rrf([a, b], k0=60, k=10)
    assert [h.unit_id for h in out] == ["x", "z", "y"]
    assert out[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert [h.unit_id for h in rrf([[Hit("b", 1)], [Hit("a", 1)]], k0=60, k=1)] == ["a"]


def test_expand_query_adds_aliases_once_and_respects_short_term_case():
    out = expand_query("Scrapers and scrapers", SKILLS)
    assert out == "Scrapers and scrapers Scraping scrapers crawler"
    assert expand_query("go-to-market plan", SKILLS) == "go-to-market plan"
    assert expand_query("I write Go daily", SKILLS).endswith(" Go")


def test_r4_expands_bm25_leg_only(conn):
    s = Settings(depth=10, rrf_k0=60)
    lad = Ladder(conn, FakeEmbedder(), SKILLS, s)
    q = "crawler"  # no unit says "crawler"; only the alias "scrapers" reaches u4 by BM25
    assert lad.run("R1", q) == []
    assert lad.run("R4", q)[0].unit_id == "u4"
    assert len(lad.run("R3", q, k=2)) == 2 and len(lad.run("R4", q, k=2)) == 2


def test_unknown_rung_raises(conn):
    with pytest.raises(ValueError):
        Ladder(conn, FakeEmbedder(), [], Settings(10, 60)).run("R9", "x")
