"""Generic style and privacy patterns, copied from jb (no jb imports)."""
from __future__ import annotations

import re

# copied from jb raw/rules/build.py:2049 on 2026-11-03
SLOP = ["—", "–", r"\bleverag", r"\brobust\b", r"\bseamless", r"cutting[- ]edge",
        r"\binnovative\b", r"\bspearhead", r"\bdelve", r"end[- ]to[- ]end", r"AI-native",
        r"\bpassionate\b", r"\btransformative\b", r"\bnot just\b", r"\bsynerg",
        r"\bworld-class\b", r"\bstate-of-the-art\b", r"\bgame[- ]chang", r"\bactually\b"]

HYPE = ["exceptional", "world-class", "passionate", "rockstar", "visionary", "ninja", "guru"]

# copied from jb raw/rules/pf_gates.py:74 on 2026-11-03
PHONE = re.compile(r"(?<!\d)(?:\+?1[-. ])?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
# copied from jb raw/rules/pf_gates.py:75 on 2026-11-03
STREET = re.compile(r"(?i)\b\d{1,5}\s+[A-Z][A-Za-z]+\s+"
                    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Way|Court|Ct|"
                    r"Circle|Cir|Terrace|Ter|Boulevard|Blvd|Place|Pl)\b\.?")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PUBLIC_EMAIL = "mike@forn.al"

_NUMW = (r"(?:\d[\d,]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozens?|hundreds?"
         r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)")
# a count of applications, interviews, rejections or offers; "applied to/for" plus a number
JOB_SEARCH = [
    re.compile(rf"\b{_NUMW}\s+(?:[\w-]+\s+){{0,2}}(?:applications?|interviews?|rejections?|offers?)\b", re.I),
    re.compile(rf"\bapplied\s+(?:to|for)\s+(?:about\s+|around\s+|over\s+|roughly\s+|nearly\s+)?{_NUMW}\b", re.I),
]

# a score, rating or hiring verdict on Mike
_FM = r"(?:fit|match)"
VERDICT = [
    re.compile(rf"\b(?:perfect|ideal|great|strong|good|excellent|poor|weak)\s+{_FM}\b", re.I),
    re.compile(rf"\bnot\s+a\s+(?:good|right|strong)\s+{_FM}\b", re.I),
    re.compile(rf"\b{_FM}\s+(?:score|rating)\b", re.I),
    re.compile(rf"\d\s?%\s+{_FM}\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s?/\s?(?:10|100)\b"),
    re.compile(r"\bout\s+of\s+(?:10|ten|5|five|100)\b", re.I),
    re.compile(r"\b(?:highly|well|over|under)[- ]?qualified\b", re.I),
    re.compile(r"\b(?:best|top)\s+candidate\b", re.I),
    re.compile(r"\brecommend(?:s|ed)?\s+hiring\b", re.I),
    re.compile(r"\bshould\s+(?:hire|interview)\b", re.I),
]
