"""The Ask Mike web app, served by `uvicorn --factory askmike.service.app:create_app`.

Routes (there are no others; /docs and /openapi.json are 404):
- `GET /`: the home page, the box posting `q` to /ask.
- `POST /ask`: the answer page. Its status is the desk's (200; 429 with Retry-After; 503).
- `GET /evidence/{unit_id}`: one corpus unit; an unknown id gets the off-topic line and the box, with 404.
- `GET /results`: the evals of record, rendered from facts.json once at start (askmike/service/results.py).
- `GET /health`: the desk's health, as JSON.
- Every GET route also answers HEAD, so an uptime monitor or link checker doesn't read a 405 as down.
- `/static/*`: the stylesheet and the favicon. Caddy serves them on the box (and /favicon.ico from the same folder);
  the app serves /static/* for a local run.

Every HTML route answers with a page, never an error page. POST /ask wraps everything it does in a catch-all that
serves the fallback page with status 200. Any other route that raises gets the same page from the request log
middleware. Unknown paths and methods get the off-topic line and the box with their 404 or 405.

The app reads the form itself: the raw body, capped at BODY_CAP bytes, decoded as UTF-8 with replacement and parsed
with parse_qs (no python-multipart). A body over the cap gets the too-long page, which the desk serves for an
over-long `q`.

Each request writes one JSON line to stderr (journald on the box): {route, method, status, outcome, served_from, ms}.
`route` is a fixed name, never the path; `method` tells an uptime monitor's HEAD from a page view. The visitor's address, text and visit id never reach that line. The
address is used once, as the desk's rate-limit key. A request the catch-all served also carries `error` (the
exception's class name) and `where` (file:line of the innermost traceback frame). It never carries the exception's
message or a traceback, since either can hold the visitor's text.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from askmike.answer.config import MAX_INPUT_CHARS
from askmike.service import desk as desk_mod
from askmike.service import pages, results
from askmike.service.config import ServiceConfig, from_env
from askmike.service.limits import client_key

BODY_CAP = 4 * MAX_INPUT_CHARS  # bytes of form body read; a form-encoded posting takes more bytes than characters
OVER_CAP_FILL = "…"  # pads what was read of an over-cap body past MAX_INPUT_CHARS, so the desk serves too_long
LOG_KEY = "askmike.request_log"  # where the middleware keeps this request's log record in the ASGI scope
REPO = Path(__file__).resolve().parents[2]


def _log(rec: dict) -> None:
    print(json.dumps(rec, sort_keys=True), file=sys.stderr, flush=True)


def _failure(e: BaseException) -> dict:
    """What the log keeps of an exception the catch-all served: its class name and where it was raised (file:line of
    the innermost traceback frame; a file inside the repo as its repo path, any other file by its name). Never the
    message or a traceback: either can hold the visitor's text."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is None:
        return {"error": type(e).__name__, "where": "unknown"}
    path = Path(tb.tb_frame.f_code.co_filename)
    try:
        name = path.resolve().relative_to(REPO).as_posix()
    except (OSError, ValueError):
        name = path.name
    return {"error": type(e).__name__, "where": f"{name}:{tb.tb_lineno}"}


def _note(request: Request, **fields) -> None:
    """Set fields of this request's log line: route, outcome, served_from."""
    rec = request.scope.get(LOG_KEY)
    if rec is not None:
        rec.update(fields)


class RequestLog:
    """One JSON line per request, written when it ends. The last resort as well: an exception that escapes a route
    gets the fallback page with 200 and outcome "error", logged with its class and file:line (`_failure`), never its
    message or a traceback."""

    def __init__(self, app, fallback_html: str):
        self.app, self.fallback_html = app, fallback_html

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.monotonic()
        rec = {"route": "static" if scope["path"].startswith("/static/") else "other", "method": scope["method"],
               "status": 500, "outcome": None, "served_from": None}
        scope[LOG_KEY] = rec
        started = False

        async def send_logged(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started, rec["status"] = True, message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        except Exception as e:
            rec.update(outcome="error", served_from="none", **_failure(e))
            if not started:  # a response already under way cannot be replaced; the server closes it
                await HTMLResponse(self.fallback_html)(scope, receive, send_logged)
        finally:
            rec["ms"] = int((time.monotonic() - t0) * 1000)
            _log(rec)


async def _read_form(request: Request) -> tuple[str, str | None]:
    """(q, prev) from the raw body. q is the first value or "", prev the first value or None. A body over BODY_CAP
    bytes is read no further: q is what was read of it, padded past MAX_INPUT_CHARS, so the desk serves (and logs)
    the too-long line."""
    body, over = bytearray(), False
    async for chunk in request.stream():
        body += chunk
        if len(body) > BODY_CAP:
            del body[BODY_CAP:]
            over = True
            break
    fields = parse_qs(bytes(body).decode("utf-8", errors="replace"), keep_blank_values=True)
    q = (fields.get("q") or [""])[0]
    prev = (fields.get("prev") or [None])[0]
    if over:
        q += OVER_CAP_FILL * max(0, MAX_INPUT_CHARS + 1 - len(q.strip()))
    return q, prev


def create_app(config: ServiceConfig | None = None, desk=None, facts_path: Path | None = None) -> FastAPI:
    """With no arguments, config comes from the environment and the desk is built here. A refusal (DeskRefused, or
    a bad env) stops the process. The unit has Restart=always and RestartSec=2, but systemd's default start limit (5 starts in
    10 s) trips on a fast refusal and the unit stays failed until someone intervenes; Caddy serves fallback.html
    meanwhile. Tests pass a desk. The results page is built here from facts_path (default: the repo's facts.json),
    so a fact it needs and the file lacks raises KeyError and the app never starts."""
    if desk is None:
        desk = desk_mod.build_desk(config or from_env())
    units = pages.load_units(desk_mod.CORPUS / "units.jsonl")
    home_html, not_found_html = pages.home_page(), pages.not_found_page()
    fallback_html = pages.fallback_page(units)
    results_html = pages.results_page(results.load_facts(facts_path or REPO / "facts.json"))

    app = FastAPI(title="Ask Mike", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestLog, fallback_html=fallback_html)
    app.mount("/static", StaticFiles(directory=pages.STATIC), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def no_such_page(request: Request, exc: StarletteHTTPException):
        return HTMLResponse(not_found_html, status_code=exc.status_code, headers=exc.headers)

    @app.api_route("/", methods=["GET", "HEAD"])
    def home(request: Request):
        _note(request, route="home")
        return HTMLResponse(home_html)

    @app.post("/ask")
    async def ask(request: Request):
        _note(request, route="ask")
        try:
            q, prev = await _read_form(request)
            host = request.client.host if request.client else "unknown"
            served = await run_in_threadpool(desk.ask, q, prev, client_key(host))
            html = pages.answer_page(served.outcome, units, q, served.visit_id)
            headers = ({"Retry-After": str(max(1, math.ceil(served.retry_after)))} if served.status == 429
                       else None)
            response = HTMLResponse(html, status_code=served.status, headers=headers)
        except Exception as e:  # never an error page; the log gets the class and file:line, never the message
            _note(request, outcome="error", served_from="none", **_failure(e))
            return HTMLResponse(fallback_html)
        _note(request, outcome=served.outcome.outcome, served_from=served.served_from)
        return response

    @app.api_route("/results", methods=["GET", "HEAD"])
    def results_route(request: Request):
        _note(request, route="results")
        return HTMLResponse(results_html)

    @app.api_route("/evidence/{unit_id}", methods=["GET", "HEAD"])
    def evidence(request: Request, unit_id: str):
        _note(request, route="evidence")
        unit = units.get(unit_id)
        if unit is None:
            return HTMLResponse(not_found_html, status_code=404)
        return HTMLResponse(pages.evidence_page(unit))

    @app.api_route("/health", methods=["GET", "HEAD"])
    def health(request: Request):
        _note(request, route="health")
        try:
            return JSONResponse(desk.health())
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)

    return app
