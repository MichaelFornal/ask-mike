"""The desk: everything between the answer page and the M2 Answerer, after Deal Terms Desk's service/desk.py.

DTD's `_answer` reserves, calls, settles and caches around its one model call. A job description here makes two or
three calls (M1's extraction, its parse retry, the answer), so the reserve and settle wrap every call instead
(`metered`), and the Answerer is given the metered runner. It is never wrapped in `ledgered_runner`, so the parse
retry is a real second call, metered like the first.

Desk.ask runs these steps in order:
1. Route. An empty, too-long or handed-off input gets its fixed line. It is logged, and makes no model call, takes
   no ask limit and uses no budget.
2. Rate limit. An address past its asks this hour gets the `limited` fallback with 429. It is not logged: a burst
   past the limit never reaches the private log.
3. Previous turn. A follow-up names an earlier answer by its id, and the server looks that turn up.
4. Cache. A hit is served and logged, even when the budget is reached.
5. Busy. With no free slot, or no fresh call left this hour, the `busy` fallback with 503 (logged).
6. Answer. The Answerer on metered calls; an `answer` outcome is cached.
7. Log and return.

`Served.served_from` is "model" when the Answerer made the Outcome on its live path (its own fallbacks too, such as
`budget`), "cache" for a cache hit, and "none" when no model call was possible: a fixed line, `limited` or `busy`.

The desk logs only to the visitor log (the app owns the request log). Every row of any kind counts against the
address's own log bucket (LOG_PER_HOUR): past it the visitor still gets the page but no row is written, so inputs that
take no ask limit cannot flood the log. A too-long input's row keeps its first LOG_TEXT_MAX characters and its
length. A write for Mike that fails (a locked cache or visitor log) is skipped, never turned into an error for the
visitor.
"""
from __future__ import annotations

import hashlib
import json
import math
import resource
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from askmike.answer.answerer import NO_CALL, Answerer, Outcome
from askmike.answer.config import MAX_TOKENS
from askmike.answer.evidence import Claim, Turn
from askmike.answer.gates import AnswerGateConfig
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.route import route
from askmike.answer.settings import AnswerSettings, load_answer_settings
from askmike.llm.api_runner import MAYBE_BILLED, USAGE_KEYS, RunnerError, make_api_runner
from askmike.llm.prices import cost_usd, load_prices, worst_case_usd
from askmike.retrieval.index import StaleIndex, open_index
from askmike.retrieval.ladder import Ladder, LockedLadder, load_settings
from askmike.retrieval.models import Embedder
from askmike.retrieval.requirements import Requirement
from askmike.service.budget import Budget
from askmike.service.cache import AnswerCache
from askmike.service.config import ServiceConfig
from askmike.service.limits import Buckets, Slots
from askmike.service.visits import ID_SHAPE, Visits

CORPUS = Path(__file__).resolve().parents[2] / "corpus"
IDS = ("git_sha", "corpus_sha", "index_sha", "template_sha", "gate_sha")
HOUR_S = 3600.0  # both rate limits count per hour
FRESH_KEY = "*"  # the fresh-call limit is one bucket for every visitor
LOG_PER_HOUR = 60  # visitor-log rows one address may write in any hour, of any kind
LOG_TEXT_MAX = 2000  # characters of a too-long input the log keeps


class DeskRefused(RuntimeError):
    """build_desk cannot build a desk that answers safely. The message names the file or setting, never a gate
    pattern."""


# ---- metered model calls -----------------------------------------------------------------------------------------

def _cost(prices, usage) -> float | None:
    """A call's cost from its usage, or None (book the worst case) when the usage is missing, malformed or counts no
    token: a call that came back was billed for something."""
    if not isinstance(usage, dict):
        return None
    try:
        if not any(int(usage.get(k) or 0) > 0 for k in USAGE_KEYS):
            return None
        usd = cost_usd(prices, usage)
    except (TypeError, ValueError):
        return None
    return usd if math.isfinite(usd) and usd >= 0 else None


def _quietly(write, *args) -> None:
    """A ledger write that finds the database locked leaves the reservation open. It is counted at its worst case
    until the stale sweep settles it there, which never under-counts."""
    try:
        write(*args)
    except sqlite3.OperationalError:
        pass


def metered(runner, budget: Budget, max_tokens: int):
    """`runner` behind the spend ledger. Each call first reserves its worst case at its own model's prices
    (`worst_case_usd(load_prices(model), len(prompt), max_tokens)`); when that would pass the month or day cap, it
    raises RunnerError("budget") and the model is not called. After the call it settles:
    - a reply: its cost from usage, or the worst case when the usage is empty;
    - a RunnerError carrying usage: that cost (the call came back, so it was billed);
    - a RunnerError with no usage and a kind in MAYBE_BILLED (timeout, connection): the worst case;
    - any other error: released, never billed.
    A process killed mid-call (or a BaseException such as SystemExit) leaves the reservation open: counted at its
    worst case at once, and settled at it after the ledger's stale window. A call with no schema and no effort
    reaches `runner` as runner(prompt, model), as M1's runners take."""
    prices: dict = {}

    def run(prompt, model, schema=None, effort=None):
        p = prices.get(model)
        if p is None:
            p = prices[model] = load_prices(model)
        rid = budget.reserve(worst_case_usd(p, len(prompt), max_tokens))
        if rid is None:
            raise RunnerError("budget", "the model budget is reached")
        try:
            if schema is None and effort is None:
                reply = runner(prompt, model)
            else:
                reply = runner(prompt, model, schema=schema, effort=effort)
        except RunnerError as e:
            if e.usage:
                _quietly(budget.settle, rid, _cost(p, e.usage))
            elif e.kind in MAYBE_BILLED:
                _quietly(budget.settle, rid, None)
            else:
                _quietly(budget.release, rid)
            raise
        except Exception:
            _quietly(budget.release, rid)
            raise
        _quietly(budget.settle, rid, _cost(p, reply.get("usage") if isinstance(reply, dict) else None))
        return reply
    return run


# ---- the cache payload -------------------------------------------------------------------------------------------

def _payload(o: Outcome) -> dict:
    """What the cache keeps of an answer: what the page renders, and what a follow-up's turn is rebuilt from."""
    return {"outcome": o.outcome, "state": o.state, "kind": o.kind, "model": o.model, "template_sha": o.template_sha,
            "kept": [{"type": c.type, "need": c.need, "req": c.req, "text": c.text, "units": list(c.units)}
                     for c in o.kept],
            "requirements": [{"id": r.id, "text": r.text, "required": r.required, "kind": r.kind}
                             for r in o.requirements]}


def _from_payload(d: dict, ms: int) -> Outcome:
    kept = tuple(Claim(c["type"], c["need"], c["req"], c["text"], tuple(c["units"])) for c in d["kept"])
    reqs = tuple(Requirement(r["id"], r["text"], r["required"], r["kind"]) for r in d["requirements"])
    return Outcome(d["outcome"], d["state"], kept, (), "", "", d["template_sha"], None, reqs, {}, ms, d["kind"],
                   d["model"])


def _turn_key(previous: Turn | None) -> str | None:
    """The previous turn as a follow-up's prompt shows it: its text and its kept claims with their ids. The cache
    keys on all of it, so a follow-up comes from the cache only after the same earlier answer."""
    if previous is None:
        return None
    return "\n".join([previous.text] + [f"{c.text} [{', '.join(c.units)}]" for c in previous.kept])


# ---- the desk ----------------------------------------------------------------------------------------------------

def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _desk_fallback(reason: str, kind: str, t0: float) -> Outcome:
    """The fallback the desk serves without asking the Answerer (`limited`, `busy`): nothing kept, no model."""
    return Outcome("fallback", "", (), (), reason, "", ANSWER_TEMPLATE_SHA, None, (), {}, _ms(t0), kind, "")


def _log_text(kind: str, text):
    """What the visitor log keeps of an input: all of it, except a too-long one, cut to LOG_TEXT_MAX characters and
    marked with its length."""
    if kind == "too_long" and isinstance(text, str) and len(text) > LOG_TEXT_MAX:
        return f"{text[:LOG_TEXT_MAX]} … [truncated from {len(text)} characters]"
    return text


def _rss_mb() -> float:
    """Peak resident memory of this process (ru_maxrss is KiB on Linux, bytes on macOS). Ported from DTD."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


@dataclass(frozen=True)
class Served:
    outcome: Outcome
    visit_id: str
    status: int  # 200; 429 for `limited`; 503 for `busy`
    served_from: str  # "model" | "cache" | "none" (see the module docstring)
    retry_after: int = 0  # whole seconds for a 429's Retry-After header; 0 otherwise


class Desk:
    def __init__(self, answerer: Answerer, budget: Budget, cache: AnswerCache, visits: Visits, asks: Buckets,
                 fresh: Buckets, slots: Slots, settings: AnswerSettings, ids: dict,
                 typical_prompt_chars: int = ServiceConfig.typical_prompt_chars, logs: Buckets | None = None):
        missing = [k for k in IDS if not isinstance(ids.get(k), str) or not ids[k]]
        if missing:
            raise ValueError(f"the desk's ids need {', '.join(missing)}")
        self.answerer, self.budget, self.cache, self.visits = answerer, budget, cache, visits
        self.asks, self.fresh, self.slots, self.settings = asks, fresh, slots, settings
        self.ids = {k: ids[k] for k in IDS}
        self.logs = logs if logs is not None else Buckets(LOG_PER_HOUR, HOUR_S)  # rows per address, in memory only
        # the budget reads "reached" once it cannot cover one typical answer on the jd route at its worst case
        self._min_call = worst_case_usd(load_prices(settings.model_for("jd")), typical_prompt_chars, MAX_TOKENS)

    def ask(self, text: str, prev_id: str | None, client: str) -> Served:
        t0 = time.monotonic()
        visit_id = self.visits.new_id()
        prev_id = prev_id if isinstance(prev_id, str) and ID_SHAPE.fullmatch(prev_id) else None
        kind = route(text).kind
        if kind in NO_CALL:  # the Answerer gives the fixed line without a model call
            return self._served(client, visit_id, kind, text, prev_id, self.answerer.ask(text), "none")

        wait = self.asks.allow(client)
        if wait > 0:
            return Served(_desk_fallback("limited", kind, t0), visit_id, 429, "none", max(1, math.ceil(wait)))

        previous = self._previous(prev_id)
        key = self.cache.key(kind, self.answerer.model_for(kind), text, _turn_key(previous), self.ids)
        hit = self._cached(key, t0)
        if hit is not None:
            return self._served(client, visit_id, kind, text, prev_id, hit, "cache")

        busy = not self.slots.try_acquire()
        if not busy:
            try:
                busy = self.fresh.allow(FRESH_KEY) > 0
                if not busy:
                    outcome = self.answerer.ask(text, previous)
            finally:
                self.slots.release()
        if busy:
            return self._served(client, visit_id, kind, text, prev_id, _desk_fallback("busy", kind, t0), "none", 503)
        if outcome.outcome == "answer":
            self._put(key, outcome)
        return self._served(client, visit_id, kind, text, prev_id, outcome, "model")

    def health(self) -> dict:
        """What /health shows: the running code, corpus, index and template, the models, whether the budget still
        covers a typical call, and peak memory. Never an amount: no cap, spend or headroom."""
        return {"ok": True, "git_sha": self.ids["git_sha"], "corpus_sha": self.ids["corpus_sha"],
                "index_sha": self.ids["index_sha"], "template_sha": self.ids["template_sha"],
                "models": dict(self.settings.model), "budget": self.budget.state(self._min_call),
                "rss_mb": _rss_mb()}

    def _previous(self, prev_id: str | None) -> Turn | None:
        try:
            return self.visits.previous(prev_id)
        except sqlite3.Error:  # a locked log: the follow-up is answered as a first turn
            return None

    def _cached(self, key: str, t0: float) -> Outcome | None:
        try:
            d = self.cache.get(key)
            return None if d is None else _from_payload(d, _ms(t0))
        except (sqlite3.Error, KeyError, TypeError, ValueError):  # a locked cache, or a payload of another shape
            return None

    def _put(self, key: str, outcome: Outcome) -> None:
        try:
            self.cache.put(key, _payload(outcome))
        except sqlite3.Error:  # the answer still goes back; the next ask calls again
            pass

    def _served(self, client: str, visit_id: str, kind: str, text, prev_id: str | None, outcome: Outcome,
                served_from: str, status: int = 200) -> Served:
        if self.logs.allow(client) == 0:  # past its log bucket an address still gets the page, with no row
            try:
                self.visits.record(visit_id, kind, _log_text(kind, text), prev_id, outcome, served_from)
            except sqlite3.Error:  # the log is for Mike: a locked log never costs the visitor the answer
                pass
        return Served(outcome, visit_id, status, served_from)


# ---- the live desk -----------------------------------------------------------------------------------------------

def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_desk(config: ServiceConfig, embedder=None, runner=None) -> Desk:
    """The live desk, built once at startup: the corpus and skills, the index at config.index_path behind one lock
    (one connection per request thread), the answer settings, the gate config, the spend ledger, the answer cache
    and the visitor log under config.state_dir, the limits, and the Messages API runner behind `metered`.
    `embedder` and `runner` are for tests. Refuses with DeskRefused, before it makes any state, when the gate
    config is unset, missing or unusable, when an answer model has no prices, or when the index is missing or was
    not built from this corpus with this embedder. It never prints the gate config."""
    if config.gate_config is None:
        raise DeskRefused("ASK_GATE_CONFIG is not set: the service will not answer without its gate config")
    gate = Path(config.gate_config)
    if not gate.is_file():
        raise DeskRefused(f"the gate config {gate} is missing (ASK_GATE_CONFIG)")
    units_path = CORPUS / "units.jsonl"
    units = {u["id"]: u for u in (json.loads(x) for x in units_path.read_text(encoding="utf-8").splitlines()
                                  if x.strip())}
    skills = json.loads((CORPUS / "skills.json").read_text(encoding="utf-8"))
    try:
        cfg = AnswerGateConfig.from_bundle(gate, skills)
    except (OSError, ValueError) as e:  # the messages name a field or a pattern's number, never a pattern
        raise DeskRefused(f"the gate config {gate} is unusable: {e}") from None
    try:
        settings = load_answer_settings()
        for model in sorted(set(settings.model.values())):
            load_prices(model)
    except (OSError, ValueError) as e:
        raise DeskRefused(f"the answer settings are unusable: {e}") from None
    except KeyError as e:
        raise DeskRefused(f"{e.args[0]}: the spend ledger cannot price an answer model's calls") from None

    embedder = embedder if embedder is not None else Embedder()
    retrieval = load_settings()

    def ladder() -> Ladder:
        return Ladder(open_index(config.index_path, units_path, model=embedder.name), embedder, skills, retrieval)
    try:
        ladder().conn.close()  # a missing or stale index refuses now, not on the first visitor's question
    except StaleIndex as e:
        raise DeskRefused(f"{config.index_path}: {e}") from None
    embedder.embed_query("warm up")  # load the embedding model now, not on the first visitor's question

    state = Path(config.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    budget = Budget(state / "budget.db", config.month_cap_usd, config.day_cap_usd)
    live = runner if runner is not None else make_api_runner(max_tokens=MAX_TOKENS, timeout=config.api_timeout_s)
    answerer = Answerer(LockedLadder(ladder), units, skills, cfg, metered(live, budget, MAX_TOKENS), settings.model,
                        settings.effort)
    ids = {"git_sha": config.git_sha, "corpus_sha": _sha256(units_path), "index_sha": _sha256(config.index_path),
           "template_sha": ANSWER_TEMPLATE_SHA, "gate_sha": _sha256(gate)}
    return Desk(answerer, budget, AnswerCache(state / "cache.db"), Visits(state / "visits.db"),
                Buckets(config.ask_per_hour, HOUR_S), Buckets(config.fresh_per_hour, HOUR_S), Slots(config.slots),
                settings, ids, typical_prompt_chars=config.typical_prompt_chars)
