"""The evidence unit: one claim about Mike, with its provenance."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

KINDS = {"role", "project", "person", "education"}
DEPTHS = {"strong", "working", "familiar", "learning", "none"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
_WHEN = re.compile(r"^(\d{4}(-\d{2})?)?(\.\.(\d{4}(-\d{2})?|present))?$")


@dataclass(frozen=True)
class SkillRef:
    term: str
    depth: str


@dataclass(frozen=True)
class Number:
    value: str
    source: str  # "text" or "fact:<file>:<key>"


@dataclass(frozen=True)
class Unit:
    id: str
    claim: str
    subject: str
    kind: str
    source: str  # private ref: <raw-relative path>[#<json-pointer>]
    origin: str  # "extract" or "draft"
    detail: str = ""
    when: str = ""
    skills: tuple[SkillRef, ...] = ()
    numbers: tuple[Number, ...] = ()
    quote: str = ""


def validate(u: Unit) -> list[str]:
    """Structural problems only; content rules belong to the gates."""
    errs: list[str] = []
    if not _ID.match(u.id):
        errs.append(f"bad id: {u.id!r}")
    if not u.claim.strip():
        errs.append("empty claim")
    if u.kind not in KINDS:
        errs.append(f"bad kind: {u.kind!r}")
    if not u.source.strip():
        errs.append("empty source")
    for s in u.skills:
        if s.depth not in DEPTHS:
            errs.append(f"bad depth: {s.depth!r}")
    if not _WHEN.match(u.when):
        errs.append(f"bad when: {u.when!r}")
    return errs


def content_hash(u: Unit) -> str:
    """sha256 over claim, detail, skills, numbers; stale when any changes."""
    payload = {
        "claim": u.claim,
        "detail": u.detail,
        "skills": [asdict(s) for s in u.skills],
        "numbers": [asdict(n) for n in u.numbers],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def dump_jsonl(units, path) -> None:
    lines = [
        json.dumps(asdict(u), sort_keys=True, ensure_ascii=False)
        for u in sorted(units, key=lambda u: u.id)
    ]
    Path(path).write_bytes(("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))


def load_jsonl(path) -> list[Unit]:
    out = []
    for line in Path(path).read_bytes().decode("utf-8").split("\n"):
        if not line.strip():
            continue
        d = json.loads(line)
        d["skills"] = tuple(SkillRef(**s) for s in d.get("skills", []))
        d["numbers"] = tuple(Number(**n) for n in d.get("numbers", []))
        out.append(Unit(**d))
    return out
