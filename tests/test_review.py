import json

from askmike.corpus import __main__ as cli
from askmike.corpus.review import Decision, merge_decisions, parse_review, render_review
from askmike.corpus.unit import Number, SkillRef, Unit, content_hash, dump_jsonl
from askmike.gates.checks import Finding


def mk(i, subject="Widget Index", origin="extract", claim=None, detail="Built the scale test."):
    return Unit(
        id=i, claim=claim or f"Mike built {i}.", subject=subject, kind="project",
        source=f"private/{i}.md", origin=origin, detail=detail, when="2025-06..2025-09",
        skills=(SkillRef("Python", "strong"),), numbers=(Number("9", "text"),),
    )


def tick(text, uid, which):
    box = "- [ ] approve   - [ ] reject"
    done = {"approve": "- [x] approve   - [ ] reject", "reject": "- [ ] approve   - [X] reject"}[which]
    head = f"## {uid}\n{box}"
    assert head in text
    return text.replace(head, f"## {uid}\n{done}")


def test_render_then_parse_roundtrip_with_one_approval_and_one_edit():
    a, b, c = mk("ev-a"), mk("ev-b"), mk("ev-c", detail="")
    text = render_review([a, b, c], [Finding("slop", "ev-b", "hype word")], {"ev-a": ["ev-b"]}, {})
    assert "FAIL slop: hype word" in text and "similar: ev-b" in text and "source: private/ev-a.md" in text
    text = tick(text, "ev-a", "approve")
    text = tick(text, "ev-b", "reject")
    text = text.replace("claim: Mike built ev-a.", "claim: Mike built ev-a, line one\\nline two.")
    dec, errs, _ = parse_review(text, [a, b, c])
    assert errs == []
    assert dec["ev-a"] == Decision("approve", "Mike built ev-a, line one\nline two.", None, content_hash(a))
    assert dec["ev-b"] == Decision("reject", None, None, content_hash(b))
    assert dec["ev-c"].status == "pending"
    # re-render shows the edit with a separate marker line
    again = render_review([a, b, c], [], {}, dec)
    assert "edited: claim\n" in again and "claim: Mike built ev-a, line one\\nline two." in again
    assert "[x] approve" in again
    dec2, errs2, _ = parse_review(again, [a, b, c])
    assert errs2 == [] and dec2 == dec


def test_edit_back_to_original_clears_override():
    a = mk("ev-a")
    dec = {"ev-a": Decision("approve", "Mike changed it.", "New detail.", content_hash(a))}
    text = render_review([a], [], {}, dec)
    assert "edited: claim, detail" in text
    text = text.replace("Mike changed it.", a.claim).replace("New detail.", a.detail)
    out, errs, _ = parse_review(text, [a])
    assert errs == [] and out["ev-a"].claim is None and out["ev-a"].detail is None


def test_missing_status_line_is_a_line_numbered_error_and_no_decision():
    a = mk("ev-a")
    text = "# hi\n\n## ev-a\nclaim: Mike built ev-a.\ndetail: x\n"
    dec, errs, _ = parse_review(text, [a])
    assert dec == {} and errs == ["line 3: block ev-a has no status line"]


def test_both_boxes_ticked_is_an_error():
    a = mk("ev-a")
    text = "## ev-a\n- [x] approve   - [x] reject\nclaim: Mike built ev-a.\ndetail: Built the scale test.\n"
    dec, errs, _ = parse_review(text, [a])
    assert dec == {} and len(errs) == 1 and errs[0].startswith("line 2:") and "ev-a" in errs[0]


def test_unknown_id_is_a_warning_and_duplicate_block_is_an_error():
    a = mk("ev-a")
    blk = "## ev-a\n- [x] approve   - [ ] reject\nclaim: Mike built ev-a.\ndetail: Built the scale test.\n"
    ghost = "## ev-ghost\n- [x] approve   - [ ] reject\nclaim: x\ndetail: \nstray wrapped text\n"
    text = blk + ghost + blk + ghost
    dec, errs, warns = parse_review(text, [a])
    assert dec == {}
    assert errs == ["line 10: duplicate block ev-a"]
    assert warns == ["line 5: block ev-ghost is not a known unit id; ignored",
                     "line 14: block ev-ghost is not a known unit id; ignored"]


def test_changed_unit_content_makes_old_approval_pending():
    a = mk("ev-a")
    old = {"ev-a": Decision("approve", "Mike edited.", None, content_hash(a)),
           "ev-gone": Decision("approve", None, None, "h")}
    changed = mk("ev-a", detail="Different detail.")
    out = merge_decisions([changed], old, {})
    assert out == {"ev-a": Decision("pending", None, None, content_hash(a))}
    same = merge_decisions([a], old, {})
    assert same["ev-a"].status == "approve" and same["ev-a"].claim == "Mike edited." and "ev-gone" not in same


def test_sheet_older_than_unit_goes_pending_via_rev():
    a = mk("ev-a")
    text = tick(render_review([a], [], {}, {}), "ev-a", "approve")
    changed = mk("ev-a", detail="Different detail.")
    dec, errs, _ = parse_review(text, [changed])
    assert errs == []
    assert merge_decisions([changed], {}, dec)["ev-a"].status == "pending"


def test_order_subject_findings_origin_id():
    us = [mk("ev-d", "B", "draft"), mk("ev-c", "B", "extract"), mk("ev-b", "A", "draft"),
          mk("ev-a", "A", "extract"), mk("ev-e", "B", "draft")]
    text = render_review(us, [Finding("slop", "ev-e", "m")], {}, {})
    ids = [l[3:] for l in text.splitlines() if l.startswith("## ")]
    assert ids == ["ev-a", "ev-b", "ev-e", "ev-c", "ev-d"]


def test_cli_review_then_ingest(tmp_path, monkeypatch, capsys):
    priv = tmp_path / "p"
    work = priv / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    a, b = mk("ev-a"), mk("ev-b")
    dump_jsonl([a, b], work / "proposed.jsonl")
    (work / "findings.json").write_text(json.dumps([{"gate": "slop", "unit_id": "ev-b", "message": "m"}]))
    (work / "dupes.json").write_text("{}")
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert "2 units" in out and "1 with findings" in out and "0 already decided" in out
    sheet = work / "review.md"
    sheet.write_text(tick(sheet.read_text(), "ev-a", "approve"))
    assert cli.main(["ingest"]) == 0
    assert "approved 1" in capsys.readouterr().out
    d = json.loads((work / "decisions.json").read_text())
    assert list(d) == ["ev-a", "ev-b"] and d["ev-a"]["status"] == "approve" and d["ev-a"]["claim"] is None
    assert cli.main(["review"]) == 0
    assert "1 already decided" in capsys.readouterr().out
    sheet.write_text(sheet.read_text().replace("- [ ] approve   - [ ] reject\nclaim: Mike built ev-b",
                                               "- [x] approve   - [x] reject\nclaim: Mike built ev-b"))
    assert cli.main(["ingest"]) == 1
    out = capsys.readouterr().out
    assert "error 1" in out and "Mike built" not in out


def test_unicode_separators_round_trip_without_an_edit():
    claim = "Mike built a b c\x85d\re\x0bf\x0cg\x1ch and a literal \\u2028 and \\n."
    a = mk("ev-a", claim=claim, detail="x\r\ny")
    text = render_review([a], [], {}, {})
    assert len([l for l in text.split("\n") if l.startswith("claim:")]) == 1
    for c in "\r\x0b\x0c\x1c\x85  ":
        assert c not in text.replace("\r\n", "")
    dec, errs, _ = parse_review(tick(text, "ev-a", "approve"), [a])
    assert errs == [] and dec["ev-a"].claim is None and dec["ev-a"].detail is None and dec["ev-a"].status == "approve"
    crlf, errs, _ = parse_review(tick(text, "ev-a", "approve").replace("\n", "\r\n"), [a])
    assert errs == [] and crlf == dec


def test_missing_or_empty_rev_is_an_error():
    a = mk("ev-a")
    text = tick(render_review([a], [], {}, {}), "ev-a", "approve")
    no_rev = "\n".join(l for l in text.split("\n") if not l.startswith("rev:"))
    for t in (no_rev, text.replace("rev: ", "rev:", 1).split("rev:")[0] + "rev:\n"):
        dec, errs, _ = parse_review(t, [a])
        assert dec == {} and len(errs) == 1 and errs[0].endswith("block ev-a has no rev line")


def test_review_keeps_uningested_edits_and_refuses_on_errors(tmp_path, monkeypatch, capsys):
    priv = tmp_path / "p"
    work = priv / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    a, b = mk("ev-a"), mk("ev-b")
    dump_jsonl([a, b], work / "proposed.jsonl")
    (work / "findings.json").write_text("[]")
    (work / "dupes.json").write_text("{}")
    assert cli.main(["review"]) == 0
    sheet = work / "review.md"
    sheet.write_text(tick(sheet.read_text(), "ev-a", "approve").replace("claim: Mike built ev-a.", "claim: Mike edited ev-a."))
    assert cli.main(["review"]) == 0
    assert "[x] approve" in sheet.read_text() and "claim: Mike edited ev-a." in sheet.read_text()
    d = json.loads((work / "decisions.json").read_text())
    assert d["ev-a"]["status"] == "approve" and d["ev-a"]["claim"] == "Mike edited ev-a."
    broken = sheet.read_text().replace("- [x] approve   - [ ] reject", "- [x] approve   - [x] reject")
    sheet.write_text(broken)
    capsys.readouterr()
    assert cli.main(["review"]) == 1
    assert "line " in capsys.readouterr().out and sheet.read_text() == broken


def _line_no(text, needle):
    return next(n for n, l in enumerate(text.split("\n"), 1) if l == needle)


def test_blanked_detail_round_trips_through_ingest_render_and_export(tmp_path, monkeypatch, capsys):
    from askmike.corpus.export import export_corpus
    from askmike.corpus.extract import FactsIndex, Skill
    from askmike.gates.checks import GateContext
    priv = tmp_path / "p"
    work = priv / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    a, b = mk("ev-a"), mk("ev-b")
    dump_jsonl([a, b], work / "proposed.jsonl")
    (work / "findings.json").write_text("[]")
    (work / "dupes.json").write_text("{}")
    assert cli.main(["review"]) == 0
    sheet = work / "review.md"
    sheet.write_text(tick(sheet.read_text(), "ev-a", "approve").replace(
        "detail: Built the scale test.\nsubject: Widget Index", "detail:\nsubject: Widget Index", 1))
    assert cli.main(["ingest"]) == 0
    d = json.loads((work / "decisions.json").read_text())
    assert d["ev-a"]["detail"] == "" and d["ev-a"]["claim"] is None
    assert d["ev-b"]["detail"] is None and d["ev-b"]["claim"] is None
    assert cli.main(["review"]) == 0
    text = sheet.read_text()
    blk = text.split("## ev-a\n", 1)[1].split("## ", 1)[0]
    assert "edited: detail\n" in blk and "\ndetail: \n" in blk and "Built the scale test." not in blk
    decisions = cli._load_decisions(work / "decisions.json")
    ctx = GateContext([Skill("Python", "strong")], FactsIndex({}), [], lambda u: "built it")
    export_corpus([a], {"ev-a": decisions["ev-a"]}, [Skill("Python", "strong")], [], ctx, tmp_path / "out", [])
    rec = json.loads((tmp_path / "out/corpus/units.jsonl").read_text())
    assert rec["detail"] == "" and rec["claim"] == a.claim


def test_blank_detail_on_a_unit_without_detail_is_untouched():
    c = mk("ev-c", detail="")
    dec, errs, _ = parse_review(tick(render_review([c], [], {}, {}), "ev-c", "approve"), [c])
    assert errs == [] and dec["ev-c"].detail is None


def test_blanked_claim_is_a_line_numbered_error():
    a = mk("ev-a")
    text = tick(render_review([a], [], {}, {}), "ev-a", "approve").replace("claim: Mike built ev-a.", "claim:   ")
    dec, errs, _ = parse_review(text, [a])
    n = _line_no(text, "claim:   ")
    assert dec == {} and errs == [f"line {n}: block ev-a has an empty claim; a unit needs a claim, so tick reject instead"]


def test_legacy_decisions_file_reads_empty_strings_as_untouched(tmp_path):
    a = mk("ev-a")
    p = tmp_path / "decisions.json"
    p.write_text(json.dumps({"ev-a": {"status": "pending", "claim": "", "detail": "", "hash": content_hash(a)},
                             "ev-b": {"status": "approve", "claim": None, "detail": "", "hash": "h"}}))
    d = cli._load_decisions(p)
    assert d["ev-a"] == Decision("pending", None, None, content_hash(a))
    assert d["ev-b"].claim is None and d["ev-b"].detail == ""


def test_wrapped_detail_is_an_error_not_a_truncated_edit():
    a = mk("ev-a", detail="Some detail here.")
    text = tick(render_review([a], [], {}, {}), "ev-a", "approve").replace(
        "detail: Some detail here.", "detail: Some detail\nhere.")
    dec, errs, _ = parse_review(text, [a])
    n = _line_no(text, "here.")
    assert dec == {} and errs == [f"line {n}: unexpected line in block ev-a; keep each field on one line"]


def test_mangled_status_line_is_an_error():
    a = mk("ev-a")
    text = render_review([a], [], {}, {}).replace("- [ ] approve   - [ ] reject", "* [x] approve   - [ ] reject")
    dec, errs, _ = parse_review(text, [a])
    n = _line_no(text, "* [x] approve   - [ ] reject")
    assert dec == {}
    assert f"line {n}: unexpected line in block ev-a; keep each field on one line" in errs


def test_rendered_sheet_has_no_unexpected_lines():
    us = [mk("ev-a"), mk("ev-b", detail="")]
    dec = {"ev-a": Decision("approve", "Mike changed it.", "", content_hash(us[0]))}
    text = render_review(us, [Finding("slop", "ev-a", "hype\nword")], {"ev-a": ["ev-b"]}, dec)
    out, errs, warns = parse_review(text, us)
    assert errs == [] and warns == [] and out["ev-a"] == dec["ev-a"]


def test_header_warns_gates_ok_is_not_safe_to_publish():
    text = render_review([], [], {}, {})
    assert '"gates: OK" means the automatic checks passed' in text
    assert "application counts" in text and "salary" in text and "two sources" in text


def test_review_rerenders_after_a_ruling_drops_a_unit(tmp_path, monkeypatch, capsys):
    import shutil
    from pathlib import Path
    from askmike.corpus.unit import load_jsonl
    priv = tmp_path / "priv"
    shutil.copytree(Path(__file__).parent / "fixtures" / "raw", priv / "raw")
    (priv / "denylist.local.txt").write_text("zzqxinvented\n")
    (priv / "dontprint.local.txt").write_text("qqzinvented\n")
    (priv / "raw/jds/applied").mkdir(parents=True)
    (priv / "raw/jds/top50_index.json").write_text(json.dumps([{"company": "Gadget Holdings"}]))
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    work = priv / "work"
    assert cli.main(["extract"]) == 0
    (work / "drafted.jsonl").write_text("")
    assert cli.main(["gate"]) == 0
    assert cli.main(["review"]) == 0
    keep, gone = [u.id for u in load_jsonl(work / "proposed.jsonl")][:2]
    sheet = work / "review.md"
    sheet.write_text(tick(sheet.read_text(), keep, "approve"))
    (work / "rulings.json").write_text(json.dumps([{"id": "R1", "question": "q", "options": [], "answer": "drop it",
                                                    "ops": [{"op": "drop", "ids": [gone]}]}]))
    assert cli.main(["gate"]) == 0
    capsys.readouterr()
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert f"block {gone} is not a known unit id; ignored" in out and "1 unknown block" in out
    text = sheet.read_text()
    assert f"## {gone}\n" not in text and f"## {keep}\n- [x] approve" in text
    d = json.loads((work / "decisions.json").read_text())
    assert d[keep]["status"] == "approve" and gone not in d
    assert cli.main(["ingest"]) == 0
