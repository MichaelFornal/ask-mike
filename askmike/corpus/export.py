"""Export: approved, gate-clean units into the public repo. The privacy boundary.

Refusals name unit ids, gate names, field names and ruling ids only, never text.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
import posixpath
from pathlib import Path

from askmike.corpus.unit import Number, Unit, content_hash, validate
from askmike.gates.checks import GateContext, any_case_hit, ground_numbers, run_gates


class ExportRefused(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class ExportReport:
    units: int
    rejected: int
    by_kind: dict
    by_origin: dict
    sources: tuple = ()  # distinct public source labels, sorted
    skills: int = 0  # rows written to skills.json


def safe_id(uid: str, patterns) -> str:
    """Print an id as-is unless it matches a private pattern; then a hash stand-in."""
    if any(any_case_hit(p, uid) for p in patterns):
        return "#" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8]
    return uid


def _notes(source: str) -> str:
    return "notes:" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]


def _project_label(path: str, repo_paths) -> str | None:
    """projects/<a>__<b>__... -> project:<basename(repo)>/<rest>; None when no repo prefixes it."""
    rebuilt = path.removeprefix("projects/").replace("__", "/")
    best = None
    for r in repo_paths:
        r = r.strip("/")
        if r and (rebuilt == r or rebuilt.startswith(r + "/")) and (best is None or len(r) > len(best)):
            best = r
    if best is None:
        return None
    rest = rebuilt[len(best):].strip("/")
    base = posixpath.basename(best)
    return f"project:{base}/{rest}" if rest else f"project:{base}"


def _resume_label(pointer: str, raw: Path | None) -> str:
    fallback = pointer.strip("/")
    try:
        if raw is None:
            from askmike.private import raw_dir
            raw = raw_dir()
        data = json.loads((Path(raw) / "resume/master.json").read_text(encoding="utf-8"))
        segs = [s for s in pointer.split("/") if s]
        nodes = [data]
        for s in segs:
            nodes.append(nodes[-1][int(s)] if isinstance(nodes[-1], list) else nodes[-1][s])
        for n in reversed(nodes):
            if isinstance(n, dict) and isinstance(n.get("id"), str) and n["id"]:
                return f"resume:{n['id']}"
    except Exception:
        pass
    return f"resume:{fallback}" if fallback else _notes("resume/master.json")


def public_source(u: Unit, repo_paths, raw: Path | None = None) -> str:
    path, _, pointer = u.source.partition("#")
    if path == "profile/profile.json":
        segs = [s for s in pointer.split("/") if s]
        if len(segs) == 4 and segs[2] == "evidence":
            return f"evidence:{segs[3]}"
    elif path == "resume/master.json":
        return _resume_label(pointer, raw)
    elif path.startswith("projects/"):
        label = _project_label(path, repo_paths)
        if label:
            return label
    return _notes(u.source)


def _public_number(n: Number, repo_paths) -> Number:
    """fact:<file>:<key> -> hide grouping folders of projects/ fact files."""
    if n.source.startswith("fact:projects/"):
        file, _, key = n.source[len("fact:"):].rpartition(":")
        label = _project_label(file, repo_paths)
        return Number(n.value, f"fact:{label or _notes(file)}:{key}")
    return n


def _record(u: Unit, repo_paths) -> dict:
    return {
        "id": u.id, "claim": u.claim, "detail": u.detail, "subject": u.subject, "kind": u.kind,
        "when": u.when,
        "skills": [{"term": s.term, "depth": s.depth} for s in u.skills],
        "numbers": [asdict(_public_number(n, repo_paths)) for n in u.numbers],
        "source": public_source(u, repo_paths), "origin": u.origin,
    }


def _strings(rec: dict):
    for k, v in rec.items():
        if isinstance(v, str):
            yield k, v
        elif k == "skills":
            for s in v:
                yield "skills.term", s["term"]
        elif k == "numbers":
            for n in v:
                yield "numbers.value", n["value"]
                yield "numbers.source", n["source"]


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def export_corpus(units, decisions, ledger, rulings, ctx: GateContext, repo, repo_paths) -> ExportReport:
    reasons: list[str] = []
    sid = lambda uid: safe_id(uid, ctx.private_patterns)
    groups = {c for r in repo_paths for c in r.strip("/").split("/")[:-1]}
    for r in rulings:
        if r.answer is None:
            reasons.append(f"ruling {sid(r.id)}: unanswered")
    out: list[Unit] = []
    rejected = 0
    for u in sorted(units, key=lambda u: u.id):
        d = decisions.get(u.id)
        if d is None or d.status not in ("approve", "reject") or d.hash != content_hash(u):
            reasons.append(f"unit {sid(u.id)}: pending")
            continue
        if d.status == "reject":
            rejected += 1
            continue
        if u.subject in groups:
            reasons.append(f"unit {sid(u.id)}: grouping folder in subject")
        v = replace(u, claim=u.claim if d.claim is None else d.claim,
                    detail=u.detail if d.detail is None else d.detail)  # "" = Mike blanked it
        grounded, _ = ground_numbers(v, ctx)
        v = replace(v, numbers=grounded)
        bad = validate(v)
        if bad:
            reasons.append(f"unit {sid(u.id)}: structure")
        for g in sorted({f.gate for f in run_gates(v, ctx)}):
            reasons.append(f"unit {sid(u.id)}: gate {g}")
        out.append(v)
    recs = [_record(u, repo_paths) for u in out]
    skills = sorted(({"term": s.term, "depth": s.depth, "aliases": list(s.aliases)} for s in ledger),
                    key=lambda s: s["term"])
    machine = {"source", "numbers.source"}  # slugs and labels: a lowercase company hit counts
    for rec in recs:
        if any(any_case_hit(p, rec["id"]) for p in ctx.private_patterns):
            reasons.append(f"unit {sid(rec['id'])}: private pattern in id")
        for field, text in _strings({k: x for k, x in rec.items() if k != "id"}):
            search = any_case_hit if field in machine else (lambda p, t: p.search(t))
            if any(search(p, text) for p in ctx.private_patterns):
                reasons.append(f"unit {sid(rec['id'])}: private pattern in {field}")
    for s in skills:
        for field in ("term", "aliases"):
            for text in ([s[field]] if field == "term" else s[field]):
                if any(p.search(text) for p in ctx.private_patterns):
                    reasons.append(f"skills.json: private pattern in {field}")
    if reasons:
        raise ExportRefused(sorted(set(reasons)))
    outdir = Path(repo) / "corpus"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "units.jsonl").write_bytes(
        ("".join(_dumps(r) + "\n" for r in recs)).encode("utf-8"))
    (outdir / "skills.json").write_bytes((_dumps(skills) + "\n").encode("utf-8"))
    by_kind: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    for u in out:
        by_kind[u.kind] = by_kind.get(u.kind, 0) + 1
        by_origin[u.origin] = by_origin.get(u.origin, 0) + 1
    return ExportReport(len(out), rejected, dict(sorted(by_kind.items())), dict(sorted(by_origin.items())),
                        tuple(sorted({r["source"] for r in recs})), len(skills))
