from askmike.evals import pool as pool_mod
from askmike.retrieval.bm25 import Hit


class FakeLadder:
    def __init__(self, conn, embedder, skills, settings=None):
        self.conn, self.embedder, self.settings = conn, embedder, settings

    def run(self, rung, query, k=10):
        s = self.settings
        tag = f"{rung}-{s.depth}-{s.rrf_k0}" if s else rung
        return [Hit(f"{tag}-{i}", 1.0) for i in range(k)]


def test_pool_includes_every_grid_config_top10(monkeypatch):
    monkeypatch.setattr(pool_mod, "Ladder", FakeLadder)
    monkeypatch.setattr(pool_mod, "keyword_rank", lambda q, u, s, k: [Hit(f"kw-{i}", 1.0) for i in range(k)])
    base = FakeLadder(None, None, [])
    got = pool_mod.pool_candidates("q", base, [], [], cap=1000)
    for d in (20, 50):
        for k0 in (10, 60):
            for rung in ("R3", "R4"):
                assert all(f"{rung}-{d}-{k0}-{i}" in got for i in range(10))
    assert all(f"{r}-{i}" in got for r in ("R1", "R2", "kw") for i in range(10))
    assert len(got) == 3 * 10 + 8 * 10


def test_pool_cap_counts_hits_and_orders_by_best_rank(monkeypatch):
    monkeypatch.setattr(pool_mod, "Ladder", FakeLadder)
    monkeypatch.setattr(pool_mod, "keyword_rank", lambda q, u, s, k: [Hit(f"kw-{i}", 1.0) for i in range(k)])
    pool_mod.pool_stats.update(queries=0, cap_hits=0)
    got = pool_mod.pool_candidates("q", FakeLadder(None, None, []), [], [], cap=60)
    assert len(got) == 60 and pool_mod.pool_stats["cap_hits"] == 1
    assert all(u.endswith("-0") for u in got[:11])


def test_once_embedder_embeds_each_query_once():
    class E:
        n = 0
        name = "e"

        def embed_query(self, q):
            E.n += 1
            return [1.0]
    o = pool_mod.OnceEmbedder(E())
    for _ in range(9):
        o.embed_query("same")
    o.embed_query("other")
    assert E.n == 2 and o.name == "e"
