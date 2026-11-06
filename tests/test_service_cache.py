from askmike.service.cache import AnswerCache

IDS = {"template_sha": "t1", "corpus_sha": "c1", "index_sha": "i1", "gate_sha": "g1"}


def test_cache_key_changes_with_each_id_and_ignores_whitespace(tmp_path):
    c = AnswerCache(tmp_path / "cache.db")
    base = c.key("question", "m1", "what did  Mike build?", "", IDS)
    assert base == c.key("question", "m1", "  what did Mike\nbuild? ", None, IDS)
    assert len({base, c.key("jd", "m1", "what did Mike build?", "", IDS),
                c.key("question", "m2", "what did Mike build?", "", IDS),
                c.key("question", "m1", "other", "", IDS),
                c.key("question", "m1", "what did Mike build?", "earlier", IDS)}) == 5
    for k in IDS:
        assert c.key("question", "m1", "what did Mike build?", "", {**IDS, k: "x"}) != base


def test_only_answers_are_cached(tmp_path):
    c = AnswerCache(tmp_path / "cache.db")
    for i, o in enumerate(("fallback", "off_topic", "handoff", "empty", "too_long")):
        c.put(f"k{i}", {"outcome": o, "x": 1})
    assert all(c.get(f"k{i}") is None for i in range(5))
    c.put("ok", {"outcome": "answer", "x": 1})
    assert c.get("ok") == {"outcome": "answer", "x": 1}
    assert AnswerCache(tmp_path / "cache.db").get("ok") == {"outcome": "answer", "x": 1}
