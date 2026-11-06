"""The eight gates every model claim passes before display, and the private gate-config bundle.

Gates, in order: structure, grounding, honesty, slop, privacy, voice, verdict, length. A verdict's
reasons are gate names only, never the matched text, so verdicts are safe to store and print.

The bundle ($ASK_PRIVATE_DIR/deploy/gate_config.json, private, never committed) carries the don't-print
patterns, the applied-company names and the honesty allow list to the server, which has no private
folder. `AnswerGateConfig.from_bundle` and `.from_private` give the same gates for the same files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from askmike.answer.config import ANSWER_MAX, MAX_CLAIMS, NEED_MAX, TEXT_MAX
from askmike.answer.evidence import Claim, Pack, _ledger, canonical_subject
from askmike.gates.checks import (
    _NUM, CompanyPattern, _is_year, _norm, digit_tokens, load_honesty_allow, load_private_patterns,
)
from askmike.gates.text import (
    LOW_DEPTHS, _term_re, always_ok_link, claimable_hits, first_person, link_tokens, low_depth_hits, privacy_hits,
    slop_hits, verdict_hits,
)

GATES = ("structure", "grounding", "honesty", "slop", "privacy", "voice", "verdict", "length")
BUNDLE = Path("deploy") / "gate_config.json"  # under $ASK_PRIVATE_DIR
_TYPES = {"jd": ("match", "gap"), "question": ("direct", "gap")}


# ---- config ----------------------------------------------------------------

def split_patterns(pats) -> tuple[list, list]:
    """M0's loader returns plain regexes, then CompanyPattern objects; the answer gates keep them apart."""
    return ([p for p in pats if not isinstance(p, CompanyPattern)],
            [p for p in pats if isinstance(p, CompanyPattern)])


@dataclass(frozen=True)
class AnswerGateConfig:
    patterns: list  # plain private regexes: the identity denylist, then the don't-print list
    companies: list  # one CompanyPattern per applied-company name
    honesty_allow: frozenset  # lowercase aliases the honesty gate reads as ordinary words
    skills: list  # the skills ledger; skills.json rows become Skill objects

    def __post_init__(self):
        object.__setattr__(self, "skills", _ledger(self.skills))

    @classmethod
    def from_private(cls, private, skills) -> AnswerGateConfig:
        """Read the private files through M0's loaders, which fail closed."""
        plain, companies = split_patterns(load_private_patterns(Path(private)))
        return cls(plain, companies, load_honesty_allow(Path(private)), skills)

    @classmethod
    def from_bundle(cls, path, skills) -> AnswerGateConfig:
        """Read a bundle written by `write_bundle`. A missing file raises FileNotFoundError; a missing,
        empty or malformed field raises ValueError, as M0's loaders do. Messages never echo a pattern."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("gate_config.json is not an object; rebuild it with gate-config")
        for key in ("patterns", "companies", "honesty_allow"):
            v = data.get(key)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"gate_config.json field {key!r} is missing or not a list of strings")
        if not isinstance(data.get("built_from"), dict):
            raise ValueError("gate_config.json field 'built_from' is missing")
        for key in ("patterns", "companies"):
            if not data[key]:
                raise ValueError(f"gate_config.json has no {key}; rebuild it with gate-config")
        patterns = []
        for i, p in enumerate(data["patterns"]):
            try:
                rx = re.compile(p, re.I)
            except re.error:
                raise ValueError(f"gate_config.json pattern #{i} is not a valid regex") from None
            if rx.search(""):
                raise ValueError(f"gate_config.json pattern #{i} matches empty text")
            patterns.append(rx)
        return cls(patterns, [CompanyPattern(n) for n in data["companies"]],
                   frozenset(x.lower() for x in data["honesty_allow"]), skills)


# ---- the bundle ------------------------------------------------------------

_INPUTS = ("denylist.local.txt", "dontprint.local.txt", "company_allow.local.txt", "honesty_allow.local.txt",
           "raw/jds/top50_index.json", "raw/profile/profile.json")
_APPLIED = "raw/jds/applied/*.txt"  # the loader reads only these file names, so the name list is hashed


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def built_from(private) -> dict:
    """sha256 of every private input the bundle is built from; None for one that is absent."""
    private = Path(private)
    out: dict = {rel: _sha((private / rel).read_bytes()) if (private / rel).is_file() else None for rel in _INPUTS}
    applied = private / "raw" / "jds" / "applied"
    names = "\n".join(sorted(f.name for f in applied.glob("*.txt"))) if applied.is_dir() else None
    out[_APPLIED] = _sha(names.encode("utf-8")) if names is not None else None
    return out


def build_bundle(private) -> dict:
    stamp = built_from(private)  # hashed first: a file edited mid-build leaves the bundle stale, not current
    cfg = AnswerGateConfig.from_private(private, [])
    return {"patterns": [p.pattern for p in cfg.patterns], "companies": [c.name for c in cfg.companies],
            "honesty_allow": sorted(cfg.honesty_allow), "built_from": stamp}


def write_bundle(private, path=None) -> dict:
    """Write the bundle (default $ASK_PRIVATE_DIR/deploy/gate_config.json), owner-only. Returns its counts."""
    path = Path(path) if path is not None else Path(private) / BUNDLE
    data = build_bundle(private)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"patterns": len(data["patterns"]), "companies": len(data["companies"]),
            "honesty_allow": len(data["honesty_allow"])}


def bundle_stale(path, private) -> bool:
    """True when the bundle is missing or unreadable, or a private input changed since it was built."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(data, dict) or data.get("built_from") != built_from(private)


# ---- text helpers ----------------------------------------------------------

_DASH = re.compile(r"[\u2014\u2013]")
_DASH_DIGITS = re.compile(r"(?<=\d)\s*[\u2014\u2013]+\s*(?=\d)")
_DASH_ANY = re.compile(r"\s*[\u2014\u2013]+\s*")


def tidy(s: str) -> str:
    """An em or en dash between digits becomes "-"; any other becomes ", "."""
    if not _DASH.search(s):
        return s
    s = _DASH_ANY.sub(", ", _DASH_DIGITS.sub("-", s))
    s = re.sub(r",\s*([,.;:!?])", r"\1", s)  # a dash before punctuation leaves no stray comma
    return s.strip().strip(",").strip()


_WORD = re.compile(r"[A-Za-z0-9]+")
_INITIAL = re.compile(r"(?:\A|[.!?\n])[\s\"'\u201c\u2018(\[]*\Z")
_ABBREV_END = re.compile(r"\b(?:e\.g|i\.e)\.\Z", re.I)  # "e.g." and "i.e." do not end a sentence
_SENT_END = r"(?<!\be\.g)(?<!\bi\.e)[.!?](?=\s|\Z)"
_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()
_EXEMPT = frozenset({"mike", "fornal", *_MONTHS})  # anywhere; "Mike's" splits into "Mike" + "s"
# The only words exempt at a sentence start: pronouns, articles, prepositions, conjunctions and common
# starters. Any other capitalised first word is a name token, so an invented name cannot open a sentence.
SENTENCE_STARTERS = frozenset("""
    i he she it we you they his her its our your their him them this that these those there here who which
    what where how why a an the in at on for with without by from to of as after before during over under
    across through into about between within since until via per using like and but or so yet while when
    although though because if once both each either neither every all most some many another other also
    then not no
""".split())


def _sentence_start(text: str, pos: int) -> bool:
    m = _INITIAL.search(text, 0, pos)
    return bool(m) and not _ABBREV_END.search(text, 0, m.start() + 1)


def _name_spans(text: str) -> list[tuple[str, int]]:
    out = []
    for m in _WORD.finditer(text):
        w = m.group(0)
        if not any(ch.isalpha() for ch in w) or w.lower() in _EXEMPT:
            continue
        if any(ch.isupper() for ch in w[1:]):
            out.append((w, m.start()))
        elif w[0].isupper() and not (w.lower() in SENTENCE_STARTERS and _sentence_start(text, m.start())):
            out.append((w, m.start()))
    return out


def name_tokens(text: str) -> list[str]:
    """Words with an inner capital, and capitalised words except a sentence-initial word on
    SENTENCE_STARTERS. Mike, Fornal and month names are exempt. A word is a run of ASCII letters and
    digits holding at least one letter, so "Mike's" is "Mike" then "s"."""
    return [w for w, _ in _name_spans(text)]


def _whole_word(w: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", re.I)


_NEGATION = re.compile(r"\bhasn['\u2019]t\b|\bhas\s+not\b|\bnot\s+yet\b|\bnever\b"
                       r"|\bno\s+(?:[\w'\u2019-]+\s+){0,3}experience\b", re.I)
# Segment cuts: ";", a sentence break, before "closest", and before a conjunction that opens a new clause
# ("and has", "but he"), so one negation cannot cover a positive clause joined to it.
_CUT = re.compile(r";|" + _SENT_END + r"|(?=\bclosest\b)"
                  r"|(?=\b(?:and|but|while|whereas|although|though)\s+(?:he|mike|has|have|had|is|was|does|did|also)\b)",
                  re.I)


# A negation reaches its object only up to a clause boundary: a comma or one of these words ("yet" right
# after "not" belongs to the negation).
_BOUNDARY = re.compile(r",|\b(?:while|when|during|after|before|and|but|as|since|though|although|because)\b"
                       r"|(?<!\bnot\s)\byet\b", re.I)


def _segment_bounds(text: str) -> list[tuple[int, int]]:
    bounds, pos = [], 0
    for m in _CUT.finditer(text):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, len(text)))
    return [(a, b) for a, b in bounds if text[a:b].strip()]


def segments(text: str) -> list[str]:
    return [text[a:b] for a, b in _segment_bounds(text)]


def negation_scopes(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(scopes, tails) as (start, end) offsets. A scope runs from a negation to the first clause boundary
    after it, within its segment: what that negation governs. A tail runs from a segment's first negation
    to the segment's end: where a denial of a claimable skill is looked for."""
    scopes, tails = [], []
    for a, b in _segment_bounds(text):
        negs = list(_NEGATION.finditer(text, a, b))
        if negs:
            tails.append((negs[0].start(), b))
        for m in negs:
            stop = _BOUNDARY.search(text, m.end(), b)
            scopes.append((m.start(), stop.start() if stop else b))
    return scopes, tails


def _within(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


def _low_depth_starts(text: str, cfg) -> list[int]:
    """Where each low-depth term or (non-allowed) alias occurs: text.low_depth_hits, with positions."""
    return [m.start() for s in cfg.skills if s.depth in LOW_DEPTHS
            for t in (s.term, *(a for a in s.aliases if a.lower() not in cfg.honesty_allow))
            for m in _term_re(t).finditer(text)]


# ---- the gates -------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    claim: Claim  # as the gates judged it: ids outside the pack stripped, a bad req blanked, dashes tidied
    kept: bool
    reasons: tuple[str, ...]  # failing gate names, in GATES order
    tidied: bool
    stripped_ids: int


def _s(x) -> str:
    return x if isinstance(x, str) else ""


_CURRENCY = re.compile(r"[$\u20ac\u00a3\u00a5]\s*\Z")
_DATE_CUE = re.compile(r"(?:\b(?:in|since|from|until|through|by|during|of|early|mid|late|" + "|".join(_MONTHS)
                       + r")\.?,?[\s-]+|(?<!\d)\d{4}\s*-\s*)\Z", re.I)
_RANGE_START = re.compile(r"\s*-\s*\d{4}(?!\d)")  # "2019" in "2019-2021" opens a range
_FOUR = re.compile(r"(?<!\d)\d{4}(?!\d)")


def _number_ok(text: str, m: re.Match, nums: set, years: set) -> bool:
    """A year-shaped token is exempt after a date cue, in a range, or when a cited unit's claim, detail or
    `when` holds it; after a currency sign it is an amount. Every other token must be in a cited unit."""
    tok = m.group(0)
    if _is_year(tok) and not _CURRENCY.search(text, 0, m.start()):
        if tok in years or _DATE_CUE.search(text, 0, m.start()) or _RANGE_START.match(text, m.end()):
            return True
    return _norm(tok) in nums


def _grounded(text: str, cited: list[dict], ledger_names: str, input_text: str, scopes) -> bool:
    """Numbers come from the cited units. A name comes from the cited units or the ledger; the input
    grounds only a name inside a negation scope (`scopes` is empty for a match or direct claim)."""
    nums = {_norm(t) for u in cited for f in ("claim", "detail") for t in digit_tokens(_s(u.get(f)))}
    years = {y for u in cited for f in ("claim", "detail", "when") for y in _FOUR.findall(_s(u.get(f)))}
    if not all(_number_ok(text, m, nums, years) for m in _NUM.finditer(text)):
        return False
    hay = [ledger_names]
    for u in cited:
        subject = _s(u.get("subject"))
        hay += [_s(u.get("claim")), _s(u.get("detail")), subject, canonical_subject(subject)]
    hay = "\n".join(hay)
    for w, pos in _name_spans(text):
        rx = _whole_word(w)
        if not (rx.search(hay) or (_within(pos, scopes) and rx.search(input_text))):
            return False
    return True


def links_ok(text: str, need: str, cited: list[dict], input_text: str) -> bool:
    """Every link in `text` (forn.al aside) is stated by a cited unit's claim or detail. Every link in `need`
    is in a cited unit or the visitor's input, so a pasted posting cannot get its own link into an answer."""
    unit_hay = "\n".join(_s(u.get(f)) for u in cited for f in ("claim", "detail")).lower()
    need_hay = (unit_hay + "\n" + "\n".join(_s(u.get(f)) for u in cited for f in ("subject", "when")).lower()
                + "\n" + _s(input_text).lower())
    return (all(always_ok_link(t) or t in unit_hay for t in link_tokens(text))
            and all(always_ok_link(t) or t in need_hay for t in link_tokens(need)))


def _honest(ctype: str, text: str, need: str, cfg: AnswerGateConfig) -> bool:
    def low(s):
        return low_depth_hits(s, cfg.skills, cfg.honesty_allow)

    if ctype != "gap":  # a match or direct claim may not name a low-depth skill, in its text or its need
        return not low(text) and not low(need)
    scopes, tails = negation_scopes(text)
    if not scopes:
        return False
    # a low-depth skill appears only as the object of a negation, with no clause boundary between them
    if not all(_within(pos, scopes) for pos in _low_depth_starts(text, cfg)):
        return False
    # a negated tail that names a claimable skill and no low-depth one denies a skill Mike has
    return not any(claimable_hits(text[a:b], cfg.skills, cfg.honesty_allow) and not low(text[a:b])
                   for a, b in tails)


def input_company_names(cfg: AnswerGateConfig, input_text) -> frozenset:
    """Lowercased applied-company names the visitor's own input names: privacy lets these through in claim
    text and need (they never ground text). The eval audit calls this too, so both allow the same names."""
    input_text = _s(input_text)
    return frozenset(c.name.lower() for c in cfg.companies if c.search(input_text))


def gate_claims(claims, pack: Pack, units: dict, cfg: AnswerGateConfig, input_text: str,
                kind: str) -> tuple[list[Verdict], list[Claim]]:
    """One Verdict per claim, in model order, and the kept claims. Every gate runs on every claim (a claim over 4x the text or need limit gets only `length`), so
    `reasons` names each gate it fails. A claim that passes every gate is kept until MAX_CLAIMS claims
    or ANSWER_MAX characters of text are kept; from then on each such claim gets `length`."""
    if kind not in _TYPES:
        raise ValueError(f"no answer gates for kind {kind!r}")
    input_text = _s(input_text)
    in_pack = set(pack.unit_ids)
    allow_names = input_company_names(cfg, input_text)
    # Any ledger term or alias, at any depth, grounds a name (honesty bars a low-depth one outside a gap).
    # The input grounds only names a gap claim negates, so nothing the visitor names is credited to Mike;
    # the visitor's own company may appear in `need` (privacy allows it) but never grounds `text`.
    ledger_names = "\n".join(t for s in cfg.skills for t in (s.term, *s.aliases))
    verdicts: list[Verdict] = []
    kept: list[Claim] = []
    used, full = 0, False
    for c in claims:
        ctype, raw_text, raw_need = _s(c.type), _s(c.text), _s(c.need)
        ids: list = []
        for u in c.units if isinstance(c.units, (list, tuple)) else ():
            if u not in ids:
                ids.append(u)
        valid = tuple(u for u in ids if isinstance(u, str) and u in in_pack and u in units)
        req = _s(c.req) if kind == "jd" and _s(c.req) in pack.req_units else ""
        text, need = tidy(raw_text), tidy(raw_need)
        claim = Claim(ctype, need, req, text, valid)
        cited = [units[u] for u in valid]
        if len(text) > 4 * TEXT_MAX or len(need) > 4 * NEED_MAX:  # before every other gate: nothing runs on it
            verdicts.append(Verdict(claim, False, ("length",), (text, need) != (raw_text, raw_need),
                                    len(ids) - len(valid)))
            continue
        fails = {
            "structure": ctype not in _TYPES[kind] or not text.strip() or not valid,
            "grounding": not (_grounded(text, cited, ledger_names, input_text,
                                        negation_scopes(text)[0] if ctype == "gap" else ())
                              and links_ok(text, need, cited, input_text)),
            "honesty": not _honest(ctype, text, need, cfg),
            "slop": bool(slop_hits(text) or slop_hits(need)),
            "privacy": bool(privacy_hits(text, cfg.patterns, cfg.companies, allow_names)
                            or privacy_hits(need, cfg.patterns, cfg.companies, allow_names)),
            "voice": first_person(text) or first_person(need),
            "verdict": bool(verdict_hits(text) or verdict_hits(need)),
            "length": len(text) > TEXT_MAX or len(need) > NEED_MAX,
        }
        reasons = tuple(g for g in GATES if fails[g])
        if not reasons and (full or len(kept) >= MAX_CLAIMS or used + len(text) > ANSWER_MAX):
            full, reasons = True, ("length",)
        if not reasons:
            kept.append(claim)
            used += len(text)
        verdicts.append(Verdict(claim, not reasons, reasons, (text, need) != (raw_text, raw_need),
                                len(ids) - len(valid)))
    return verdicts, kept
