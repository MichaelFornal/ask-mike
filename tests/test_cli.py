import json
import shutil
from pathlib import Path

from askmike.corpus import __main__ as cli

FIX = Path(__file__).parent / "fixtures" / "raw"


def test_extract_then_gate_on_fixture_raw(tmp_path, monkeypatch, capsys):
    priv = tmp_path / "priv"
    shutil.copytree(FIX, priv / "raw")
    (priv / "denylist.local.txt").write_text("zzqxinvented\n")
    (priv / "dontprint.local.txt").write_text("qqzinvented\n")
    jds = priv / "raw" / "jds"
    (jds / "applied").mkdir(parents=True)
    (jds / "top50_index.json").write_text(json.dumps([{"company": "Gadget Holdings"}]))
    (jds / "applied" / "gizmo-works-J1.txt").write_text("x")
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))

    assert cli.main(["extract"]) == 0
    out = capsys.readouterr().out
    work = priv / "work"
    assert (work / "extracted.jsonl").is_file()
    n = len((work / "extracted.jsonl").read_text().splitlines())
    assert n > 0 and f"extracted {n} units" in out

    (work / "drafted.jsonl").write_text("")  # no draft step in this test
    assert cli.main(["gate"]) == 0
    out = capsys.readouterr().out
    assert (work / "proposed.jsonl").is_file()
    assert (work / "dupes.json").is_file()
    findings = json.loads((work / "findings.json").read_text())
    assert isinstance(findings, list)
    assert findings == sorted(findings, key=lambda f: (f["unit_id"], f["gate"]))
    assert f"proposed {n} units" in out
    assert "Acme" not in out


def test_draft_only_with_runner_and_errors(tmp_path, monkeypatch, capsys):
    priv = tmp_path / "priv"
    shutil.copytree(FIX, priv / "raw")
    (priv / "raw" / "memory").mkdir()
    (priv / "raw" / "memory" / "a.md").write_text("Mike built it in Python.")
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))

    def boom(prompt, model):
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "run_claude", boom)
    assert cli.main(["draft", "--only", "memory/a.md"]) == 0
    stats = json.loads((priv / "work" / "draft_stats.json").read_text())
    assert stats[0]["doc_ref"] == "memory/a.md" and stats[0]["error"]


# ---- draft / gate behaviour (fake runner, invented data) -------------------

def _priv(tmp_path, monkeypatch, docs):
    priv = tmp_path / "priv"
    shutil.copytree(FIX, priv / "raw")
    (priv / "denylist.local.txt").write_text("zzqxinvented\n")
    (priv / "dontprint.local.txt").write_text("qqzinvented\n")
    jds = priv / "raw" / "jds"
    (jds / "applied").mkdir(parents=True)
    (jds / "top50_index.json").write_text(json.dumps([{"company": "Gadget Holdings"}]))
    (jds / "applied" / "gizmo-works-J1.txt").write_text("x")
    (priv / "raw" / "memory").mkdir()
    for name, text in docs.items():
        (priv / "raw" / "memory" / name).write_text(text)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    return priv


def _runner(calls=None, fail=()):
    def run(prompt, model):
        if calls is not None:
            calls.append(prompt)
        if any(f in prompt for f in fail):
            raise RuntimeError("boom")
        body = prompt.split("<document>")[1]
        quote = "Mike built alpha." if "alpha" in body else "Mike built beta."
        claim = "Mike built zzqxinvented alpha." if "alpha" in body else "Mike built beta."
        return {"result": json.dumps([{"claim": claim, "subject": "Widget Index", "kind": "project",
                                       "quote": quote, "skills": []}])}
    return run


DOCS = {"a.md": "Mike built alpha.", "b.md": "Mike built beta."}


def _sources(priv):
    from askmike.corpus.unit import load_jsonl
    return sorted(u.source for u in load_jsonl(priv / "work" / "drafted.jsonl"))


def test_draft_keeps_units_and_writes_stats(tmp_path, monkeypatch, capsys):
    priv = _priv(tmp_path, monkeypatch, DOCS)
    monkeypatch.setattr(cli, "run_claude", _runner())
    assert cli.main(["draft"]) == 0
    assert _sources(priv) == ["memory/a.md", "memory/b.md"]
    stats = json.loads((priv / "work" / "draft_stats.json").read_text())
    assert [(s["doc_ref"], s["proposed"], s["kept"], s["error"]) for s in stats] == [
        ("memory/a.md", 1, 1, ""), ("memory/b.md", 1, 1, "")]
    assert "drafted 2 documents, kept 2 units, errors 0, remaining uncached 0" in capsys.readouterr().out


def test_max_seconds_skips_uncached_but_processes_cached(tmp_path, monkeypatch, capsys):
    priv = _priv(tmp_path, monkeypatch, DOCS)
    monkeypatch.setattr(cli, "run_claude", _runner())
    assert cli.main(["draft", "--only", "memory/a.md"]) == 0  # a is now cached
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(cli, "run_claude", _runner(calls))
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks, 100.0))
    assert cli.main(["draft", "--max-seconds", "5"]) == 0
    assert calls == []  # b was uncached and over budget, a came from cache
    assert "remaining uncached 1" in capsys.readouterr().out
    assert _sources(priv) == ["memory/a.md"]


def test_only_merges_into_existing_drafts(tmp_path, monkeypatch):
    priv = _priv(tmp_path, monkeypatch, DOCS)
    monkeypatch.setattr(cli, "run_claude", _runner())
    assert cli.main(["draft"]) == 0
    assert cli.main(["draft", "--only", "memory/a.md"]) == 0
    assert _sources(priv) == ["memory/a.md", "memory/b.md"]
    stats = json.loads((priv / "work" / "draft_stats.json").read_text())
    assert [s["doc_ref"] for s in stats] == ["memory/a.md", "memory/b.md"]


def test_errored_doc_keeps_its_earlier_units(tmp_path, monkeypatch, capsys):
    priv = _priv(tmp_path, monkeypatch, DOCS)
    monkeypatch.setattr(cli, "run_claude", _runner())
    assert cli.main(["draft"]) == 0
    for f in (priv / "work" / "drafts_cache").glob("*.json"):
        f.unlink()  # force re-run, so the failing runner is hit
    monkeypatch.setattr(cli, "run_claude", _runner(fail=("alpha",)))
    assert cli.main(["draft"]) == 0
    assert _sources(priv) == ["memory/a.md", "memory/b.md"]
    stats = {s["doc_ref"]: s for s in json.loads((priv / "work" / "draft_stats.json").read_text())}
    assert stats["memory/a.md"]["error"] and not stats["memory/b.md"]["error"]
    assert "errors 1" in capsys.readouterr().out


def test_gate_requires_extract_and_draft(tmp_path, monkeypatch, capsys):
    priv = _priv(tmp_path, monkeypatch, DOCS)
    assert cli.main(["gate"]) == 1
    assert "run extract first" in capsys.readouterr().out
    assert cli.main(["extract"]) == 0
    assert cli.main(["gate"]) == 1
    assert "run draft first" in capsys.readouterr().out


def test_gate_flags_denylist_term_and_prints_no_terms(tmp_path, monkeypatch, capsys):
    priv = _priv(tmp_path, monkeypatch, DOCS)
    monkeypatch.setattr(cli, "run_claude", _runner())
    assert cli.main(["extract"]) == 0
    assert cli.main(["draft"]) == 0
    assert cli.main(["gate"]) == 0
    out = capsys.readouterr().out
    findings = json.loads((priv / "work" / "findings.json").read_text())
    assert any(f["gate"] == "privacy" for f in findings)
    for term in ("zzqxinvented", "qqzinvented", "Gadget Holdings", "Gizmo Works"):
        assert term not in out and term not in json.dumps(findings)


def test_export_refuses_after_ruling_answered_after_gate(tmp_path, monkeypatch, capsys):
    from askmike.corpus.review import Decision
    from askmike.corpus.unit import content_hash, load_jsonl
    priv = tmp_path / "priv"
    shutil.copytree(FIX, priv / "raw")
    (priv / "denylist.local.txt").write_text("zzqxinvented\n")
    (priv / "dontprint.local.txt").write_text("qqzinvented\n")
    jds = priv / "raw" / "jds"
    (jds / "applied").mkdir(parents=True)
    (jds / "top50_index.json").write_text(json.dumps([{"company": "Gadget Holdings"}]))
    (jds / "applied" / "gizmo-works-J1.txt").write_text("x")
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    assert cli.main(["extract"]) == 0
    work = priv / "work"
    (work / "drafted.jsonl").write_text("")
    first = load_jsonl(work / "extracted.jsonl")[0]
    find = first.claim.split()[1]
    (work / "rulings.json").write_text(json.dumps([{"id": "R1", "question": "q", "options": [], "answer": None,
                                                    "ops": [{"op": "replace", "find": find, "with": "zork"}]}]))
    assert cli.main(["gate"]) == 0
    proposed = load_jsonl(work / "proposed.jsonl")
    (work / "decisions.json").write_text(json.dumps({
        u.id: {"status": "approve", "claim": None, "detail": None, "hash": content_hash(u)} for u in proposed}))
    capsys.readouterr()
    assert cli.main(["export", "--repo", str(tmp_path / "out")]) == 1
    assert "ruling R1: unanswered" in capsys.readouterr().out
    # answer the ruling after gate: the changed unit's decision hash is stale -> pending
    r = json.loads((work / "rulings.json").read_text())
    r[0]["answer"] = "yes"
    (work / "rulings.json").write_text(json.dumps(r))
    assert cli.main(["export", "--repo", str(tmp_path / "out")]) == 1
    out = capsys.readouterr().out
    assert f"unit {first.id}: pending" in out and "unanswered" not in out
    assert not (tmp_path / "out" / "corpus").exists()


def test_export_success_prints_source_labels_and_skill_rows(tmp_path, monkeypatch, capsys):
    from askmike.corpus.unit import content_hash, load_jsonl
    _priv(tmp_path, monkeypatch, {})
    priv = tmp_path / "priv"
    assert cli.main(["extract"]) == 0
    work = priv / "work"
    (work / "drafted.jsonl").write_text("")
    assert cli.main(["gate"]) == 0
    flagged = {f["unit_id"] for f in json.loads((work / "findings.json").read_text())}
    proposed = load_jsonl(work / "proposed.jsonl")
    (work / "decisions.json").write_text(json.dumps({
        u.id: {"status": "reject" if u.id in flagged else "approve", "claim": None, "detail": None,
               "hash": content_hash(u)} for u in proposed}))
    capsys.readouterr()
    out_repo = tmp_path / "out"
    assert cli.main(["export", "--repo", str(out_repo)]) == 0
    out = capsys.readouterr().out
    recs = [json.loads(x) for x in (out_repo / "corpus/units.jsonl").read_text().splitlines()]
    labels = sorted({r["source"] for r in recs})
    rows = len(json.loads((out_repo / "corpus/skills.json").read_text()))
    assert labels and f"skills.json rows: {rows}" in out
    assert f"source labels ({len(labels)}):\n" + "".join(f"  {s}\n" for s in labels) in out


def test_export_defaults_to_the_package_repo():
    assert cli._package_repo() == Path(__file__).resolve().parents[1]
    assert (cli._package_repo() / ".git").exists()


def test_export_without_repo_refuses_when_package_is_not_in_a_repo(tmp_path, monkeypatch, capsys):
    import askmike
    monkeypatch.setattr(askmike, "__file__", str(tmp_path / "site" / "askmike" / "__init__.py"))
    assert cli._package_repo() is None
    assert cli.main(["export"]) == 1
    assert "pass --repo PATH" in capsys.readouterr().out


def test_gate_strips_low_depth_skill_refs(tmp_path, monkeypatch):
    from askmike.corpus.unit import SkillRef, dump_jsonl, load_jsonl
    priv = _priv(tmp_path, monkeypatch, DOCS)
    monkeypatch.setattr(cli, "run_claude", _runner())
    assert cli.main(["extract"]) == 0
    assert cli.main(["draft"]) == 0
    ex = load_jsonl(priv / "work" / "extracted.jsonl")
    ex[0] = __import__("dataclasses").replace(
        ex[0], skills=(SkillRef("Alpha", "familiar"), SkillRef("Beta", "learning"),
                       SkillRef("Gamma", "none"), SkillRef("Delta", "strong")))
    dump_jsonl(ex, priv / "work" / "extracted.jsonl")
    assert cli.main(["gate"]) == 0
    got = {u.id: u for u in load_jsonl(priv / "work" / "proposed.jsonl")}[ex[0].id]
    assert [(r.term, r.depth) for r in got.skills] == [("Delta", "strong")]
