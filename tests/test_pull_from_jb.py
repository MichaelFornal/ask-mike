"""Offline tests for tools/pull_from_jb.py. Run: uv run pytest"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pull_from_jb as pull  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Fixture:
    """A fake JB/DOCS/MEMORY tree holding one file per source pattern."""

    def __init__(self, base: Path):
        self.jb, self.docs, self.mem = base / "jb", base / "docs", base / "mem"
        roots = {"JB": self.jb, "DOCS": self.docs, "MEMORY": self.mem}
        for _group, root, pattern in pull.SOURCES:
            rel = pattern.replace("m*", "m1").replace("*", "acme-J1")
            write(roots[root] / rel, f"{root}:{rel}\n")
        run = self.jb / "top50" / "runs" / "2026-10-29"
        write(run / "jd" / "c001.txt", "TITLE: AI Engineer\n")
        write(run / "final.json", json.dumps([
            {"key": "c001", "rank": 1, "company": "Acme", "title": "AI Engineer"},
            {"key": "c999", "rank": 2, "company": "Gone", "title": "No JD"},
        ]))
        db = self.jb / "apply" / "apply.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE answers (question_hash TEXT, question_text TEXT, answer TEXT, times_used INTEGER)")
        con.execute("CREATE TABLE ref_answers (ref_key TEXT, qhash TEXT, question TEXT, answer TEXT, written_at TEXT, source TEXT)")
        con.execute("INSERT INTO answers VALUES ('h', 'Why us?', 'Because.', 1)")
        con.commit()
        con.close()

    def env(self, private: Path) -> dict[str, str]:
        return {"JB_ROOT": str(self.jb), "DOCS_ROOT": str(self.docs),
                "MEMORY_ROOT": str(self.mem), "ASK_PRIVATE_DIR": str(private)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path
    fx = Fixture(base)
    private = base / "private"
    write(private / pull.MEMORY_LIST, "# comment\nmemory story-a.md\nrules rules-note.md\n")
    write(private / pull.PROJECT_LIST, "# comment\nwidgets/demo\n")
    write(fx.docs / "widgets" / "demo" / "README.md", "a project\n")
    write(fx.mem / "story-a.md", "a story\n")
    write(fx.mem / "rules-note.md", "a rule\n")
    return Env(base, fx, private, monkeypatch)


class Env:
    def __init__(self, base, fx, private, monkeypatch):
        self.base, self.fx, self.private, self.mp = base, fx, private, monkeypatch

    def run_main(self, *args: str) -> int:
        for k, v in self.fx.env(self.private).items():
            self.mp.setenv(k, v)
        return pull.main(list(args))


def test_pull_then_check_passes(env):
    assert env.run_main() == 0
    assert env.run_main("--check") == 0
    manifest = json.loads((env.private / pull.MANIFEST).read_text())
    dests = {f["dest"] for f in manifest["files"]}
    assert "jds/applied/acme-J1.txt" in dests
    assert "stories/acme-J1__interview_prep.md" in dests
    assert "jds/top50/c001.txt" in dests
    assert "answers/apply_db_answers.json" in dests
    assert "memory/story-a.md" in dests
    assert "rules/rules-note.md" in dests
    assert manifest["top50_unresolved_picks"] == 1
    assert manifest["missing"] == []


def test_check_catches_an_edited_file(env):
    env.run_main()
    (env.private / "raw" / "profile" / "profile.json").write_text("edited\n")
    assert env.run_main("--check") == 1


def test_check_catches_an_extra_file(env):
    env.run_main()
    write(env.private / "raw" / "stray.txt", "x")
    assert env.run_main("--check") == 1


def test_missing_source_is_named_and_fails(env):
    (env.fx.jb / "site" / "copy.md").unlink()
    assert env.run_main() == 1
    manifest = json.loads((env.private / pull.MANIFEST).read_text())
    assert "JB:site/copy.md" in manifest["missing"]


def test_missing_memory_list_is_named_and_fails(env):
    (env.private / pull.MEMORY_LIST).unlink()
    assert env.run_main() == 1
    manifest = json.loads((env.private / pull.MANIFEST).read_text())
    assert f"PRIVATE:{pull.MEMORY_LIST}" in manifest["missing"]


def test_missing_project_list_is_named_and_fails(env):
    (env.private / pull.PROJECT_LIST).unlink()
    assert env.run_main() == 1
    manifest = json.loads((env.private / pull.MANIFEST).read_text())
    assert f"PRIVATE:{pull.PROJECT_LIST}" in manifest["missing"]


def test_project_list_folder_is_pulled_and_its_head_recorded(env):
    assert env.run_main() == 0
    manifest = json.loads((env.private / pull.MANIFEST).read_text())
    assert any(f["group"] == "projects" and f["source"].endswith("widgets/demo/README.md") for f in manifest["files"])
    assert "DOCS:widgets/demo" in manifest["heads"]


def test_public_script_names_no_memory_file():
    assert not [s for s in pull.SOURCES if s[1] == "MEMORY"]


def test_refuses_a_destination_inside_a_git_repo(env):
    repo = env.base / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env.private = repo / "private"
    assert env.run_main() == 2
    assert not (env.private / "raw").exists()


def test_memory_dir_follows_claude_code_naming():
    got = pull.memory_dir_for(Path("/home/x/Documents/jb"))
    assert got.parent.name == "-home-x-Documents-jb"
    assert got.name == "memory"
