"""Draft evidence units from prose with `claude -p`; keep only items with a verbatim quote."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from askmike.corpus.claude import run_claude
from askmike.corpus.extract import Skill
from askmike.corpus.sources import doc_text
from askmike.corpus.unit import SkillRef, Unit, validate
from askmike.llm.ask import first_json

DRAFT_MODEL = "claude-opus-5-5"
MAX_UNITS = 6
_TEMPLATE = Path(__file__).parent / "prompts" / "draft_units.md"


@dataclass(frozen=True)
class SourceDoc:
    ref: str
    text: str
    subject_hint: str
    kind_hint: str


@dataclass(frozen=True)
class DraftStats:
    doc_ref: str
    proposed: int
    kept: int
    dropped_no_quote: int
    dropped_invalid: int  # includes repeats of an id already emitted in this document
    dropped_over_cap: int
    parse_error: bool


def _files(d: Path, suffixes=None):
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and (suffixes is None or p.suffix.lower() in suffixes))


def prose_docs(raw: Path) -> list[SourceDoc]:
    raw = Path(raw)
    refs: list[tuple[str, str, str]] = []  # (ref, subject_hint, kind_hint)
    for p in _files(raw / "memory", {".md"}):
        refs.append((f"memory/{p.name}", "Mike", "person"))
    for p in _files(raw / "stories"):
        refs.append((f"stories/{p.name}", "Mike", "person"))
    for p in _files(raw / "projects", {".md"}):
        refs.append((f"projects/{p.name}", p.name.split("__")[0], "project"))
    for p in _files(raw / "writeups", {".md", ".html", ".py"}):
        if p.suffix.lower() != ".md" and p.name not in ("portfolio.html", "build_cover_letter.py"):
            continue
        refs.append((f"writeups/{p.name}", "Mike", "project"))
    prof = raw / "profile/profile.json"
    if prof.is_file():
        narrative = json.loads(prof.read_text(encoding="utf-8")).get("narrative") or {}
        for k, v in narrative.items():
            if isinstance(v, str) and v.strip():
                refs.append((f"profile/profile.json#narrative/{k}", "Mike", "person"))
    ans = raw / "answers/answers.json"
    if ans.is_file():
        drafted = json.loads(ans.read_text(encoding="utf-8")).get("drafted") or {}
        for k, v in drafted.items():
            if isinstance(v, str) and v.strip() and "DRAFT" not in v:
                refs.append((f"answers/answers.json#drafted/{k}", "Mike", "person"))
    return [SourceDoc(r, doc_text(raw, r), s, kd) for r, s, kd in refs]


def build_prompt(doc: SourceDoc) -> str:
    return (_TEMPLATE.read_text(encoding="utf-8")
            + f"\nDocument reference: {doc.ref}\n"
            + f"Default subject: {doc.subject_hint}\nDefault kind: {doc.kind_hint}\n\n"
            + "<document>\n" + doc.text + "\n</document>\n")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _reply(prompt: str, runner, cache_dir, model: str) -> dict:
    path = Path(cache_dir) / (hashlib.sha256(prompt.encode("utf-8")).hexdigest() + ".json") if cache_dir else None
    if path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    reply = runner(prompt, model)  # RuntimeError propagates, uncached
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
    return reply


def draft_units(doc: SourceDoc, ledger: list[Skill], runner=run_claude,
                cache_dir: Path | None = None) -> tuple[list[Unit], DraftStats]:
    reply = _reply(build_prompt(doc), runner, cache_dir, DRAFT_MODEL)
    items = first_json(str(reply.get("result", "")), list)
    if items is None:
        return [], DraftStats(doc.ref, 0, 0, 0, 0, 0, True)
    smap: dict[str, Skill] = {}
    for s in ledger:
        for name in (s.term, *s.aliases):
            smap.setdefault(name.lower(), s)
    text = _norm(doc.text)
    units: list[Unit] = []
    no_quote = invalid = 0
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            invalid += 1
            continue
        quote = it.get("quote")
        if not isinstance(quote, str) or not _norm(quote) or _norm(quote) not in text:
            no_quote += 1
            continue
        claim = it.get("claim")
        if not isinstance(claim, str):
            invalid += 1
            continue
        refs: list[SkillRef] = []
        for term in it.get("skills") or []:
            s = smap.get(str(term).lower())
            if s and SkillRef(s.term, s.depth) not in refs:
                refs.append(SkillRef(s.term, s.depth))
        def _s(k, default=""):
            v = it.get(k)
            return v if isinstance(v, str) else default
        u = Unit(
            id="dr-" + hashlib.sha256((doc.ref + claim.strip()).encode("utf-8")).hexdigest()[:10],
            claim=claim.strip(), subject=_s("subject", doc.subject_hint) or doc.subject_hint,
            kind=_s("kind", doc.kind_hint), source=doc.ref, origin="draft",
            detail=_s("detail").strip(), when=_s("when").strip(), skills=tuple(refs), quote=_norm(quote))
        if validate(u) or u.id in seen:
            invalid += 1
            continue
        seen.add(u.id)
        units.append(u)
    over = max(0, len(units) - MAX_UNITS)
    units = units[:MAX_UNITS]
    return units, DraftStats(doc.ref, len(items), len(units), no_quote, invalid, over, False)
