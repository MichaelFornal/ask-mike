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
from askmike.service.results import results_view

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
STYLE = STATIC / "style.css"
SAVED_LINE = "Questions are saved so Mike can read them."  # the page's one line about the visitor log
RESULTS_DESCRIPTION = "How Ask Mike is measured: retrieval, answer and cost results from its public evals."
WAIT_QUESTION = "Reading Mike's evidence"
WAIT_JD = "Reading the job description and Mike's evidence"
JD_LABEL = "Your job description"
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


def _head(kind: str, text, waiting: bool, title: str) -> str:
    asked = text if kind in ASKED_KINDS and isinstance(text, str) and not (waiting and kind == "jd") else ""
    label = "You asked" if kind in ASKED_KINDS and asked else (JD_LABEL if waiting and kind == "jd" else "")
    wait = (WAIT_JD if kind == "jd" else WAIT_QUESTION) if waiting else ""
    return _render("answer_head.html", title=title, page="answer", label=label, asked=asked, wait=wait)


def answer_head(kind: str, text, waiting: bool) -> str:
    """The first part of an answer page, from the doctype through what was asked, ending with <main> open. A question
    or handoff shows `text` under "You asked". A jd shows only JD_LABEL, and only while waiting; its requirements
    come in the tail, and the posting is never shown. With `waiting` it adds the reading line."""
    return _head(kind, text, waiting, "Answer")


def _tail(o, units: dict, prev: str | None, requirements: list) -> str:
    return _render("answer_tail.html", box=True, prev=prev, requirements=requirements, arrow=ARROW,
                   next_step=linked(NEXT_STEP), **_answer_view(o, units))


def answer_tail(o, units: dict, prev: str | None) -> str:
    """The rest of an answer page: a jd's requirements, the answer, NEXT_STEP, the box and the closing tags. The box
    carries `prev` only after an answer."""
    requirements = [r.text for r in o.requirements] if o.kind == "jd" else []
    return _tail(o, units, prev if o.outcome == "answer" else None, requirements)


def failed_tail(units: dict) -> str:
    """The tail when the model call failed after the head went out: the fallback view, with no `prev`."""
    return _render("answer_tail.html", box=True, prev=None, requirements=[], arrow=ARROW,
                   next_step=linked(NEXT_STEP), **_fallback_view(units))


def answer_page(o, units: dict, text, prev: str | None) -> str:
    """The answer page for one Outcome: what was asked, the answer, NEXT_STEP and the box. `text` is the visitor's
    input, shown only for a question; a jd shows its requirements."""
    return answer_head(o.kind, text, False) + answer_tail(o, units, prev)


def fallback_page(units: dict) -> str:
    """The fallback page with nothing asked: what the app serves when a request fails, and the static fallback.html
    Caddy serves while the app is down."""
    return _head("", "", False, "") + failed_tail(units)


def results_page(facts: dict) -> str:
    """The results page: the evals of record from facts.json (askmike/service/results.py). Raises KeyError for a
    missing fact."""
    return _render("results.html", title="Results", page="results", description=RESULTS_DESCRIPTION,
                   **results_view(facts))


def evidence_page(unit: dict) -> str:
    """One corpus unit: its subject's display name, when, claim and detail."""
    when = (unit.get("when") or "").replace("..", " to ")
    subject = canonical_subject(unit["subject"])
    return _render("evidence.html", title=subject, page="evidence", subject=subject, when=when,
                   claim=unit["claim"], detail=unit.get("detail") or "", unit_id=unit["id"])
