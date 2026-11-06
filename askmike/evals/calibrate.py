"""The API calibration: ~40 tune-split items through the real Messages API, compared with the same items
answered by claude -p. Offline except in Task 11's paid run. Nothing here reads, logs or writes the API key:
only the SDK reads it, from the environment.

Pattern from deal-terms-desk's calibration stop rule: stop if outcome agreement < 0.8, |support gap| > 0.15,
prompt parity < 1.0 or a worst-case reservation was ever below an actual cost.

The calibrated setup is one model id, or Mike's pick (PICKED, a route map from settings.json): each item then runs
on its own route's model (item_model), priced at that model's rates, and is compared with that model's claude -p
record and judge-A verdicts (cli_side). Output for the pick lives under work/answers/calibration/picked/.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

from askmike.answer.answerer import JD_ID, NO_CALL, model_for
from askmike.answer.config import MAX_TOKENS, MODEL_SLUGS
from askmike.answer.route import route
from askmike.evals import support as sup
from askmike.evals.answer_run import _ask, _Capture
from askmike.evals.steps import read_jsonl
from askmike.llm.api_runner import MAYBE_BILLED
from askmike.llm.ask import ledgered_runner, runner_key
from askmike.llm.ledger import MemoryLedger
from askmike.llm.prices import Prices, cost_usd, load_prices, worst_case_usd
from askmike.retrieval.requirements import extract_requirements

QUOTAS = {"jd": 16, "evidence": 12, "gap": 4, "personal": 4, "handoff": 4}  # at n=40
PACK_CHARS = 30000  # generous bound on what an answer prompt adds to the visitor's text (template + 40 units)
TEMPLATE_CHARS = 6000  # the extraction prompt's fixed text, bounded high
AGREEMENT_MIN, GAP_MAX, PARITY_MIN = 0.8, 0.15, 1.0
PICKED = "picked"  # `calibrate --model picked`: Mike's route map from askmike/answer/settings.json


class BudgetStop(RuntimeError):
    """A call refused because it would take spend over max_usd."""


def _order(item) -> str:
    return hashlib.sha1(item.item_id.encode("utf-8")).hexdigest()


def item_model(item, model) -> str:
    """The model an item runs on: `model` when it is one id; for a route map, the model of the item's route, and
    the question model for a route that makes no call (a handoff's claude -p record is the question model's)."""
    if isinstance(model, str):
        return model
    return model_for(model, "jd" if route(item.text).kind == "jd" else "question")


def models_of(model) -> tuple[str, ...]:
    """Every model a setup uses, sorted."""
    return (model,) if isinstance(model, str) else tuple(sorted(set(model.values())))


def _price(prices, model: str) -> Prices:
    """One model's prices: `prices` itself when it is one Prices, else its entry in a {model: Prices} map."""
    if isinstance(prices, Prices):
        return prices
    if model not in prices:
        raise KeyError(f"no prices for model {model!r}")
    return prices[model]


def sample(items, n: int = 40) -> list:
    """Tune-split items only, the 16/12/4/4/4 split by set (scaled for other n), each set ordered by sha1 of
    the item id, so the same items come back every time."""
    quotas = {k: v * n // 40 for k, v in QUOTAS.items()}
    quotas["jd"] += n - sum(quotas.values())
    tune = [i for i in items if i.split == "tune"]
    out = []
    for kind, q in quotas.items():
        out += sorted((i for i in tune if i.set == kind), key=_order)[:max(q, 0)]
    return out


class _Metered(_Capture):
    """_Capture that also logs every call (kind, model, usage, wall ms, live) and refuses a live call that would take
    spend over the cap. `spent` is shared across items through `meter`."""

    def __init__(self, runner, ledger, prices, max_usd, meter):
        super().__init__(runner, ledger)
        self.prices, self.max_usd, self.meter, self.calls = prices, max_usd, meter, []

    def __call__(self, prompt, model, schema=None, effort=None):
        live = self.ledger.get(runner_key(prompt, model, schema, effort)) is None
        worst = worst_case_usd(self.prices, len(prompt), self.meter["max_tokens"])
        if live and self.meter["spent"] + worst > self.max_usd:
            self.meter["stopped"] = True
            raise BudgetStop("budget: the next call could take spend over max_usd")
        call = {"kind": "answer" if schema is not None else "extract", "model": model, "live": live,
                "prompt_chars": len(prompt),
                "max_tokens": self.meter["max_tokens"], "worst_case_usd": worst, "usage": {}, "ms": 0, "error": ""}
        t0 = time.monotonic()
        booked = None  # None: book the usage; a number: book that instead
        try:
            reply = super().__call__(prompt, model, schema=schema, effort=effort)
        except Exception as e:
            call["error"] = str(e)[:200]
            call["usage"] = dict(getattr(e, "usage", None) or {})
            # a failure that may have been billed but came back with no usage is booked at its worst case
            if not call["usage"] and getattr(e, "kind", "connection") in MAYBE_BILLED:
                booked = worst
            raise
        else:
            u = reply.get("usage") if isinstance(reply, dict) else None
            call["usage"] = dict(u) if isinstance(u, dict) else {}
            if not any(call["usage"].values()):  # a billed success with no usage is booked at worst case
                booked = worst
        finally:
            call["ms"] = int((time.monotonic() - t0) * 1000)
            call["cost_usd"] = cost_usd(self.prices, call["usage"]) if booked is None else booked
            call["booked_usd"] = call["cost_usd"]
            if live:
                self.meter["spent"] += call["cost_usd"]
                self.meter["log"](call["kind"], call["cost_usd"])
            self.calls.append(call)
        return reply


def run(items, api_answerer, ledger, max_usd: float, prices=None, max_tokens: int = MAX_TOKENS,
        spend_path=None) -> dict:
    """Ask every item through `api_answerer` (an Answerer whose runner is the API runner), one at a time.
    `api_answerer.model` is one model id or a route map (the picked setup): each item runs on
    item_model(item, model) and is reserved and settled at that model's prices (`prices` is one Prices, a
    {model: Prices} map, or None to read each model's from prices.json). jd items make an extraction call on
    their model through the API (recorded) and are then answered with M1's requirements. Before an item, the
    worst case of its calls (both for a jd; none for a route that makes no call) is reserved: if settled spend
    plus that reservation would pass `max_usd`, the run stops there. Each live call is settled from its usage.
    `spend_path` holds every live call's booked cost; spend is seeded from it. A failed call with no usage that
    may have been billed (timeout, connection), or a success with none, is booked at its worst case. Returns
    {"records", "spent", "prior", "stopped", "remaining"}."""
    loaded: dict = {}

    def price(model):
        if prices is not None:
            return _price(prices, model)
        if model not in loaded:
            loaded[model] = load_prices(model)
        return loaded[model]

    shared = ledgered_runner(api_answerer.runner, ledger)
    prior = 0.0
    if spend_path is not None:  # append-only, so --max-usd caps the whole calibration across invocations
        spend_path = Path(spend_path)
        if spend_path.exists():
            prior = sum(json.loads(x)["cost_usd"] for x in spend_path.read_text(encoding="utf-8").splitlines() if x.strip())

    def log(kind, cost):
        if spend_path is not None:
            spend_path.parent.mkdir(parents=True, exist_ok=True)
            with open(spend_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind, "cost_usd": cost}) + "\n")
                f.flush()
                os.fsync(f.fileno())

    meter = {"spent": prior, "stopped": False, "max_tokens": max_tokens, "log": log}
    records, done = [], 0
    for item in items:
        model = item_model(item, api_answerer.model)
        p = price(model)
        answer = 0.0 if route(item.text).kind in NO_CALL else worst_case_usd(p, len(item.text) + PACK_CHARS, max_tokens)
        extract = worst_case_usd(p, len(item.text) + TEMPLATE_CHARS, max_tokens) if item.set == "jd" else 0.0
        if meter["spent"] + answer + extract > max_usd:
            meter["stopped"] = True
            break
        cap = _Metered(shared, ledger, p, max_usd, meter)
        a = copy.copy(api_answerer)
        a.runner = cap
        t0 = time.monotonic()
        n_extracted = extracted = None
        reqs = None
        if item.set == "jd":
            # the extraction call goes through the API and is recorded (usage, ms, the count and the texts) to set
            # against M1's extraction; the answer then uses M1's requirements, so its prompt is byte-identical to
            # the CLI record's
            reqs = list(item.requirements)
            got, _ = extract_requirements(JD_ID, item.text, cap, MemoryLedger(), model=model)
            n_extracted, extracted = len(got), [r.text for r in got]
        o, rec = _ask(a, cap, item.text, None, reqs, item.must_not)
        if meter["stopped"]:
            break
        rec = dict(rec, item_id=item.item_id, set=item.set, split=item.split, cluster=item.cluster,
                   model=model, calls=cap.calls, errors=cap.errors,
                   ms_total=sum(c["ms"] for c in cap.calls), wall_ms=int((time.monotonic() - t0) * 1000),
                   cost_usd=sum(c["cost_usd"] for c in cap.calls), cached=not any(c["live"] for c in cap.calls),
                   extracted_requirements=n_extracted, extracted_requirement_texts=extracted)
        records.append(rec)
        done += 1
    return {"records": records, "spent": meter["spent"], "prior": prior, "stopped": meter["stopped"], "remaining": len(items) - done}


# ---- the summary -----------------------------------------------------------

def _pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, -(-len(v) * q // 100) - 1))]


def _mean(values):
    return sum(values) / len(values) if values else None


def _support_mean(verdicts, records):
    r = sup.rates(verdicts or {}, records)["support_rate"]
    return None if r is None else r["mean"]


def cli_side(api_records, cli_by_model: dict) -> tuple[list, dict]:
    """What each API record is compared with: the claude -p record of the same item by the API record's own
    model, and that model's judge-A verdicts for it. `cli_by_model` is {model: (records, {key: verdict row})}.
    Verdict keys ("<item_id>#<n>") repeat across models, so each item reads only its own model's file."""
    index = {m: ({r["item_id"]: r for r in recs}, rows) for m, (recs, rows) in cli_by_model.items()}
    records, verdicts = [], {}
    for r in api_records:
        by_id, rows = index.get(r.get("model"), ({}, {}))
        if r["item_id"] in by_id:
            records.append(by_id[r["item_id"]])
            verdicts.update({k: v for k, v in rows.items() if k.rsplit("#", 1)[0] == r["item_id"]})
    return records, verdicts


def extraction_counts(items, api_records) -> dict:
    """The API calibration's requirement extraction set against M1's, by count: for each sampled job description
    whose API record kept its extracted count, that count and the number of M1 requirements the evals answer it
    on. Records made before the extracted texts were kept (`extracted_requirement_texts`) give counts only, so
    how many requirements matched is not measured here."""
    api = {r["item_id"]: r for r in api_records}
    pairs = [(api[i.item_id]["extracted_requirements"], len(i.requirements)) for i in items
             if i.set == "jd" and isinstance((api.get(i.item_id) or {}).get("extracted_requirements"), int)]
    return {"extract_n_jd": len(pairs), "extract_reqs_api_mean": _mean([a for a, _ in pairs]),
            "extract_reqs_cli_mean": _mean([c for _, c in pairs]),
            "extract_n_jd_differ": sum(1 for a, c in pairs if a != c)}


def _booked(prices, record: dict, call: dict) -> float:
    """What a call booked against the spend cap: its booked_usd (its usage priced, or its worst case when it may
    have been billed with no usage), else its usage priced at the record's model's rates."""
    if "booked_usd" in call:
        return call["booked_usd"]
    return cost_usd(_price(prices, record.get("model")), call["usage"])


def summarise(items, api_records, cli_records, api_support, cli_support, prices, model=None) -> dict:
    """The calibration numbers and the stop rule. `items` are the sampled items; records are the private answer
    records (the CLI ones from cli_side for a mixed setup); the support args are {key: verdict row} from
    support_A.jsonl; `prices` is one Prices or a {model: Prices} map, each API record priced at its own model's.
    `cost_total` is the booked spend of every call the sampled items' API records made; the per-answer costs
    price each answer's usage.
    `complete` is True when three things hold: every sampled item was compared, every item whose route makes a
    call was called, and no kept API claim is unjudged by judge A. A cap stop leaves it False, and the published
    facts refuse an incomplete calibration."""
    ids = {i.item_id for i in items}
    api = {r["item_id"]: r for r in api_records if r["item_id"] in ids}
    cli = {r["item_id"]: r for r in cli_records if r["item_id"] in ids}
    both = sorted(api.keys() & cli.keys())
    called = [r for r in api.values() if r.get("calls")]
    errs = [e for r in api.values() for e in r.get("errors") or []]

    agree = [api[i]["outcome"] == cli[i]["outcome"] for i in both]
    agreement = _mean([1.0 if a else 0.0 for a in agree])
    # every called, error-free item's answer prompt must equal its CLI record's (a jd answers with M1's requirements)
    ok = [i for i in both if api[i].get("calls") and not api[i].get("errors")]
    parity = _mean([1.0 if api[i]["prompt_sha"] == cli[i]["prompt_sha"] else 0.0 for i in ok])
    a_sup, c_sup = _support_mean(api_support, [api[i] for i in both]), _support_mean(cli_support, [cli[i] for i in both])
    gap = None if a_sup is None or c_sup is None else a_sup - c_sup
    api_unjudged = sup.rates(api_support or {}, [api[i] for i in both])["n_kept_unjudged"]

    tin = [sum(c["usage"].get("input_tokens", 0) for c in r["calls"]) for r in called]
    tout = [sum(c["usage"].get("output_tokens", 0) for c in r["calls"]) for r in called]
    lat = [sum(c["ms"] for c in r["calls"]) for r in called]
    cost = [sum(cost_usd(_price(prices, r.get("model")), c["usage"]) for c in r["calls"]) for r in called]
    worst_ok = all(c["worst_case_usd"] >= cost_usd(_price(prices, r.get("model")), c["usage"])
                   for r in api.values() for c in r.get("calls") or [])
    n_to_call = sum(1 for i in items if route(i.text).kind not in NO_CALL)
    plist = [prices] if isinstance(prices, Prices) else [prices[m] for m in sorted(prices)]

    reasons = []
    if agreement is None or agreement < AGREEMENT_MIN:
        reasons.append(f"outcome agreement {agreement} below {AGREEMENT_MIN}")
    if gap is None:
        reasons.append("support gap unmeasured")
    elif abs(gap) > GAP_MAX:
        reasons.append(f"|support gap| {abs(gap):.3f} above {GAP_MAX}")
    if parity is None or parity < PARITY_MIN:
        reasons.append(f"prompt parity {parity} below {PARITY_MIN}")
    if not worst_ok:
        reasons.append("a worst-case reservation was below an actual cost")
    return {
        "model": model, "n": len(ids), "called": len(called), "n_to_call": n_to_call,
        "n_api_unjudged": api_unjudged,
        "complete": len(both) == len(ids) and len(called) == n_to_call and api_unjudged == 0, "errors": len(errs),
        "truncated": sum(e.startswith("truncated") for e in errs), "refused": sum(e.startswith("refusal") for e in errs),
        "outcome_agreement": agreement, "n_compared": len(both),
        "support_rate_api": a_sup, "support_rate_cli": c_sup, "support_gap": gap,
        "prompt_parity": parity, "n_parity": len(ok),
        "note": "run `support --records work/answers/calibration/<slug>/records.jsonl` (and the CLI support_A) before --summarise, or the support gap is unmeasured and the verdict is stop",
        "tokens_in_mean": _mean(tin), "tokens_in_p95": _pct(tin, 95),
        "tokens_out_mean": _mean(tout), "tokens_out_p95": _pct(tout, 95),
        "latency_ms_p50": _pct(lat, 50), "latency_ms_p95": _pct(lat, 95),
        "cost_per_answer_mean": _mean(cost), "cost_per_answer_p95": _pct(cost, 95),
        "cost_total": sum(_booked(prices, r, c) for r in api.values() for c in r.get("calls") or []),
        "worst_case_ok": worst_ok, **extraction_counts(items, api.values()),
        "prices": [{"model": p.model, "source": p.source, "checked": p.checked} for p in plist],
        "stop_rule": {"verdict": "stop" if reasons else "go", "reasons": reasons},
    }


# ---- the summary from the private files, and when it goes stale ------------------

def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _verdict_rows(path: Path) -> dict:
    return {r["key"]: r for r in read_jsonl(path)}


def summarise_files(answers, slug: str, model, items, prices, slugs=MODEL_SLUGS) -> dict:
    """summarise() over the files under `answers` (work/answers): the API records and judge-A verdicts in
    calibration/<slug>/, each item compared with its own model's claude -p record and verdicts in <model slug>/.
    `inputs` holds the sha256 of every file read (None for one that did not exist), keyed by its name under
    `answers`, so stale_inputs can tell when the summary no longer matches the files."""
    answers = Path(answers)
    api_name, sup_name = f"calibration/{slug}/records.jsonl", f"calibration/{slug}/support_A.jsonl"
    names, cli_by_model = [api_name, sup_name], {}
    for m in models_of(model):
        recs, rows = f"{slugs[m]}/records.jsonl", f"{slugs[m]}/support_A.jsonl"
        names += [recs, rows]
        cli_by_model[m] = (read_jsonl(answers / recs), _verdict_rows(answers / rows))
    api = read_jsonl(answers / api_name)
    cli_records, cli_support = cli_side(api, cli_by_model)
    s = summarise(items, api, cli_records, _verdict_rows(answers / sup_name), cli_support, prices, model=model)
    return dict(s, inputs={n: _sha256(answers / n) for n in sorted(names)})


def stale_inputs(answers, summary: dict) -> list[str]:
    """The input names whose file changed (or appeared, or went) since summarise_files wrote `summary`."""
    answers = Path(answers)
    return sorted(n for n, sha in (summary.get("inputs") or {}).items() if _sha256(answers / n) != sha)
