import json

import pytest

from askmike.corpus.draft import SourceDoc, build_prompt, draft_units, prose_docs
from askmike.corpus.extract import Skill
from askmike.corpus.unit import SkillRef

LEDGER = [Skill("Python", "strong", ("py",)), Skill("Rust", "familiar")]
DOC = SourceDoc("memory/widget.md", "Widget Index   is a search tool.\nMike built it in Python.", "Mike", "person")


def item(**kw):
    d = {"claim": "Mike built Widget Index.", "detail": "", "subject": "Widget Index", "kind": "project",
         "when": "2024", "quote": "Mike built it in Python.", "skills": ["py"]}
    d.update(kw)
    return d


def runner_for(text, calls=None):
    def run(prompt, model):
        if calls is not None:
            calls.append((prompt, model))
        return {"result": text}
    return run


def test_keeps_only_items_whose_quote_is_in_the_doc():
    items = [item(), item(claim="Mike did other.", quote="Mike built a rocket."),
             item(claim="Mike searched.", quote="Widget Index is a search tool."),
             item(claim="Mike x.", kind="bogus")]
    units, st = draft_units(DOC, LEDGER, runner_for(json.dumps(items)))
    assert [u.claim for u in units] == ["Mike built Widget Index.", "Mike searched."]
    assert (st.proposed, st.kept, st.dropped_no_quote, st.dropped_invalid, st.parse_error) == (4, 2, 1, 1, False)
    assert st.dropped_over_cap == 0
    u = units[0]
    assert u.origin == "draft" and u.source == "memory/widget.md" and u.id.startswith("dr-") and len(u.id) == 13
    assert u.skills == (SkillRef("Python", "strong"),) and u.quote == "Mike built it in Python."
    assert units[1].quote == "Widget Index is a search tool."


def test_prose_around_json_is_tolerated():
    units, st = draft_units(DOC, LEDGER, runner_for("Here you go:\n" + json.dumps([item()]) + "\nThanks"))
    assert len(units) == 1 and not st.parse_error


def test_malformed_json_is_counted_not_raised():
    units, st = draft_units(DOC, LEDGER, runner_for("[{oops"))
    assert units == [] and st.parse_error is True and st.doc_ref == DOC.ref


def test_cache_hit_skips_the_runner(tmp_path):
    calls = []
    r = runner_for(json.dumps([item()]), calls)
    a, _ = draft_units(DOC, LEDGER, r, cache_dir=tmp_path)
    b, _ = draft_units(DOC, LEDGER, r, cache_dir=tmp_path)
    assert len(calls) == 1 and a == b


def test_parse_error_cached_but_runner_error_not(tmp_path):
    calls = []
    draft_units(DOC, LEDGER, runner_for("nope", calls), cache_dir=tmp_path)
    _, st = draft_units(DOC, LEDGER, runner_for("nope", calls), cache_dir=tmp_path)
    assert len(calls) == 1 and st.parse_error

    def boom(prompt, model):
        raise RuntimeError("down")
    other = SourceDoc("memory/other.md", "text", "Mike", "person")
    with pytest.raises(RuntimeError):
        draft_units(other, LEDGER, boom, cache_dir=tmp_path)
    assert len(list(tmp_path.iterdir())) == 1


def test_build_prompt_contains_doc_and_hints():
    p = build_prompt(DOC)
    assert "Mike built it in Python." in p and "memory/widget.md" in p


def test_prose_docs_skips_drafts_and_never_reads_rules(tmp_path):
    raw = tmp_path
    for d in ("memory", "stories", "projects", "writeups", "profile", "answers", "rules", "tools", "jds"):
        (raw / d).mkdir()
    (raw / "memory/a.md").write_text("Memory a")
    (raw / "stories/acme__interview_prep.md").write_text("Story")
    (raw / "projects/widget-index__docs__PRD.md").write_text("PRD")
    (raw / "projects/widget-index__facts.json").write_text("{}")
    (raw / "writeups/note.md").write_text("Note")
    (raw / "writeups/portfolio.html").write_text("<p>Hello <b>there</b></p>")
    (raw / "writeups/other.py").write_text("x")
    (raw / "writeups/facts_extra.json").write_text("{}")
    (raw / "writeups/build_cover_letter.py").write_text("# letter")
    (raw / "profile/profile.json").write_text(json.dumps({"narrative": {"arc": "An arc", "empty": ""}}))
    (raw / "answers/answers.json").write_text(json.dumps({"drafted": {"ok": "Fine", "bad": "DRAFT: x", "n": 3}}))
    (raw / "answers/apply_db_x.json").write_text("{}")
    (raw / "rules/r.md").write_text("RULE"); (raw / "tools/t.md").write_text("T"); (raw / "jds/j.md").write_text("J")
    docs = {d.ref: d for d in prose_docs(raw)}
    assert set(docs) == {
        "memory/a.md", "stories/acme__interview_prep.md", "projects/widget-index__docs__PRD.md",
        "writeups/note.md", "writeups/portfolio.html", "writeups/build_cover_letter.py",
        "profile/profile.json#narrative/arc", "answers/answers.json#drafted/ok"}
    assert docs["writeups/portfolio.html"].text == "Hello there"
    p = docs["projects/widget-index__docs__PRD.md"]
    assert (p.subject_hint, p.kind_hint) == ("widget-index", "project")
    assert docs["memory/a.md"].subject_hint == "Mike" and docs["memory/a.md"].kind_hint == "person"
    assert docs["writeups/note.md"].kind_hint == "project"


def test_duplicate_claim_is_dropped_and_counted():
    items = [item(), item(claim="  Mike built Widget Index.  ")]
    units, st = draft_units(DOC, LEDGER, runner_for(json.dumps(items)))
    assert len(units) == 1 and st.dropped_invalid == 1 and st.proposed == 2


def test_cap_keeps_first_six_in_model_order():
    items = [item(claim=f"Mike did thing {i}.") for i in range(8)]
    units, st = draft_units(DOC, LEDGER, runner_for(json.dumps(items)))
    assert [u.claim for u in units] == [f"Mike did thing {i}." for i in range(6)]
    assert st.kept == 6 and st.dropped_over_cap == 2
