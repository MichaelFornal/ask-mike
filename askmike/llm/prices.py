# ported from deal-terms-desk service/prices.py on 2026-11-04 (Mike's own MIT repo); prices are per model in one file
import json
from dataclasses import dataclass
from pathlib import Path

PER = 1_000_000
# Deliberately low: English runs nearer four characters a token, and Haiku 5.5's tokenizer counts about 30% more
# tokens than the older ones, so this over-reserves on purpose.
CHARS_PER_TOKEN = 2
PRICES_PATH = Path(__file__).with_name("prices.json")


@dataclass(frozen=True)
class Prices:
    """USD per million tokens for one model, with where and when the numbers were read."""
    model: str
    input: float
    output: float
    cache_write: float
    cache_read: float
    source: str
    checked: str


def load_prices(model: str, path=PRICES_PATH) -> Prices:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    m = d["models"].get(model)
    if m is None:
        raise KeyError(f"no prices for model {model!r}")
    return Prices(model, float(m["input"]), float(m["output"]), float(m["cache_write"]), float(m["cache_read"]),
                  d["source"], d["checked"])


def cost_usd(p: Prices, usage: dict | None) -> float:
    """What one call cost, from its usage (the four API counters; missing or None count as zero)."""
    u = usage or {}

    def n(k: str) -> int:
        return int(u.get(k) or 0)
    return (n("input_tokens") * p.input + n("output_tokens") * p.output
            + n("cache_creation_input_tokens") * p.cache_write + n("cache_read_input_tokens") * p.cache_read) / PER


def worst_case_usd(p: Prices, prompt_chars: int, max_tokens: int) -> float:
    """The most a call can cost before it is made: the prompt at CHARS_PER_TOKEN, plus the full output cap."""
    return (prompt_chars / CHARS_PER_TOKEN * p.input + max_tokens * p.output) / PER
