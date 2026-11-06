"""Smoke test of the live Ask Mike, run from the repo root on the development machine after deploy/push.sh:

    uv run python deploy/smoke.py https://ask.forn.al [--ask "a question the corpus answers"]

Ported from deal-terms-desk deploy/smoke.py (branch caddy-split, 7976f1f) on 2026-11-05, for Ask Mike's pages.

Checks, in order:
- /health: git_sha, corpus_sha, index_sha and template_sha are this checkout's, and a budget state is present.
- GET /: status 200, the exact CSP (set by deploy/ask.caddy) and the saved line.
- A handoff question gets HANDOFF_TEXT, and an input of MAX_INPUT_CHARS + 1 characters gets TOO_LONG_TEXT. Both are
  fixed lines the app serves before any model call or rate limit.
- GET /evidence/<the fallback's first unit> and GET /static/style.css: status 200.
- GET /results: status 200, and its visible text shows this checkout's recall@10 for hybrid search (the
  m1_jdreq_R4_report_recall_at_10 display in facts.json), so the page is this release's. HEAD /: status 200.
- With --ask: the question gets an answer page or the fallback, either one with NEXT_STEP. That is one model call
  (Haiku, for a question) unless the answer is cached.
- Last, the burst: it re-asks the --ask question until the per-address limit answers 429 with the fallback page
  (NEXT_STEP present and Retry-After set), then sends the same question with a forged X-Forwarded-For, which must
  still get 429 (Caddy replaces that header). An `answer` comes back from the cache, so the burst makes no model
  call. It runs only after the --ask question came back as an answer (the page carries the follow-up field, which the
  app adds only for an answer, the one outcome the cache holds). A fallback is not cached, so the burst would call
  the model each time: it is not run and the smoke fails. Without --ask the rate-limit and forged-header checks cannot
  run, and the smoke fails saying so: it never passes without them. The burst leaves this machine rate-limited for up
  to an hour.

Pages are compared by their visible text, not raw HTML: Jinja escapes the apostrophe in HANDOFF_TEXT, and NEXT_STEP's
URL and address are links. The parser follows tests/page_html.py. The smoke prints check names, statuses and header
values only, never a question, page text or the visitor log (which it never reads). Exits 1 on any problem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from askmike.answer.config import HANDOFF_TEXT, MAX_INPUT_CHARS, NEXT_STEP, TOO_LONG_TEXT
from askmike.answer.render import load_fallback
from askmike.answer.settings import load_answer_settings
from askmike.service.pages import SAVED_LINE

REPO = Path(__file__).resolve().parents[1]
CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")
HANDOFF_Q = "What is Mike's hourly rate?"  # routes to handoff: a fixed line, no model call
FORGED = "203.0.113.9"  # a documentation address (TEST-NET-3)
FALLBACK = load_fallback()
BLOCK = frozenset("html head title body div header nav main section form label button p li ol ul h1 h2 h3 footer"
                  " textarea".split())


class _Visible(HTMLParser):
    """The body's visible text, one line per block element, and whether the page has the follow-up `prev` field."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.in_head = False
        self.has_prev = False

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        if tag == "input" and dict(attrs).get("name") == "prev":
            self.has_prev = True
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in BLOCK:
            self.parts.append("\n")
        if tag == "head":
            self.in_head = False

    def handle_data(self, data):
        if not self.in_head:
            self.parts.append(data)


def read_page(raw: bytes) -> tuple[list[str], bool]:
    """(the page's visible lines with whitespace collapsed, whether it carries the follow-up field)."""
    p = _Visible()
    p.feed(raw.decode("utf-8", "replace"))
    p.close()
    lines = [re.sub(r"\s+", " ", x).strip() for x in "".join(p.parts).split("\n")]
    return [x for x in lines if x], p.has_prev


def fetch(url, data: bytes | None = None, headers=None, timeout=120.0, method: str | None = None):
    """(status, headers, raw body); an HTTP error status is returned, not raised."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                                           method=method), timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except (urllib.error.URLError, OSError):  # refused, DNS, timeout, reset: a failed check, not a traceback
        return 0, {}, b""


def ask(base, q: str, headers=None):
    """POST /ask as the page's form does: urlencoded, field q."""
    h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return fetch(f"{base}/ask", urllib.parse.urlencode({"q": q}).encode("utf-8"), h)


def check_health(base, want: dict) -> list[str]:
    status, _, raw = fetch(f"{base}/health")
    try:
        h = json.loads(raw.decode("utf-8")) if status == 200 else None
    except ValueError:
        h = None
    if not isinstance(h, dict):
        return [f"health: status {status}"]
    out = [f"health: {k} is {h.get(k)!r}, want {v!r}" for k, v in want.items() if h.get(k) != v]
    if not h.get("budget"):
        out.append("health: no budget state")
    return out


def check_home(base) -> list[str]:
    status, headers, raw = fetch(f"{base}/")
    if status != 200:
        return [f"/: status {status}"]
    out = []
    if headers.get("Content-Security-Policy") != CSP:
        out.append(f"/: CSP is {headers.get('Content-Security-Policy')!r}")
    if SAVED_LINE not in read_page(raw)[0]:
        out.append("/: no saved line")
    return out


def check_fixed(base, text: str, want: str, label: str) -> list[str]:
    """A fixed line: the page shows `want` and NEXT_STEP."""
    status, _, raw = ask(base, text)
    if status != 200:
        return [f"{label}: status {status}"]
    lines, _ = read_page(raw)
    out = [] if want in lines else [f"{label}: the page does not show the fixed line"]
    return out + ([] if NEXT_STEP in lines else [f"{label}: no next step"])


def check_evidence(base, unit_id: str) -> list[str]:
    status, _, _ = fetch(f"{base}/evidence/{urllib.parse.quote(unit_id)}")
    return [] if status == 200 else [f"evidence {unit_id}: status {status}"]


def check_static(base) -> list[str]:
    status, _, _ = fetch(f"{base}/static/style.css")
    return [] if status == 200 else [f"/static/style.css: status {status}"]


def check_results(base, facts: dict) -> list[str]:
    status, _, raw = fetch(f"{base}/results")
    if status != 200:
        return [f"/results: status {status}"]
    want = facts["m1_jdreq_R4_report_recall_at_10"]["display"]
    lines, _ = read_page(raw)
    return [] if want in "\n".join(lines) else [f"/results: does not show recall@10 {want} from this checkout's facts"]


def check_head(base) -> list[str]:
    status, _, _ = fetch(f"{base}/", method="HEAD")
    return [] if status == 200 else [f"HEAD /: status {status}"]


def check_ask(base, q: str) -> tuple[list[str], bool]:
    """A live question: an answer page or the fallback, either one with NEXT_STEP. -> (problems, was it an answer)."""
    status, _, raw = ask(base, q)
    if status != 200:
        return [f"ask: status {status}"], False
    lines, answered = read_page(raw)
    return ([] if NEXT_STEP in lines else ["ask: no next step on the page"]), answered


def check_burst(base, q: str, tries: int = 40) -> list[str]:
    """Re-ask `q` (already cached as an answer) until the per-address limit answers 429. That page must be the
    fallback with Retry-After, and a forged X-Forwarded-For must still get 429. An ask under the limit that comes back
    without an answer was not served from the cache, so the burst stops there rather than call the model again."""
    for i in range(tries):
        status, headers, raw = ask(base, q)
        if status == 429:
            lines, _ = read_page(raw)
            out = [] if headers.get("Retry-After") else ["burst: 429 without Retry-After"]
            if NEXT_STEP not in lines or FALLBACK["intro"] not in lines:
                out.append("burst: the 429 page is not the fallback")
            forged, _, _ = ask(base, q, {"X-Forwarded-For": FORGED})
            return out + ([] if forged == 429 else [f"forged X-Forwarded-For: status {forged}, want 429"])
        if status != 200:
            return [f"burst: status {status} after {i} asks, before any 429"]
        if not read_page(raw)[1]:
            return ["burst: an ask came back without an answer, so the cache did not serve it; stopped before "
                    "another model call"]
    return [f"burst: no 429 after {tries} asks"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_shas(root: Path = REPO) -> dict:
    """What /health must report: this checkout's commit, corpus, index (build/index.db, built by push.sh's gates)
    and answer template."""
    return {"git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "corpus_sha": _sha256(root / "corpus" / "units.jsonl"),
            "index_sha": _sha256(root / "build" / "index.db"),
            "template_sha": load_answer_settings().template_sha}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="smoke")
    ap.add_argument("base")
    ap.add_argument("--ask", metavar="Q", help="a live question: one model call unless cached; the burst re-asks it")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    problems: list[str] = []
    try:
        want = local_shas()
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        want = None
        problems.append(f"local: cannot read this checkout's shas ({type(e).__name__}); run from the repo root after "
                        "deploy/push.sh, which builds build/index.db")
    if want is not None:
        problems += check_health(base, want)
    problems += (check_home(base) + check_fixed(base, HANDOFF_Q, HANDOFF_TEXT, "handoff")
                 + check_fixed(base, "a" * (MAX_INPUT_CHARS + 1), TOO_LONG_TEXT, "too long")
                 + check_evidence(base, FALLBACK["units"][0]) + check_static(base) + check_head(base))
    try:
        facts = json.loads((REPO / "facts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        problems.append(f"local: cannot read this checkout's facts.json ({type(e).__name__})")
    else:
        problems += check_results(base, facts)
    if args.ask:
        got, answered = check_ask(base, args.ask)
        problems += got
        if answered:
            problems += check_burst(base, args.ask)  # last: it leaves this machine rate-limited
        else:
            problems.append("burst: not run: the --ask question did not come back as an answer, so the cache does "
                            "not hold it and every burst ask would call the model; rerun with a question the corpus "
                            "answers")
    else:
        problems.append("burst: not run: the rate-limit and forged X-Forwarded-For checks need --ask (they re-ask that "
                        "question from the cache, so they make no model call); rerun with --ask")
    for p in problems:
        print("FAIL", p)
    print("smoke: ok" if not problems else f"smoke: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
