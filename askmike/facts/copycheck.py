"""Copy check: every number in hand-written or generated Markdown (README, docs/m1/REPORT.md) checked against
facts.json. It reports and never writes.

  uv run python -m askmike.facts.copycheck README.md docs/m1/REPORT.md

Exit 0 when nothing is flagged, 1 when a number matches no fact, 2 when facts.json or a file is missing.

Ported from deal-terms-desk `facts/copycheck.py` (`check`, `render`) and its `dtd copycheck` command on 2026-11-03.
Changes: `@` joins the number lookbehind, so "recall@10" is an identifier, not a number; facts are
{value, display, cmd, inputs}, and a number that equals a fact's display string matches that fact; the
machine-built label logic is dropped.
"""
from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FACTS = Path(__file__).resolve().parents[2] / "facts.json"
FLAGGED = ("no fact",)
SHOWN = 4  # matching keys printed per number before "+k more"

FENCE = re.compile(r"^\s*(```|~~~)")
REFDEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$")  # [7]: https://… (a link reference definition)
# Code spans, link targets, footnote markers, HTML tags and bare URLs carry no copy numbers.
INLINE = re.compile(r"`[^`]*`|\]\([^)]*\)|\[\^[^\]]*\]|</?[A-Za-z][^>]*>|https?://\S+")
ORDINAL = re.compile(r"^(\s*)\d+[.)](?=\s)")
# Letters and digits are spelled out rather than \w, which holds "_": Markdown emphasis (_60%_, __88,628__) must not
# hide a number.
DATE = re.compile(r"(?<![A-Za-z0-9.-])\d{4}-\d{2}-\d{2}(?![A-Za-z0-9-])")
# A number that touches no letter, dot, hyphen, "@" or digit on either side: "M1", "R4", "v1.5", model ids and
# "recall@10" are names.
NUMBER = re.compile(r"(?<![A-Za-z0-9.@-])(-?)(\$?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)(?![A-Za-z0-9%]|\.\d)")


@dataclass(frozen=True)
class Found:
    line: int
    raw: str
    status: str
    keys: tuple[str, ...]


def _value(fact):
    return fact.get("value") if isinstance(fact, dict) else fact


def _display(fact):
    return fact.get("display") if isinstance(fact, dict) else None


def _match(m: re.Match, numeric: list[tuple[str, float]], displays: list[tuple[str, str]]):
    raw = m.group()
    shown_as = tuple(k for k, d in displays if d == raw)
    if shown_as:
        return "fact", shown_as
    sign, _dollar, whole, frac, pct = m.groups()
    digits = whole.replace(",", "")
    value = float(sign + digits + (frac or ""))
    places = len(frac) - 1 if frac else 0
    significant = len((digits + (frac or "")[1:]).lstrip("0"))
    exact, rounded = [], []
    for key, fact in numeric:
        # "60%": a rate shown as a percent, or a fact already held in percent (the _pct keys)
        shown = fact if (not pct or key.endswith("_pct")) else fact * 100
        if math.isclose(shown, value, rel_tol=1e-9, abs_tol=1e-9):
            exact.append(key)
        elif significant >= 2 and math.isclose(round(shown, places), value, rel_tol=1e-9, abs_tol=1e-9):
            rounded.append(key)
    if exact:
        return "fact", tuple(exact)
    if rounded:
        return "rounded", tuple(rounded)
    return "no fact", ()


def check(text: str, facts: dict) -> list[Found]:
    """Every number in `text` (Markdown), in reading order, with the facts it matches."""
    lines = text.splitlines()
    code, fenced = [], False
    for line in lines:
        if FENCE.match(line):
            code.append(True)
            fenced = not fenced
        else:
            code.append(fenced)
    values = {k: _value(f) for k, f in facts.items()}
    numeric = [(k, float(v)) for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    strings = [(k, v) for k, v in values.items() if isinstance(v, str)]
    strings += [(k, d) for k, f in facts.items() if isinstance(d := _display(f), str)]
    displays = [(k, d) for k, f in facts.items() if isinstance(d := _display(f), str)]
    found = []
    for i, line in enumerate(lines):
        if code[i] or REFDEF.match(line):
            continue
        prose = ORDINAL.sub(lambda m: m.group(1), INLINE.sub(" ", line))
        hits = []
        for m in DATE.finditer(prose):
            keys = tuple(dict.fromkeys(k for k, v in strings if m.group() in v))
            hits.append((m.start(), m.group(), "fact" if keys else "no fact", keys))
        prose = DATE.sub(lambda m: " " * len(m.group()), prose)
        for m in NUMBER.finditer(prose):
            hits.append((m.start(), m.group(), *_match(m, numeric, displays)))
        found += [Found(i + 1, raw, status, keys) for _, raw, status, keys in sorted(hits)]
    return found


def render(name: str, found: list[Found]) -> str:
    out = []
    for f in found:
        keys = ", ".join(f.keys[:SHOWN]) + (f" +{len(f.keys) - SHOWN} more" if len(f.keys) > SHOWN else "")
        out.append(f'{name}:{f.line}  "{f.raw}"  {f.status}  {keys}'.rstrip())
    exact = sum(f.status == "fact" for f in found)
    rounded = sum(f.status == "rounded" for f in found)
    flagged = sum(f.status in FLAGGED for f in found)
    numbers = f"{len(found)} number" + ("" if len(found) == 1 else "s")
    check_rounded = ": check each names the fact you mean" if rounded else ""
    out.append(f"{name}: {numbers} ({exact} exact, {rounded} rounded{check_rounded}), {flagged} flagged")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    files = list(sys.argv[1:] if argv is None else argv)
    if not files:
        print("usage: python -m askmike.facts.copycheck FILE [FILE ...]", file=sys.stderr)
        return 2
    if not FACTS.exists():
        print("facts.json missing; run `uv run python -m askmike.evals facts` first", file=sys.stderr)
        return 2
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        print(f"no such file: {', '.join(missing)}", file=sys.stderr)
        return 2
    facts = json.loads(FACTS.read_text(encoding="utf-8"))
    flagged = 0
    for name in files:
        found = check(Path(name).read_text(encoding="utf-8"), facts)
        print(render(name, found))
        flagged += sum(f.status in FLAGGED for f in found)
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
