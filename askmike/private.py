"""Resolve the private folder ($ASK_PRIVATE_DIR) that holds raw data and work files.

The folder must live outside every git work tree, so `git add -A` can never
publish what is in it. Python 3.9-compatible syntax: tools/pull_from_jb.py
imports this under the system Python.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class PrivateDirError(RuntimeError):
    """ASK_PRIVATE_DIR is unset or unsafe."""


def inside_git_worktree(path: Path) -> Optional[Path]:
    """Return the work-tree root containing `path`, or None."""
    p = path if path.exists() else path.parent
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def private_dir() -> Path:
    raw = os.environ.get("ASK_PRIVATE_DIR")
    if not raw:
        raise PrivateDirError("ASK_PRIVATE_DIR is not set (the private folder outside every git repo)")
    path = Path(raw).expanduser().resolve()
    repo = inside_git_worktree(path)
    if repo is not None:
        raise PrivateDirError(f"{path} is inside the git work tree {repo}")
    return path


def raw_dir() -> Path:
    return private_dir() / "raw"


def work_dir() -> Path:
    path = private_dir() / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path
