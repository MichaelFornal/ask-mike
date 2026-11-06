"""Answer pipeline: ASK_PRIVATE_DIR=... uv run python -m askmike.answer <step>.

gate-config prints counts only. ask prints the rendered answer, which holds only gated text.
Without --model, ask answers with Mike's pick in askmike/answer/settings.json (one model per route).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from askmike.answer.answerer import Answerer
from askmike.answer.gates import BUNDLE, AnswerGateConfig, bundle_stale, write_bundle
from askmike.answer.render import render_text
from askmike.answer.settings import load_answer_settings
from askmike.corpus.claude import run_claude
from askmike.llm.ask import ledgered_runner
from askmike.llm.ledger import MemoryLedger
from askmike.private import PrivateDirError, private_dir
from askmike.retrieval.index import StaleIndex, default_db, open_index
from askmike.retrieval.ladder import Ladder, load_settings
from askmike.retrieval.models import Embedder

CORPUS = Path(__file__).resolve().parents[2] / "corpus"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="askmike.answer")
    sub = ap.add_subparsers(dest="step", required=True)
    sub.add_parser("gate-config", help=f"write the private gate-config bundle to $ASK_PRIVATE_DIR/{BUNDLE}")
    ask = sub.add_parser("ask", help="answer one question or job description through claude -p")
    ask.add_argument("text")
    ask.add_argument("--model", default=None, help="one model for every route; default: the pick in settings.json")
    args = ap.parse_args(argv)
    if args.step == "gate-config":
        return _gate_config()
    if args.step == "ask":
        try:
            model = args.model or load_answer_settings().model
        except (FileNotFoundError, ValueError) as e:  # names the settings file and the field, never a secret
            print(f"ask: {e}", file=sys.stderr)
            return 1
        return _ask(args.text, model)
    return 2


def _gate_config() -> int:
    try:
        n = write_bundle(private_dir())
    except (PrivateDirError, FileNotFoundError, ValueError) as e:  # their messages name files, never contents
        print(f"gate-config: {e}", file=sys.stderr)
        return 1
    print(f"gate-config: wrote $ASK_PRIVATE_DIR/{BUNDLE.as_posix()} ({n['patterns']} patterns, "
          f"{n['companies']} companies, {n['honesty_allow']} honesty-allow aliases)")
    return 0


def gate_config(skills) -> AnswerGateConfig:
    """The gate bundle when it is current and readable, else the private files themselves. Both fail closed."""
    private = private_dir()
    path = private / BUNDLE
    if not bundle_stale(path, private):
        try:
            return AnswerGateConfig.from_bundle(path, skills)
        except ValueError:
            pass
    return AnswerGateConfig.from_private(private, skills)


def _ask(text: str, model: str | dict[str, str]) -> int:
    units_path = CORPUS / "units.jsonl"
    units = {u["id"]: u for u in (json.loads(x) for x in units_path.read_text(encoding="utf-8").splitlines()
                                  if x.strip())}
    skills = json.loads((CORPUS / "skills.json").read_text(encoding="utf-8"))
    try:
        emb = Embedder()
        conn = open_index(default_db(), units_path, emb.name)
    except StaleIndex as e:
        print(f"ask: {e}", file=sys.stderr)
        return 1
    try:
        cfg = gate_config(skills)
    except (PrivateDirError, FileNotFoundError, ValueError) as e:  # messages name files, never contents
        print(f"ask: {e}", file=sys.stderr)
        return 1
    ladder = Ladder(conn, emb, skills, load_settings())
    answerer = Answerer(ladder, units, skills, cfg, ledgered_runner(run_claude, MemoryLedger()), model)
    print(render_text(answerer.ask(text), units))
    return 0


if __name__ == "__main__":
    sys.exit(main())
