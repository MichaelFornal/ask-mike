"""Mike's pick in askmike/answer/settings.json, and the loader that validates it."""
import json

import pytest

from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.settings import SETTINGS_PATH, load_answer_settings

PICK = {"model": {"question": "claude-haiku-5-5", "jd": "claude-sonnet-5-5"}, "effort": "medium",
        "max_tokens": 8000, "picked_by": "Mike", "picked_on": "2026-11-04", "template_sha": "8f2412145427"}


def test_settings_json_holds_mikes_split_pick():
    assert json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) == PICK
    s = load_answer_settings()
    assert s.model == {"question": "claude-haiku-5-5", "jd": "claude-sonnet-5-5"}
    assert s.model_for("question") == "claude-haiku-5-5" and s.model_for("jd") == "claude-sonnet-5-5"
    assert (s.effort, s.max_tokens, s.picked_by, s.picked_on) == ("medium", 8000, "Mike", "2026-11-04")
    assert s.template_sha == ANSWER_TEMPLATE_SHA


@pytest.mark.parametrize("change, match", [
    ({"model": {"question": "claude-haiku-5-5"}}, "question and jd"),
    ({"model": {"question": "claude-haiku-5-5", "jd": "claude-sonnet-5-5", "handoff": "claude-haiku-5-5"}},
     "question and jd"),
    ({"model": {"question": "claude-haiku-5-5", "jd": "claude-opus-5-5"}}, "ANSWER_MODELS"),
    ({"model": "claude-haiku-5-5"}, "map"),
    ({"model": None}, "map"),
    ({"template_sha": "000000000000"}, "ANSWER_TEMPLATE_SHA"),
    ({"effort": "high"}, "ANSWER_EFFORT"),
    ({"max_tokens": 4000}, "MAX_TOKENS"),
])
def test_loader_refuses_a_bad_pick(tmp_path, change, match):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({**PICK, **change}), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_answer_settings(p)
