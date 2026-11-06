import subprocess

import pytest

from askmike.private import PrivateDirError, inside_git_worktree, private_dir, raw_dir, work_dir


def test_private_dir_unset_raises(monkeypatch):
    monkeypatch.delenv("ASK_PRIVATE_DIR", raising=False)
    with pytest.raises(PrivateDirError, match="ASK_PRIVATE_DIR"):
        private_dir()


def test_private_dir_inside_git_tree_raises(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path / "priv"))
    with pytest.raises(PrivateDirError, match="git work tree"):
        private_dir()


def test_work_dir_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path / "priv"))
    assert work_dir() == (tmp_path / "priv").resolve() / "work" and work_dir().is_dir()


def test_raw_dir_is_under_private(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(tmp_path / "priv"))
    assert raw_dir() == (tmp_path / "priv").resolve() / "raw"


def test_private_dir_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASK_PRIVATE_DIR", "~/priv")
    assert private_dir() == (tmp_path / "priv").resolve()


def test_inside_git_worktree_none_outside(tmp_path):
    assert inside_git_worktree(tmp_path / "x") is None
