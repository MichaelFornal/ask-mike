"""The five gates every unit passes: privacy, slop, voice, honesty, numbers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from askmike.corpus.extract import FactsIndex, Skill
from askmike.corpus.unit import Number, Unit
from askmike.gates.text import (
    _term_re,  # noqa: F401  re-exported: retrieval.expand and facts.build import it from here
    first_person, low_depth_hits, privacy_hits, slop_hits,
)

GATES = ("privacy", "slop", "voice", "honesty", "numbers")
CLAIM_MAX, DETAIL_MAX = 280, 600


@dataclass(frozen=True)
class Finding:
    gate: str
    unit_id: str
    message: str


@dataclass
class GateContext:
    ledger: list[Skill]
    facts: FactsIndex
    private_patterns: list  # objects with .search(text) and .pattern
    source_text: Callable[[Unit], str]
    honesty_allow: frozenset = frozenset()  # lowercase aliases the honesty gate ignores in claim/detail text


# ---- private patterns ------------------------------------------------------

def _lines(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _title_words(s: str, seps: str) -> str:
    return " ".join(w.capitalize() for w in re.split(f"[{seps}]+", s) if w)


def applied_company_names(private: Path) -> list[str]:
    jds = Path(private) / "raw" / "jds"
    idx = jds / "top50_index.json"
    applied = jds / "applied"
    if not idx.is_file():
        raise FileNotFoundError("raw/jds/top50_index.json is missing from the private folder")
    if not applied.is_dir():
        raise FileNotFoundError("raw/jds/applied/ is missing from the private folder")
    names: set[str] = set()
    for item in json.loads(idx.read_text(encoding="utf-8")):
        n = (item.get("company") or "").strip()
        if n:
            names.add(_title_words(n, " ") if n == n.lower() else n)
    for f in applied.glob("*.txt"):
        stem = re.sub(r"^ats_[a-z]+_", "", re.sub(r"-J\d+$", "", f.stem))
        n = _title_words(stem, "-_")
        if n:
            names.add(n)
    allow_file = Path(private) / "company_allow.local.txt"
    allow = {x.lower() for x in _lines(allow_file)} if allow_file.is_file() else set()
    out = sorted(n for n in names if len(n) >= 3 and n.lower() not in allow)
    # R21: a name that collides with a skill-ledger term or alias is a tool name, not an application signal
    if (Path(private) / "raw" / "profile" / "profile.json").is_file():
        from askmike.corpus.extract import load_ledger
        terms = [t for sk in load_ledger(Path(private) / "raw") for t in (sk.term, *sk.aliases)]
        out = [n for n in out if not any(CompanyPattern(n).search_any_case(t) for t in terms)]
    if not out:
        raise ValueError("no applied-company names were derived from raw/jds")
    return out


class CompanyPattern:
    """Applied-company name: case-insensitive, flexible word separators, but a hit that is
    entirely lowercase is ignored (ordinary words, hostnames). Exposes .search and .pattern."""

    def __init__(self, name: str):
        self.name = name
        words = [re.escape(w) for w in re.split(r"[ _-]+", name) if w]
        self.pattern = r"\b" + "[ -]?".join(words) + r"(?-i:(?![a-z]))"
        self._rx = re.compile(self.pattern, re.I)

    def search(self, text: str):
        for m in self._rx.finditer(text):
            if m.group(0) != m.group(0).lower():
                return m
        return None

    def search_any_case(self, text: str):
        """Every hit, lowercase included: for machine strings (ids, slugs, paths, source labels)."""
        return self._rx.search(text)


def any_case_hit(p, text: str):
    """Search a machine string with any private pattern kind, lowercase hits included."""
    f = getattr(p, "search_any_case", None)
    return f(text) if f is not None else p.search(text)


class PatternFileError(ValueError):
    """A denylist or don't-print line failed its self-check. The message names the file and the
    line number only, never the line's text, so it is safe to print."""


_ESCAPE_HINT = "write a literal through re.escape, or start a pure regex with 're:'"
_ESCAPED_PUNCT = re.compile(r"\\([^0-9A-Za-z])")


def _pattern_file(f: Path) -> list[re.Pattern]:
    """One case-insensitive regex per non-comment line, each self-checked. A plain line must match
    the text it spells, reading a backslash-escaped punctuation character (re.escape output) as that
    character; so an unescaped literal such as "$1,000" is caught ("$" is an anchor). A line starting
    with "re:" is a pure regex and skips that check. Errors name the file and line number, never the text."""
    out: list[re.Pattern] = []
    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        body, self_check = (s[3:].strip(), False) if s.startswith("re:") else (s, True)
        where = f"{f.name} line {n}"
        try:
            rx = re.compile(body, re.I)
        except re.error:
            raise PatternFileError(f"{where}: not a valid regex; {_ESCAPE_HINT}") from None
        if rx.search(""):
            raise PatternFileError(f"{where}: matches empty text, so it would match everything; {_ESCAPE_HINT}")
        if self_check and not rx.search(_ESCAPED_PUNCT.sub(r"\1", body)):
            raise PatternFileError(f"{where}: does not match its own text; {_ESCAPE_HINT}")
        out.append(rx)
    if not out:
        raise PatternFileError(f"{f.name} has no patterns")
    return out


def load_private_patterns(private: Path) -> list:
    """Identity + don't-print (case-insensitive regexes), then CompanyPattern objects.

    Fails closed: a missing denylist, don't-print file, jds index or applied folder raises
    FileNotFoundError; no derivable company names, a pattern file with no patterns, or a pattern
    line that fails its self-check raises ValueError.
    """
    private = Path(private)
    pats: list = []
    for name in ("denylist.local.txt", "dontprint.local.txt"):
        f = private / name
        if not f.is_file():
            raise FileNotFoundError(f"{name} is missing from the private folder")
        pats += _pattern_file(f)
    pats += [CompanyPattern(n) for n in applied_company_names(private)]
    return pats


def load_honesty_allow(private: Path) -> frozenset:
    """R27: aliases (lowercase) the honesty gate ignores in claim/detail text. Missing file = empty."""
    f = Path(private) / "honesty_allow.local.txt"
    return frozenset(x.lower() for x in _lines(f)) if f.is_file() else frozenset()


# ---- gates -----------------------------------------------------------------

def _f(gate: str, u: Unit, msg: str) -> Finding:
    return Finding(gate, u.id, msg)


def privacy(u: Unit, ctx: GateContext) -> list[Finding]:
    plain = [p for p in ctx.private_patterns if not isinstance(p, CompanyPattern)]
    cos = [p for p in ctx.private_patterns if isinstance(p, CompanyPattern)]
    out: list[Finding] = []
    for field in ("claim", "detail", "quote"):
        for msg in privacy_hits(getattr(u, field), plain, cos):
            out.append(_f("privacy", u, f"{field}: {msg}"))
    return out


def slop(u: Unit, ctx: GateContext) -> list[Finding]:
    return [_f("slop", u, f"{field}: {m}")
            for field in ("claim", "detail") for m in slop_hits(getattr(u, field))]


_MIKE = re.compile(r"\bMike\b")


def voice(u: Unit, ctx: GateContext) -> list[Finding]:
    out: list[Finding] = []
    for field in ("claim", "detail"):
        if first_person(getattr(u, field)):
            out.append(_f("voice", u, f"{field}: first person"))
    if not _MIKE.search(u.claim):
        out.append(_f("voice", u, 'claim does not contain "Mike"'))
    if len(u.claim) > CLAIM_MAX:
        out.append(_f("voice", u, f"claim is {len(u.claim)} characters (max {CLAIM_MAX})"))
    if len(u.detail) > DETAIL_MAX:
        out.append(_f("voice", u, f"detail is {len(u.detail)} characters (max {DETAIL_MAX})"))
    return out


_LOW = {"none", "familiar", "learning"}


def honesty(u: Unit, ctx: GateContext) -> list[Finding]:
    out: list[Finding] = []
    for field in ("claim", "detail"):
        for s in low_depth_hits(getattr(u, field), ctx.ledger, ctx.honesty_allow):
            out.append(_f("honesty", u, f"{field}: asserts {s.depth} skill {s.term!r}"))
    for s in (x for x in ctx.ledger if x.depth in _LOW):
        if any(r.term.lower() in {t.lower() for t in (s.term, *s.aliases)} for r in u.skills):
            out.append(_f("honesty", u, f"skills: lists {s.depth} skill {s.term!r}"))
    for r in u.skills:
        if r.depth in _LOW:
            out.append(_f("honesty", u, f"skills: {r.term!r} is tagged {r.depth}"))
    return out


# ---- numbers ---------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?([KMB](?![A-Za-z])|%)?")


def digit_tokens(text: str) -> list[str]:
    """Digit runs as written: thousands commas, decimals, trailing K/M/B/% suffix."""
    return [m.group(0) for m in _NUM.finditer(text)]


def _norm(tok: str) -> str:
    return tok.replace(",", "")


def _is_year(tok: str) -> bool:
    return re.fullmatch(r"\d{4}", tok) is not None and 1990 <= int(tok) <= 2030


def ground_numbers(u: Unit, ctx: GateContext) -> tuple[tuple[Number, ...], list[str]]:
    src = {_norm(t) for t in digit_tokens(ctx.source_text(u))}
    grounded: list[Number] = []
    ungrounded: list[str] = []
    seen: set[str] = set()
    for tok in digit_tokens(u.claim + "\n" + u.detail):
        if tok in seen or _is_year(tok):
            continue
        seen.add(tok)
        if _norm(tok) in src:
            grounded.append(Number(tok, "text"))
            continue
        hits = ctx.facts.match(_norm(tok)) or ctx.facts.match(tok)
        if hits:
            grounded.append(Number(tok, hits[0]))
        else:
            ungrounded.append(tok)
    return tuple(grounded), ungrounded


def numbers(u: Unit, ctx: GateContext) -> list[Finding]:
    _, bad = ground_numbers(u, ctx)
    # Name figures by position only: the tokens may be phone, street or salary digits.
    return [_f("numbers", u, f"claim/detail figure #{i} of {len(bad)} is ungrounded")
            for i in range(1, len(bad) + 1)]


def run_gates(u: Unit, ctx: GateContext) -> list[Finding]:
    out: list[Finding] = []
    for gate in (privacy, slop, voice, honesty, numbers):
        out += gate(u, ctx)
    return out
