"""The service's steps: uv run python -m askmike.service <step>.

- site --out DIR: writes DIR/fallback.html, the static page Caddy serves while the app is down. It holds the fallback
  content, NEXT_STEP, the box (posting to /ask) and the saved line, and links /static/style.css.
- config-check: prints, as JSON, the answer settings and the caps this environment would run with:
  {models, effort, max_tokens, template_sha, month_cap_usd, day_cap_usd}. push.sh compares them with local values.
  It exits 2 with the reason when the settings or the env are invalid (ASK_MONTH_CAP_USD must be set).
- pull-log --ssh TARGET: merges the box's visitor log by id into $ASK_PRIVATE_DIR/logs/visits.jsonl (mode 600) and
  prints "new N, total M". It never prints visitor text, ids or the target. Exits 1 with a one-line error on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from askmike.answer.settings import load_answer_settings
from askmike.private import PrivateDirError, private_dir
from askmike.service import pages
from askmike.service.config import from_env


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="askmike.service")
    sub = ap.add_subparsers(dest="step", required=True)
    site = sub.add_parser("site", help="write DIR/fallback.html, the static page Caddy serves while the app is down")
    site.add_argument("--out", required=True, type=Path)
    sub.add_parser("config-check", help="print the answer settings and the caps as JSON")
    pull = sub.add_parser("pull-log", help="merge the box's visitor log into $ASK_PRIVATE_DIR/logs/visits.jsonl")
    pull.add_argument("--ssh", required=True, metavar="TARGET")
    args = ap.parse_args(argv)
    if args.step == "site":
        return _site(args.out)
    if args.step == "config-check":
        return _config_check()
    if args.step == "pull-log":
        return _pull_log(args.ssh)
    return 2


PULL_CMD = "sudo -u ask sqlite3 -json /var/lib/ask/visits.db 'SELECT * FROM visits ORDER BY ts'"


def _pull_log(target: str) -> int:
    try:
        logs = private_dir() / "logs"
    except PrivateDirError as e:
        print(f"pull-log: {e}", file=sys.stderr)
        return 1
    # Every failure below prints a fixed line: never the ssh output, a row or the target.
    try:
        res = subprocess.run(["ssh", "--", target, PULL_CMD], capture_output=True, timeout=120)
        stdout = res.stdout.decode("utf-8", errors="strict") if isinstance(res.stdout, bytes) else res.stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        print("pull-log: ssh did not run", file=sys.stderr)
        return 1
    if res.returncode != 0:
        print(f"pull-log: ssh failed (exit {res.returncode})", file=sys.stderr)
        return 1
    try:
        rows = json.loads(stdout) if stdout.strip() else []
        if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("id") for r in rows):
            raise ValueError
    except ValueError:
        print("pull-log: the log came back unreadable or without ids", file=sys.stderr)
        return 1
    dest = logs / "visits.jsonl"
    try:
        merged: dict = {}
        if dest.exists():
            for line in dest.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    merged[row["id"]] = row
        new = sum(1 for r in rows if r["id"] not in merged)
        for r in rows:
            merged[r["id"]] = r
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in merged.values():
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(part, 0o600)
        os.replace(part, dest)
    except (OSError, ValueError, KeyError, TypeError):
        print("pull-log: could not merge into the private log", file=sys.stderr)
        return 1
    print(f"new {new}, total {len(merged)}")
    return 0


def _site(out: Path) -> int:
    from askmike.service.desk import CORPUS  # the desk's corpus; imported here, as the desk pulls in the API client
    out.mkdir(parents=True, exist_ok=True)
    (out / "fallback.html").write_text(pages.fallback_page(pages.load_units(CORPUS / "units.jsonl")),
                                       encoding="utf-8")
    print(f"site: wrote {out / 'fallback.html'}")
    return 0


def _config_check() -> int:
    try:
        settings = load_answer_settings()
        config = from_env()
    except (OSError, ValueError) as e:  # the messages name a file, a field or a variable, never a secret
        print(f"config-check: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"models": settings.model, "effort": settings.effort, "max_tokens": settings.max_tokens,
                      "template_sha": settings.template_sha, "month_cap_usd": config.month_cap_usd,
                      "day_cap_usd": config.day_cap_usd}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
