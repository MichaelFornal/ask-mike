import hashlib
import json
from pathlib import Path

import pytest

from askmike.corpus.export import ExportRefused, export_corpus, public_source
from askmike.corpus.extract import FactsIndex, Skill
from askmike.corpus.review import Decision
from askmike.corpus.rulings import Ruling
from askmike.corpus.unit import Number, SkillRef, Unit, content_hash
from askmike.gates.checks import CompanyPattern, GateContext

REPOS = ["builds/meter-to-cash", "agent-census"]


def mk(i, claim=None, **kw):
    d = dict(id=i, claim=claim or f"Mike built the {i} tool in Python.", subject="Widget Index", kind="project",
             source="stories/s1.md", origin="draft", detail="", when="2025-06",
             skills=(SkillRef("Python", "strong"),), numbers=(), quote="a private quote")
    d.update(kw)
    return Unit(**d)


def ctx(patterns=(), text="built it"):
    return GateContext([Skill("Python", "strong"), Skill("Rust", "none")], FactsIndex({}), list(patterns),
                       lambda u: text)


def ok(u, claim=None, detail=None):
    return Decision("approve", claim, detail, content_hash(u))


LEDGER = [Skill("Python", "strong", ("py",)), Skill("Go", "working")]


def run(units, decisions, tmp_path, rulings=(), c=None):
    return export_corpus(units, decisions, LEDGER, list(rulings), c or ctx(), tmp_path, REPOS)


def test_export_refuses_pending_unit_and_pending_ruling(tmp_path):
    a, b = mk("ev-a"), mk("ev-b")
    r = Ruling("R1", "q?", ["x"], None)
    with pytest.raises(ExportRefused) as e:
        run([a, b], {"ev-a": ok(a), "ev-b": Decision("pending", None, None, content_hash(b))}, tmp_path, [r])
    text = "\n".join(e.value.reasons)
    assert "R1" in text and "ev-b" in text and "ev-a" not in text
    assert not (tmp_path / "corpus").exists()


def test_export_refuses_stale_decision_hash(tmp_path):
    a = mk("ev-a")
    with pytest.raises(ExportRefused) as e:
        run([a], {"ev-a": Decision("approve", None, None, "0" * 64)}, tmp_path)
    assert "ev-a" in e.value.reasons[0]


def test_export_regates_edited_claim_with_em_dash_or_none_skill(tmp_path):
    a, b = mk("ev-a"), mk("ev-b")
    with pytest.raises(ExportRefused) as e:
        run([a, b], {"ev-a": ok(a, claim="Mike built it — fast."),
                     "ev-b": ok(b, claim="Mike wrote Rust services.")}, tmp_path)
    text = "\n".join(e.value.reasons)
    assert "ev-a" in text and "slop" in text and "ev-b" in text and "honesty" in text
    assert "Rust" not in text and "—" not in text
    assert not (tmp_path / "corpus").exists()


def test_export_regrounds_numbers_after_edit(tmp_path):
    a = mk("ev-a", numbers=(Number("9", "text"),))
    out = run([a], {"ev-a": ok(a, claim="Mike built the tool in Python.")}, tmp_path)
    assert out.units == 1
    rec = json.loads((tmp_path / "corpus/units.jsonl").read_text())
    assert rec["numbers"] == []
    b = mk("ev-b")
    with pytest.raises(ExportRefused):
        run([b], {"ev-b": ok(b, claim="Mike built 4821 things in Python.")}, tmp_path)


def test_export_drops_quote_and_maps_story_source_to_notes_hash(tmp_path):
    a = mk("ev-a", source="stories/s1.md")
    rep = run([a], {"ev-a": ok(a)}, tmp_path)
    line = (tmp_path / "corpus/units.jsonl").read_bytes().decode()
    assert line.endswith("\n")
    rec = json.loads(line)
    assert "quote" not in rec and "private quote" not in line
    assert rec["source"] == "notes:" + hashlib.sha256(b"stories/s1.md").hexdigest()[:8]
    assert set(rec) == {"id", "claim", "detail", "subject", "kind", "when", "skills", "numbers", "source", "origin"}
    assert rec["skills"] == [{"term": "Python", "depth": "strong"}]
    assert (rep.units, rep.rejected, rep.by_kind, rep.by_origin) == (1, 0, {"project": 1}, {"draft": 1})


def test_export_applies_edits_and_sorts_and_counts_rejected(tmp_path):
    a, b, c = mk("ev-c"), mk("ev-a"), mk("ev-b")
    dec = {"ev-c": ok(a, claim="Mike shipped the edited thing."), "ev-a": ok(b),
           "ev-b": Decision("reject", None, None, content_hash(c))}
    rep = run([a, b, c], dec, tmp_path)
    recs = [json.loads(x) for x in (tmp_path / "corpus/units.jsonl").read_text().splitlines()]
    assert [r["id"] for r in recs] == ["ev-a", "ev-c"] and recs[1]["claim"] == "Mike shipped the edited thing."
    assert rep.rejected == 1 and rep.units == 2


def test_export_refuses_applied_company_name_in_claim(tmp_path):
    a = mk("ev-a")
    c = ctx([CompanyPattern("Gadget Holdings")])
    with pytest.raises(ExportRefused) as e:
        run([a], {"ev-a": ok(a, claim="Mike pitched Gadget Holdings on Python.")}, tmp_path, c=c)
    assert "Gadget" not in "\n".join(e.value.reasons)
    assert not (tmp_path / "corpus").exists()


def test_export_private_pattern_hit_in_other_fields_names_field(tmp_path):
    a = mk("ev-a", subject="Zzqx Corp")
    c = ctx([__import__("re").compile("zzqx", __import__("re").I)])
    with pytest.raises(ExportRefused) as e:
        run([a], {"ev-a": ok(a)}, tmp_path, c=c)
    r = "\n".join(e.value.reasons)
    assert "ev-a" in r and "subject" in r and "Zzqx" not in r


def test_skills_json_has_no_private_fields(tmp_path):
    a = mk("ev-a")
    run([a], {"ev-a": ok(a)}, tmp_path)
    raw = (tmp_path / "corpus/skills.json").read_text()
    data = json.loads(raw)
    assert data == [{"term": "Go", "depth": "working", "aliases": []},
                    {"term": "Python", "depth": "strong", "aliases": ["py"]}]
    assert raw.endswith("\n")


def test_export_is_deterministic(tmp_path):
    a = mk("ev-a")
    run([a], {"ev-a": ok(a)}, tmp_path)
    one = (tmp_path / "corpus/units.jsonl").read_bytes()
    run([a], {"ev-a": ok(a)}, tmp_path)
    assert (tmp_path / "corpus/units.jsonl").read_bytes() == one


def test_public_source_mappings():
    h = lambda s: "notes:" + hashlib.sha256(s.encode()).hexdigest()[:8]
    ps = lambda s: public_source(mk("ev-a", source=s), ["groupdir/myrepo", "groupdir"])
    assert ps("profile/profile.json#roles/0/evidence/ev-one") == "evidence:ev-one"
    assert ps("profile/profile.json#narrative/x") == h("profile/profile.json#narrative/x")
    assert ps("memory/m.md") == h("memory/m.md")
    assert ps("projects/groupdir__myrepo__src__facts.json") == "project:myrepo/src/facts.json"
    assert ps("projects/other__x.md") == h("projects/other__x.md")


def test_public_source_never_contains_grouping_folder():
    for s in ("projects/groupdir__myrepo__README.md", "projects/groupdir__myrepo__a__b.json#x"):
        out = public_source(mk("ev-a", source=s), ["groupdir/myrepo"])
        assert out.startswith("project:myrepo/") and "groupdir" not in out


def test_public_source_resume_uses_entry_id(tmp_path, monkeypatch):
    raw = tmp_path / "raw/resume"
    raw.mkdir(parents=True)
    (raw / "master.json").write_text(json.dumps({"experience": [{"id": "acme", "title": "T"}, {"title": "U"}]}))
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path))
    assert public_source(mk("a-b", source="resume/master.json#experience/0"), []) == "resume:acme"
    assert public_source(mk("a-b", source="resume/master.json#experience/1"), []) == "resume:experience/1"


def test_fact_number_sources_hide_grouping_folder(tmp_path):
    a = mk("ev-a", numbers=(Number("9", "fact:projects/groupdir__myrepo__facts.json:k"),))
    c = GateContext(LEDGER, FactsIndex({"fact:projects/groupdir__myrepo__facts.json:k": {"9"}}), [],
                    lambda u: "none")
    export_corpus([a], {"ev-a": ok(a, claim="Mike ran 9 Python jobs.")}, LEDGER, [], c, tmp_path, ["groupdir/myrepo"])
    rec = json.loads((tmp_path / "corpus/units.jsonl").read_text())
    assert "groupdir" not in json.dumps(rec) and rec["numbers"][0]["source"].startswith("fact:")


def test_refusal_prints_hash_for_id_matching_private_pattern(tmp_path):
    import re
    a, b = mk("ev-zzqxid"), mk("ev-b")
    c = ctx([re.compile("zzqx", re.I)])
    with pytest.raises(ExportRefused) as e:
        run([a, b], {"ev-zzqxid": Decision("pending", None, None, content_hash(a)), "ev-b": ok(b)}, tmp_path, c=c)
    text = "\n".join(e.value.reasons)
    assert "zzqx" not in text and "#" + hashlib.sha256(b"ev-zzqxid").hexdigest()[:8] in text


def test_export_refuses_grouping_folder_subject(tmp_path):
    a = mk("ev-a", subject="groupdir")
    with pytest.raises(ExportRefused) as e:
        export_corpus([a], {"ev-a": ok(a)}, LEDGER, [], ctx(), tmp_path, ["groupdir/myrepo"])
    assert e.value.reasons == ["unit ev-a: grouping folder in subject"]
    b = mk("ev-b", subject="myrepo")
    export_corpus([b], {"ev-b": ok(b)}, LEDGER, [], ctx(), tmp_path, ["groupdir/myrepo"])


def test_slug_id_with_company_name_is_hashed_and_refused(tmp_path):
    from askmike.corpus.export import safe_id
    pat = CompanyPattern("Gadget Holdings")
    assert safe_id("role-gadget-holdings", [pat]) == "#" + hashlib.sha256(b"role-gadget-holdings").hexdigest()[:8]
    a = mk("role-gadget-holdings")
    with pytest.raises(ExportRefused) as e:
        run([a], {a.id: ok(a)}, tmp_path, c=ctx([pat]))
    assert "gadget" not in "\n".join(e.value.reasons) and any("in id" in r for r in e.value.reasons)


def test_lowercase_company_slug_in_source_label_is_refused(tmp_path):
    pat = CompanyPattern("Gadget Holdings")
    a = mk("ev-a", source="projects/groupdir__gadget-holdings__README.md")
    assert public_source(a, ["groupdir/gadget-holdings"]) == "project:gadget-holdings/README.md"
    with pytest.raises(ExportRefused) as e:
        export_corpus([a], {"ev-a": ok(a)}, LEDGER, [], ctx([pat]), tmp_path, ["groupdir/gadget-holdings"])
    assert e.value.reasons == ["unit ev-a: private pattern in source"]
    assert not (tmp_path / "corpus").exists()


def test_lowercase_company_slug_in_number_source_is_refused(tmp_path):
    pat = CompanyPattern("Gadget Holdings")
    a = mk("ev-a", numbers=(Number("9", "fact:projects/groupdir__gadget-holdings__facts.json:k"),))
    c = GateContext(LEDGER, FactsIndex({"fact:projects/groupdir__gadget-holdings__facts.json:k": {"9"}}), [pat],
                    lambda u: "none")
    with pytest.raises(ExportRefused) as e:
        export_corpus([a], {"ev-a": ok(a, claim="Mike ran 9 Python jobs.")}, LEDGER, [], c, tmp_path,
                      ["groupdir/gadget-holdings"])
    assert e.value.reasons == ["unit ev-a: private pattern in numbers.source"]


def test_lowercase_company_word_in_prose_is_still_ignored(tmp_path):
    a = mk("ev-a", detail="Mike sorted the gadget holdings bin.")
    run([a], {"ev-a": ok(a)}, tmp_path, c=ctx([CompanyPattern("Gadget Holdings")]))


def test_company_pattern_search_any_case():
    pat = CompanyPattern("Gadget Holdings")
    assert pat.search("gadget-holdings") is None and pat.search_any_case("gadget-holdings")
    assert pat.search_any_case("Gadget Holdings") and not pat.search_any_case("gadgets")


def test_blanked_claim_decision_is_refused_as_structure(tmp_path):
    a = mk("ev-a")
    with pytest.raises(ExportRefused) as e:
        run([a], {"ev-a": ok(a, claim="")}, tmp_path)
    assert "unit ev-a: structure" in e.value.reasons


def test_report_lists_distinct_sorted_source_labels_and_skill_rows(tmp_path):
    a, b, c = mk("ev-a", source="stories/s1.md"), mk("ev-b", source="stories/s1.md"), \
        mk("ev-c", source="profile/profile.json#roles/0/evidence/ev-one")
    rep = run([a, b, c], {"ev-a": ok(a), "ev-b": ok(b), "ev-c": ok(c)}, tmp_path)
    assert rep.sources == ("evidence:ev-one", "notes:" + hashlib.sha256(b"stories/s1.md").hexdigest()[:8])
    assert rep.skills == 2
