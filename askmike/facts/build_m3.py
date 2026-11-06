"""M3 facts: the month budget and the caps the box enforces, each as {value, display, cmd, inputs}, with PREFIX "m3_".

Pattern from askmike/facts/build_m2.py (`build_m2`, `check`, values rounded to 4 places, inputs by sha256), adapted
2026-11-05 for M3 Task 7. The one input is deploy/budget.json:
- the month budget all-in (`total_usd_month`);
- the server's share and Deal Terms Desk's model cap, both from DTD's deploy/hosting.json;
- the day cap's share of the month (`day_cap_share`, DTD's ratio).

Ask Mike's model cap is what is left: total - server - DTD's cap. The day cap is that times day_cap_share. push.sh
refuses unless the box's ASK_MONTH_CAP_USD and ASK_DAY_CAP_USD equal m3_model_cap_usd and m3_day_cap_usd.

build_m3 raises FactsRefused, naming the file and the field, when the record is missing or not a JSON object, when a
field is missing, not a number or not positive, when day_cap_share is above 1, or when nothing is left for the model.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

PREFIX = "m3_"
CMD_FACTS = "uv run python -m askmike.evals facts-m3"
BUDGET = "deploy/budget.json"
FIELDS = ("total_usd_month", "server_usd_month", "dtd_model_cap_usd_month", "day_cap_share")


class FactsRefused(ValueError):
    """deploy/budget.json cannot give the caps: no m3 fact is written."""


def display(value: float) -> str:
    """Every m3 fact is an amount in dollars, printed to the cent."""
    return f"${value:,.2f}"


def _round(v: float) -> float:
    v = round(v, 4)
    return 0.0 if v == 0 else v


def build_m3(repo) -> dict[str, dict]:
    """Every m3_ fact from the repo's deploy/budget.json, or FactsRefused."""
    path = Path(repo) / BUDGET
    try:
        raw = path.read_bytes()
    except OSError:
        raise FactsRefused(f"{BUDGET} is missing") from None
    try:
        budget = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise FactsRefused(f"{BUDGET} is not JSON") from None
    if not isinstance(budget, dict):
        raise FactsRefused(f"{BUDGET} is not a JSON object")
    for field in FIELDS:
        v = budget.get(field)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise FactsRefused(f"{BUDGET}: {field} must be a positive number, got {v!r}")
    if budget["day_cap_share"] > 1:
        raise FactsRefused(f"{BUDGET}: day_cap_share must be at most 1, got {budget['day_cap_share']!r}")
    total, server, dtd = budget["total_usd_month"], budget["server_usd_month"], budget["dtd_model_cap_usd_month"]
    model = _round(total - server - dtd)
    if model <= 0:
        raise FactsRefused(f"{BUDGET}: total_usd_month {total} leaves no model budget after the server ({server}) "
                           f"and DTD's model cap ({dtd})")
    inputs = {BUDGET: hashlib.sha256(raw).hexdigest()}
    values = {"m3_total_usd_month": total, "m3_server_usd_month": server, "m3_dtd_model_cap_usd_month": dtd,
              "m3_model_cap_usd": model, "m3_day_cap_usd": model * budget["day_cap_share"]}
    facts = {}
    for key, v in values.items():
        v = _round(float(v))
        facts[key] = {"value": v, "display": display(v), "cmd": CMD_FACTS, "inputs": dict(inputs)}
    return dict(sorted(facts.items()))


def check(repo, facts_path) -> list[str]:
    """M3 keys whose recomputed fact differs from facts.json (missing and extra keys included), sorted."""
    fresh = build_m3(repo)
    path = Path(facts_path)
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    stored = {k: v for k, v in stored.items() if k.startswith(PREFIX)}
    return sorted(k for k in set(fresh) | set(stored) if fresh.get(k) != stored.get(k))
