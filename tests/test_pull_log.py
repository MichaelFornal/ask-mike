"""pull-log merges the box's visitor log into the private folder. No test reaches a server; ssh is faked. Every row here is invented."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from askmike.service import __main__ as svc


def _fake_ssh(monkeypatch, stdout="", returncode=0):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, returncode, stdout, "boom host 10.0.0.9")
    monkeypatch.setattr(svc.subprocess, "run", run)
    return calls


def _rows(*ids, text="invented question"):
    return json.dumps([{"id": i, "ts": f"2026-11-0{n+1}", "kind": "q", "input": f"{text} {i}"} for n, i in enumerate(ids)])


def _log(tmp_path):
    return tmp_path / "logs" / "visits.jsonl"


def test_pull_merges_by_id_and_prints_counts_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path))
    calls = _fake_ssh(monkeypatch, _rows("a1", "b2"))
    assert svc.main(["pull-log", "--ssh", "root@203.0.113.5"]) == 0
    assert capsys.readouterr().out.strip() == "new 2, total 2"
    cmd, kw = calls[0]
    assert cmd[:3] == ["ssh", "--", "root@203.0.113.5"] and kw["timeout"] == 120
    assert "sqlite3 -json /var/lib/ask/visits.db" in cmd[3]
    _fake_ssh(monkeypatch, _rows("b2", "c3", text="changed"))
    assert svc.main(["pull-log", "--ssh", "t"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "new 1, total 3"
    for secret in ("changed", "c3", "203.0.113.5"):
        assert secret not in out
    got = {json.loads(l)["id"]: json.loads(l) for l in _log(tmp_path).read_text().splitlines()}
    assert set(got) == {"a1", "b2", "c3"} and "changed" in got["b2"]["input"]
    assert (_log(tmp_path).stat().st_mode & 0o777) == 0o600
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700
    assert not list((tmp_path / "logs").glob("*.part"))


def test_pull_empty_table_is_no_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path))
    _fake_ssh(monkeypatch, "")
    assert svc.main(["pull-log", "--ssh", "t"]) == 0
    assert capsys.readouterr().out.strip() == "new 0, total 0"


@pytest.mark.parametrize("out,rc", [("whatever", 255), ("not json secret-text", 0), ('[{"ts": "x", "input": "secret-text"}]', 0)])
def test_pull_errors_write_nothing_and_leak_nothing(tmp_path, monkeypatch, capsys, out, rc):
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path))
    _fake_ssh(monkeypatch, out, rc)
    assert svc.main(["pull-log", "--ssh", "t"]) == 1
    cap = capsys.readouterr()
    assert "secret-text" not in cap.out + cap.err and "10.0.0.9" not in cap.out + cap.err
    assert len((cap.err).strip().splitlines()) == 1
    assert not _log(tmp_path).exists()


def test_pull_invalid_utf8_is_the_one_line_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path))
    _fake_ssh(monkeypatch, b"[\xff\xfe]")
    assert svc.main(["pull-log", "--ssh=-oProxyCommand=x"]) == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and not _log(tmp_path).exists()


def test_pull_refuses_without_a_private_dir(monkeypatch, capsys):
    monkeypatch.delenv("ASK_PRIVATE_DIR", raising=False)
    calls = _fake_ssh(monkeypatch, _rows("a1"))
    assert svc.main(["pull-log", "--ssh", "t"]) == 1
    assert calls == [] and "ASK_PRIVATE_DIR" in capsys.readouterr().err
