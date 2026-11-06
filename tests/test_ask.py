import json
import threading

from askmike.llm.ask import ask_json, first_json, prompt_sha
from askmike.llm.ledger import Ledger
from tests.fakes import fake_claude


def test_prompt_sha_is_sha1_12():
    assert len(prompt_sha("x")) == 12


def test_first_json_list_and_dict():
    assert first_json('prose [1, 2] tail', list) == [1, 2]
    assert first_json('a [bad {"k": 1} b', dict) == {"k": 1}
    assert first_json('{"k": 1}', list) is None
    assert first_json("nothing", dict) is None


def test_ledger_hit_skips_runner_and_prompt_change_reasks(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", key="key")
    run = fake_claude('[1]')
    r1 = ask_json("k", "p1", "m", run, led, list)
    r2 = ask_json("k", "p1", "m", run, led, list)
    assert r1.value == r2.value == [1] and r1.ok and len(run.calls) == 1
    ask_json("k", "p2", "m", run, led, list)
    assert len(run.calls) == 2
    ask_json("k", "p1", "m2", run, led, list)
    assert len(run.calls) == 3
    # survives reload
    led2 = Ledger(tmp_path / "l.jsonl", key="key")
    ask_json("k", "p1", "m2", run, led2, list)
    assert len(run.calls) == 3


def test_runner_error_not_cached(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", key="key")

    def boom(prompt, model):
        raise RuntimeError("x" * 500)
    r = ask_json("k", "p", "m", boom, led, list)
    assert not r.ok and len(r.error) == 200
    assert led.get("k") is None


def test_parse_failure_retries_once_then_fails(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", key="key")
    run = fake_claude("no json")
    r = ask_json("k", "p", "m", run, led, list)
    assert not r.ok and r.error == "parse" and r.value is None
    assert len(run.calls) == 2 and led.get("k#retry1") is not None
    ask_json("k", "p", "m", run, led, list)
    assert len(run.calls) == 2  # both reused


def test_ledger_torn_line_and_threads(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(p, key="key")
    ts = [threading.Thread(target=lambda i=i: led.put({"key": f"k{i}", "n": i})) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with open(p, "a") as f:
        f.write('{"key": "torn"')
    led2 = Ledger(p, key="key")
    assert led2.get("torn") is None and led2.get("k7")["n"] == 7
    assert all(json.loads(l) for l in p.read_text().splitlines())
