"""What a visitor sees. render_text takes a gated Outcome, never a raw model reply.

An answer shows only the claims the gates kept. Every fixed outcome shows its fixed string. A fallback,
or any outcome this module does not know, shows the reviewed fallback page from fallback.json.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from askmike.answer.config import EMPTY_TEXT, HANDOFF_TEXT, NEXT_STEP, OFF_TOPIC_TEXT, TOO_LONG_TEXT

FALLBACK_PATH = Path(__file__).with_name("fallback.json")
ARROW = "→"
FIXED = {"handoff": HANDOFF_TEXT, "off_topic": OFF_TOPIC_TEXT, "empty": EMPTY_TEXT, "too_long": TOO_LONG_TEXT}


@lru_cache(maxsize=1)
def _fallback() -> dict:
    return json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))


def load_fallback() -> dict:
    """{"intro": str, "units": [unit ids]}: the fixed, reviewed fallback page (Mike approves the set)."""
    fb = _fallback()
    return {"intro": fb["intro"], "units": list(fb["units"])}


def _ids(units) -> str:
    return "[" + ", ".join(units) + "]"


def _fallback_lines(units: dict) -> list[str]:
    fb = _fallback()
    lines = [fb["intro"]]
    lines += [f"- {units[u]['claim']} {_ids([u])}" for u in fb["units"] if u in units]
    return lines


def render_text(o, units: dict) -> str:
    """Plain text for one Outcome. `units` is the corpus, keyed by id, for the fallback page's claims."""
    if o.outcome in FIXED:
        return FIXED[o.outcome]
    if o.outcome == "answer" and o.kept:
        lines = [f"- {c.need} {ARROW} {c.text} {_ids(c.units)}" if o.kind == "jd" and c.need
                 else f"- {c.text} {_ids(c.units)}" for c in o.kept]
    else:
        lines = _fallback_lines(units)
    return "\n".join([*lines, "", NEXT_STEP])
