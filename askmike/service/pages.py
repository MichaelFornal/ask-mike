"""The pages Ask Mike serves: server-rendered HTML in forn.al's design, with no JavaScript at all.

Jinja2 with autoescape on renders every page, so all visitor and model text is escaped. No page has a script, an
inline style or an event handler. A visitor sees only:
- fixed strings (askmike/answer/config.py and the template labels);
- gated claims;
- corpus text (the fallback's units and the evidence pages).

The answer section follows render_text: one line per kept claim (`need → text` for a jd) with its unit ids, each a
link to /evidence/<id>. A fixed outcome shows its fixed string. A fallback shows its intro, then its units' claims.
Every answer page then shows NEXT_STEP. The URL and email in a fixed string are links. Model and visitor text is
never linked.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from askmike.answer.config import EMPTY_TEXT, NEXT_STEP, OFF_TOPIC_TEXT
from askmike.answer.evidence import canonical_subject
from askmike.answer.render import ARROW, FIXED, load_fallback

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
STYLE = STATIC / "style.css"
SAVED_LINE = "Questions are saved so Mike can read them."  # the page's one line about the visitor log
ASKED_KINDS = ("question", "handoff")  # inputs shown back as typed; a jd shows its requirements, never the posting

_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True, undefined=StrictUndefined,
                   trim_blocks=True, lstrip_blocks=True)
# an https URL (trailing punctuation left out) or an email address, in a fixed string
_LINKABLE = re.compile(r"https://[^\s]*[^\s.,;:!?)]|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


@lru_cache(maxsize=1)
def style_href() -> str:
    """The stylesheet's URL, versioned by its content so a new release is never served a stale copy."""
    return f"/static/style.css?v={hashlib.sha256(STYLE.read_bytes()).hexdigest()[:12]}"


def load_units(path) -> dict:
    """The corpus units, keyed by id (corpus/units.jsonl)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return {u["id"]: u for u in (json.loads(x) for x in lines if x.strip())}


def linked(text: str) -> list[tuple[str, str]]:
    """A fixed string as (text, href) parts, href "" for plain text: each https URL and email address in it becomes
    a link. Only for the fixed strings in askmike/answer/config.py, never for model or visitor text."""
    parts: list[tuple[str, str]] = []
    at = 0
    for m in _LINKABLE.finditer(text):
        if m.start() > at:
            parts.append((text[at:m.start()], ""))
        s = m.group(0)
        parts.append((s, s if s.startswith("https://") else f"mailto:{s}"))
        at = m.end()
    if at < len(text):
        parts.append((text[at:], ""))
    return parts


def _render(name: str, **ctx) -> str:
    base = {"title": "", "page": "", "box": False, "prev": None, "saved_line": SAVED_LINE, "style_href": style_href(),
            "description": EMPTY_TEXT}
    return _env.get_template(name).render(**{**base, **ctx})


def _line(need: str, text: str, units) -> dict:
    return {"need": need, "text": text, "units": list(units)}


def _fallback_view(units: dict) -> dict:
    fb = load_fallback()
    return {"fixed": None, "intro": fb["intro"],
            "lines": [_line("", units[u]["claim"], [u]) for u in fb["units"] if u in units]}


def _answer_view(o, units: dict) -> dict:
    """What the answer section shows, line for line as render_text."""
    if o.outcome in FIXED:
        return {"fixed": linked(FIXED[o.outcome]), "intro": "", "lines": []}
    if o.outcome == "answer" and o.kept:
        return {"fixed": None, "intro": "",
                "lines": [_line(c.need if o.kind == "jd" and c.need else "", c.text, c.units) for c in o.kept]}
    return _fallback_view(units)


def home_page() -> str:
    return _render("home.html", page="home", box=True, lede=EMPTY_TEXT)


def not_found_page() -> str:
    """An unknown evidence id or path: the off-topic line and the box."""
    return _render("home.html", title="Not found", page="not-found", box=True, lede=OFF_TOPIC_TEXT)


def answer_page(o, units: dict, text, prev: str | None) -> str:
    """The answer page for one Outcome: what was asked, the answer, NEXT_STEP and the box. `text` is the visitor's
    input, shown only for a question; a jd shows its requirements. The box carries `prev` only after an answer."""
    asked = text if o.kind in ASKED_KINDS and isinstance(text, str) else ""
    requirements = [r.text for r in o.requirements] if o.kind == "jd" else []
    return _render("answer.html", title="Answer", page="answer", box=True,
                   prev=prev if o.outcome == "answer" else None, asked=asked, requirements=requirements,
                   arrow=ARROW, next_step=linked(NEXT_STEP), **_answer_view(o, units))


def fallback_page(units: dict) -> str:
    """The fallback page with nothing asked: what the app serves when a request fails, and the static fallback.html
    Caddy serves while the app is down."""
    return _render("answer.html", page="answer", box=True, asked="", requirements=[], arrow=ARROW,
                   next_step=linked(NEXT_STEP), **_fallback_view(units))


def evidence_page(unit: dict) -> str:
    """One corpus unit: its subject's display name, when, claim and detail."""
    when = (unit.get("when") or "").replace("..", " to ")
    subject = canonical_subject(unit["subject"])
    return _render("evidence.html", title=subject, page="evidence", subject=subject, when=when,
                   claim=unit["claim"], detail=unit.get("detail") or "", unit_id=unit["id"])
