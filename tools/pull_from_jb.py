#!/usr/bin/env python3
"""Copy the raw material Ask Mike is built from into the private folder.

    python3 tools/pull_from_jb.py            # pull, write MANIFEST.json
    python3 tools/pull_from_jb.py --check    # re-hash raw/ against MANIFEST.json

The destination is $ASK_PRIVATE_DIR. It must live OUTSIDE every git work tree:
the raw files hold personal data and third-party text, and a folder no repo can
see is the only kind `git add -A` can never publish.

Roots (environment variables, defaults under the home directory):
    JB_ROOT       the private job-search repo
    DOCS_ROOT     the folder that holds the other project repos
    MEMORY_ROOT   Claude Code's memory folder for JB_ROOT

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from askmike.private import inside_git_worktree  # noqa: E402

MANIFEST = "MANIFEST.json"
MEMORY_LIST = "memory_sources.local.txt"
PROJECT_LIST = "project_sources.local.txt"

# Each entry: (group, root, pattern). A pattern with "*" must match at least one
# file; a plain path must exist. Patterns are relative to their root and name no
# employer or applicant detail: the concrete file list lands only in the private
# manifest.
SOURCES: list[tuple[str, str, str]] = [
    # 1. the evidence store
    ("profile", "JB", "apply/profile.json"),
    # 2. the canonical resume
    ("resume", "JB", "apply/resume/v2/content/master.json"),
    ("resume", "JB", "apply/resume/v2/content/final.json"),
    # 3-4. machine-counted figures and already-public wording
    ("site", "JB", "site/facts.json"),
    ("site", "JB", "site/copy.md"),
    # 5. written answers (filtered to `drafted` in M0, not here)
    ("answers", "JB", "apply/answers.json"),
    # 6. interview-prep stories
    ("stories", "JB", "apply/artifacts/*/interview_prep.md"),
    # 7. verified story notes: listed in $ASK_PRIVATE_DIR/memory_sources.local.txt
    #    (some file names name a company, so the list stays private; see memory_sources())
    # 8. other written material
    ("writeups", "JB", "apply/writeups/alder_creek.md"),
    ("writeups", "JB", "apply/writeups/water_learnings.md"),
    ("writeups", "JB", "apply/portfolio/portfolio.html"),
    ("writeups", "JB", "apply/portfolio/facts_extra.json"),
    ("writeups", "JB", "apply/resume/build_cover_letter.py"),
    # 9. the projects themselves
    ("projects", "DOCS", "deal-terms-desk/README.md"),
    ("projects", "DOCS", "deal-terms-desk/facts.json"),
    ("projects", "DOCS", "deal-terms-desk/docs/PRD.md"),
    ("projects", "DOCS", "deal-terms-desk/docs/m*/REPORT.md"),
    # Ask Mike itself (2026-11-07): until now the box had no evidence about the app it is.
    ("projects", "DOCS", "ask-mike/README.md"),
    ("projects", "DOCS", "ask-mike/facts.json"),
    ("projects", "DOCS", "ask-mike/docs/m*/REPORT.md"),
    ("projects", "DOCS", "agent-census/docs/PRD.md"),
    ("projects", "DOCS", "agent-census/site/src/data/facts.json"),
    ("projects", "DOCS", "agent-census/docs/m0/ledger-findings.md"),
    ("projects", "DOCS", "job-net/README.md"),
    ("projects", "DOCS", "gh-profile/README.md"),
    ("projects", "DOCS", "gh-profile/eval-harness.md"),
    ("projects", "DOCS", "builds/meter-to-cash/README.md"),
    ("projects", "DOCS", "builds/meter-to-cash/facts.json"),
    ("projects", "DOCS", "connectors/blurb.md"),
    # more project folders under DOCS_ROOT: listed in $ASK_PRIVATE_DIR/project_sources.local.txt
    #    (a folder name can name a company, so the list stays private; see project_sources())
    # rules: what must never be said, and how jb already checks it
    ("rules", "JB", "apply/resume/build.py"),
    ("rules", "JB", "apply/resume/gate.py"),
    ("rules", "JB", "apply/resume/draft_check.py"),
    ("rules", "JB", "apply/portfolio/pf_gates.py"),
    # the requirement-coverage baseline the evals compare against
    ("tools", "JB", "apply/resume/jd_match.py"),
    # eval job descriptions: jobs actually applied to
    ("jds", "JB", "apply/artifacts/*/jd.txt"),
]

# Repos whose HEAD is recorded, relative to their root.
REPOS: list[tuple[str, str]] = [
    ("JB", "."),
    ("JB", "site"),
    ("DOCS", "deal-terms-desk"),
    ("DOCS", "agent-census"),
    ("DOCS", "job-net"),
    ("DOCS", "gh-profile"),
    ("DOCS", "builds/meter-to-cash"),
    ("DOCS", "connectors"),
]

# apply.db tables of real application-form questions, exported to JSON.
DB_TABLES = [
    ("answers", "SELECT question_hash, question_text, answer, times_used FROM answers"),
    ("ref_answers", "SELECT ref_key, qhash, question, answer, written_at, source FROM ref_answers"),
]


def memory_dir_for(jb_root: Path) -> Path:
    """Claude Code names a project's folder after its path with '/' -> '-'."""
    return Path.home() / ".claude" / "projects" / str(jb_root).replace("/", "-") / "memory"


def roots_from_env() -> dict[str, Path]:
    jb = Path(os.environ.get("JB_ROOT") or Path.home() / "Documents" / "jb").resolve()
    docs = Path(os.environ.get("DOCS_ROOT") or Path.home() / "Documents").resolve()
    mem = Path(os.environ.get("MEMORY_ROOT") or memory_dir_for(jb)).resolve()
    return {"JB": jb, "DOCS": docs, "MEMORY": mem}


def private_dir_from_env() -> Path:
    raw = os.environ.get("ASK_PRIVATE_DIR")
    if not raw:
        sys.exit("ASK_PRIVATE_DIR is not set (the private folder outside every git repo)")
    return Path(raw).expanduser().resolve()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def dest_name(group: str, rel: str) -> str:
    """Flatten a source path into raw/<group>/; keep a parent folder when files share a name."""
    parts = Path(rel).parts
    name = parts[-1]
    if group in ("stories", "jds") and len(parts) >= 2:
        # one file per application folder: the folder name is the identity
        return f"{group}/{parts[-2]}__{name}" if group == "stories" else f"jds/applied/{parts[-2]}.txt"
    if group == "projects":
        return f"projects/{'__'.join(parts)}"
    return f"{group}/{name}"


def copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    os.utime(dest, (src.stat().st_atime, src.stat().st_mtime))


def entry(group: str, root: str, rel: str, src: Path, raw: Path, dest_rel: str) -> dict:
    dest = raw / dest_rel
    return {
        "group": group, "root": root, "source": rel, "dest": dest_rel,
        "sha256": sha256(dest), "bytes": dest.stat().st_size,
        "source_mtime": datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(),
    }


def memory_sources(private: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Read the private list of Claude-memory notes to pull: one "<group> <file name>" per line.

    The names live outside the repo because some of them name a company.
    """
    path = private / MEMORY_LIST
    if not path.is_file():
        return [], [f"PRIVATE:{MEMORY_LIST}"]
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            group, name = line.split(None, 1)
            out.append((group, "MEMORY", name))
    return out, []


def project_sources(private: Path) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]], list[str]]:
    """Read the private list of more project folders under DOCS_ROOT: one folder per line. Each folder's README.md
    is pulled and its HEAD recorded. -> (sources, repos, missing).

    The folders live outside the repo because a folder name can name a company.
    """
    path = private / PROJECT_LIST
    if not path.is_file():
        return [], [], [f"PRIVATE:{PROJECT_LIST}"]
    sources, repos = [], []
    for line in path.read_text().splitlines():
        line = line.strip().strip("/")
        if line and not line.startswith("#"):
            sources.append(("projects", "DOCS", f"{line}/README.md"))
            repos.append(("DOCS", line))
    return sources, repos, []


def pull_sources(roots: dict[str, Path], raw: Path,
                 sources: list[tuple[str, str, str]]) -> tuple[list[dict], list[str]]:
    files, missing = [], []
    for group, root, pattern in sources:
        base = roots[root]
        if "*" in pattern:
            matches = sorted(glob.glob(str(base / pattern)))
            if not matches:
                missing.append(f"{root}:{pattern} (no match)")
                continue
        else:
            p = base / pattern
            if not p.is_file():
                missing.append(f"{root}:{pattern}")
                continue
            matches = [str(p)]
        for m in matches:
            src = Path(m)
            rel = str(src.relative_to(base))
            dest_rel = dest_name(group, rel)
            if any(f["dest"] == dest_rel for f in files):
                raise SystemExit(f"two sources map to raw/{dest_rel}; extend dest_name()")
            copy_file(src, raw / dest_rel)
            files.append(entry(group, root, rel, src, raw, dest_rel))
    return files, missing


def pull_top50_jds(roots: dict[str, Path], raw: Path) -> tuple[list[dict], list[str], int]:
    """Copy the job description of every top50 pick (the jobs judged a fit)."""
    runs = roots["JB"] / "top50" / "runs"
    if not runs.is_dir():
        return [], ["JB:top50/runs"], 0
    by_key: dict[str, Path] = {}
    for p in sorted(runs.glob("*/jd/*.txt")):
        by_key.setdefault(p.stem, p)
    index, files, unresolved = [], [], 0
    seen: set[str] = set()
    for final in sorted(runs.glob("*/final.json")):
        run = final.parent.name
        if "smoke" in run:
            continue
        for item in json.loads(final.read_text()):
            key = item.get("key")
            if not key or key not in by_key:
                unresolved += 1
                continue
            index.append({"run": run, "key": key, "rank": item.get("rank"),
                          "company": item.get("company"), "title": item.get("title")})
            if key in seen:
                continue
            seen.add(key)
            src = by_key[key]
            dest_rel = f"jds/top50/{key}.txt"
            copy_file(src, raw / dest_rel)
            files.append(entry("jds", "JB", str(src.relative_to(roots["JB"])), src, raw, dest_rel))
    idx_path = raw / "jds" / "top50_index.json"
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    files.append({"group": "jds", "root": "derived", "source": "top50/runs/*/final.json",
                  "dest": "jds/top50_index.json", "sha256": sha256(idx_path),
                  "bytes": idx_path.stat().st_size, "source_mtime": None})
    return files, [], unresolved


def export_db(roots: dict[str, Path], raw: Path) -> tuple[list[dict], list[str]]:
    db = roots["JB"] / "apply" / "apply.db"
    if not db.is_file():
        return [], ["JB:apply/apply.db"]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    files = []
    try:
        for table, sql in DB_TABLES:
            rows = [dict(r) for r in con.execute(sql)]
            dest_rel = f"answers/apply_db_{table}.json"
            out = raw / dest_rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
            files.append({"group": "answers", "root": "derived",
                          "source": f"apply/apply.db:{table}", "dest": dest_rel,
                          "sha256": sha256(out), "bytes": out.stat().st_size,
                          "source_mtime": datetime.fromtimestamp(db.stat().st_mtime, timezone.utc).isoformat(),
                          "rows": len(rows)})
    finally:
        con.close()
    return files, []


def pull(private: Path) -> int:
    repo = inside_git_worktree(private)
    if repo is not None:
        print(f"REFUSED: {private} is inside the git work tree {repo}", file=sys.stderr)
        return 2
    roots = roots_from_env()
    raw = private / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    mem_sources, missing = memory_sources(private)
    proj_sources, proj_repos, proj_missing = project_sources(private)
    missing += proj_missing
    files, src_missing = pull_sources(roots, raw, SOURCES + mem_sources + proj_sources)
    missing += src_missing
    jd_files, jd_missing, unresolved = pull_top50_jds(roots, raw)
    db_files, db_missing = export_db(roots, raw)
    files += jd_files + db_files
    missing += jd_missing + db_missing

    manifest = {
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": {k: "~/" + str(v.relative_to(Path.home())) if str(v).startswith(str(Path.home())) else str(v)
                  for k, v in roots.items()},
        "heads": {f"{root}:{rel}": git_head(roots[root] / rel) for root, rel in REPOS + proj_repos},
        "files": files,
        "missing": missing,
        "top50_unresolved_picks": unresolved,
    }
    (private / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    by_group: dict[str, int] = {}
    for f in files:
        by_group[f["group"]] = by_group.get(f["group"], 0) + 1
    print(f"pulled {len(files)} files into {raw}")
    for g in sorted(by_group):
        print(f"  {g:9} {by_group[g]}")
    print(f"  top50 picks with no saved JD: {unresolved}")
    for m in missing:
        print(f"MISSING {m}", file=sys.stderr)
    return 1 if missing else 0


def check(private: Path) -> int:
    path = private / MANIFEST
    if not path.is_file():
        print(f"no {MANIFEST} in {private}", file=sys.stderr)
        return 1
    manifest = json.loads(path.read_text())
    raw = private / "raw"
    bad = 0
    listed = set()
    for f in manifest["files"]:
        listed.add(f["dest"])
        p = raw / f["dest"]
        if not p.is_file():
            print(f"GONE    {f['dest']}", file=sys.stderr)
            bad += 1
        elif sha256(p) != f["sha256"]:
            print(f"CHANGED {f['dest']}", file=sys.stderr)
            bad += 1
    extra = sorted(str(p.relative_to(raw)) for p in raw.rglob("*") if p.is_file()
                   and str(p.relative_to(raw)) not in listed)
    for e in extra:
        print(f"EXTRA   {e}", file=sys.stderr)
    for m in manifest.get("missing", []):
        print(f"MISSING {m}", file=sys.stderr)
    ok = not bad and not extra and not manifest.get("missing")
    print(f"{len(manifest['files'])} files checked: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify raw/ against MANIFEST.json")
    args = ap.parse_args(argv)
    private = private_dir_from_env()
    return check(private) if args.check else pull(private)


if __name__ == "__main__":
    sys.exit(main())
