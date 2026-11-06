import re
from dataclasses import dataclass
from pathlib import Path

from askmike.llm.ask import ask_json

EXTRACT_MODEL = "claude-sonnet-5-5"
KINDS = {"skill", "experience", "domain", "responsibility", "other"}
MAX_REQS = 15
MAX_TEXT = 200
_TEMPLATE = Path(__file__).parent / "prompts" / "extract_requirements.md"


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    required: bool
    kind: str


@dataclass(frozen=True)
class ExtractStats:
    proposed: int
    kept: int
    dropped_invalid: int
    error: str = ""

    @property
    def parse_error(self) -> bool:
        return self.error == "parse"


def build_extract_prompt(jd_text: str) -> str:
    safe = re.sub(r"</(\s*)job_description", r"</ job_description", jd_text, flags=re.I)
    return _TEMPLATE.read_text(encoding="utf-8").replace("{jd}", safe)


def _valid(item) -> bool:
    if not isinstance(item, dict):
        return False
    text = item.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_TEXT:
        return False
    kind = item.get("kind")
    return isinstance(kind, str) and kind in KINDS and isinstance(item.get("required"), bool)


def extract_requirements(jd_id, jd_text, runner, ledger, model: str = EXTRACT_MODEL
                         ) -> tuple[list[Requirement], ExtractStats]:
    """M1's extraction call. `model` defaults to EXTRACT_MODEL, so every M1 call is unchanged; the answerer
    passes its own answer model."""
    res = ask_json(f"extract:{jd_id}", build_extract_prompt(jd_text), model, runner, ledger, list)
    if not res.ok:
        return [], ExtractStats(0, 0, 0, res.error)
    items = res.value
    good = [i for i in items if _valid(i)][:MAX_REQS]
    reqs = [Requirement(f"{jd_id}-r{n:02d}", i["text"].strip(), i["required"], i["kind"])
            for n, i in enumerate(good, 1)]
    return reqs, ExtractStats(len(items), len(reqs), sum(1 for i in items if not _valid(i)), "")
