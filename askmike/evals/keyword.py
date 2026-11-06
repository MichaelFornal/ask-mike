"""Keyword resume-match baseline ("KW").

_forms, _pattern and _hit are adapted from jb apply/resume/jd_match.py on 2026-11-03.
"""
from __future__ import annotations

import re

from askmike.retrieval.bm25 import Hit
from askmike.retrieval.index import unit_text

# adapted from jb apply/resume/jd_match.py on 2026-11-03
def _norm(text):
    return re.sub(r"\s+", " ", (text or "").replace("​", "")).strip().lower()


# adapted from jb apply/resume/jd_match.py on 2026-11-03
def _forms(phrase):
    """A phrase, its hyphen/space spellings and its plural/singular. No plural for two-letter terms."""
    p = _norm(phrase)
    forms = {p, p.replace("-", " "), p.replace(" ", "-")}
    if len(p) <= 2:
        return forms
    for f in list(forms):
        if f[-1:].isalpha():
            forms |= {f + "s", f + "es"}
        if f.endswith("s") and len(f) > 3:
            forms.add(f[:-1])
    return forms


# adapted from jb apply/resume/jd_match.py on 2026-11-03
def _pattern(phrase):
    """Whole-word, case-insensitive; a two-letter term must not end in a hyphenated compound."""
    p = _norm(phrase)
    tail = r"(?![a-z0-9])" + (r"(?!-[a-z])" if len(p) <= 2 else "")
    alt = "|".join(re.escape(f) for f in sorted(_forms(p), key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + alt + ")" + tail)


# adapted from jb apply/resume/jd_match.py on 2026-11-03
def _hit(text, phrase):
    return bool(_norm(phrase)) and _pattern(phrase).search(_norm(text)) is not None


_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")
STOP = frozenset("""a an and are as at be but by can for from has have in into is it its of on or our that the their
them they this to we will with you your who what when where which how do does did not any all more most such than
then these those using use used including include able ability work working experience years year strong
knowledge understanding etc per via within across about over other also both each new""".split())


def content_words(query: str) -> list[str]:
    return list(dict.fromkeys(w for w in _WORD.findall(_norm(query)) if len(w) >= 2 and w not in STOP))


def keyword_rank(query: str, units: list[dict], skills: list[dict], k: int = 10) -> list[Hit]:
    """Score = distinct query content words plus distinct mentioned ledger entries that hit the unit text."""
    words = content_words(query)
    entries = []
    for s in skills:
        names = [s["term"], *s.get("aliases", [])]
        if any(_hit(query, n) for n in names if len(_norm(n)) >= 2):
            entries.append(names)
    scored = []
    for u in units:
        text = unit_text(u)
        n = sum(1 for w in words if _hit(text, w))
        n += sum(1 for names in entries if any(_hit(text, x) for x in names))
        if n:
            scored.append((-n, u["id"]))
    scored.sort()
    return [Hit(uid, float(-neg)) for neg, uid in scored[:k]]
