import json
import warnings

import pytest

from askmike.evals.sets import (SYNTHETIC_PERSONAL, PERSONAL_GOLD, build_sets, jd_title, load_question_map, personal_topics, question_kind,
                                role_family)
from askmike.evals.split import split_of


def test_split_is_deterministic_and_about_30pct_tune():
    ids = [f"id-{i}" for i in range(1000)]
    first = [split_of(i) for i in ids]
    assert first == [split_of(i) for i in ids]
    assert 0.25 <= first.count("tune") / 1000 <= 0.35


@pytest.mark.parametrize("title,fam", [
    ("Machine Learning Engineer", "ai_ml"),
    ("Applied AI Engineer", "ai_ml"),
    ("Software Engineer", "software"),
    ("Backend Developer", "software"),
    ("Forward Deployed Engineer", "fde_solutions"),
    ("Solutions Engineer", "fde_solutions"),
    ("Sales Engineer", "fde_solutions"),
    ("Implementation Engineer", "fde_solutions"),
    ("Account Executive", "gtm"),
    ("Growth Marketing Manager", "gtm"),
    ("Data Analyst", "data"),
    ("Product Manager", "product"),
    ("Receptionist", "other"),
    ("MLOps Engineer", "ai_ml"),
    ("Research Engineer", "ai_ml"),
    ("Member of Technical Staff", "ai_ml"),
    ("Data Scientist", "data"),
    ("Data Engineer", "data"),
    ("Analytics Engineer", "data"),
    ("Product Manager Platform", "product"),
    ("Solutions Marketing Manager", "gtm"),
    ("Customer Success Engineer", "gtm"),
    ("Operations Manager", "gtm"),
    ("", "other"),
])
def test_role_family_buckets(title, fam):
    assert role_family(title) == fam


def test_jd_title_rules():
    body = "TITLE: Real Title\nother"
    assert jd_title(body, "Index Title") == "Index Title"
    assert jd_title(body, None) == "Real Title"
    assert jd_title("URL: http://x\nCOMPANY: Acme\n\nFirst Line Role\nmore", None) == "First Line Role"
    assert jd_title("URL: http://x\n" + "x" * 130, None) == ""


def _write_raw(tmp_path, with_map=True):
    raw = tmp_path / "raw"
    (raw / "jds" / "applied").mkdir(parents=True)
    (raw / "jds" / "top50").mkdir(parents=True)
    (raw / "answers").mkdir()
    for i in range(3):
        (raw / "jds" / "applied" / f"a{i}.txt").write_text(f"Software Engineer\nApplied body {i}")
    index = []
    titles = ["Machine Learning Engineer"] * 10 + ["Account Executive"] * 6 + ["Data Analyst"] * 3 + ["Zookeeper"] * 1
    for i, t in enumerate(titles):
        (raw / "jds" / "top50" / f"k{i}.txt").write_text(f"{t}\ntop body {i}")
        index.append({"company": "C", "key": f"k{i}", "rank": i, "run": "r", "title": t})
    (raw / "jds" / "top50" / "dupe.txt").write_text("Software Engineer\nApplied body 0")  # exact dupe of applied
    (raw / "jds" / "top50_index.json").write_text(json.dumps(index))
    (raw / "answers" / "apply_db_answers.json").write_text(json.dumps([
        {"question_text": "Are you legally authorized to work in the US?"},
        {"question_text": "What pay range are you targeting?"},
        {"question_text": "Describe a data pipeline you built."},
    ]))
    (raw / "answers" / "apply_db_ref_answers.json").write_text(json.dumps([
        {"question": "Where are you located?"},
        {"question": "Where are you located and when can you start?"},
        {"question": "What is your notice period?"},
        {"question": "Describe a data pipeline you built."},  # dupe
    ]))
    qmap = tmp_path / "qmap.json"
    if with_map:
        qmap.write_text(json.dumps({"invented_key": "What project is Mike proudest of?"}))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "skills.json").write_text(json.dumps([
        {"term": "Rust", "depth": "none", "aliases": []},
        {"term": "BigQuery / Redshift", "depth": "familiar", "aliases": []},
        {"term": "Elixir", "depth": "learning", "aliases": []},
        {"term": "Python", "depth": "strong", "aliases": []},
        {"term": "AWS", "depth": "working", "aliases": []},
    ]))
    return raw, corpus, qmap


def _rows(out, name):
    return [json.loads(line) for line in (out / f"{name}.jsonl").read_text().splitlines()]


def test_build_sets_includes_all_applied_and_stratifies_rest(tmp_path):
    raw, corpus, qmap = _write_raw(tmp_path)
    out = tmp_path / "out"
    st = build_sets(raw, corpus, out, n_jds=12, seed=0, question_map=qmap)
    jds = _rows(out, "jds")
    texts = [r["text"] for r in jds]
    assert len(set(texts)) == len(texts) == 12
    assert all(f"Applied body {i}" in " ".join(texts) for i in range(3))
    fams = st.jd_families
    assert sum(fams.values()) == 12
    # 9 sampled from a pool of 19: every non-empty pool family gets >= 2 (the singleton gets its 1)
    assert fams["ai_ml"] >= 2 and fams["gtm"] >= 2 and fams["data"] >= 2 and fams["other"] == 1
    assert fams["ai_ml"] > fams["gtm"] - 1  # mix roughly follows the pool
    again = tmp_path / "out2"
    build_sets(raw, corpus, again, n_jds=12, seed=0, question_map=qmap)
    assert (again / "jds.jsonl").read_text() == (out / "jds.jsonl").read_text()
    for name in ("jds", "questions", "gaps", "handoffs", "injections"):
        assert all(r["split"] == split_of(r["id"]) for r in _rows(out, name))


@pytest.mark.parametrize("text,kind", [
    ("Are you authorized to work in the United States?", "personal"),
    ("Will you now or in the future require visa sponsorship?", "personal"),
    ("Are you open to relocation?", "personal"),
    ("Where are you located?", "personal"),
    ("When is your earliest possible start date?", "personal"),
    ("What is your notice period?", "personal"),
    ("Are you legally able to work in the US?", "personal"),
    ("Are you legally eligible to work in this country?", "personal"),
    ("Do you have the right to work in the US?", "personal"),
    ("What is your current location?", "personal"),
    ("Where do you live?", "personal"),
    ("What city do you live in?", "personal"),
    ("Are you able to work onsite?", "personal"),
    ("Is this role remote, hybrid or onsite for you?", "personal"),
    ("What is your availability?", "personal"),
    ("How soon can you start?", "personal"),
    ("How much notice do you need to give?", "personal"),
    ("Date available", "personal"),
    ("What is your availability to start?", "personal"),
    ("What is your pay range?", "handoff"),
    ("What is your phone number?", "handoff"),
    ("Please list two references", "handoff"),
    ("Why do you want to work at this company?", "handoff"),
    ("Why are you leaving your current job?", "handoff"),
    ("What is your gender?", "handoff"),
    ("How did you hear about us?", "handoff"),
    ("Are you open to a relocation package?", "handoff"),
    ("What pay range do you want, and what is your earliest start date?", "handoff"),
    ("What is the hardest bug you have fixed?", "evidence"),
    ("How do you address ambiguity in a project?", "evidence"),
    ("Do you authorize a background check?", "evidence"),
    ("What is the pay rate at start of employment?", "handoff"),
    ("Experience with high availability systems?", "evidence"),
    ("Do you have iPhone app experience?", "evidence"),
    ("Experience with a reference architecture?", "evidence"),
    ("Describe your experience working remotely.", "evidence"),
    ("How would you start a new project?", "evidence"),
    ("Did you notice any issues in the pipeline?", "evidence"),
])
def test_question_kind_routes_personal_and_handoff(text, kind):
    assert question_kind(text) == kind


def test_personal_topics_multi_and_start_date():
    assert personal_topics("When can you start?") == ["start_date"]
    assert personal_topics("Are you authorized to work here, and what is your notice period?") == [
        "work_auth", "start_date"]
    assert personal_topics("Where are you located and when can you start?") == ["location", "start_date"]


def test_personal_questions_get_gold_unit_ids(tmp_path):
    raw, corpus, qmap = _write_raw(tmp_path)
    out = tmp_path / "out"
    build_sets(raw, corpus, out, n_jds=12, question_map=qmap)
    qs = {r["text"]: r for r in _rows(out, "questions")}
    assert qs["Are you legally authorized to work in the US?"]["gold"] == [PERSONAL_GOLD["work_auth"]]
    assert qs["Where are you located?"]["gold"] == [PERSONAL_GOLD["location"]]
    assert qs["Where are you located and when can you start?"]["gold"] == [
        PERSONAL_GOLD["location"], PERSONAL_GOLD["start_date"]]
    assert qs["What is your notice period?"]["gold"] == [PERSONAL_GOLD["start_date"]]
    assert qs["What pay range are you targeting?"]["gold"] == []
    assert qs["What project is Mike proudest of?"]["kind"] == "evidence"
    assert sum(1 for r in qs.values() if r["text"] == "Describe a data pipeline you built.") == 1
    assert set(PERSONAL_GOLD.values()) == {"person-work-authorization", "person-location", "person-start-date"}
    hs = [r["text"] for r in _rows(out, "handoffs")]
    assert "What pay range are you targeting?" in hs and len(hs) >= 12


def test_missing_map_warns_and_counts(tmp_path):
    raw, corpus, _ = _write_raw(tmp_path, with_map=False)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        st = build_sets(raw, corpus, tmp_path / "out", n_jds=12, question_map=tmp_path / "nope.json")
    assert st.map_missing and st.map_questions == 0 and w


def test_gap_rows_cover_every_low_depth_term(tmp_path):
    raw, corpus, qmap = _write_raw(tmp_path)
    out = tmp_path / "out"
    build_sets(raw, corpus, out, n_jds=12, question_map=qmap)
    gaps = {r["term"]: r for r in _rows(out, "gaps")}
    assert set(gaps) == {"Rust", "BigQuery / Redshift", "Elixir"}
    assert gaps["Rust"]["id"] == "gap-rust" and gaps["BigQuery / Redshift"]["id"] == "gap-bigquery-redshift"
    assert gaps["Rust"]["text"] == "Has Mike worked with Rust?" and gaps["Elixir"]["depth"] == "learning"


def test_no_source_field_in_jd_rows(tmp_path):
    raw, corpus, qmap = _write_raw(tmp_path)
    out = tmp_path / "out"
    build_sets(raw, corpus, out, n_jds=12, question_map=qmap)
    for r in _rows(out, "jds"):
        assert set(r) == {"id", "family", "text", "split"}
        assert r["id"].startswith("jd-") and len(r["id"]) == 11


def test_injections_copied_from_fixture(tmp_path):
    raw, corpus, qmap = _write_raw(tmp_path)
    out = tmp_path / "out"
    build_sets(raw, corpus, out, n_jds=12, question_map=qmap)
    inj = _rows(out, "injections")
    assert len(inj) >= 15 and {r["kind"] for r in inj} == {"jd", "question"}


def test_load_question_map_skips_blank(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"a": "Q one?", "b": "  ", "c": 3}))
    assert load_question_map(p) == {"a": "Q one?"}


@pytest.mark.parametrize("text,topic", SYNTHETIC_PERSONAL)
def test_synthetic_personal_phrasings_route_with_expected_gold(text, topic):
    assert question_kind(text) == "personal"
    assert personal_topics(text) == [topic]


def test_synthetic_personal_rows_flagged(tmp_path):
    raw, corpus, qmap = _write_raw(tmp_path)
    out = tmp_path / "out"
    st = build_sets(raw, corpus, out, n_jds=12, question_map=qmap)
    qs = _rows(out, "questions")
    syn = [r for r in qs if r["synthetic"]]
    assert len(syn) == 12 == st.personal_synthetic
    assert all(r["kind"] == "personal" and len(r["gold"]) == 1 and r["id"].startswith("q-") for r in syn)
    assert all(r["synthetic"] is False for r in qs if r not in syn)
