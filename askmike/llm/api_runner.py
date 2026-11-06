# ported from deal-terms-desk answer/api_runner.py on 2026-11-04 (Mike's own MIT repo); thinking budget dropped,
# output_config (effort + json_schema format) added, text blocks joined, structured_output parsed
import json
import os
import threading

import anthropic

from askmike.answer.config import MAX_TOKENS

# "budget": the spend ledger refused the call before it was made (askmike/service/desk.py `metered`)
KINDS = ("rate_limited", "overloaded", "timeout", "connection", "bad_request", "billing", "refusal", "truncated",
         "budget")
# Failures with no usage after which the call may still have been billed: booked at the worst case. Every other
# failure with no usage was refused unbilled and is released. Ported from DTD; the desk and calibrate read it here.
MAYBE_BILLED = ("timeout", "connection")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# A 400/402/403 that says one of these is the account's money running out; "limit" alone is not enough.
BILLING_WORDS = ("credit", "billing", "spend", "usage limit")
TRUNCATED = ("max_tokens", "model_context_window_exceeded")


class RunnerError(RuntimeError):
    """A failed API call. `kind` is one of KINDS; `usage` is what the call was billed for ({} when nothing came
    back). A RuntimeError, so Answerer.ask turns it into a fallback."""

    def __init__(self, kind: str, message: str, usage: dict | None = None):
        if kind not in KINDS:
            raise ValueError(f"unknown runner error kind {kind!r}")
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.usage = dict(usage or {})


def _scrub(text: str) -> str:
    """Never let the key reach a message, whatever an exception printed."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    return text.replace(key, "[key]") if key else text


def _usage(u) -> dict:
    return {k: int(getattr(u, k, None) or 0) for k in USAGE_KEYS}


def _kind(e: Exception) -> str:
    if isinstance(e, anthropic.APITimeoutError):  # a subclass of APIConnectionError: test it first
        return "timeout"
    if isinstance(e, anthropic.APIConnectionError):
        return "connection"
    if isinstance(e, anthropic.RateLimitError):
        return "rate_limited"
    if isinstance(e, (anthropic.OverloadedError, anthropic.InternalServerError)):
        return "overloaded"
    if (isinstance(e, anthropic.APIStatusError) and e.status_code in (400, 402, 403)
            and any(w in str(e).lower() for w in BILLING_WORDS)):
        return "billing"
    return "bad_request"


def make_api_runner(max_tokens: int = MAX_TOKENS, client=None, timeout: float = 90.0):
    """runner(prompt, model, schema=None, effort=None) -> {"result", "usage", "structured_output"?}: the
    `run_claude` contract over the Messages API. One user turn, no system prompt, so the prompt is the bytes
    `claude -p` gets on stdin. Adaptive thinking is the model default (no thinking parameter). `effort` goes in
    output_config["effort"], `schema` in output_config["format"]. The client is built on first use with no SDK
    retry. A truncated or refused reply raises RunnerError with its billed usage."""
    state, lock = {"client": client}, threading.Lock()

    def runner(prompt: str, model: str, schema=None, effort=None) -> dict:
        with lock:
            if state["client"] is None:
                state["client"] = anthropic.Anthropic(max_retries=0, timeout=timeout)
            c = state["client"]
        cfg: dict = {}
        if effort is not None:
            cfg["effort"] = effort
        if schema is not None:
            cfg["format"] = {"type": "json_schema", "schema": schema}
        extra = {"output_config": cfg} if cfg else {}
        try:
            msg = c.messages.create(model=model, max_tokens=max_tokens,
                                    messages=[{"role": "user", "content": prompt}], **extra)
        except anthropic.AnthropicError as e:
            raise RunnerError(_kind(e), _scrub(str(e))[:300]) from None
        except Exception as e:  # noqa: BLE001 - outside the SDK's classes: whether it was billed is unknown
            raise RunnerError("connection", _scrub(f"{type(e).__name__}: {e}")[:300]) from None
        usage = _usage(msg.usage)
        if msg.stop_reason in TRUNCATED:
            raise RunnerError("truncated", f"reply stopped at {msg.stop_reason} (max_tokens={max_tokens})", usage)
        if msg.stop_reason == "refusal":
            raise RunnerError("refusal", "the model declined to answer", usage)
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        out = {"result": text, "usage": usage, "stop_reason": msg.stop_reason}
        if schema is not None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                out["structured_output"] = data
        return out
    return runner
