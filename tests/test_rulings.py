import json

import pytest

from askmike.corpus.extract import FactsIndex, Skill
from askmike.corpus.rulings import PendingRulings, Ruling, apply_rulings, load_rulings
from askmike.corpus.unit import Number, SkillRef, Unit, validate
from askmike.gates.checks import GateContext, honesty


def _u(i="acme-robotics-role", claim="Mike built Widget Index at Acme Robotics.", **kw):
    base = dict(id=i, claim=claim, subject="Acme Robotics", kind="role", source="a.json", origin="extract")
    base.update(kw)
    return Unit(**base)


def test_pending_ruling_blocks():
    rs = [Ruling("R1", "q", ["a", "b"], None, []), Ruling("R2", "q", [], "a", [])]
    with pytest.raises(PendingRulings) as e:
        apply_rulings([_u()], [], rs)
    assert e.value.ids == ["R1"]


def test_allow_pending_skips_unanswered_and_applies_answered():
    rs = [
        Ruling("R1", "q", [], None, [{"op": "drop", "ids": ["acme-robotics-role"]}]),
        Ruling("R2", "q", [], "x", [{"op": "replace", "find": "Widget", "with": "Gadget"}]),
    ]
    units, _ = apply_rulings([_u()], [], rs, allow_pending=True)
    assert [u.id for u in units] == ["acme-robotics-role"]
    assert "Gadget Index" in units[0].claim


def test_set_depth_changes_ledger_and_regates_honesty():
    ledger = [Skill("Rust", "working")]
    u = _u(claim="Mike wrote Rust services at Acme Robotics.", skills=(SkillRef("Rust", "working"),))
    ctx = lambda l: GateContext(l, FactsIndex({}), [], lambda x: "")
    assert honesty(u, ctx(ledger)) == []
    rs = [Ruling("R1", "q", [], "a", [{"op": "set_depth", "term": "rust", "depth": "familiar"}])]
    units, new_ledger = apply_rulings([u], ledger, rs)
    assert new_ledger[0].depth == "familiar" and ledger[0].depth == "working"
    assert units[0].skills[0].depth == "familiar" and u.skills[0].depth == "working"
    assert honesty(units[0], ctx(new_ledger))


def test_replace_and_drop_and_add():
    a = _u("acme-one", "Mike shipped Widget Index.", detail="Widget Index details.")
    b = _u("acme-two", "Mike did other work.")
    new = {"id": "acme-three", "claim": "Mike runs Acme Robotics ops.", "subject": "Acme Robotics",
           "kind": "role", "source": "b.json", "origin": "draft",
           "skills": [{"term": "Rust", "depth": "working"}],
           "numbers": [{"value": "3", "source": "text"}]}
    rs = [Ruling("R1", "q", [], "a", [
        {"op": "replace", "find": "Widget Index", "with": "Gadget Hub"},
        {"op": "drop", "ids": ["acme-two"]},
        {"op": "add", "unit": new},
    ])]
    inputs = [a, b]
    units, _ = apply_rulings(inputs, [], rs)
    assert inputs == [a, b]
    assert [u.id for u in units] == ["acme-one", "acme-three"]
    assert units[0].claim == "Mike shipped Gadget Hub."
    assert units[0].detail == "Gadget Hub details."
    assert units[1].skills == (SkillRef("Rust", "working"),)
    assert units[1].numbers == (Number("3", "text"),)
    assert validate(units[1]) == []


def test_add_that_fails_validation_raises():
    rs = [Ruling("R1", "q", [], "a", [{"op": "add", "unit": {
        "id": "x", "claim": "Mike", "subject": "s", "kind": "role", "source": "a", "origin": "draft"}}])]
    with pytest.raises(ValueError):
        apply_rulings([], [], rs)


def test_unknown_op_raises():
    rs = [Ruling("R1", "q", [], "a", [{"op": "explode"}])]
    with pytest.raises(ValueError):
        apply_rulings([_u()], [], rs)


def test_load_rulings(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([{"id": "R1", "question": "q", "options": ["a"], "answer": None, "ops": []}]))
    assert load_rulings(p) == [Ruling("R1", "q", ["a"], None, [])]


def _bad(op, units=None):
    with pytest.raises(ValueError, match="ruling R9"):
        apply_rulings(units if units is not None else [_u()], [Skill("Rust", "working")],
                      [Ruling("R9", "q", [], "a", [op])])


def test_empty_find_raises():
    _bad({"op": "replace", "find": "", "with": "x"})


def test_replace_that_empties_claim_raises():
    _bad({"op": "replace", "find": "Mike built Widget Index at Acme Robotics.", "with": ""})


def test_malformed_ops_raise_value_error():
    _bad({"op": "replace", "find": "x"})
    _bad({"op": "drop"})
    _bad({"op": "set_depth", "term": "Rust"})
    _bad({"op": "add", "unit": {"id": "acme-new", "bogus": 1}})
    _bad({"op": "add"})


def test_bad_depth_raises():
    _bad({"op": "set_depth", "term": "Rust", "depth": "expert"})


def test_load_rulings_rejects_bad_files(tmp_path):
    p = tmp_path / "r.json"
    p.write_text('{"id": "R1"}')
    with pytest.raises(ValueError):
        load_rulings(p)
    p.write_text('[{"id": "R1"}]')
    with pytest.raises(ValueError):
        load_rulings(p)


def test_set_depth_that_matches_no_ledger_term_raises():
    with pytest.raises(ValueError, match="^ruling R9: set_depth matched no ledger term$"):
        apply_rulings([_u()], [Skill("Rust", "working")],
                      [Ruling("R9", "q", [], "a", [{"op": "set_depth", "term": "Zig", "depth": "working"}])])


def test_drop_that_matches_no_unit_raises_by_position_not_id():
    rs = [Ruling("R9", "q", [], "a", [{"op": "drop", "ids": ["acme-robotics-role", "acme-gone", "acme-also-gone"]}])]
    with pytest.raises(ValueError) as e:
        apply_rulings([_u()], [], rs)
    msg = str(e.value)
    assert msg == "ruling R9: drop matched no unit for 2 of 3 ids (positions 2, 3)"


def test_drop_twice_raises_on_the_second():
    rs = [Ruling("R1", "q", [], "a", [{"op": "drop", "ids": ["acme-robotics-role"]}]),
          Ruling("R2", "q", [], "a", [{"op": "drop", "ids": ["acme-robotics-role"]}])]
    with pytest.raises(ValueError, match="ruling R2: drop matched no unit"):
        apply_rulings([_u()], [], rs)


def test_add_with_an_existing_id_raises():
    new = {"id": "acme-robotics-role", "claim": "Mike runs Acme Robotics ops.", "subject": "Acme Robotics",
           "kind": "role", "source": "b.json", "origin": "draft"}
    with pytest.raises(ValueError, match="ruling R9: add would duplicate an existing unit id"):
        apply_rulings([_u()], [], [Ruling("R9", "q", [], "a", [{"op": "add", "unit": new}])])


def test_pipeline_refuses_duplicate_ids_across_extracted_and_drafted(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from askmike.corpus import __main__ as cli
    from askmike.corpus.unit import dump_jsonl, load_jsonl
    priv = tmp_path / "priv"
    shutil.copytree(Path(__file__).parent / "fixtures" / "raw", priv / "raw")
    (priv / "denylist.local.txt").write_text("zzqxinvented\n")
    (priv / "dontprint.local.txt").write_text("qqzinvented\n")
    (priv / "raw/jds/applied").mkdir(parents=True)
    (priv / "raw/jds/top50_index.json").write_text(json.dumps([{"company": "Gadget Holdings"}]))
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    assert cli.main(["extract"]) == 0
    work = priv / "work"
    dump_jsonl(load_jsonl(work / "extracted.jsonl")[:1], work / "drafted.jsonl")
    with pytest.raises(ValueError, match="duplicate unit id"):
        cli.main(["gate"])
