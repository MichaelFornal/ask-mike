import dataclasses

import pytest

from askmike.corpus.unit import (
    Number,
    SkillRef,
    Unit,
    content_hash,
    dump_jsonl,
    load_jsonl,
    validate,
)


def make(**kw):
    base = dict(
        id="widget-index-launch",
        claim="Mike built the Widget Index for Acme Robotics.",
        subject="Widget Index",
        kind="project",
        source="profile/profile.json#roles/0/evidence/widget-index",
        origin="extract",
        detail="Shipped in 2024 é.",
        when="2024-06..present",
        skills=(SkillRef("Python", "strong"),),
        numbers=(Number("3", "text"), Number("40%", "fact:kpis.json:uplift")),
        quote="built it",
    )
    base.update(kw)
    return Unit(**base)


def test_roundtrip_is_byte_stable(tmp_path):
    units = [make(id="zeta-unit"), make(id="alpha-unit")]
    p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    dump_jsonl(units, p1)
    loaded = load_jsonl(p1)
    assert [u.id for u in loaded] == ["alpha-unit", "zeta-unit"]
    assert sorted(loaded, key=lambda u: u.id) == sorted(units, key=lambda u: u.id)
    dump_jsonl(loaded, p2)
    assert p1.read_bytes() == p2.read_bytes()
    assert "é".encode() in p1.read_bytes()
    assert len(p1.read_text().splitlines()) == 2


def test_validate_accepts_good_unit():
    assert validate(make()) == []
    assert validate(make(when="")) == []
    assert validate(make(when="..present")) == []


@pytest.mark.parametrize(
    "kw",
    [
        dict(id="Bad Id"),
        dict(kind="job"),
        dict(skills=(SkillRef("Python", "expert"),)),
        dict(when="2024/06"),
    ],
)
def test_validate_rejects_bad_id_kind_depth_when(kw):
    assert validate(make(**kw))


def test_validate_rejects_empty_claim_and_source():
    assert validate(make(claim=""))
    assert validate(make(source=""))


def test_content_hash_ignores_source_and_quote_but_not_claim():
    a = make()
    assert content_hash(a) == content_hash(
        dataclasses.replace(a, source="x#y", quote="other", id="other-id")
    )
    assert content_hash(a) != content_hash(dataclasses.replace(a, claim="Mike did more."))
    assert content_hash(a) != content_hash(dataclasses.replace(a, detail="x"))
    assert content_hash(a) != content_hash(dataclasses.replace(a, skills=()))
    assert content_hash(a) != content_hash(dataclasses.replace(a, numbers=()))


def test_roundtrip_with_unicode_line_separators(tmp_path):
    u = make(claim="Mike built a\u2028b\u2029c\u0085d.", detail="x\u2028y")
    p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    dump_jsonl([u], p1)
    assert len(p1.read_bytes().split(b"\n")) == 2
    loaded = load_jsonl(p1)
    assert loaded == [u]
    dump_jsonl(loaded, p2)
    assert p1.read_bytes() == p2.read_bytes()
