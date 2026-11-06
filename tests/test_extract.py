import json
from pathlib import Path

from askmike.corpus.extract import (
    FactsIndex, Skill, extract_evidence_units, extract_fact_units,
    load_ledger, number_forms,
)
from askmike.corpus.sources import doc_text, source_text, strip_tags
from askmike.corpus.unit import SkillRef, validate

RAW = Path(__file__).parent / "fixtures" / "raw"


def _profile():
    return json.loads((RAW / "profile/profile.json").read_text())


def _resume():
    return json.loads((RAW / "resume/master.json").read_text())


def _units():
    p = _profile()
    return extract_evidence_units(p, load_ledger(RAW)) + extract_fact_units(p, _resume())


def test_evidence_unit_is_third_person_with_mapped_skills():
    u = {x.id: x for x in _units()}["ev-acme-queue"]
    assert u.claim.startswith("Mike built")
    assert u.skills == (SkillRef("Python", "strong"),)
    assert u.kind == "role" and u.subject == "Acme Robotics" and u.origin == "extract"
    assert u.when == "2021-03..present"
    assert u.detail == "Replaced cron scripts with a queue worker. 1,200 jobs a day"


def test_claim_keeps_uppercase_acronym():
    u = {x.id: x for x in _units()}["ev-acme-ai"]
    assert u.claim.startswith("Mike AI-assisted")
    assert u.skills == (SkillRef("Kubernetes", "familiar"),)


def test_project_evidence_unit():
    u = {x.id: x for x in _units()}["ev-widget-search"]
    assert u.kind == "project" and u.subject == "Widget Index"
    assert u.skills == (SkillRef("Rust", "none"),)  # mapped as-is; gates refuse to assert none
    assert u.source == "profile/profile.json#projects/0/evidence/widget-search"


def test_role_unit_prefers_resume_title_and_dates():
    u = {x.id: x for x in _units()}["role-acme-robotics"]
    assert u.claim.startswith("Mike was Senior Software Engineer at")
    assert u.when == "2021-04..present"
    assert u.source == "resume/master.json#experience/0"
    e = {x.id: x for x in _units()}["edu-example-state-university"]
    assert e.kind == "education" and e.source == "profile/profile.json#education/0"


def test_role_unit_falls_back_to_profile():
    r = {"experience": []}
    u = {x.id: x for x in extract_fact_units(_profile(), r)}["role-acme-robotics"]
    assert "Software Engineer" in u.claim and u.source == "profile/profile.json#roles/0"


def test_all_units_validate():
    us = _units()
    assert len(us) == 5
    for u in us:
        assert validate(u) == [], u.id


def test_facts_index_reads_all_three_shapes():
    ix = FactsIndex.load(RAW)
    assert "fact:site/facts.json:widgets_indexed" in ix.match("7.67M")
    assert "fact:projects/builds__meter-to-cash__facts.json:invoices_total" in ix.match("1,500")
    assert "fact:projects/agent-census__site__src__data__facts.json:agents_counted" in ix.match("812")
    assert "fact:projects/deal-terms-desk__facts.json:clauses_tagged" in ix.match("96")
    assert ix.match("123456") == []


def test_number_forms_truncate_never_round_up():
    f = number_forms(7_675_000)
    assert "7.68M" not in f and "7.67M" in f and "7.6M" in f
    g = number_forms(7_672_104)
    assert {"7672104", "7,672,104", "7.67M", "7.6M"} <= g
    assert "12.5K" in number_forms(12_500)


def test_doc_text_rules():
    t = doc_text(RAW, "profile/profile.json#roles/0/evidence/acme-queue")
    assert t.split("\n")[0].startswith("Built a job queue") and "queue worker" in t and "1,200 jobs" in t
    assert doc_text(RAW, "profile/profile.json#roles/0/company") == "Acme Robotics"
    assert json.loads(doc_text(RAW, "resume/master.json#experience/0/bullets")) == [{"id": "b1", "text": "Built things."}]
    assert strip_tags("<p>a  <b>b</b>\n c</p><script>x</script>") .split() [:3] == ["a", "b", "c"]
    u = _units()[0]
    assert source_text(RAW, u) == doc_text(RAW, u.source)


def test_two_roles_at_one_company_get_own_entries_and_ids():
    p = {"roles": [
        {"company": "Acme Robotics", "title": "Engineer", "start": "2019-01", "end": "2021-02", "evidence": []},
        {"company": "Acme Robotics", "title": "Lead", "start": "2021-03", "end": "present", "evidence": []},
    ], "education": []}
    r = {"experience": [
        {"company": "Acme Robotics", "title": "Staff Engineer", "start": "Mar 2021", "end": "Present"},
        {"company": "Acme Robotics", "title": "Software Engineer", "start": "Jan 2019", "end": "Feb 2021"},
    ]}
    us = extract_fact_units(p, r)
    assert [u.id for u in us] == ["role-acme-robotics-2019", "role-acme-robotics-2021"]
    assert us[0].claim.startswith("Mike was Software Engineer") and us[0].source.endswith("experience/1")
    assert us[1].claim.startswith("Mike was Staff Engineer") and us[1].source.endswith("experience/0")
    assert all(validate(u) == [] for u in us)


def test_duplicate_ids_raise():
    import pytest
    p = {"roles": [], "education": [
        {"school": "Example U", "credential": "BS", "field": "X"},
        {"school": "Example U", "credential": "MS", "field": "Y"}]}
    with pytest.raises(ValueError):
        extract_fact_units(p, {})


def test_doc_text_refuses_paths_outside_raw(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        doc_text(RAW, "../fixtures/raw/../../test_extract.py")
    with pytest.raises(ValueError):
        doc_text(RAW, str(tmp_path / "x.txt"))
