"""Deterministic extractors: ledger, facts index, evidence and fact units."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from math import floor
from pathlib import Path

from askmike.corpus.unit import Number, SkillRef, Unit

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass(frozen=True)
class Skill:
    term: str
    depth: str
    aliases: tuple[str, ...] = ()


def load_ledger(raw: Path) -> list[Skill]:
    data = json.loads((Path(raw) / "profile/profile.json").read_text(encoding="utf-8"))
    return [Skill(s["term"], s["depth"], tuple(s.get("aliases") or ())) for s in data.get("skill_depth", [])]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def norm_date(s) -> str:
    """'2021-03' | 'Apr 2021' | '2021' | 'Present' -> YYYY-MM | YYYY | present | ''."""
    s = (s or "").strip()
    if s.lower() == "present":
        return "present"
    if re.fullmatch(r"\d{4}(-\d{2})?", s):
        return s
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    return ""


def make_when(start, end) -> str:
    a, b = norm_date(start), norm_date(end)
    if not a or a == "present":
        return ""
    return f"{a}..{b}" if b else a


# ---- numbers and the facts index -------------------------------------------

def _trunc(x: float, places: int) -> float:
    f = 10 ** places
    return floor(x * f + 1e-9) / f


def number_forms(value) -> set[str]:
    """Forms one fact may appear as: plain, commas, M/K truncated (never rounded up)."""
    try:
        v = float(str(value).replace(",", ""))
    except ValueError:
        return {str(value)}
    forms: set[str] = set()
    if v == int(v):
        n = int(v)
        forms |= {str(n), f"{n:,}"}
    else:
        forms.add(repr(v))
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            for places in (1, 2):
                t = _trunc(abs(v) / div, places)
                forms.add(f"{t:.{places}f}{suffix}")
                forms.add(f"{t:.{places}f}".rstrip("0").rstrip(".") + suffix)
            break
    return forms


class FactsIndex:
    def __init__(self, entries: dict[str, set[str]]):
        self._entries = entries  # "fact:<file>:<key>" -> forms

    @classmethod
    def load(cls, raw: Path) -> "FactsIndex":
        raw = Path(raw)
        files = [raw / "site/facts.json", *sorted((raw / "projects").glob("*facts.json"))]
        entries: dict[str, set[str]] = {}
        for f in files:
            if not f.is_file():
                continue
            rel = f.relative_to(raw).as_posix()
            data = json.loads(f.read_text(encoding="utf-8"))
            facts = data["facts"] if isinstance(data.get("facts"), dict) else data
            for key, item in facts.items():
                forms: set[str] = set()
                vals = [item.get("value"), item.get("display")] if isinstance(item, dict) else [item]
                for v in vals:
                    if isinstance(v, bool) or v is None:
                        continue
                    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"[\d,.]+[KMB]?", v)):
                        forms |= number_forms(v) if not (isinstance(v, str) and v[-1:] in "KMB") else {v}
                if forms:
                    entries[f"fact:{rel}:{key}"] = forms
        return cls(entries)

    def match(self, token: str) -> list[str]:
        t = token.strip()
        return sorted(ref for ref, forms in self._entries.items() if t in forms)


# ---- units -----------------------------------------------------------------

def _ev_claim(c: str) -> str:
    c = c.strip()
    if len(c) > 1 and c[1].islower():
        c = c[0].lower() + c[1:]
    elif len(c) == 1:
        c = c.lower()
    return "Mike " + c


def _skill_map(ledger: list[Skill]) -> dict[str, Skill]:
    m: dict[str, Skill] = {}
    for s in ledger:
        for name in (s.term, *s.aliases):
            m.setdefault(name.lower(), s)
    return m


def extract_evidence_units(profile: dict, ledger: list[Skill]) -> list[Unit]:
    smap = _skill_map(ledger)
    out: list[Unit] = []
    for group, kind, namekey in (("roles", "role", "company"), ("projects", "project", "name")):
        for i, parent in enumerate(profile.get(group, [])):
            when = make_when(parent.get("start"), parent.get("end"))
            for item in parent.get("evidence", []):
                bullet = (item.get("bullet") or "").strip()
                metric = (item.get("metric") or "").strip()
                detail = bullet
                if metric and metric not in bullet:
                    detail = f"{bullet} {metric}".strip()
                refs: list[SkillRef] = []
                for term in item.get("stack") or []:
                    s = smap.get(str(term).lower())
                    if s and SkillRef(s.term, s.depth) not in refs:
                        refs.append(SkillRef(s.term, s.depth))
                out.append(Unit(
                    id=f"ev-{slug(item['id'])}",
                    claim=_ev_claim(item["claim"]),
                    subject=parent[namekey],
                    kind=kind,
                    source=f"profile/profile.json#{group}/{i}/evidence/{item['id']}",
                    origin="extract",
                    detail=detail,
                    when=when,
                    skills=tuple(refs),
                ))
    return _check_unique(out)


def _check_unique(units: list[Unit]) -> list[Unit]:
    seen: set[str] = set()
    for u in units:
        if u.id in seen:
            raise ValueError(f"duplicate unit id: {u.id}")
        seen.add(u.id)
    return units


def extract_fact_units(profile: dict, resume: dict) -> list[Unit]:
    by_company: dict[str, list[tuple[int, dict]]] = {}
    for j, e in enumerate(resume.get("experience", [])):
        by_company.setdefault((e.get("company") or "").strip().lower(), []).append((j, e))
    roles = profile.get("roles", [])
    counts: dict[str, int] = {}
    for r in roles:
        counts[slug(r["company"])] = counts.get(slug(r["company"]), 0) + 1
    out: list[Unit] = []
    for i, r in enumerate(roles):
        company = r["company"]
        cands = by_company.get(company.strip().lower(), [])
        pick = next((c for c in cands if norm_date(c[1].get("start")) == norm_date(r.get("start"))), None)
        if pick is None and cands:
            pick = cands[0]
        if pick:
            cands.remove(pick)
            j, src = pick
            source = f"resume/master.json#experience/{j}"
        else:
            src, source = r, f"profile/profile.json#roles/{i}"
        rid = f"role-{slug(company)}"
        if counts[slug(company)] > 1:
            yr = norm_date(src.get("start"))[:4] or str(i)
            rid = f"{rid}-{yr}"
        out.append(Unit(
            id=rid,
            claim=f"Mike was {src['title']} at {company}",
            subject=company,
            kind="role",
            source=source,
            origin="extract",
            when=make_when(src.get("start"), src.get("end")),
        ))
    for i, e in enumerate(profile.get("education", [])):
        what = " ".join(x for x in (e.get("credential"), e.get("field")) if x)
        claim = f"Mike earned a {what} from {e['school']}" if what else f"Mike studied at {e['school']}"
        out.append(Unit(
            id=f"edu-{slug(e['school'])}",
            claim=claim,
            subject=e["school"],
            kind="education",
            source=f"profile/profile.json#education/{i}",
            origin="extract",
        ))
    return _check_unique(out)
