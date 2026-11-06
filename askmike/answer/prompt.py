"""The answer prompt: one public template, filled in a single pass.

The same string goes to `claude -p` (stdin) and to the API (one user turn, no system prompt).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from askmike.answer.config import PREV_MAX
from askmike.answer.evidence import Pack, Turn, canonical_subject, req_key

TEMPLATE_PATH = Path(__file__).parent / "prompts" / "answer.md"
_TEMPLATE_BYTES = TEMPLATE_PATH.read_bytes()
TEMPLATE = _TEMPLATE_BYTES.decode("utf-8")
ANSWER_TEMPLATE_SHA = hashlib.sha1(_TEMPLATE_BYTES).hexdigest()[:12]

_KINDS = {"jd": "job description", "question": "question"}
_TAGS = ("question", "requirements", "previous_input", "previous_answer", "evidence")
_TAG = re.compile(r"<(\s*/?)\s*(" + "|".join(_TAGS) + r")\b", re.I)
_SLOT = re.compile(r"\{([a-z_]+)\}")


def _safe(s: str) -> str:
    """Break the prompt's own tags inside visitor-sourced text ("</question>" -> "</ question"),
    so it cannot close its block or open another (as M1's extract prompt does)."""
    return _TAG.sub(lambda m: "<" + m.group(1) + " " + m.group(2), s)


def _one_line(s) -> str:
    return " ".join(str(s or "").split())


def evidence_line(uid: str, unit: dict) -> str:
    """`[<id>] (<canonical subject>, <when>) <claim> <detail>`; an empty when or detail is left out."""
    when = _one_line(unit.get("when"))
    head = canonical_subject(unit.get("subject") or "") + (f", {when}" if when else "")
    body = " ".join(p for p in (_one_line(unit.get("claim")), _one_line(unit.get("detail"))) if p)
    return f"[{uid}] ({head}) {body}"


def _input_block(kind: str, text: str, requirements) -> str:
    if kind == "jd":  # the requirement list only, never the raw posting
        lines = [f"{req_key(r.id)} ({'required' if r.required else 'preferred'}): {_safe(_one_line(r.text))}"
                 for r in requirements]
        return "<requirements>\n" + "\n".join(lines) + "\n</requirements>"
    return "<question>\n" + _safe(text.strip()) + "\n</question>"


def _previous_block(previous: Turn | None) -> str:
    if previous is None:
        return ""
    claims = "\n".join(f"- {_safe(_one_line(c.text))} [{', '.join(c.units)}]" for c in previous.kept)
    return ("<previous_input>\n" + _safe(previous.text[:PREV_MAX].strip()) + "\n</previous_input>\n"
            "<previous_answer>\n" + (claims or "(none)") + "\n</previous_answer>\n\n")


def render_prompt(kind: str, text: str, requirements, pack: Pack, units: dict[str, dict],
                  previous: Turn | None) -> str:
    """Fill the template once. Values are inserted literally, so "{evidence}" in the input stays as typed.
    Every pack id must be in `units`: a missing one is a stale index or a forged turn, and raises KeyError."""
    if kind not in _KINDS:
        raise ValueError(f"no answer prompt for kind {kind!r}")
    values = {
        "kind": _KINDS[kind],
        "evidence": "\n".join(evidence_line(u, units[u]) for u in pack.unit_ids) or "(none)",
        "ledger": "\n".join(pack.ledger_lines) or "(none)",
        "previous": _previous_block(previous),
        "input": _input_block(kind, text, requirements),
    }
    return _SLOT.sub(lambda m: values[m.group(1)], TEMPLATE)
