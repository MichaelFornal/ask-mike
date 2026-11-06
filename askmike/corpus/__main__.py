"""Pipeline command: uv run python -m askmike.corpus <step>. Prints counts only, never unit text."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

import askmike
from askmike.corpus.claude import run_claude
from askmike.corpus.draft import build_prompt, draft_units, prose_docs
from askmike.corpus.dupes import near_dupes
from askmike.corpus.extract import (
    FactsIndex, _check_unique, extract_evidence_units, extract_fact_units, load_ledger,
)
from askmike.corpus.export import ExportRefused, export_corpus
from askmike.corpus.review import Decision, merge_decisions, parse_review, render_review
from askmike.corpus.rulings import PendingRulings, apply_rulings, load_rulings
from askmike.corpus.sources import source_text
from askmike.corpus.unit import dump_jsonl, load_jsonl
from askmike.gates.checks import GateContext, ground_numbers, load_honesty_allow, load_private_patterns, run_gates
from askmike.private import private_dir, raw_dir, work_dir


def _extract() -> int:
    raw = raw_dir()
    profile = json.loads((raw / "profile/profile.json").read_text(encoding="utf-8"))
    resume = json.loads((raw / "resume/master.json").read_text(encoding="utf-8"))
    units = extract_evidence_units(profile, load_ledger(raw)) + extract_fact_units(profile, resume)
    dump_jsonl(units, work_dir() / "extracted.jsonl")
    print(f"extracted {len(units)} units")
    return 0


def _draft(only: str | None, max_seconds: float | None) -> int:
    raw, work = raw_dir(), work_dir()
    ledger = load_ledger(raw)
    cache = work / "drafts_cache"
    docs = prose_docs(raw)
    if only:
        docs = [d for d in docs if d.ref == only]
        if not docs:
            print("no such document")
            return 1
    old_units = load_jsonl(work / "drafted.jsonl") if (work / "drafted.jsonl").is_file() else []
    sp = work / "draft_stats.json"
    old_stats = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else []
    kept_units = {d.ref: [u for u in old_units if u.source == d.ref] for d in docs}
    new_stats = {s["doc_ref"]: s for s in old_stats}
    start = time.monotonic()
    remaining = 0
    for doc in docs:
        key = hashlib.sha256(build_prompt(doc).encode("utf-8")).hexdigest() + ".json"
        cached = (cache / key).is_file()
        if not cached and max_seconds is not None and time.monotonic() - start >= max_seconds:
            remaining += 1
            continue
        try:
            us, st = draft_units(doc, ledger, run_claude, cache_dir=cache)
        except RuntimeError as e:
            # keep the earlier units; record the error on this document's stats entry
            new_stats[doc.ref] = {**dataclasses.asdict(_empty(doc.ref)), "error": str(e)[:200]}
            continue
        kept_units[doc.ref] = us
        new_stats[doc.ref] = {**dataclasses.asdict(st), "error": ""}
    replaced = set(kept_units)
    units = [u for u in old_units if u.source not in replaced] + [u for us in kept_units.values() for u in us]
    stats = [new_stats[k] for k in sorted(new_stats)]
    dump_jsonl(units, work / "drafted.jsonl")
    sp.write_text(json.dumps(stats, indent=1, sort_keys=True), encoding="utf-8")
    tot = Counter()
    for s in stats:
        for k, v in s.items():
            if isinstance(v, (int, bool)) and k != "error":
                tot[k] += int(v)
    print(f"drafted {len(stats)} documents, kept {len(units)} units, "
          f"errors {sum(1 for s in stats if s['error'])}, remaining uncached {remaining}")
    print("totals " + ", ".join(f"{k}={tot[k]}" for k in sorted(tot)))
    return 0


def _empty(ref):
    from askmike.corpus.draft import DraftStats
    return DraftStats(ref, 0, 0, 0, 0, 0, False)


def _pipeline(allow_pending: bool):
    """Shared by gate and export: extracted+drafted units, rulings applied, numbers re-grounded.
    Returns (proposed, findings, ctx, rulings, ledger), or an error string."""
    raw, work, priv = raw_dir(), work_dir(), private_dir()
    drafted = work / "drafted.jsonl"
    for name, step in (("extracted.jsonl", "extract"), (drafted.name, "draft")):
        if not (work / name).is_file():
            return f"run {step} first ({name} is missing)"
    units = load_jsonl(work / "extracted.jsonl") + load_jsonl(drafted)
    rpath = work / "rulings.json"
    rulings = load_rulings(rpath) if rpath.is_file() else []
    units, ledger = apply_rulings(units, load_ledger(raw), rulings, allow_pending=allow_pending)
    _check_unique(units)
    # R27: Mike cannot edit `skills` in the review sheet, so familiar/learning/none refs are stripped here
    units = [dataclasses.replace(u, skills=tuple(r for r in u.skills if r.depth not in ("familiar", "learning", "none")))
             for u in units]
    ctx = GateContext(ledger, FactsIndex.load(raw), load_private_patterns(priv), lambda u: source_text(raw, u),
                      load_honesty_allow(priv))
    proposed, findings = [], []
    for u in units:
        grounded, _ = ground_numbers(u, ctx)
        proposed.append(dataclasses.replace(u, numbers=grounded))
        findings += [dataclasses.asdict(f) for f in run_gates(u, ctx)]
    findings.sort(key=lambda f: (f["unit_id"], f["gate"]))
    return proposed, findings, ctx, rulings, ledger


def _gate() -> int:
    work = work_dir()
    got = _pipeline(True)
    if isinstance(got, str):
        print(got)
        return 1
    proposed, findings = got[0], got[1]
    dump_jsonl(proposed, work / "proposed.jsonl")
    (work / "findings.json").write_text(json.dumps(findings, indent=1, ensure_ascii=False), encoding="utf-8")
    dupes = near_dupes(proposed)
    (work / "dupes.json").write_text(json.dumps(dupes, indent=1, sort_keys=True), encoding="utf-8")
    by_gate = Counter(f["gate"] for f in findings)
    pairs = sum(len(v) for v in dupes.values()) // 2
    print(f"proposed {len(proposed)} units, findings {len(findings)} "
          f"({', '.join(f'{g}={n}' for g, n in sorted(by_gate.items())) or 'none'}), "
          f"units with findings {len({f['unit_id'] for f in findings})}, dupe pairs {pairs}")
    return 0


def _repo_paths(priv) -> list[str]:
    mp = priv / "MANIFEST.json"
    heads = json.loads(mp.read_text(encoding="utf-8")).get("heads", {}) if mp.is_file() else {}
    return sorted(k[len("DOCS:"):] for k in heads if k.startswith("DOCS:") and k != "DOCS:.")


def _package_repo() -> Path | None:
    """The repo that holds this package (not the cwd's repo), or None when it is not in one."""
    root = Path(askmike.__file__).resolve().parents[1]
    return root if (root / ".git").exists() else None


def _export(repo_arg: str | None) -> int:
    repo = Path(repo_arg) if repo_arg else _package_repo()
    if repo is None:
        print("this package is not inside a git work tree; pass --repo PATH")
        return 1
    work, priv = work_dir(), private_dir()
    try:
        got = _pipeline(False)
    except PendingRulings:  # R19: pending rulings are a refusal reason; still compute the rest
        got = _pipeline(True)
    if isinstance(got, str):
        print(got)
        return 1
    proposed, _, ctx, rulings, ledger = got
    decisions = merge_decisions(proposed, _load_decisions(work / "decisions.json"), {})
    try:
        rep = export_corpus(proposed, decisions, ledger, rulings, ctx, repo, _repo_paths(priv))
    except ExportRefused as e:
        print(f"export refused ({len(e.reasons)} reasons)")
        for r in e.reasons:
            print(r)
        return 1
    print(f"exported {rep.units} units, rejected {rep.rejected}, "
          f"by kind {rep.by_kind}, by origin {rep.by_origin}")
    print(f"skills.json rows: {rep.skills}")
    print(f"source labels ({len(rep.sources)}):")  # public-safe by construction; eyeball before committing
    for s in rep.sources:
        print(f"  {s}")
    return 0


def _load_decisions(path) -> dict:
    """None = untouched, "" = blanked detail. The older format wrote "" for untouched; a stored
    claim of "" can only come from it (a claim is never blanked), so read that record's "" as None."""
    if not path.is_file():
        return {}
    out = {}
    for k, v in json.loads(path.read_text(encoding="utf-8")).items():
        if v.get("claim") == "":
            v = {**v, "claim": None, "detail": v.get("detail") or None}
        out[k] = Decision(**v)
    return out


def _proposed_inputs(work):
    if not (work / "proposed.jsonl").is_file():
        return None
    units = load_jsonl(work / "proposed.jsonl")
    fp, dp = work / "findings.json", work / "dupes.json"
    findings = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    dupes = json.loads(dp.read_text(encoding="utf-8")) if dp.is_file() else {}
    return units, findings, dupes


def _merge_sheet(work, units):
    """Parse work/review.md, merge into decisions.json. Returns (merged, errors, warnings)."""
    new, errors, warnings = parse_review((work / "review.md").read_text(encoding="utf-8"), units)
    merged = merge_decisions(units, _load_decisions(work / "decisions.json"), new)
    (work / "decisions.json").write_text(
        json.dumps({k: dataclasses.asdict(v) for k, v in merged.items()}, indent=1, sort_keys=True),
        encoding="utf-8")
    return merged, errors, warnings


def _print_warnings(warnings) -> None:
    for w in warnings:
        print(w)
    if warnings:
        print(f"review.md: {len(warnings)} unknown block(s) ignored (units dropped since the sheet was rendered)")


def _review() -> int:
    work = work_dir()
    got = _proposed_inputs(work)
    if got is None:
        print("run gate first (proposed.jsonl is missing)")
        return 1
    units, findings, dupes = got
    if (work / "review.md").is_file():  # keep Mike's un-ingested ticks and edits
        _, errors, warnings = _merge_sheet(work, units)
        _print_warnings(warnings)
        if errors:
            print("review.md has errors; fix them or run ingest; not overwriting")
            for e in errors:
                print(e)
            return 1
    decisions = merge_decisions(units, _load_decisions(work / "decisions.json"), {})
    (work / "review.md").write_text(render_review(units, findings, dupes, decisions), encoding="utf-8")
    decided = sum(1 for d in decisions.values() if d.status != "pending")
    print(f"review.md: {len(units)} units, {len({f['unit_id'] for f in findings})} with findings, "
          f"{decided} already decided")
    return 0


def _ingest() -> int:
    work = work_dir()
    got = _proposed_inputs(work)
    if got is None or not (work / "review.md").is_file():
        print("run review first (proposed.jsonl or review.md is missing)")
        return 1
    merged, errors, warnings = _merge_sheet(work, got[0])
    _print_warnings(warnings)
    c = Counter(d.status for d in merged.values())
    print(f"approved {c['approve']}, rejected {c['reject']}, pending {c['pending']}, error {len(errors)}, "
          f"unknown blocks {len(warnings)}")
    for e in errors:
        print(e)
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="askmike.corpus")
    sub = ap.add_subparsers(dest="step", required=True)
    sub.add_parser("extract")
    d = sub.add_parser("draft")
    d.add_argument("--only")
    d.add_argument("--max-seconds", type=float)
    sub.add_parser("gate")
    sub.add_parser("review")
    sub.add_parser("ingest")
    e = sub.add_parser("export")
    e.add_argument("--repo")
    a = ap.parse_args(argv)
    if a.step == "extract":
        return _extract()
    if a.step == "draft":
        return _draft(a.only, a.max_seconds)
    if a.step == "review":
        return _review()
    if a.step == "ingest":
        return _ingest()
    if a.step == "export":
        return _export(a.repo)
    return _gate()


if __name__ == "__main__":
    sys.exit(main())
