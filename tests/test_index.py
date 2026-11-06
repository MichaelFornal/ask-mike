import json
import sqlite3

from fakes import FakeEmbedder

from askmike.retrieval.index import IndexStats, build_index, connect, unit_text


def _corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    units = [
        {"id": "a", "claim": "Built a billing system.", "detail": "Metered usage.", "subject": "Acme",
         "kind": "role", "skills": [{"term": "python", "depth": "deep"}, {"term": "sql", "depth": "working"}]},
        {"id": "b", "claim": "Wrote retrieval evals.", "detail": "", "subject": "Side project",
         "kind": "project", "skills": []},
        {"id": "c", "claim": "Shipped a storefront.", "detail": "Headless checkout.", "subject": "Shop",
         "kind": "role", "skills": [{"term": "shopify", "depth": "deep"}]},
    ]
    (d / "units.jsonl").write_text("".join(json.dumps(u) + "\n" for u in units))
    (d / "skills.json").write_text("[]")
    return d


def test_unit_text_joins_fields_in_order():
    u = {"claim": "C.", "detail": "", "subject": "S", "skills": [{"term": "x"}, {"term": "y"}]}
    assert unit_text(u) == "C.\nS\nx, y"
    assert unit_text({"claim": "C", "detail": "D", "subject": "S", "skills": []}) == "C\nD\nS"


def test_build_index_counts_and_meta(tmp_path):
    d = _corpus(tmp_path)
    db = tmp_path / "out" / "index.db"
    st = build_index(d, db, FakeEmbedder(), tmp_path / "cache.db")
    assert st == IndexStats(units=3, embedded=3, cached=0)
    assert not db.with_name("index.db.tmp").exists()
    c = connect(db)
    assert c.execute("select count(*) from units").fetchone()[0] == 3
    assert c.execute("select count(*) from units_fts").fetchone()[0] == 3
    assert c.execute("select count(*) from units_vec").fetchone()[0] == 3
    meta = dict(c.execute("select key, value from meta"))
    assert meta["model"] == "fake-embedder" and len(meta["corpus_sha256"]) == 64
    rid = c.execute("select rowid from units where id='c'").fetchone()[0]
    assert c.execute("select rowid from units_fts where units_fts match 'shopify'").fetchone()[0] == rid
    assert c.execute("select unit_rowid from units_vec where embedding match ? and k=1",
                     (__import__("sqlite_vec").serialize_float32(FakeEmbedder().embed_query("shopify storefront")),)
                     ).fetchone()[0] == rid


def test_rebuild_reuses_embedding_cache(tmp_path):
    d = _corpus(tmp_path)
    db, cache = tmp_path / "i.db", tmp_path / "cache.db"
    build_index(d, db, FakeEmbedder(), cache)
    e = FakeEmbedder()
    st = build_index(d, db, e, cache)
    assert (st.embedded, st.cached, e.embedded) == (0, 3, 0)


# ---- freshness guard (final review 6): run, pool, tune and search refuse an index built from another corpus ----

STALE_MSG = "index is stale: rebuild with `python -m askmike.retrieval index`"


def test_open_index_accepts_the_corpus_it_was_built_from(tmp_path):
    from askmike.retrieval.index import open_index
    d = _corpus(tmp_path)
    db = tmp_path / "i.db"
    build_index(d, db, FakeEmbedder(), tmp_path / "cache.db")
    conn = open_index(db, d / "units.jsonl")
    assert conn.execute("select count(*) from units").fetchone()[0] == 3


def test_open_index_refuses_a_changed_corpus(tmp_path):
    import pytest

    from askmike.retrieval.index import StaleIndex, open_index
    d = _corpus(tmp_path)
    db = tmp_path / "i.db"
    build_index(d, db, FakeEmbedder(), tmp_path / "cache.db")
    with (d / "units.jsonl").open("a") as fh:
        fh.write(json.dumps({"id": "d", "claim": "Invented new unit.", "detail": "", "subject": "X",
                             "kind": "project", "skills": []}) + "\n")
    with pytest.raises(StaleIndex) as e:
        open_index(db, d / "units.jsonl")
    assert str(e.value) == STALE_MSG


def test_open_index_refuses_a_missing_or_meta_less_index(tmp_path):
    import pytest

    from askmike.retrieval.index import StaleIndex, open_index
    d = _corpus(tmp_path)
    missing = tmp_path / "none.db"
    with pytest.raises(StaleIndex, match="python -m askmike.retrieval index"):
        open_index(missing, d / "units.jsonl")
    assert not missing.exists()  # sqlite would have created it
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(StaleIndex) as e:
        open_index(bare, d / "units.jsonl")
    assert str(e.value) == STALE_MSG


def _stale_setup(tmp_path, monkeypatch):
    """An index built from one invented corpus, the CLIs pointed at a changed copy of it."""
    from askmike.retrieval import index as ix
    for sub in ("built", "now"):
        (tmp_path / sub).mkdir()
    built = _corpus(tmp_path / "built")
    db = tmp_path / "i.db"
    build_index(built, db, FakeEmbedder(), tmp_path / "cache.db")
    now = _corpus(tmp_path / "now")
    (now / "units.jsonl").write_text((now / "units.jsonl").read_text().replace("billing", "invoicing"))
    monkeypatch.setattr(ix, "default_db", lambda: db)
    (tmp_path / "priv" / "work" / "evals").mkdir(parents=True)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path / "priv"))
    return now, db


def test_evals_steps_refuse_a_stale_index(tmp_path, monkeypatch, capsys):
    from askmike.evals import __main__ as cli
    now, _ = _stale_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "CORPUS", now)
    for argv in (["run", "--labels", "A"], ["tune"], ["pool"]):
        capsys.readouterr()
        assert cli.main(argv) == 1, argv
        assert STALE_MSG in capsys.readouterr().err, argv


def test_retrieval_search_refuses_a_stale_index(tmp_path, monkeypatch, capsys):
    from askmike.retrieval import __main__ as rcli
    now, db = _stale_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcli, "CORPUS", now)
    assert rcli.main(["search", "billing", "--db", str(db)]) == 1
    assert STALE_MSG in capsys.readouterr().err


def test_index_stale_when_model_differs(tmp_path):
    import pytest

    from askmike.retrieval.index import StaleIndex, open_index
    d = _corpus(tmp_path)
    db = tmp_path / "i.db"
    build_index(d, db, FakeEmbedder(), tmp_path / "cache.db")
    open_index(db, d / "units.jsonl", model="fake-embedder").close()
    with pytest.raises(StaleIndex) as e:
        open_index(db, d / "units.jsonl", model="another-model")
    assert str(e.value) == STALE_MSG
