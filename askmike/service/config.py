# ported from deal-terms-desk service/config.py on 2026-11-05; ASK_* variables, Ask Mike's fields
import math
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    month_cap_usd: float
    day_cap_usd: float
    state_dir: Path
    index_path: Path
    gate_config: Path | None  # required by the app, which refuses to start without it; the config only carries it
    ask_per_hour: int
    fresh_per_hour: int
    slots: int
    api_timeout_s: float
    git_sha: str
    typical_prompt_chars: int = 16000


def _num(env, name: str, cast, default):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a {cast.__name__}, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number, got {raw!r}")
    return value


def _git_sha(env) -> str:
    """The deployed commit: ASK_GIT_SHA, else the GIT_SHA file push writes into each release, else unknown."""
    if (env.get("ASK_GIT_SHA") or "").strip():
        return env["ASK_GIT_SHA"].strip()
    f = Path("GIT_SHA")
    return (f.read_text(encoding="utf-8").strip() or "unknown") if f.exists() else "unknown"


def from_env(env=os.environ) -> ServiceConfig:
    """The service's settings from ASK_* variables (on the box: /etc/ask/env). The month cap has no default:
    a service that does not know its budget must not start."""
    if not (env.get("ASK_MONTH_CAP_USD") or "").strip():
        raise ValueError("ASK_MONTH_CAP_USD is not set: the month's model budget in USD (a fact, in facts.json)")
    month = _num(env, "ASK_MONTH_CAP_USD", float, None)
    gate = (env.get("ASK_GATE_CONFIG") or "").strip()
    return ServiceConfig(
        month_cap_usd=month,
        day_cap_usd=_num(env, "ASK_DAY_CAP_USD", float, month / 10),
        state_dir=Path(env.get("ASK_STATE") or "build/state"),
        index_path=Path(env.get("ASK_INDEX") or "build/index.db"),
        gate_config=Path(gate) if gate else None,
        ask_per_hour=_num(env, "ASK_ASK_PER_HOUR", int, 20),
        fresh_per_hour=_num(env, "ASK_FRESH_PER_HOUR", int, 60),
        slots=_num(env, "ASK_SLOTS", int, 2),
        api_timeout_s=_num(env, "ASK_API_TIMEOUT_S", float, 90.0),
        git_sha=_git_sha(env),
    )
