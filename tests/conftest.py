"""Shared M1 fixture: the invented inputs in tests/fixtures/m1, scored by the real `run_labels` with fake rungs."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

M1_FIX = Path(__file__).parent / "fixtures" / "m1"


def fake_rung(name: str, units: list[str]):
    """A deterministic invented ranking: units ordered by a hash of (rung, query text, unit)."""
    def retrieve(text):
        return sorted(units, key=lambda u: hashlib.sha1(f"{name}|{text}|{u}".encode()).hexdigest())[:10]
    return retrieve


def make_m1(root: Path) -> tuple[Path, Path]:
    """Copy the fixture into root and write results/A and results/B. Returns (work_evals, repo)."""
    from askmike.evals.run import ALL_RUNGS, run_labels

    evals, repo = root / "work" / "evals", root / "repo"
    shutil.copytree(M1_FIX / "evals", evals)
    shutil.copytree(M1_FIX / "repo", repo)
    units = [json.loads(x)["id"] for x in (repo / "corpus" / "units.jsonl").read_text().splitlines() if x.strip()]
    rungs = {r: fake_rung(r, units) for r in ALL_RUNGS}
    for label in ("A", "B"):
        run_labels(evals, label, rungs)
    return evals, repo


@pytest.fixture(scope="session")
def m1_dirs(tmp_path_factory):
    return make_m1(tmp_path_factory.mktemp("m1"))


@pytest.fixture(scope="session")
def m1_facts(m1_dirs):
    from askmike.facts.build import build_m1
    return build_m1(*m1_dirs)


@pytest.fixture(scope="session")
def m2_dirs(tmp_path_factory):
    """(work, repo) from tests/m2_fixture.py: invented M2 records, verdicts and a complete picked calibration."""
    from m2_fixture import make_m2
    return make_m2(tmp_path_factory.mktemp("m2"))


@pytest.fixture(scope="session")
def m2_facts(m2_dirs):
    from answer_fakes import cfg
    from askmike.facts.build_m2 import build_m2
    return build_m2(*m2_dirs, cfg=cfg())
