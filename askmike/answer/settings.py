"""The answer settings Mike picked (askmike/answer/settings.json), validated on load.

`model` maps each route that makes a model call to the model that answers it: "question" and "jd" (a jd's
requirement extraction runs on the jd model too). The pick was made on one frozen prompt, so the file names that
template's sha and the loader refuses it once the template has changed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from askmike.answer.config import ANSWER_EFFORT, ANSWER_MODELS, MAX_TOKENS
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA

SETTINGS_PATH = Path(__file__).with_name("settings.json")
ROUTE_KINDS = ("question", "jd")  # the routes that make a model call


@dataclass(frozen=True)
class AnswerSettings:
    model: dict  # {"question": model id, "jd": model id}
    effort: str
    max_tokens: int
    picked_by: str
    picked_on: str
    template_sha: str

    def model_for(self, kind: str) -> str:
        return self.model[kind]


def load_answer_settings(path=SETTINGS_PATH) -> AnswerSettings:
    """The pick, or ValueError naming what is wrong: the model map needs exactly the kinds question and jd, each
    model in ANSWER_MODELS, and effort, max_tokens and template_sha equal to the code's constants."""
    path = Path(path)
    d = json.loads(path.read_text(encoding="utf-8"))
    model = d.get("model")
    if not isinstance(model, dict):
        raise ValueError(f"{path.name}: model must be a map from route kind to model id")
    if set(model) != set(ROUTE_KINDS):
        raise ValueError(f"{path.name}: model needs exactly the kinds question and jd, not {sorted(model)}")
    for kind in ROUTE_KINDS:
        if model[kind] not in ANSWER_MODELS:
            raise ValueError(f"{path.name}: the {kind} model {model[kind]!r} is not in ANSWER_MODELS")
    if d.get("template_sha") != ANSWER_TEMPLATE_SHA:
        raise ValueError(f"{path.name}: template_sha {d.get('template_sha')!r} is not the current "
                         f"ANSWER_TEMPLATE_SHA {ANSWER_TEMPLATE_SHA}; the pick was made on another prompt")
    if d.get("effort") != ANSWER_EFFORT:
        raise ValueError(f"{path.name}: effort {d.get('effort')!r} is not ANSWER_EFFORT {ANSWER_EFFORT!r}")
    if d.get("max_tokens") != MAX_TOKENS:
        raise ValueError(f"{path.name}: max_tokens {d.get('max_tokens')!r} is not MAX_TOKENS {MAX_TOKENS}")
    return AnswerSettings({k: model[k] for k in ROUTE_KINDS}, d["effort"], d["max_tokens"], d.get("picked_by"),
                          d.get("picked_on"), d["template_sha"])
