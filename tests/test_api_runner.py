"""The API runner and prices, against a fake client: no network, no real key."""
import json
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest

from askmike.llm.api_runner import RunnerError, make_api_runner
from askmike.llm.prices import CHARS_PER_TOKEN, cost_usd, load_prices, worst_case_usd

SCHEMA = {"type": "object", "properties": {"state": {"type": "string"}}}
FAKE_KEY = "sk-ant-FAKEKEY-do-not-leak-123"


def reply(text='{"state": "answered"}', stop="end_turn", usage=None):
    u = usage or NS(input_tokens=100, output_tokens=20, cache_creation_input_tokens=None, cache_read_input_tokens=5)
    return NS(content=[NS(type="thinking", thinking="hmm"), NS(type="text", text=text[:5]), NS(type="text", text=text[5:])],
              stop_reason=stop, usage=u)


class Client:
    def __init__(self, result):
        self.result, self.calls = result, []
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def status_error(cls, code, msg):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(msg, response=httpx.Response(code, request=req), body=None)


def test_api_runner_sends_output_config_format_and_effort():
    c = Client(reply())
    out = make_api_runner(1234, client=c)("hello", "claude-haiku-5-5", schema=SCHEMA, effort="medium")
    kw = c.calls[0]
    assert kw["messages"] == [{"role": "user", "content": "hello"}] and "system" not in kw
    assert kw["max_tokens"] == 1234 and "thinking" not in kw
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert out["result"] == '{"state": "answered"}'  # text blocks joined, thinking dropped
    assert out["structured_output"] == {"state": "answered"}
    assert out["usage"] == {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 5}
    # no schema, no effort: no output_config, no structured_output
    c2 = Client(reply("plain words"))
    out2 = make_api_runner(10, client=c2)("p", "claude-haiku-5-5")
    assert "output_config" not in c2.calls[0] and "structured_output" not in out2 and out2["result"] == "plain words"
    # unparseable text under a schema is still returned as text
    assert "structured_output" not in make_api_runner(10, client=Client(reply("not json")))("p", "m", schema=SCHEMA)


def test_api_runner_maps_errors_truncation_refusal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    req = httpx.Request("POST", "https://x")
    cases = [
        (anthropic.APITimeoutError(req), "timeout"),
        (anthropic.APIConnectionError(request=req), "connection"),
        (status_error(anthropic.RateLimitError, 429, "slow"), "rate_limited"),
        (status_error(anthropic.InternalServerError, 500, "boom"), "overloaded"),
        (status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), "billing"),
        (status_error(anthropic.BadRequestError, 400, "max_tokens exceeds the limit"), "bad_request"),
        (ValueError("odd"), "connection"),
    ]
    for exc, kind in cases:
        with pytest.raises(RunnerError) as e:
            make_api_runner(10, client=Client(exc))("p", "m")
        assert e.value.kind == kind and isinstance(e.value, RuntimeError) and e.value.usage == {}
    with pytest.raises(RunnerError) as e:
        make_api_runner(10, client=Client(reply(stop="max_tokens")))("p", "m")
    assert e.value.kind == "truncated" and e.value.usage["output_tokens"] == 20
    with pytest.raises(RunnerError) as e:
        make_api_runner(10, client=Client(reply(stop="refusal")))("p", "m")
    assert e.value.kind == "refusal" and e.value.usage["input_tokens"] == 100
    with pytest.raises(ValueError):
        RunnerError("nope", "x")


def test_runner_error_never_carries_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    for exc in (status_error(anthropic.AuthenticationError, 401, f"bad key {FAKE_KEY}"),
                ValueError(f"header x-api-key: {FAKE_KEY}")):
        with pytest.raises(RunnerError) as e:
            make_api_runner(10, client=Client(exc))("p", "m")
        assert FAKE_KEY not in str(e.value) and FAKE_KEY not in repr(e.value)


def test_cost_and_worst_case():
    p = load_prices("claude-sonnet-5-5")
    assert (p.input, p.output, p.cache_write, p.cache_read) == (2.0, 10.0, 2.5, 0.10)
    assert p.source.startswith("https://") and p.checked == "2026-11-04"
    h = load_prices("claude-haiku-5-5")
    assert (h.input, h.output) == (0.10, 0.50)
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
         "cache_read_input_tokens": 1_000_000}
    assert cost_usd(h, u) == pytest.approx(0.10 + 0.50 + 0.125 + 0.01)
    assert cost_usd(h, None) == 0 and cost_usd(h, {"input_tokens": None}) == 0
    assert CHARS_PER_TOKEN == 2
    assert worst_case_usd(h, 2_000_000, 1_000_000) == pytest.approx(0.10 + 0.50)
    # the reservation covers a call whose prompt tokenises at 1 token per 2 chars
    assert worst_case_usd(h, 4000, 8000) >= cost_usd(h, {"input_tokens": 2000, "output_tokens": 8000})
    with pytest.raises(KeyError):
        load_prices("claude-nope")


def test_budget_is_a_runner_error_kind_and_maybe_billed_has_one_home():
    from askmike.evals import calibrate as cal
    from askmike.llm import api_runner

    e = RunnerError("budget", "the model budget is reached")
    assert e.kind == "budget" and e.usage == {} and isinstance(e, RuntimeError)
    assert api_runner.MAYBE_BILLED == ("timeout", "connection") and cal.MAYBE_BILLED is api_runner.MAYBE_BILLED
    assert set(api_runner.MAYBE_BILLED) <= set(api_runner.KINDS)
