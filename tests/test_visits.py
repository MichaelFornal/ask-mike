import sqlite3

from askmike.answer.answerer import Outcome
from askmike.answer.evidence import Claim
from askmike.retrieval.requirements import Requirement
from askmike.service.visits import Visits

CLAIM = Claim("direct", "queues", "", "Mike ran invented load tests.", ("u2",))


def out(kind="question", outcome="answer", kept=(CLAIM,), reqs=()):
    return Outcome(outcome, "answered", tuple(kept), (), "", "p", "t1", None, tuple(reqs), {}, 5,
                   kind=kind, model="m1")


def test_visits_schema_has_no_address_column(tmp_path):
    Visits(tmp_path / "visits.db")
    cols = [r[1] for r in sqlite3.connect(tmp_path / "visits.db").execute("PRAGMA table_info(visits)")]
    assert cols == ["id", "ts", "kind", "input", "prev_id", "outcome", "fallback_reason", "model",
                    "served_from", "turn_text", "kept_json", "template_sha"]


def test_previous_rebuilds_the_turn_and_ignores_unknown_or_malformed_ids(tmp_path):
    v = Visits(tmp_path / "visits.db")
    vid = v.new_id()
    assert len(vid) == 22
    v.record(vid, "question", "what queues?", None, out(), "model")
    t = v.previous(vid)
    assert t.text == "what queues?" and t.kept == (CLAIM,)
    nid = v.new_id()
    v.record(nid, "question", "", None, out(kind="", outcome="empty", kept=()), "none")
    for bad in (nid, v.new_id(), "x", "", None, "a" * 22 + "'; DROP TABLE visits;--", "../" * 8, 7,
                "é" * 22, vid + "A"):
        assert v.previous(bad) is None
    assert v.export()[0]["id"] == vid  # table still there


def test_jd_turn_text_holds_requirements_not_the_posting(tmp_path):
    v = Visits(tmp_path / "visits.db")
    vid = v.new_id()
    reqs = [Requirement("input-r01", "Python batch pipelines", True, "skill"),
            Requirement("input-r02", "SQL for reporting", True, "skill")]
    v.record(vid, "jd", "SECRET POSTING TEXT about Gadgetron", None, out("jd", reqs=reqs), "model")
    row = v.export()[0]
    assert row["input"] == "SECRET POSTING TEXT about Gadgetron"  # the log keeps it for Mike
    assert row["turn_text"] == "Python batch pipelines\nSQL for reporting"
    assert "POSTING" not in v.previous(vid).text


def test_export_is_ts_ordered(tmp_path):
    v = Visits(tmp_path / "visits.db")
    ids = []
    for i in range(3):
        ids.append(v.new_id())
        v.record(ids[-1], "question", f"q{i}", None, out(), "cache")
    v._conn.execute("UPDATE visits SET ts = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", ids[2]))
    rows = v.export()
    assert [r["id"] for r in rows][0] == ids[2]
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    assert rows[0]["served_from"] == "cache" and rows[0]["prev_id"] is None


def test_previous_is_none_for_a_visit_with_no_kept_claims(tmp_path):
    v = Visits(tmp_path / "visits.db")
    vid = v.new_id()
    v.record(vid, "question", "what queues?", None, out(kept=()), "model")
    assert v.export()[0]["turn_text"] == "what queues?" and v.export()[0]["kept_json"] == "[]"
    assert v.previous(vid) is None
