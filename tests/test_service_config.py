# ported from deal-terms-desk tests/test_config.py on 2026-11-05
from pathlib import Path

import pytest

from askmike.service.config import ServiceConfig, from_env


def test_defaults_need_only_the_month_cap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no GIT_SHA file here
    assert from_env({"ASK_MONTH_CAP_USD": "15"}) == ServiceConfig(
        month_cap_usd=15.0, day_cap_usd=1.5, state_dir=Path("build/state"), index_path=Path("build/index.db"),
        gate_config=None, ask_per_hour=20, fresh_per_hour=60, slots=2, api_timeout_s=90.0, git_sha="unknown",
        typical_prompt_chars=16000)


def test_config_requires_month_cap_and_refuses_bad_numbers_by_name():
    for env in ({}, {"ASK_MONTH_CAP_USD": "  "}):
        with pytest.raises(ValueError, match="ASK_MONTH_CAP_USD"):
            from_env(env)
    bad = [("ASK_MONTH_CAP_USD", v) for v in ("lots", "0", "-1", "nan", "inf")]
    bad += [("ASK_DAY_CAP_USD", v) for v in ("-1", "0", "nan", "inf", "-inf")]
    bad += [("ASK_ASK_PER_HOUR", "1.5"), ("ASK_FRESH_PER_HOUR", "0"), ("ASK_SLOTS", "-2"),
            ("ASK_API_TIMEOUT_S", "nan"), ("ASK_API_TIMEOUT_S", "0")]
    for name, value in bad:
        with pytest.raises(ValueError, match=name):
            from_env({"ASK_MONTH_CAP_USD": "15"} | {name: value})


def test_default_day_cap_is_a_tenth():
    assert from_env({"ASK_MONTH_CAP_USD": "15"}).day_cap_usd == pytest.approx(1.5)
    assert from_env({"ASK_MONTH_CAP_USD": "0.0001"}).month_cap_usd == 0.0001  # a tiny cap is allowed


def test_every_setting_can_be_overridden():
    env = {"ASK_MONTH_CAP_USD": "12", "ASK_DAY_CAP_USD": "2", "ASK_STATE": "/var/lib/ask",
           "ASK_INDEX": "/srv/ask/index.db", "ASK_GATE_CONFIG": "/etc/ask/gate_config.json",
           "ASK_ASK_PER_HOUR": "10", "ASK_FRESH_PER_HOUR": "40", "ASK_SLOTS": "3", "ASK_API_TIMEOUT_S": "45.5",
           "ASK_GIT_SHA": "abc1234"}
    assert from_env(env) == ServiceConfig(
        12.0, 2.0, Path("/var/lib/ask"), Path("/srv/ask/index.db"), Path("/etc/ask/gate_config.json"),
        10, 40, 3, 45.5, "abc1234", 16000)


def test_git_sha_comes_from_the_env_then_the_release_file_then_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert from_env({"ASK_MONTH_CAP_USD": "1"}).git_sha == "unknown"
    (tmp_path / "GIT_SHA").write_text("abc1234\n")
    assert from_env({"ASK_MONTH_CAP_USD": "1"}).git_sha == "abc1234"
    assert from_env({"ASK_MONTH_CAP_USD": "1", "ASK_GIT_SHA": "def5678"}).git_sha == "def5678"
