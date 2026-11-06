"""Block private patterns from the public repo.

  uv run python tools/scan_public.py            scan every tracked file
  uv run python tools/scan_public.py --staged   scan the staged blobs (pre-commit)

Prints `file:line: <kind> pattern #N`, never the matched text. Exit 1 on any hit.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from askmike.gates.checks import PatternFileError, any_case_hit, load_private_patterns  # noqa: E402
from askmike.gates.patterns import EMAIL, PHONE, STREET  # noqa: E402
from askmike.private import PrivateDirError, private_dir  # noqa: E402

HOME_PATH = re.compile("/" + "Users/")  # built in two parts so this file never matches itself
ALLOWED_EMAILS = {"mike@forn.al", "noreply@anthropic.com"}
BINARY_OK = {".png", ".jpg", ".jpeg", ".ico", ".gif", ".webp", ".woff", ".woff2"}
ALLOWED_EMAIL_DOMAIN = "@users.noreply.github.com"


def _repo() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def _git(*args: str, repo: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def _phone_real(m: re.Match) -> bool:
    return not re.sub(r"\D", "", m.group(0))[-7:].startswith("55501")  # R13: fictional 555-01XX


def _email_real(m: re.Match) -> bool:
    e = m.group(0).lower()
    return not (e in ALLOWED_EMAILS or e.endswith(ALLOWED_EMAIL_DOMAIN))


def scan_path(rel: str, private: list) -> list[str]:
    kinds = []
    if HOME_PATH.search(rel):
        kinds.append("home-path pattern #4")
    kinds += [_private_kind(i, p) for i, p in enumerate(private) if any_case_hit(p, rel)]
    return kinds


def _private_kind(i: int, p) -> str:
    return f"{'applied-company' if 'Company' in type(p).__name__ else 'private'} pattern #{i}"


def scan_text(label: str, text: str, private: list, rel: str | None = None) -> list[str]:
    in_tests = (rel if rel is not None else label).split("/", 1)[0] == "tests"
    hits: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        kinds: list[str] = []
        if not in_tests:
            if any(_phone_real(m) for m in PHONE.finditer(line)):
                kinds.append("phone pattern #1")
            if STREET.search(line):
                kinds.append("street pattern #2")
            if any(_email_real(m) for m in EMAIL.finditer(line)):
                kinds.append("email pattern #3")
        if HOME_PATH.search(line):
            kinds.append("home-path pattern #4")
        for i, p in enumerate(private):
            if p.search(line):
                kinds.append(_private_kind(i, p))
        hits += [f"{label}:{n}: {k}" for k in kinds]
    return hits


def main(argv: list[str]) -> int:
    staged = "--staged" in argv
    repo = _repo()
    try:
        private = load_private_patterns(private_dir())
    except PatternFileError as e:  # names a private file and a line number, never the text
        print(f"cannot load private patterns: {e}")
        return 1
    except (PrivateDirError, FileNotFoundError, ValueError) as e:
        print(f"cannot load private patterns: {type(e).__name__}")
        return 1
    if staged:
        # every path in the index that differs from HEAD (renames and type changes included), minus deletions
        names = _git("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=d", "-z", repo=repo)
    else:
        names = _git("ls-files", "-z", repo=repo)
    hits: list[str] = []
    paths = sorted(x for x in names.decode("utf-8").split("\0") if x)
    for k, rel in enumerate(paths, 1):
        path_kinds = scan_path(rel, private)
        label = f"<path #{k}>" if path_kinds else rel  # never print a path that matched
        hits += [f"<path #{k}>: {kind}" for kind in path_kinds]
        try:
            data = _git("show", f":0:{rel}", repo=repo) if staged else (repo / rel).read_bytes()
        except (subprocess.CalledProcessError, OSError):
            if staged:
                hits.append(f"{label}:1: could not read staged file")
            continue  # full mode: deleted from the working tree, or a submodule
        if b"\0" in data:
            if Path(rel).suffix.lower() not in BINARY_OK:
                hits.append(f"{label}:1: binary file not scanned")
            continue
        hits += scan_text(label, data.decode("utf-8", errors="replace"), private, rel)
    for h in hits:
        print(h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
