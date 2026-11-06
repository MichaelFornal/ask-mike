"""Ledgered JSON-returning model calls. Pattern from deal-terms-desk evals/tmachine.py (re-ask when the prompt changes)."""
import hashlib
import json
import time
from dataclasses import dataclass


@dataclass
class AskResult:
    value: object
    ok: bool
    error: str
    prompt_sha: str


def prompt_sha(prompt: str) -> str:
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]


def first_json(text: str, want: type):
    """First JSON array (want=list) or object (want=dict) in text, found by raw_decode at each bracket."""
    opener = "[" if want is list else "{"
    dec = json.JSONDecoder()
    i = text.find(opener)
    while i != -1:
        try:
            val, _ = dec.raw_decode(text, i)
            if isinstance(val, want):
                return val
        except ValueError:
            pass
        i = text.find(opener, i + 1)
    return None


def _one(key, prompt, sha, model, runner, ledger):
    """Returns (result_text, error). A ledger hit needs the same prompt_sha and model."""
    rec = ledger.get(key)
    if rec and rec.get("prompt_sha") == sha and rec.get("model") == model:
        return rec["result"], ""
    try:
        reply = runner(prompt, model)
    except RuntimeError as e:
        return None, str(e)[:200]
    text = str(reply.get("result", ""))
    ledger.put({"key": key, "prompt_sha": sha, "model": model, "result": text, "ts": time.time()})
    return text, ""


REPLY_KEYS = ("result", "structured_output", "usage")  # the runner contract's fields, all a cache keeps


def schema_sha(schema) -> str:
    """sha1[:12] of the schema's canonical JSON, or "None" when there is no schema."""
    if schema is None:
        return "None"
    return hashlib.sha1(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]


def runner_key(prompt: str, model: str, schema=None, effort=None) -> str:
    return f"{model}|{effort}|{schema_sha(schema)}|{prompt_sha(prompt)}"


def ledgered_runner(runner, ledger):
    """Wrap a runner(prompt, model, schema=None, effort=None) so a repeat of the same prompt and settings
    is answered from `ledger`. A RuntimeError is never cached, so the next ask calls the runner again.
    A call with no schema and no effort reaches the runner as runner(prompt, model), as M1's runners take."""
    field = getattr(ledger, "key", "key")

    def run(prompt, model, schema=None, effort=None) -> dict:
        key = runner_key(prompt, model, schema, effort)
        rec = ledger.get(key)
        if rec is not None and isinstance(rec.get("reply"), dict):
            return dict(rec["reply"])
        if schema is None and effort is None:
            reply = runner(prompt, model)
        else:
            reply = runner(prompt, model, schema=schema, effort=effort)
        if not isinstance(reply, dict):
            return reply  # a broken runner: not cached, and the caller sees the contract break
        kept = {k: reply[k] for k in REPLY_KEYS if k in reply}
        ledger.put({field: key, "model": model, "effort": effort, "schema_sha": schema_sha(schema),
                    "prompt_sha": prompt_sha(prompt), "reply": kept, "ts": time.time()})
        return dict(kept)

    return run


def ask_json(key, prompt, model, runner, ledger, want) -> AskResult:
    sha = prompt_sha(prompt)
    for k in (key, key + "#retry1"):
        text, err = _one(k, prompt, sha, model, runner, ledger)
        if err:
            return AskResult(None, False, err, sha)
        val = first_json(text, want)
        if val is not None:
            return AskResult(val, True, "", sha)
    return AskResult(None, False, "parse", sha)
