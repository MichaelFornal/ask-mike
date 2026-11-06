"""Text checks shared by the unit gates and the answer layer. Messages name a pattern by
index or kind, never by the matched text, so they are safe to print."""
from __future__ import annotations

import re

from askmike.corpus.extract import Skill
from askmike.gates.patterns import (
    EMAIL, HYPE, JOB_SEARCH, PHONE, PUBLIC_EMAIL, SLOP, STREET, VERDICT,
)

LOW_DEPTHS = frozenset({"none", "familiar", "learning"})
CLAIMABLE_DEPTHS = frozenset({"working", "strong"})


def _term_re(term: str) -> re.Pattern:
    # R27: a term or alias of 3 characters or fewer ("R", "Go", "ids") matches its Capitalized or
    # ALL-CAPS form only, case-sensitively. Longer ones match in any case.
    if len(term) <= 3:
        cap = term[:1].upper() + term[1:].lower()
        body, flags = "(?:" + re.escape(cap) + "|" + re.escape(term.upper()) + ")", 0
    else:
        body, flags = re.escape(term), re.I
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", flags)


def privacy_hits(text: str, patterns: list, companies: list,
                 allow_names: frozenset[str] = frozenset()) -> list[str]:
    """patterns: objects with .search(text). companies: the same, plus a .name or .pattern
    attribute. Company indices continue after the pattern indices."""
    out: list[str] = []
    if PHONE.search(text):
        out.append("phone number")
    if STREET.search(text):
        out.append("street address")
    if any(m.group(0).lower() != PUBLIC_EMAIL for m in EMAIL.finditer(text)):
        out.append("email address")
    for i, p in enumerate(patterns):
        if p.search(text):
            out.append(f"private pattern #{i}")
    for j, c in enumerate(companies):
        m = c.search(text)
        if not m:
            continue
        name = getattr(c, "name", None)
        if name is not None and name.lower() in allow_names:
            continue
        out.append(f"applied-company #{len(patterns) + j}")
    for i, p in enumerate(JOB_SEARCH):
        if p.search(text):
            out.append(f"job-search count #{i}")
    return out


def slop_hits(text: str) -> list[str]:
    out: list[str] = []
    for pat in SLOP:
        if re.search(pat, text, re.I):
            out.append({"—": "em dash", "–": "en dash"}.get(pat, pat.replace("\\b", "")))
    for w in HYPE:
        if re.search(rf"\b{re.escape(w)}\b", text, re.I):
            out.append(f"hype word {w!r}")
    return out


_FIRST_CASE = re.compile(r"\b(?:I|I'm|I've|I'll|I'd)\b")
# "us" stays out: it collides with "US" (work authorization)
_FIRST_LOWER = re.compile(r"\b(?:my|me|myself|mine|we|our|ourselves)\b", re.I)


def first_person(text: str) -> bool:
    return bool(_FIRST_CASE.search(text) or _FIRST_LOWER.search(text))


def verdict_hits(text: str) -> list[str]:
    return [f"verdict pattern #{i}" for i, p in enumerate(VERDICT) if p.search(text)]


def as_skill(row: dict) -> Skill:
    return Skill(row["term"], row["depth"], tuple(row.get("aliases") or ()))


def _depth_hits(text: str, ledger: list, allow: frozenset, depths: frozenset) -> list:
    out = []
    for s in ledger:
        if s.depth not in depths:
            continue
        names = (s.term, *(a for a in s.aliases if a.lower() not in allow))
        if any(_term_re(t).search(text) for t in names):
            out.append(s)
    return out


def low_depth_hits(text: str, ledger: list, allow: frozenset) -> list:
    return _depth_hits(text, ledger, allow, LOW_DEPTHS)


def claimable_hits(text: str, ledger: list, allow: frozenset) -> list:
    return _depth_hits(text, ledger, allow, CLAIMABLE_DEPTHS)


# ---- links ------------------------------------------------------------------

ALWAYS_OK_DOMAIN = "forn.al"
_URL = re.compile(r"https?://\S+", re.I)
_BARE = re.compile(r"(?<![\w@.-])(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|al|dev|app|co|us|uk|me|xyz|info|example)\b(?:[/?#]\S*)?", re.I)
_TRAIL = ".,;:!?)]}>\"'\u201d\u2019"


def link_tokens(text: str) -> list[str]:
    """Every http(s) URL and every bare domain in text, lowercased, trailing punctuation stripped. A bare
    domain inside a URL is listed as well. A bare domain keeps any path that follows it (`github.com/x/y` is one token). An email address is not a link (privacy's job)."""
    out = [m.group(0).lower().rstrip(_TRAIL) for m in _URL.finditer(text)]
    out += [m.group(0).lower().rstrip(_TRAIL) for m in _BARE.finditer(text)]
    return out


def always_ok_link(token: str) -> bool:
    """True for a token whose host is forn.al or a subdomain of it."""
    host = re.sub(r"^https?://", "", token, flags=re.I)
    host = re.split(r"[/?#:]", host, maxsplit=1)[0].lower()
    return host == ALWAYS_OK_DOMAIN or host.endswith("." + ALWAYS_OK_DOMAIN)
