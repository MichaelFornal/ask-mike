"""The `claude -p` runner's flags and the ledgered runner. subprocess is mocked: no real CLI call."""
import json
import subprocess

import pytest

from askmike.answer.schema import ANSWER_SCHEMA
from askmike.corpus import claude
from askmike.corpus.claude import run_claude
from askmike.llm.ask import ledgered_runner, prompt_sha, runner_key, schema_sha
from askmike.llm.ledger import Ledger, MemoryLedger

M1_CMD = ["claude", "-p", "--model", "claude-test-1", "--output-format", "json", "--tools", "",
          "--setting-sources", ""]


class FakeRun:
    """Stands in for subprocess.run: records each call and answers with one JSON reply."""

    def __init__(self, reply=None, exc=None):
        self.calls, self.exc = [], exc
        self.reply = reply if reply is not None else {"result": "{}", "is_error": False}

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(self.reply), stderr="")


def test_run_claude_adds_schema_and_effort_flags_only_when_given(monkeypatch):
    fake = FakeRun({"result": '{"state": "off_topic", "claims": []}', "is_error": False,
                    "structured_output": {"state": "off_topic", "claims": []}, "usage": {"input_tokens": 9}})
    monkeypatch.setattr(claude.subprocess, "run", fake)

    run_claude("the prompt", "claude-test-1")  # M1's call: byte-identical command
    assert fake.calls[-1][0] == M1_CMD
    assert fake.calls[-1][1]["input"] == "the prompt" and fake.calls[-1][1]["timeout"] == 900

    reply = run_claude("the prompt", "claude-test-1", schema=ANSWER_SCHEMA, effort="medium")
    cmd = fake.calls[-1][0]
    assert cmd[:len(M1_CMD)] == M1_CMD and len(cmd) == len(M1_CMD) + 4
    assert cmd[cmd.index("--json-schema") + 1] == json.dumps(ANSWER_SCHEMA, sort_keys=True, separators=(",", ":"))
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == ANSWER_SCHEMA
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert fake.calls[-1][1]["input"] == "the prompt"  # the prompt still goes on stdin, never argv
    assert reply["structured_output"] == {"state": "off_topic", "claims": []}

    run_claude("p", "claude-test-1", effort="low")
    assert fake.calls[-1][0] == M1_CMD + ["--effort", "low"]
    run_claude("p", "claude-test-1", schema={"type": "object"})
    assert fake.calls[-1][0] == M1_CMD + ["--json-schema", '{"type":"object"}']

    # failures still raise RuntimeError with a plain message
    monkeypatch.setattr(claude.subprocess, "run", FakeRun(exc=subprocess.TimeoutExpired("claude", 900)))
    with pytest.raises(RuntimeError, match="timed out"):
        run_claude("p", "claude-test-1", schema=ANSWER_SCHEMA, effort="medium")
    monkeypatch.setattr(claude.subprocess, "run", FakeRun({"result": "x", "is_error": True}))
    with pytest.raises(RuntimeError, match="error reply"):
        run_claude("p", "claude-test-1", schema=ANSWER_SCHEMA)


class Inner:
    """A runner that counts its calls; `fail` makes the next calls raise RuntimeError."""

    def __init__(self):
        self.calls, self.fail = [], False

    def __call__(self, prompt, model, schema=None, effort=None):
        self.calls.append((prompt, model, schema, effort))
        if self.fail:
            raise RuntimeError("claude exited 1: overloaded")
        return {"result": f"reply {len(self.calls)}", "structured_output": {"n": len(self.calls)},
                "usage": {"input_tokens": 3}, "session_id": "not-kept"}


def test_ledgered_runner_caches_by_prompt_and_settings_not_errors(tmp_path):
    inner, led = Inner(), MemoryLedger()
    run = ledgered_runner(inner, led)
    first = run("p1", "m1", schema=ANSWER_SCHEMA, effort="medium")
    again = run("p1", "m1", schema=ANSWER_SCHEMA, effort="medium")
    assert len(inner.calls) == 1 and first == again
    assert first == {"result": "reply 1", "structured_output": {"n": 1}, "usage": {"input_tokens": 3}}
    key = f"m1|medium|{schema_sha(ANSWER_SCHEMA)}|{prompt_sha('p1')}"
    assert runner_key("p1", "m1", ANSWER_SCHEMA, "medium") == key and led.get(key)["reply"] == first
    assert len(schema_sha(ANSWER_SCHEMA)) == 12

    # each setting is part of the key: prompt, model, effort, schema
    run("p2", "m1", schema=ANSWER_SCHEMA, effort="medium")
    run("p1", "m2", schema=ANSWER_SCHEMA, effort="medium")
    run("p1", "m1", schema=ANSWER_SCHEMA, effort="low")
    run("p1", "m1", schema={"type": "object"}, effort="medium")
    run("p1", "m1")
    assert len(inner.calls) == 6
    assert inner.calls[-1] == ("p1", "m1", None, None)

    # a RuntimeError is never cached: the next ask calls the runner again
    inner.fail = True
    for _ in range(2):
        with pytest.raises(RuntimeError):
            run("p3", "m1", schema=ANSWER_SCHEMA, effort="medium")
    assert len(inner.calls) == 8 and led.get(runner_key("p3", "m1", ANSWER_SCHEMA, "medium")) is None
    inner.fail = False
    assert run("p3", "m1", schema=ANSWER_SCHEMA, effort="medium")["result"] == "reply 9"

    # an M1-style two-argument runner still works when no schema or effort is given
    m1_calls = []

    def m1_runner(prompt, model):
        m1_calls.append((prompt, model))
        return {"result": "[1]"}
    m1 = ledgered_runner(m1_runner, MemoryLedger())
    assert m1("p", "m") == m1("p", "m") == {"result": "[1]"} and m1_calls == [("p", "m")]

    # a file ledger keeps the cache across a reload
    path = tmp_path / "ledger.jsonl"
    inner2 = Inner()
    ledgered_runner(inner2, Ledger(path))("p1", "m1", schema=ANSWER_SCHEMA, effort="medium")
    reloaded = ledgered_runner(inner2, Ledger(path))("p1", "m1", schema=ANSWER_SCHEMA, effort="medium")
    assert len(inner2.calls) == 1 and reloaded["result"] == "reply 1"


def test_memory_ledger_get_put():
    led = MemoryLedger()
    assert led.get("k") is None
    led.put({"key": "k", "v": 1})
    led.put({"key": "k", "v": 2})
    assert led.get("k") == {"key": "k", "v": 2}
    other = MemoryLedger(key="id")
    other.put({"id": "a", "v": 3})
    assert other.get("a")["v"] == 3
