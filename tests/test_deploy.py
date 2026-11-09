"""Ask Mike's deploy files: the Caddy site file, the unit, the env example, provision, push, prune and the smoke test.

Ported from deal-terms-desk tests/test_deploy.py (branch caddy-split, 7976f1f) on 2026-11-05. Nothing here reaches a
server. push.sh's own blocks run locally under a fake ssh, with a fake caddy, curl, systemctl or install where a block
needs one. provision.sh runs against a fake root. The smoke test runs against a fake box that serves the app's real
pages. Every question here is invented.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import pytest

from askmike.answer.answerer import NO_CALL, Outcome
from askmike.answer.config import HANDOFF_TEXT, MAX_INPUT_CHARS, NEXT_STEP, TOO_LONG_TEXT
from askmike.answer.evidence import Claim
from askmike.answer.gates import built_from
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.render import load_fallback
from askmike.answer.route import route
from askmike.answer.settings import load_answer_settings
from askmike.service import pages
from askmike.service.__main__ import main as service_main
from askmike.service.app import BODY_CAP

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
PUSH, PROVISION, PRUNE, HOSTPORT = (DEPLOY / n for n in ("push.sh", "provision.sh", "prune.sh", "hostport.sh"))
CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")
# The plan's exact site file, with the two rulings: the body cap is above the app's own (BODY_CAP), and a 413 gets
# the static fallback page too, so no body size and no upstream failure ever shows a Caddy error page. Review fix
# round 1: the error responses repeat the site's header block, so the fallback carries the same CSP, HSTS, nosniff
# and Referrer-Policy.
ASK_CADDY = """\
# ask.forn.al: Ask Mike. The base /etc/caddy/Caddyfile, installed by Deal Terms Desk's deploy, holds the global
# log filter that deletes visitor addresses, URIs and headers. This file adds no log directive of its own.
ask.forn.al {
	@compressible not path /ask
	encode @compressible zstd gzip
	header {
		Content-Security-Policy "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
		-Server
	}
	request_body {
		max_size 96KB
	}
	handle /static/* {
		root * /srv/ask/current/askmike/service
		file_server
	}
	handle /favicon.ico {
		root * /srv/ask/current/askmike/service/static
		file_server
	}
	handle {
		reverse_proxy 127.0.0.1:8001
	}
	handle_errors 413 502 503 504 {
		header {
			Content-Security-Policy "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
			Strict-Transport-Security "max-age=31536000"
			X-Content-Type-Options "nosniff"
			Referrer-Policy "no-referrer"
			-Server
		}
		root * /srv/ask/current/site/dist
		rewrite * /fallback.html
		file_server
	}
}
"""
DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")  # RFC 5737: documentation only, never a real server
DTD_OWNED = ("/srv/dtd", "/var/lib/dtd", "/etc/dtd", "dtd.service", "deals.caddy", "DTD_HOST")


@functools.cache
def smoke():
    spec = importlib.util.spec_from_file_location("ask_smoke", DEPLOY / "smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def push() -> str:
    return PUSH.read_text(encoding="utf-8")


def code_lines(text: str) -> list[str]:
    """The lines that are not comments (heredoc bodies included)."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def first_code(text: str, needle: str) -> int:
    """Offset of the first occurrence of `needle` on a line that is not a comment."""
    at = 0
    for ln in text.splitlines(keepends=True):
        if needle in ln and not ln.lstrip().startswith("#"):
            return at + ln.index(needle)
        at += len(ln)
    raise ValueError(needle)


def block(text: str, start: str, end_after: str) -> str:
    """text from `start` to the end of the first `fi` line after `end_after`."""
    i = text.index(start)
    j = text.index(end_after, i)
    return text[i:text.index("\nfi\n", j) + len("\nfi\n")]


def fake_uv() -> str:
    """`uv run python ...` as the test's own interpreter: push.sh's Python blocks run, with no uv."""
    return f"uv() {{ shift 2; '{sys.executable}' \"$@\"; }}\n"


def fake_mktemp(bin_: Path, remote_tmp: Path) -> None:
    """mktemp [-d] on the fake box: a fresh path under remote_tmp. macOS's mktemp ignores TMPDIR, so without this the
    box's temp files would land where the test cannot see whether they are removed."""
    (bin_ / "mktemp").write_text(
        f'#!/bin/bash\np="{remote_tmp}/tmp.$$.$RANDOM"\n'
        'if [ "${1:-}" = -d ]; then mkdir "$p"; else ( umask 077; : > "$p" ); fi\necho "$p"\n')
    (bin_ / "mktemp").chmod(0o755)


# ---- the site file, the unit and the env example ---------------------------------------------------------------------

def test_ask_caddy_exact_csp_without_script_src_and_proxies_8001():
    text = (DEPLOY / "ask.caddy").read_text(encoding="utf-8")
    assert text == ASK_CADDY
    assert text.count(f'Content-Security-Policy "{CSP}"') == 2 and smoke().CSP == CSP  # the site, and its errors
    assert "script-src" not in text and "unsafe-" not in text and "connect-src" not in text
    assert "reverse_proxy 127.0.0.1:8001" in text and "127.0.0.1:8000" not in text
    assert "root * /srv/ask/current/askmike/service" in text  # /static/style.css from the live release


def test_ask_caddy_has_no_log_and_no_global_block():
    text = (DEPLOY / "ask.caddy").read_text(encoding="utf-8")
    code = code_lines(text)
    assert code[0] == "ask.forn.al {" and code[-1] == "}"  # one site block, no global options block before it
    assert not re.search(r"^\s*log\b", text, re.M)  # the base's global filter is the only log config
    assert not re.search(r"^\s*import\b", text, re.M) and not re.search(r"^\{", text, re.M)
    depth = 0
    for ln in code:  # nothing outside the one site block
        assert depth > 0 or ln == "ask.forn.al {", ln
        depth += ln.count("{") - ln.count("}")
    assert depth == 0


def test_ask_caddy_serves_the_static_fallback_on_upstream_errors(tmp_path):
    """Review Focus 4: a restarting or crashed app, or an over-size body, gets fallback.html, never Caddy's error
    page. push.sh builds that page into each release; the app's own body cap sits below Caddy's."""
    text = (DEPLOY / "ask.caddy").read_text(encoding="utf-8")
    m = re.search(r"\n\thandle_errors ([\d ]+) \{\n(.*?)\n\t\}\n", text, re.S)
    assert m and set(m.group(1).split()) == {"413", "502", "503", "504"}
    site_header = re.search(r"\n(\theader \{\n.*?\n\t\}\n)", text, re.S).group(1)
    error_header = "".join("\t" + ln for ln in site_header.splitlines(keepends=True))
    assert m.group(2) + "\n" == error_header + ("\t\troot * /srv/ask/current/site/dist\n\t\trewrite * /fallback.html\n"
                                              "\t\tfile_server\n")
    for h in ("Content-Security-Policy", "Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy"):
        assert h in error_header, h  # the fallback carries the same security headers as every other response
    size = re.search(r"request_body \{\n\t\tmax_size (\d+)KB\n\t\}", text)
    assert size and int(size.group(1)) * 1000 > BODY_CAP  # KB or KiB: both above the app's 80,000-byte cap
    assert "-m askmike.service site --out site/dist" in push()
    assert "site/dist/" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert service_main(["site", "--out", str(tmp_path / "dist")]) == 0
    assert (tmp_path / "dist" / "fallback.html").read_text(encoding="utf-8") == pages.fallback_page(
        pages.load_units(REPO / "corpus" / "units.jsonl"))


def test_unit_runs_one_worker_on_8001_as_ask_without_access_log():
    text = (DEPLOY / "ask.service").read_text(encoding="utf-8")
    lines = code_lines(text)
    assert ("ExecStart=/srv/ask/current/.venv/bin/uvicorn --factory askmike.service.app:create_app --host 127.0.0.1 "
            "--port 8001 --workers 1 --no-access-log --forwarded-allow-ips 127.0.0.1") in lines
    for line in ("[Unit]", "[Service]", "[Install]", "User=ask", "Group=ask", "WorkingDirectory=/srv/ask/current",
                 "EnvironmentFile=/etc/ask/env", "MemoryMax=1200M", "ReadWritePaths=/var/lib/ask", "Restart=always",
                 "RestartSec=2", "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true",
                 "PrivateTmp=true", "WantedBy=multi-user.target", "After=network-online.target",
                 "Wants=network-online.target"):
        assert line in lines, line
    assert sum(ln.startswith("ExecStart=") for ln in lines) == 1 and "dtd" not in text.lower()


def test_env_example_names_and_no_secrets():
    text = (DEPLOY / "env.example").read_text(encoding="utf-8")
    pairs = dict(ln.split("=", 1) for ln in code_lines(text))
    assert pairs == {"ANTHROPIC_API_KEY": "", "ASK_MONTH_CAP_USD": "", "ASK_DAY_CAP_USD": "",
                     "ASK_STATE": "/var/lib/ask", "ASK_INDEX": "/srv/ask/current/index.db",
                     "ASK_GATE_CONFIG": "/etc/ask/gate_config.json", "FASTEMBED_CACHE_PATH": "/var/lib/ask/fastembed",
                     "HF_HOME": "/var/lib/ask/huggingface"}
    assert "sk-ant-" not in text and "@" not in text
    assert "m3_model_cap_usd" in text and "m3_day_cap_usd" in text  # where the cap values come from


def test_deploy_files_name_no_server_and_none_of_dtds_resources():
    """The repo is public: no server address or host. And neither app's files reach the other's resources: Ask Mike
    never names DTD's directories, env, unit or site file (it reaches deals.caddy only through the sites glob)."""
    for f in sorted(x for x in DEPLOY.iterdir() if x.is_file()):
        text = f.read_text(encoding="utf-8")
        ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)) - {"127.0.0.1"}
        assert all(ip.startswith(DOC_NETS) for ip in ips), (f.name, ips)  # only RFC 5737 documentation addresses
        assert not re.search(r"\broot@[\w-]+\.", text), f.name
        for owned in DTD_OWNED:
            assert owned not in text, (f.name, owned)


def test_scripts_parse():
    for f in (PUSH, PROVISION, PRUNE, HOSTPORT):
        subprocess.run(["bash", "-n", str(f)], check=True)


# ---- push.sh: gates and order ----------------------------------------------------------------------------------------

def test_push_runs_every_gate_before_anything_reaches_the_box():
    text = push()
    assert "set -euo pipefail" in text
    assert ': "${ASK_HOST:?' in text and 'BASE="${ASK_BASE:-https://ask.forn.al}"' in text
    first_ssh = first_code(text, 'ssh "$ASK_HOST"')
    gates = ("git status --porcelain", "uv run pytest -q", "uv run python -m askmike.evals facts --check",
             "uv run python -m askmike.evals facts-m2 --check", "uv run python -m askmike.evals facts-m3 --check",
             "uv run python tools/scan_public.py",
             "uv run python -m askmike.facts.copycheck README.md docs/m1/REPORT.md docs/m2/REPORT.md",
             "bundle_stale", "uv run python -m askmike.retrieval index")
    at = [first_code(text, g) for g in gates]
    assert at == sorted(at) and at[-1] < first_ssh
    assert first_code(text, "uv run python -m askmike.retrieval index") < first_code(text, "INDEX_SHA=")
    assert "sha256 build/index.db" in text and "sha256 corpus/units.jsonl" in text


def test_push_refuses_a_dirty_tree_and_ships_only_committed_code():
    text = push()
    for s in ("git archive --format=tar HEAD", "tar -x --no-same-owner", "echo $SHA > GIT_SHA",
              "UV_PYTHON_PREFERENCE=only-system UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev", "touch .complete",
              "refusing to deploy: the working tree is dirty"):
        assert s in text, s
    assert not re.search(r"rsync[^\n]*\s\.\s", text)  # the code never leaves as the working tree
    sync = text.index("uv sync --frozen --no-dev")
    site = text.index(".venv/bin/python -m askmike.service site --out site/dist")
    assert sync < site < text.index("touch .complete")  # built as root by the release's own code, after uv sync


def test_push_sources_its_helper_before_anything_ships():
    lines = code_lines(push())
    cd = lines.index('cd "$(git rev-parse --show-toplevel)"')
    assert lines[cd + 1] == ". deploy/hostport.sh"
    assert sum(ln.strip() == ". deploy/hostport.sh" for ln in lines) == 1


def test_push_checks_env_file_bounds_curl_and_owns_files_on_the_box():
    text = push()
    assert "stat -c '%U %a' /etc/ask/env" in text and '"root 600"' in text
    assert first_code(text, "/etc/ask/env must exist") < first_code(text, "git archive")
    rsyncs = []
    for line in text.splitlines(keepends=True):
        if "curl " in line and not line.lstrip().startswith("#"):
            assert "--max-time 10" in line, line
        if line.lstrip().startswith("rsync "):
            assert "--chown" not in line, line
            rsyncs.append(text.index(line))
    chown = text.index('ssh "$ASK_HOST" "chown -R root:root \'$REL\' \'$BUN\'"')
    gate = text.index("/etc/ask/gate_config.json' < \"$GATE\"")
    site = text.index("-m askmike.service site --out site/dist")
    assert len(rsyncs) == 1 and max(rsyncs + [site]) < chown
    assert chown < gate  # the gate config is installed after validation (see the gate-config test)
    assert chown < first_code(text, "Embedder().embed_query") < first_code(text, "if ! validate_site") < gate
    assert "cd $REL &&" not in text


def test_push_ships_the_index_as_a_checked_bundle():
    text = push()
    for s in ('BUN="/srv/ask/bundles/$INDEX_SHA"', "rsync -a --partial build/index.db \"$ASK_HOST:$BUN/index.db.part\"",
              "sha256sum -c --quiet -", "mv index.db.part index.db",
              "ssh \"$ASK_HOST\" \"ln -sfn '$BUN/index.db' '$REL/index.db'\""):
        assert s in text, s
    assert text.index("sha256sum -c") < text.index("ln -sfn '$BUN/index.db'") < text.index("current.next")


def test_push_warms_the_embedder_as_ask_before_the_swap():
    line = next(ln for ln in push().splitlines() if "Embedder().embed_query" in ln)
    assert "cd '$REL'" in line and "$LOAD_ENV" in line and "$AS_ASK .venv/bin/python" in line
    assert 'AS_ASK="sudo -E -u ask env HOME=/var/lib/ask"' in push()


def caps_check(tmp_path, facts: dict, box: str) -> subprocess.CompletedProcess:
    """push.sh's cap comparison as push.sh runs it: in the repo root, with the box's two values as one argument."""
    code = re.search(r"<<'CAPS'\n(.*?)\nCAPS\n", push(), re.S).group(1)
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    return subprocess.run([sys.executable, "-", box], input=code, text=True, capture_output=True, cwd=tmp_path)


def fact(v):
    return {"value": v, "display": f"${v:.2f}", "cmd": "x", "inputs": {}}


def test_push_refuses_caps_that_differ_from_the_published_ones(tmp_path):
    text = push()
    assert 'BOX_CAPS="$(ssh "$ASK_HOST" "$LOAD_ENV; printf' in text and 'caps_match "$BOX_CAPS"' in text
    assert text.index("/etc/ask/env must exist") < text.index("BOX_CAPS=") < text.index("git archive")
    pub = {"m3_model_cap_usd": fact(15.0), "m3_day_cap_usd": fact(1.5)}
    assert caps_check(tmp_path, pub, "15|1.5").returncode == 0
    assert caps_check(tmp_path, pub, "15.0000000001|1.50").returncode == 0  # within 1e-6
    got = caps_check(tmp_path, pub, "20|1.5")
    assert got.returncode == 1 and "refusing to deploy" in got.stderr
    assert "ASK_MONTH_CAP_USD=20" in got.stderr and "m3_model_cap_usd=15.0" in got.stderr
    assert "ASK_DAY_CAP_USD" not in got.stderr
    got = caps_check(tmp_path, pub, "15|")
    assert got.returncode == 1 and "/etc/ask/env does not set ASK_DAY_CAP_USD" in got.stderr
    got = caps_check(tmp_path, {"m3_day_cap_usd": fact(1.5)}, "15|1.5")
    assert got.returncode == 1 and "facts.json has no m3_model_cap_usd" in got.stderr
    got = caps_check(tmp_path, pub, "fifteen|1.5")
    assert got.returncode == 1 and "ASK_MONTH_CAP_USD" in got.stderr and "not a number" in got.stderr


def env_block() -> str:
    """push.sh from the env file's owner check to the end of the cap check."""
    text = push()
    start = text.index("# The env file holds the API key")
    return text[start:text.index("\nfi\n", text.index('if ! caps_match "$BOX_CAPS"; then')) + len("\nfi\n")]


def run_env_block(tmp_path, env_text: str | None):
    """env_block under a fake ssh: `stat` reports root 600 and every other remote command runs locally, with
    /etc/ask/env pointed at a file holding env_text (None: no file). -> (the run, every remote command)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    envf, log = tmp_path / "env", tmp_path / "ssh.log"
    if env_text is not None:
        envf.write_text(env_text)
    (tmp_path / "facts.json").write_text(json.dumps({"m3_model_cap_usd": fact(15.0), "m3_day_cap_usd": fact(1.5)}))
    load = next(ln for ln in push().splitlines() if ln.startswith("LOAD_ENV="))
    fake = (f"set -euo pipefail\nASK_HOST=box\n{load}\n"
            f"ssh() {{\n  printf '%s\\0' \"$2\" >> '{log}'\n"
            f"  case \"$2\" in\n    stat*) echo 'root 600' ;;\n"
            f"    *) bash -c \"${{2//\\/etc\\/ask\\/env/{envf}}}\" ;;\n  esac\n}}\n" + fake_uv())
    got = subprocess.run(["bash", "-c", fake + env_block() + "echo ENV OK\n"], capture_output=True, text=True,
                         cwd=tmp_path)
    return got, [c for c in log.read_text().split("\0") if c] if log.exists() else []


GOOD_ENV = ("# Ask Mike's env, written by Mike\nANTHROPIC_API_KEY=sk-ant-CANARY-good\n\nASK_MONTH_CAP_USD=15\n"
            "ASK_DAY_CAP_USD=1.5\nASK_STATE=/var/lib/ask\n")
BAD_ENV = ("# Ask Mike's env\nANTHROPIC_API_KEY= sk-ant-CANARY\nASK_MONTH_CAP_USD=15\n ASK_DAY_CAP_USD=1.5\n\n"
           "ASK_STATE = /var/lib/ask\n")


def test_push_refuses_an_env_file_that_sourcing_would_run(tmp_path):
    """Review fix round 1. `KEY= sk-ant-...` makes bash run the key as a command when the file is sourced, and
    "command not found" would print it to the operator's terminal. Push refuses such a file before it is ever
    sourced, naming line numbers only; and every source discards its stderr."""
    text = push()
    assert 'LOAD_ENV="set -a; . /etc/ask/env 2>/dev/null; set +a"' in text
    assert first_code(text, "/etc/ask/env must exist") < first_code(text, "ENV_LINES=") < first_code(text, "$LOAD_ENV;")

    got, calls = run_env_block(tmp_path / "bad", BAD_ENV)
    assert got.returncode == 1 and "ENV OK" not in got.stdout
    assert "refusing to deploy: /etc/ask/env line(s) 2 4 6 are not NAME=value" in got.stderr
    assert "CANARY" not in got.stdout + got.stderr
    assert not any("set -a" in c for c in calls)  # the file was never sourced

    got, calls = run_env_block(tmp_path / "good", GOOD_ENV)
    assert got.returncode == 0 and got.stdout.endswith("ENV OK\n"), got.stderr
    assert "CANARY" not in got.stdout + got.stderr and any("set -a" in c for c in calls)

    got, _ = run_env_block(tmp_path / "none", None)
    assert got.returncode == 1 and "could not check the lines of /etc/ask/env" in got.stderr

    # the threat is real, and the discarded stderr is what stops it: sourced bare, the bad file prints the key
    envf = tmp_path / "bad" / "env"
    bare = subprocess.run(["bash", "-c", f"set -a; . '{envf}'; set +a"], capture_output=True, text=True)
    assert "sk-ant-CANARY" in bare.stderr
    quiet = subprocess.run(["bash", "-c", f"set -a; . '{envf}' 2>/dev/null; set +a; echo sourced"],
                           capture_output=True, text=True)
    assert "CANARY" not in quiet.stdout + quiet.stderr and quiet.stdout == "sourced\n"


def config_check(tmp_path, facts: dict, box: str, prefix="refusing to switch") -> subprocess.CompletedProcess:
    """push.sh's config-check comparison as push.sh runs it: in the repo root, with the box's output and a prefix."""
    code = re.search(r"<<'CONFIG'\n(.*?)\nCONFIG\n", push(), re.S).group(1)
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    return subprocess.run([sys.executable, "-", box, prefix], input=code, text=True, capture_output=True,
                          cwd=tmp_path)


def test_push_checks_the_new_release_config_before_the_swap(tmp_path):
    """config-check runs on the box as ask, from the new release, with /etc/ask/env loaded, before `current` moves.
    Its models, effort, max_tokens and template_sha must be this commit's settings, and its caps the m3 facts."""
    text = push()
    line = next(ln for ln in text.splitlines() if "-m askmike.service config-check" in ln)
    assert "cd '$REL'" in line and "$LOAD_ENV" in line and "$AS_ASK .venv/bin/python" in line
    swap = text.index("current.next")
    assert text.index("uv sync --frozen") < first_code(text, "-m askmike.service config-check") < swap
    assert text.index('config_match "$NEW_CFG" "refusing to switch"') < swap

    s = load_answer_settings()
    pub = {"m3_model_cap_usd": fact(15.0), "m3_day_cap_usd": fact(1.5)}
    ok = {"models": s.model, "effort": s.effort, "max_tokens": s.max_tokens, "template_sha": s.template_sha,
          "month_cap_usd": 15.0, "day_cap_usd": 1.5}
    assert config_check(tmp_path, pub, json.dumps(ok)).returncode == 0
    assert config_check(tmp_path, pub, "uv: a warning\n" + json.dumps(ok)).returncode == 0  # the last line is read
    got = config_check(tmp_path, pub, json.dumps({**ok, "models": {"question": "x", "jd": "y"}, "max_tokens": 1}))
    assert got.returncode == 1 and "refusing to switch: " in got.stderr
    assert "models=" in got.stderr and "max_tokens=1" in got.stderr and "effort" not in got.stderr
    got = config_check(tmp_path, pub, json.dumps({**ok, "month_cap_usd": 20.0}))
    assert got.returncode == 1 and "month_cap_usd=20.0" in got.stderr and "m3_model_cap_usd=15.0" in got.stderr
    got = config_check(tmp_path, pub, json.dumps({k: v for k, v in ok.items() if k != "day_cap_usd"}))
    assert got.returncode == 1 and "day_cap_usd=None" in got.stderr
    got = config_check(tmp_path, {}, json.dumps(ok))
    assert got.returncode == 1 and "facts.json has no m3_model_cap_usd" in got.stderr
    for raw in ("", "not json", "[]"):  # a config-check that failed on the box prints nothing: refused
        got = config_check(tmp_path, pub, raw)
        assert got.returncode == 1 and "refusing to switch: could not read" in got.stderr, raw


# ---- push.sh: the gate config ----------------------------------------------------------------------------------------

def test_push_refuses_a_stale_gate_config(tmp_path):
    priv = tmp_path / "private"
    (priv / "deploy").mkdir(parents=True)
    code = block(push(), 'PRIV="$(', "bundle_stale", )
    script = f"set -euo pipefail\nexport ASK_PRIVATE_DIR='{priv}'\n" + fake_uv() + code + "echo FRESH\n"
    got = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert got.returncode == 1 and "FRESH" not in got.stdout
    assert "refusing to deploy: the gate config" in got.stderr and "gate-config" in got.stderr
    (priv / "deploy" / "gate_config.json").write_text(json.dumps({"built_from": built_from(priv)}))
    got = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert got.returncode == 0 and got.stdout.endswith("FRESH\n"), got.stderr


def test_gate_config_installed_root_ask_640_and_never_echoed(tmp_path):
    """The bundle goes over ssh's stdin to a temp file on the box, is installed root:ask 640 as
    /etc/ask/gate_config.json, and the temp file is removed. No command line and no output holds its content."""
    text = push()
    assert 'GATE="$PRIV/deploy/gate_config.json"' in code_lines(text)
    gate_lines = [ln for ln in code_lines(text) if "$GATE" in ln]
    assert len(gate_lines) == 2  # checked for staleness, then installed
    assert not any(re.search(r"\b(cat|echo|printf|tee|rsync|scp)\b[^|<]*\$GATE", ln) for ln in gate_lines)
    line = next(ln for ln in gate_lines if "/etc/ask/gate_config.json" in ln)
    assert "install -o root -g ask -m 640" in line and line.rstrip().endswith('< "$GATE"') and "$REL" not in line
    assert first_code(text, "bundle_stale") < first_code(text, 'ssh "$ASK_HOST"')
    # Installed only after the validation if/fi block, before the swap: a refused push never touches /etc/ask.
    validation = block(text, "if ! validate_site", "the live release is untouched")
    gate_at = text.index("/etc/ask/gate_config.json' < \"$GATE\"")
    assert text.index(validation) + len(validation) <= gate_at < first_code(text, 'PREV="$(ssh')
    assert text.count("trap ") >= 1 and "/etc/ask/gate_config.json" not in validation

    canary = "zzq-invented-gate-pattern-41"
    gate = tmp_path / "gate_config.json"
    gate.write_text(json.dumps({"patterns": [canary]}))
    remote_tmp, bin_ = tmp_path / "remote-tmp", tmp_path / "bin"
    remote_tmp.mkdir()
    bin_.mkdir()
    fake_mktemp(bin_, remote_tmp)
    args, got_copy, cmd = tmp_path / "install.args", tmp_path / "installed", tmp_path / "ssh.cmd"
    (bin_ / "install").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > \'{args}\'\ncp "${{@: -2:1}}" \'{got_copy}\'\n')
    (bin_ / "install").chmod(0o755)
    script = (f"set -euo pipefail\nASK_HOST=box GATE='{gate}'\nexport PATH='{bin_}':$PATH\n"
              f"ssh() {{ printf '%s' \"$2\" > '{cmd}'; bash -c \"$2\"; }}\n{line}\necho INSTALLED\n")
    got = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert got.returncode == 0 and got.stdout == "INSTALLED\n", got.stderr
    assert got_copy.read_text() == gate.read_text()  # the content arrived through stdin
    a = args.read_text().splitlines()
    assert a[:6] == ["-o", "root", "-g", "ask", "-m", "640"] and a[7:] == ["/etc/ask/gate_config.json"]
    assert Path(a[6]).parent == remote_tmp
    assert list(remote_tmp.iterdir()) == []  # the temp copy is gone
    assert canary not in got.stdout + got.stderr + cmd.read_text()


# ---- push.sh: validate the combined Caddy config on the box, then swap -----------------------------------------------

BOX_BASE = ("# box base\n{{\n\tlog default {{\n\t\tformat filter {{\n\t\t\trequest>remote_ip delete\n"
            "\t\t}}\n\t}}\n}}\n\nimport {box}/sites/*.caddy\n")
PRE_SPLIT_BASE = "# box base\n{{\n}}\ndeals.forn.al {{\n}}\n"
COMMENTED_BASE = "# box base\n{{\n}}\n# import {box}/sites/*.caddy\n"


def validate_run(tmp_path, base: str | None = BOX_BASE, caddy_rc: int = 0, block_: bool = False, edit=None):
    """Run push.sh's validate_site (or, with block_, the whole validation `if` before the swap, then print SWAPPED) on
    a fake box. The fake ssh runs the remote script locally with /etc/caddy pointed at tmp_path/box. The fake caddy
    refuses (exit 97) unless the import line it is handed names that Caddyfile's own temp sites dir, then records the
    tree. -> (the run, what caddy saw or None, the box, the temp dir caddy saw or None, the box's temp dir)."""
    box = tmp_path / "box"
    (box / "sites").mkdir(parents=True)
    if base is not None:
        (box / "Caddyfile").write_text(base.format(box=box))
    (box / "sites" / "deals.caddy").write_text("deals.live {\n}\n")
    (box / "sites" / "ask.caddy").write_text("ask.old {\n}\n")
    rel = tmp_path / "rel" / "deploy"
    rel.mkdir(parents=True)
    (rel / "ask.caddy").write_text("ask.new {\n}\n")
    remote_tmp, bin_ = tmp_path / "remote-tmp", tmp_path / "bin"
    remote_tmp.mkdir()
    bin_.mkdir()
    fake_mktemp(bin_, remote_tmp)
    seen, tree = tmp_path / "seen.txt", tmp_path / "tree.txt"
    (bin_ / "caddy").write_text(
        f'#!/bin/bash\ncfg="${{@: -1}}"; d=$(dirname "$cfg"); echo "$d" > {tree}\n'
        f'grep -qxF "import $d/sites/*.caddy" "$cfg" || {{ echo "import not rewritten to $d/sites" >&2; exit 97; }}\n'
        f'{{ cat "$cfg"; for f in "$d"/sites/*; do echo "== $(basename $f)"; cat "$f"; done; }} > {seen}\n'
        f'exit {caddy_rc}\n')
    (bin_ / "caddy").chmod(0o755)
    text = push()
    start = text.index("validate_site() {")
    fn = text[start:text.index("\n}\n", start) + 3]
    if edit:
        fn = edit(fn)
    if block_:
        call = f"REL={tmp_path / 'rel'}\n" + block(text, "if ! validate_site", "if ! validate_site") + "echo SWAPPED\n"
    else:
        call = f"validate_site {rel}/ask.caddy ask\n"
    script = (f"set -euo pipefail\nASK_HOST=box\nPATH={bin_}:$PATH\n"
              f"ssh() {{ case \"$2\" in 'bash -s'*) sed 's#/etc/caddy#{box}#g' | eval \"$2\" ;; *) return 0 ;; esac; }}"
              "\n"
              f"{fn}\n{call}")
    got = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return (got, seen.read_text() if seen.exists() else None, box,
            Path(tree.read_text().strip()) if tree.exists() else None, remote_tmp)


def box_files(box: Path) -> dict:
    return {p.relative_to(box).as_posix(): p.read_bytes() for p in sorted(box.rglob("*")) if p.is_file()}


def test_push_validates_the_combined_config_before_the_swap(tmp_path):
    """The temp tree is the box's live base (its import line rewritten to the temp sites dir), every current site
    file except ask.caddy, and the candidate ask.caddy. The box is untouched and the temp tree is gone after."""
    text = push()
    swap = text.index("current.next")
    assert text.index('validate_site "$REL/deploy/ask.caddy" ask') < swap
    assert text.index("systemd-analyze verify") < swap
    start = text.index("validate_site() {")
    fn = text[start:text.index("\n}\n", start)]
    assert "set -eu\n" in fn and "trap 'rm -rf \"$T\"' EXIT" in fn and "base=/etc/caddy/Caddyfile" in fn

    got, seen, box, tree, remote_tmp = validate_run(tmp_path / "ok")
    before = box_files(box)
    assert got.returncode == 0, got.stderr
    assert seen.startswith("# box base\n") and f"import {tree}/sites/*.caddy" in seen
    assert "== deals.caddy\ndeals.live" in seen and "== ask.caddy\nask.new" in seen and "ask.old" not in seen
    assert box_files(box) == before and (box / "sites" / "ask.caddy").read_text() == "ask.old {\n}\n"
    assert not tree.exists() and list(remote_tmp.iterdir()) == []

    # a failing validate refuses: the push block exits 1 and never reaches the swap; the temp tree is gone too
    got, _, _, tree, remote_tmp = validate_run(tmp_path / "bad", caddy_rc=1, block_=True)
    assert got.returncode == 1 and "SWAPPED" not in got.stdout and "refusing to switch" in got.stderr
    assert not tree.exists() and list(remote_tmp.iterdir()) == []
    got, _, _, tree, _ = validate_run(tmp_path / "good", block_=True)
    assert got.returncode == 0 and "SWAPPED" in got.stdout and not tree.exists()


def test_validation_rewrites_the_import_line_rather_than_copying_the_base(tmp_path):
    """A `cp` of the base in place of the `sed` rewrite would validate the box's live sites, not the candidate: the
    fake caddy refuses it (exit 97), and so does push's own check of the rewritten line."""
    def cp_instead(fn: str, keep_check: bool) -> str:
        out = []
        for ln in fn.splitlines():
            if ln.startswith('sed "s#^import'):
                ln = 'cp "$base" "$T/Caddyfile"'
            if not keep_check and ln.startswith('grep -qxF "import $T/sites/'):
                continue
            out.append(ln)
        return "\n".join(out) + "\n"

    got, seen, _, tree, remote_tmp = validate_run(tmp_path / "caddy", edit=lambda fn: cp_instead(fn, False))
    assert got.returncode != 0 and "import not rewritten" in got.stderr and seen is None
    assert not tree.exists() and list(remote_tmp.iterdir()) == []
    got, seen, _, tree, remote_tmp = validate_run(tmp_path / "own", edit=lambda fn: cp_instead(fn, True))
    assert got.returncode != 0 and seen is None and tree is None and list(remote_tmp.iterdir()) == []


@pytest.mark.parametrize("base", [PRE_SPLIT_BASE, COMMENTED_BASE, None], ids=["pre-split", "commented", "missing"])
def test_push_refuses_when_the_box_lacks_the_sites_import(tmp_path, base):
    """Ask Mike never writes the base: before DTD's split is live there is nowhere for ask.caddy to go, so the push
    refuses before the swap, without running caddy or leaving a temp tree."""
    got, seen, box, tree, remote_tmp = validate_run(tmp_path / "fn", base=base)
    assert got.returncode != 0 and "does not import" in got.stderr and seen is None and tree is None
    assert list(remote_tmp.iterdir()) == []
    got, seen, box, _, remote_tmp = validate_run(tmp_path / "block", base=base, block_=True)
    assert got.returncode == 1 and "SWAPPED" not in got.stdout and "refusing to switch" in got.stderr
    assert seen is None and list(remote_tmp.iterdir()) == []
    assert (box / "sites" / "ask.caddy").read_text() == "ask.old {\n}\n"


def swap_block() -> str:
    """push.sh from reading the previous release to the end of the health poll."""
    return block(push(), 'PREV="$(ssh', 'revert "release $SHA never reported healthy"')


HEALTHY = {"ok": True, "git_sha": "new", "corpus_sha": "c", "index_sha": "i", "template_sha": "t", "budget": "ok"}


def run_swap(tmp_path, prev: str, fail_on: str = "never", health: dict | None = None):
    """Run swap_block under a fake ssh: the read of /srv/ask/current prints `prev` (empty: no symlink, so NONE; "SSHFAIL": ssh itself fails), `hostname -I`
    prints the box's addresses, and any other remote command containing `fail_on` fails. curl prints `health`.
    -> (the run, every remote command in order, every curl's arguments)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log, curls = tmp_path / "ssh.log", tmp_path / "curl.log"
    reply = json.dumps(health if health is not None else {})
    fake = (f"set -euo pipefail\nASK_HOST=box SHA=new REL=/srv/ask/releases/new BASE=https://ask.forn.al\n"
            f"CORPUS_SHA=c INDEX_SHA=i TEMPLATE_SHA=t\n. '{HOSTPORT}'\n"
            f"ssh() {{\n  printf '%s\\0' \"$2\" >> '{log}'\n"
            f"  case \"$2\" in\n    'if [ -L /srv/ask/current ]'*) case '{prev}' in SSHFAIL) return 255 ;; '') echo NONE ;; *) echo '{prev}' ;; esac ;;\n"
            f"    'hostname -I') echo '203.0.113.5 2001:db8::5' ;;\n"
            f"    *'{fail_on}'*) return 1 ;;\n  esac\n}}\n" + fake_uv() +
            f"curl() {{ printf '%s\\n' \"$*\" >> '{curls}'; printf '%s' '{reply}'; }}\n"
            "sleep() { :; }\nseq() { command seq 3; }\n")  # the poll's length is pinned statically below
    got = subprocess.run(["bash", "-c", fake + swap_block() + "echo LIVE\n"], capture_output=True, text=True)
    calls = [c for c in log.read_text().split("\0") if c] if log.exists() else []
    return got, calls, curls.read_text().splitlines() if curls.exists() else []


def test_push_reverts_an_unhealthy_release(tmp_path):
    text = push()
    assert "for _ in $(seq 60)" in text and "sleep 2" in text and text.count('install_release "$PREV"') == 1
    assert 'if ! install_release "$REL"; then' in text and text.count('install_release "$REL"') == 1

    got, calls, curls = run_swap(tmp_path / "sick", "/srv/ask/releases/old", health={**HEALTHY, "index_sha": "x"})
    assert got.returncode == 1 and "LIVE" not in got.stdout
    assert [c.split()[0] for c in calls] == ["if", "ln", "hostname", "ln"]
    assert "ln -sfn '/srv/ask/releases/new' /srv/ask/current.next" in calls[1]
    assert "ln -sfn '/srv/ask/releases/old' /srv/ask/current.next" in calls[3]
    assert "install -m 644 '/srv/ask/releases/old/deploy/ask.caddy' /etc/caddy/sites/ask.caddy" in calls[3]
    assert "release new never reported healthy; reverting to the previous release old" in got.stderr
    assert "reverted from new to old" in got.stderr
    assert len(curls) == 3
    for c in curls:
        assert c == "-fsS --max-time 10 --resolve ask.forn.al:443:203.0.113.5 https://ask.forn.al/health", c

    got, calls, _ = run_swap(tmp_path / "fine", "/srv/ask/releases/old", health=HEALTHY)
    assert got.returncode == 0 and got.stdout == "LIVE\n" and len(calls) == 3, got.stderr

    got, calls, _ = run_swap(tmp_path / "first", "", health={})
    assert got.returncode == 1 and len(calls) == 4  # nothing to revert to: no second install, the cleanup instead
    assert calls[3].startswith("systemctl disable --now ask;")
    assert "never reported healthy, and there is no previous release to revert to" in got.stderr
    assert "the first release new was removed; the push failed" in got.stderr


def test_push_refuses_when_current_cannot_be_read(tmp_path):
    """An ssh failure reading /srv/ask/current is not "no symlink": the push refuses before the swap, so no install,
    no uninstall, and the live release keeps serving."""
    got, calls, curls = run_swap(tmp_path / "x", "SSHFAIL", health={})
    assert got.returncode == 1 and "LIVE" not in got.stdout
    assert "refusing to switch: could not read /srv/ask/current" in got.stderr
    assert len(calls) == 1 and calls[0].startswith("if [ -L /srv/ask/current ]") and curls == []
    assert not any("ln -sfn" in c or "disable --now" in c for c in calls)


def test_a_missing_current_is_the_first_push_and_an_existing_one_is_prev(tmp_path):
    got, calls, _ = run_swap(tmp_path / "none", "", health={})
    assert got.returncode == 1 and "no previous release to revert to" in got.stderr
    assert calls[-1].startswith("systemctl disable --now ask;")  # first-push cleanup, as before
    got, calls, _ = run_swap(tmp_path / "some", "/srv/ask/releases/old", health={**HEALTHY, "index_sha": "x"})
    assert "reverting to the previous release old" in got.stderr and "disable --now" not in calls[-1]


def test_a_failed_install_reverts_to_the_previous_release(tmp_path):
    """Any step of the install can fail after `current` moved: the previous release is swapped back, its site file
    and unit reinstalled and the service restarted, and the push fails naming both."""
    got, calls, _ = run_swap(tmp_path / "a", "/srv/ask/releases/old", fail_on="/srv/ask/releases/new")
    assert got.returncode == 1 and "LIVE" not in got.stdout and len(calls) == 3
    assert "systemctl reload caddy" in calls[2] and "systemctl restart ask" in calls[2]
    assert "installing release new failed; reverting to the previous release old" in got.stderr

    got, calls, _ = run_swap(tmp_path / "b", "", fail_on="/srv/ask/releases/new")
    assert got.returncode == 1 and len(calls) == 3 and calls[2].startswith("systemctl disable --now ask;")
    assert "no previous release to revert to" in got.stderr

    got, calls, _ = run_swap(tmp_path / "d", "", fail_on="systemctl")
    assert got.returncode == 1 and "the cleanup itself failed" in got.stderr

    got, _, _ = run_swap(tmp_path / "c", "/srv/ask/releases/old", fail_on="/srv/ask/releases/")
    assert got.returncode == 1 and "the revert itself failed" in got.stderr


def test_push_never_writes_the_base_caddyfile_or_another_site(tmp_path):
    """Review Focus 1. Statically: the only Caddy file push installs is /etc/caddy/sites/ask.caddy, the base is only
    read (to validate), and provision only reads it too. Then the install command itself runs on a fake box holding
    a base and DTD's site file: both come out byte for byte as they went in."""
    text = push()
    targets = re.findall(r"install -m 644 \S+ (\S+)", text)
    assert targets == ["/etc/caddy/sites/ask.caddy", "/etc/systemd/system/ask.service"]
    assert set(re.findall(r"/etc/caddy/sites/[\w.*-]+", text)) == {"/etc/caddy/sites/ask.caddy",
                                                                    "/etc/caddy/sites/*.caddy"}
    assert not re.search(r"/etc/caddy/(?:sites/)?[\w.*-]*\$", text)  # no Caddy path built from a variable
    for script, reads in ((PUSH, ["base=/etc/caddy/Caddyfile"]),
                          (PROVISION, ["if [ ! -d /etc/caddy/sites ] || ! grep -qxF 'import /etc/caddy/sites/*.caddy' "
                                       "/etc/caddy/Caddyfile; then"])):
        lines = code_lines(script.read_text(encoding="utf-8"))
        assert [ln.strip() for ln in lines if "/etc/caddy/Caddyfile" in ln] == reads, script.name
        for ln in lines:
            assert not re.search(r"\bsed -i\b|\btee\b|>\s*/etc/caddy", ln), ln
            if re.search(r"\b(cp|mv|install|ln|rm)\b", ln) and "/etc/caddy" in ln:
                assert "/etc/caddy/sites/ask.caddy" in ln or "$T/sites/" in ln, ln
    removes = [[x for x in r.split() if x != "\\"] for r in re.findall(r"\brm -f ([^;&|\"]+)", text) if "/etc/" in r]
    assert removes == [["/etc/caddy/sites/ask.caddy", "/etc/systemd/system/ask.service", "/srv/ask/current"]]
    base_uses = [ln.strip() for ln in code_lines(text) if '"$base"' in ln]
    assert base_uses == ['if ! grep -qxF "$import_line" "$base"; then',
                         'sed "s#^import /etc/caddy/sites/\\*\\.caddy\\$#import $T/sites/*.caddy#" "$base" > '
                         '"$T/Caddyfile"']
    for s in ("systemctl daemon-reload", "systemctl enable --now ask", "systemctl reload caddy",
              "systemctl restart ask", "systemctl disable --now ask"):
        assert s in text, s
    assert not re.search(r"systemctl (restart|stop|start|enable --now) caddy", text)

    _, calls, _ = run_swap(tmp_path / "s", "/srv/ask/releases/old", health=HEALTHY)
    root, sysd = fake_install_box(tmp_path)
    before = box_files(root / "etc" / "caddy")
    run = run_on_box(calls[1], root, tmp_path / "bin")
    assert run.returncode == 0, run.stderr
    after = box_files(root / "etc" / "caddy")
    assert sorted(after) == ["Caddyfile", "sites/ask.caddy", "sites/deals.caddy"]
    assert after["Caddyfile"] == before["Caddyfile"] and after["sites/deals.caddy"] == before["sites/deals.caddy"]
    assert after["sites/ask.caddy"] == b"ask.new {\n}\n"
    assert (root / "etc" / "systemd" / "system" / "ask.service").read_text() == "[Unit]\n"
    assert (root / "srv" / "ask" / "current").resolve() == (root / "srv" / "ask" / "releases" / "new").resolve()
    assert sysd.read_text().splitlines() == ["daemon-reload", "enable --now ask", "reload caddy", "restart ask"]


def fake_install_box(tmp_path) -> tuple[Path, Path]:
    """A box for install_release's and the cleanup's commands: the base Caddyfile, DTD's site file and an old
    ask.caddy, the new release's site file and unit, `current` on the old release, and tmp_path/bin holding a
    systemctl that records its arguments and an `mv -T` (GNU only) for macOS. -> (the box root, systemctl's log)."""
    root = tmp_path / "box"
    (root / "etc" / "caddy" / "sites").mkdir(parents=True)
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    (root / "etc" / "caddy" / "Caddyfile").write_text("{\n}\nimport /etc/caddy/sites/*.caddy\n")
    (root / "etc" / "caddy" / "sites" / "deals.caddy").write_text("deals.forn.al {\n}\n")
    (root / "etc" / "caddy" / "sites" / "ask.caddy").write_text("ask.old {\n}\n")
    rel = root / "srv" / "ask" / "releases" / "new" / "deploy"
    rel.mkdir(parents=True)
    (rel / "ask.caddy").write_text("ask.new {\n}\n")
    (rel / "ask.service").write_text("[Unit]\n")
    (root / "srv" / "ask" / "releases" / "old").mkdir()
    (root / "srv" / "ask" / "current").symlink_to(root / "srv" / "ask" / "releases" / "old")
    bin_, sysd = tmp_path / "bin", tmp_path / "systemctl.log"
    bin_.mkdir(exist_ok=True)
    (bin_ / "systemctl").write_text(f'#!/bin/bash\necho "$*" >> \'{sysd}\'\n')
    (bin_ / "mv").write_text('#!/bin/bash\nif [ "$1" = -T ]; then shift; rm -f "$2"; fi\nexec /bin/mv "$@"\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    return root, sysd


def run_on_box(cmd: str, root: Path, bin_: Path) -> subprocess.CompletedProcess:
    """A remote command run locally with /srv/ask and /etc pointed into the fake box."""
    local = cmd.replace("/srv/ask", f"{root}/srv/ask").replace("/etc/", f"{root}/etc/")
    return subprocess.run(["bash", "-c", local], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"})


def test_a_failed_first_push_leaves_nothing_installed_or_running(tmp_path):
    """With no previous release there is nothing to revert to: the failed first release is stopped and disabled
    (never left restarting), its site file, unit and `current` are removed, and Caddy is reloaded, which restores the
    config that was live before. DTD's base and site file are untouched."""
    for case, kw in (("sick", {"health": {}}), ("broken", {"fail_on": "/srv/ask/releases/new"})):
        got, calls, _ = run_swap(tmp_path / case / "s", "", **kw)
        assert got.returncode == 1 and "LIVE" not in got.stdout
        cleanup = calls[-1]
        root, sysd = fake_install_box(tmp_path / case)
        (root / "etc" / "systemd" / "system" / "ask.service").write_text("[Unit]\n")  # as the failed install left it
        before = box_files(root / "etc" / "caddy")
        run = run_on_box(cleanup, root, tmp_path / case / "bin")
        assert run.returncode == 0, run.stderr
        after = box_files(root / "etc" / "caddy")
        assert sorted(after) == ["Caddyfile", "sites/deals.caddy"]  # no ask.caddy left
        assert after["Caddyfile"] == before["Caddyfile"] and after["sites/deals.caddy"] == before["sites/deals.caddy"]
        assert not (root / "etc" / "systemd" / "system" / "ask.service").exists()
        assert not (root / "srv" / "ask" / "current").is_symlink()
        assert (root / "srv" / "ask" / "releases" / "new").is_dir()  # the release itself stays, for a look
        assert sysd.read_text().splitlines() == ["disable --now ask", "daemon-reload", "reload caddy"]


@pytest.mark.parametrize("base,want", [
    ("https://ask.forn.al", "ask.forn.al:443"),
    ("https://ask.forn.al:8443/x", "ask.forn.al:8443"),
    ("http://example.test", "example.test:80"),
])
def test_host_port_derivation(base, want):
    out = subprocess.run(["bash", "-c", '. deploy/hostport.sh; host_port "$1"', "_", base], check=True,
                         capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == want
    first = HOSTPORT.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# Copied from deal-terms-desk deploy/hostport.sh")


def test_push_prunes_only_after_a_good_deploy():
    text = push()
    prune = text.index("$REL/deploy/prune.sh")
    assert prune > text.index('echo "deployed $SHA"') > text.index("never reported healthy")
    assert "|| echo" in text[prune:text.index("\n", prune)]  # a failed prune leaves the deploy standing


@pytest.mark.parametrize("prev", ["/srv/ask/releases/old", ""])
def test_push_tells_prune_which_release_was_live_before(tmp_path, prev):
    text = push()
    start = text.rindex("\n", 0, text.index("$REL/deploy/prune.sh")) + 1
    line = text[start:text.index("\n", start)]
    log = tmp_path / "ssh.log"
    fake = (f"set -euo pipefail\nASK_HOST=box REL=/srv/ask/releases/new PREV='{prev}'\n"
            f"ssh() {{ printf '%s' \"$2\" > '{log}'; }}\n")
    got = subprocess.run(["bash", "-c", fake + line + "\n"], capture_output=True, text=True)
    assert got.returncode == 0, got.stderr
    assert log.read_text() == f"bash '/srv/ask/releases/new/deploy/prune.sh' /srv/ask '{prev}'"


# ---- provision.sh ----------------------------------------------------------------------------------------------------

TOOLS = ("caddy", "uv", "rsync", "sqlite3", "sudo", "systemd-analyze")


def run_provision(tmp_path, base: str | None = "{{\n}}\nimport {root}/etc/caddy/sites/*.caddy\n", sites=True,
                  missing: str = ""):
    """provision.sh against a fake root: /etc, /srv and /var/lib point into tmp_path/root, and id, useradd, install
    and the tools it checks for are stubs that record their arguments. -> (the run, useradd's log, install's log)."""
    root = tmp_path / "root"
    (root / "etc" / "caddy").mkdir(parents=True, exist_ok=True)
    if sites:
        (root / "etc" / "caddy" / "sites").mkdir(exist_ok=True)
    if base is not None:
        (root / "etc" / "caddy" / "Caddyfile").write_text(base.format(root=root))
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    users, installs = tmp_path / "useradd.log", tmp_path / "install.log"
    stubs = {"id": f'#!/bin/bash\n[ -e \'{tmp_path}/user-ask\' ]\n',
             "useradd": f'#!/bin/bash\necho "$*" >> \'{users}\'\ntouch \'{tmp_path}/user-ask\'\n',
             "install": (f'#!/bin/bash\necho "$*" >> \'{installs}\'\nargs=()\nwhile [ $# -gt 0 ]; do\n'
                         '  case "$1" in -d) shift ;; -o|-g|-m) shift 2 ;; *) args+=("$1"); shift ;; esac\n'
                         'done\nmkdir -p "${args[@]}"\n')}
    stubs.update({t: "#!/bin/bash\nexit 0\n" for t in TOOLS if t != missing})
    for name, body in stubs.items():
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    script = (PROVISION.read_text(encoding="utf-8").replace("/etc/", f"{root}/etc/").replace("/srv/", f"{root}/srv/")
              .replace("/var/lib/", f"{root}/var/lib/"))
    got = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True,
                         env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path)})
    read = lambda p: p.read_text().splitlines() if p.exists() else []  # noqa: E731
    return got, read(users), read(installs), root


def test_provision_is_additive(tmp_path):
    text = PROVISION.read_text(encoding="utf-8")
    for banned in ("sshd_config", "journald", "ufw", "apt-get", "10-dtd", "/etc/ssh", "systemctl"):
        assert banned not in text, banned
    assert "set -euo pipefail" in text
    assert "useradd --system --home-dir /var/lib/ask --shell /usr/sbin/nologin ask" in text
    assert text.index("import /etc/caddy/sites/*.caddy") < text.index("useradd")

    got, users, installs, root = run_provision(tmp_path)
    assert got.returncode == 0, got.stderr
    assert users == [f"--system --home-dir {root}/var/lib/ask --shell /usr/sbin/nologin ask"]
    assert installs == [f"-d -o root -g root -m 755 {root}/srv/ask {root}/srv/ask/releases {root}/srv/ask/bundles",
                        f"-d -o ask -g ask -m 750 {root}/var/lib/ask {root}/var/lib/ask/fastembed "
                        f"{root}/var/lib/ask/huggingface",
                        f"-d -o root -g ask -m 750 {root}/etc/ask"]
    # idempotent: a second run adds no user and sets the same directories again
    got, users, installs2, _ = run_provision(tmp_path)
    assert got.returncode == 0 and len(users) == 1 and installs2 == installs * 2
    # it touched nothing of Caddy's: the base and the sites dir are as they were
    assert sorted(p.name for p in (root / "etc").iterdir()) == ["ask", "caddy"]
    assert list((root / "etc" / "caddy" / "sites").iterdir()) == []


@pytest.mark.parametrize("case", ["no-import", "no-sites-dir", "no-base", "no-caddy"])
def test_provision_refuses_before_dtds_split_or_without_its_tools(tmp_path, case):
    kw = {"no-import": {"base": "{{\n}}\ndeals.forn.al {{\n}}\n"}, "no-sites-dir": {"sites": False},
          "no-base": {"base": None}, "no-caddy": {"missing": "caddy"}}[case]
    got, users, installs, _ = run_provision(tmp_path, **kw)
    assert got.returncode == 1 and "refusing to provision" in got.stderr
    assert users == [] and installs == []


# ---- prune.sh (DTD's tests, retargeted to /srv/ask and index.db) -----------------------------------------------------

def test_prune_names_where_it_came_from():
    text = PRUNE.read_text(encoding="utf-8")
    assert text.splitlines()[1].startswith("# Copied from deal-terms-desk deploy/prune.sh")
    assert 'TREE="$(cd "${1:-/srv/ask}" && pwd -P)"' in text and "live.db" not in text


def prune_tree(tmp_path) -> Path:
    """Releases r1..r4 completed in that order, each linked to its own bundle, plus a partial upload and an orphan
    bundle. `current` is r3, not the newest, as after a rollback."""
    root = tmp_path / "srv"
    for b in ("b0", "b1", "b2", "b3", "b9"):
        (root / "bundles" / b).mkdir(parents=True)
        (root / "bundles" / b / "index.db").write_text(b)
    for i, (r, b) in enumerate((("r1", "b0"), ("r2", "b1"), ("r3", "b2"), ("r4", "b3"))):
        rel = root / "releases" / r
        rel.mkdir(parents=True)
        (rel / "index.db").symlink_to(root / "bundles" / b / "index.db")
        (rel / ".complete").touch()
        os.utime(rel / ".complete", (1_000_000 + i, 1_000_000 + i))
    (root / "releases" / "partial").mkdir()
    (root / "current").symlink_to(root / "releases" / "r3")
    return root


def run_prune(root: Path, *prev) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(PRUNE), str(root), *map(str, prev)], capture_output=True, text=True)


def assert_pruned_like_the_plain_tree(root: Path, got: subprocess.CompletedProcess) -> None:
    assert got.returncode == 0, got.stderr
    assert sorted(p.name for p in (root / "releases").iterdir()) == ["r2", "r3", "r4"]
    assert sorted(p.name for p in (root / "bundles").iterdir()) == ["b1", "b2", "b3"]
    assert (root / "current").resolve() == (root / "releases" / "r3").resolve()
    assert (root / "releases" / "r3" / "index.db").read_text() == "b2"  # the live release still reaches its bundle
    assert sorted(got.stdout.splitlines()) == ["removed bundle b0", "removed bundle b9",
                                               "removed release partial", "removed release r1"]


def test_prune_keeps_the_live_release_two_rollbacks_and_their_bundles(tmp_path):
    root = prune_tree(tmp_path)
    assert_pruned_like_the_plain_tree(root, run_prune(root))
    again = run_prune(root)
    assert again.returncode == 0 and again.stdout == ""


@pytest.mark.parametrize("linked", ["root", "releases", "bundles"])
def test_prune_through_symlinked_directories_keeps_the_same_releases_and_bundles(tmp_path, linked):
    root = prune_tree(tmp_path)
    called = root
    if linked == "root":
        called = tmp_path / "srv-link"
        called.symlink_to(root)
    else:
        away = tmp_path / "away" / linked
        away.parent.mkdir()
        (root / linked).rename(away)
        (root / linked).symlink_to(away)
    assert_pruned_like_the_plain_tree(root, run_prune(called))


def test_prune_refuses_when_current_does_not_resolve(tmp_path):
    root = prune_tree(tmp_path)
    (root / "current").unlink()
    (root / "current").symlink_to(root / "releases" / "gone")
    got = run_prune(root)
    assert got.returncode == 1 and "refusing to prune" in got.stderr
    assert len(list((root / "releases").iterdir())) == 5 and len(list((root / "bundles").iterdir())) == 5


def test_prune_refuses_when_current_is_not_a_release(tmp_path):
    root = prune_tree(tmp_path)
    (tmp_path / "stray").mkdir()
    (root / "current").unlink()
    (root / "current").symlink_to(tmp_path / "stray")
    got = run_prune(root)
    assert got.returncode == 1 and "refusing to prune" in got.stderr and got.stdout == ""
    assert len(list((root / "releases").iterdir())) == 5 and len(list((root / "bundles").iterdir())) == 5


def test_prune_refuses_before_deleting_when_a_kept_release_links_outside_bundles(tmp_path):
    root = prune_tree(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "index.db").write_text("x")
    (root / "releases" / "r4" / "index.db").unlink()
    (root / "releases" / "r4" / "index.db").symlink_to(tmp_path / "elsewhere" / "index.db")
    got = run_prune(root)
    assert got.returncode == 1 and "refusing to prune" in got.stderr and got.stdout == ""
    assert len(list((root / "releases").iterdir())) == 5 and len(list((root / "bundles").iterdir())) == 5


def releases_tree(tmp_path, names: str, current: str) -> Path:
    root = tmp_path / "srv"
    for i, r in enumerate(names):
        (root / "bundles" / f"b{r}").mkdir(parents=True)
        (root / "bundles" / f"b{r}" / "index.db").write_text(r)
        rel = root / "releases" / r
        rel.mkdir(parents=True)
        (rel / "index.db").symlink_to(root / "bundles" / f"b{r}" / "index.db")
        (rel / ".complete").touch()
        os.utime(rel / ".complete", (1_000_000 + i, 1_000_000 + i))
    (root / "current").symlink_to(root / "releases" / current)
    return root


def test_prune_keeps_the_release_that_was_live_before_current(tmp_path):
    root = releases_tree(tmp_path, "ABCDE", current="E")
    got = run_prune(root, root / "releases" / "B")
    assert got.returncode == 0, got.stderr
    assert sorted(p.name for p in (root / "releases").iterdir()) == ["B", "D", "E"]
    assert sorted(p.name for p in (root / "bundles").iterdir()) == ["bB", "bD", "bE"]


@pytest.mark.parametrize("prev", ["", "outside", "missing", "current"])
def test_prune_ignores_an_empty_or_unusable_previous_release(tmp_path, prev):
    root = releases_tree(tmp_path, "ABCDE", current="E")
    (tmp_path / "outside").mkdir()
    arg = {"": "", "outside": tmp_path / "outside", "missing": root / "releases" / "gone",
           "current": root / "current"}[prev]
    got = run_prune(root, arg)
    assert got.returncode == 0, got.stderr
    assert sorted(p.name for p in (root / "releases").iterdir()) == ["C", "D", "E"]
    assert (tmp_path / "outside").is_dir()


# ---- smoke.py against a fake box -------------------------------------------------------------------------------------

UNITS = pages.load_units(REPO / "corpus" / "units.jsonl")
FB = load_fallback()
QUESTION = "Has Mike built an invented queue for nightly jobs?"
VISIT = "AbCdEfGhIjKlMnOpQrStUv"
CLAIM = Claim("direct", "", "", "Mike wrote an invented importer for the depot.", (FB["units"][0],))
FACTS = json.loads((REPO / "facts.json").read_text(encoding="utf-8"))
SHAS = {"git_sha": "g" * 40, "corpus_sha": "c" * 64, "index_sha": "i" * 64, "template_sha": ANSWER_TEMPLATE_SHA}


def out(outcome: str, kind: str, kept=(), reason: str = "") -> Outcome:
    return Outcome(outcome, "answered" if outcome == "answer" else "", tuple(kept), (), reason, "",
                   ANSWER_TEMPLATE_SHA, None, (), {}, 3, kind, "")


class Box(BaseHTTPRequestHandler):
    """A fake live Ask Mike behind Caddy: the app's real pages, Caddy's CSP on every response, and an ask limit keyed
    on the socket address whatever X-Forwarded-For says (Caddy replaces that header), unless honor_xff."""
    csp, limit_after, honor_xff, answers, health, limited_html = CSP, 3, False, True, None, None
    results, head_ok, favicon = True, True, True
    stream, gap = True, 1.2  # a first answer goes out as the head, a pause, then the tail; a repeat comes whole
    asks: dict = {}
    posts: list = []
    forged: list = []
    cached: set = set()

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        if Box.csp:
            self.send_header("Content-Security-Policy", Box.csp)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_parts(self, head: str, tail: str):
        """The streamed answer: the head, a pause, then the tail, with no length, closed at the end."""
        self.send_response(200)
        self.send_header("Content-Security-Policy", Box.csp)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(head.encode())
        self.wfile.flush()
        time.sleep(Box.gap)
        self.wfile.write(tail.encode())
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            h = Box.health if Box.health is not None else {"ok": True, **SHAS, "budget": "ok"}
            return self._send(200, json.dumps(h).encode(), "application/json")
        if path == "/":
            return self._send(200, pages.home_page().encode())
        if path.startswith("/evidence/") and path.removeprefix("/evidence/") in UNITS:
            return self._send(200, pages.evidence_page(UNITS[path.removeprefix("/evidence/")]).encode())
        if path == "/static/style.css":
            return self._send(200, b"body{}", "text/css")
        if path == "/favicon.ico" and Box.favicon:
            return self._send(200, b"\x00\x00\x01\x00", "image/x-icon")
        if path == "/results" and Box.results:
            return self._send(200, pages.results_page(FACTS).encode())
        self._send(404, pages.not_found_page().encode())

    def do_HEAD(self):
        self.send_response(200 if Box.head_ok else 405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        q = (parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True).get("q") or [""])[0]
        Box.posts.append(q)
        kind = route(q).kind
        if kind in NO_CALL:  # the app's fixed lines: no limit taken
            return self._send(200, pages.answer_page(out(kind, kind), UNITS, q, None).encode())
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            Box.forged.append(xff)
        key = xff if (Box.honor_xff and xff) else self.client_address[0]
        Box.asks[key] = Box.asks.get(key, 0) + 1
        if Box.asks[key] > Box.limit_after:
            html = Box.limited_html or pages.answer_page(out("fallback", kind, reason="limited"), UNITS, q, VISIT)
            return self._send(429, html.encode(), headers=[("Retry-After", "180")])
        o = out("answer", kind, kept=(CLAIM,)) if Box.answers else out("fallback", kind, reason="no_evidence")
        if Box.answers and Box.stream and q not in Box.cached:
            Box.cached.add(q)
            return self._send_parts(pages.answer_head(kind, q, True), pages.answer_tail(o, UNITS, VISIT))
        self._send(200, pages.answer_page(o, UNITS, q, VISIT).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def box():
    Box.csp, Box.limit_after, Box.honor_xff, Box.answers, Box.health, Box.limited_html = CSP, 3, False, True, None, None
    Box.results, Box.head_ok, Box.favicon = True, True, True
    Box.stream, Box.gap, Box.cached = True, 1.2, set()
    Box.asks, Box.posts, Box.forged = {}, [], []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Box)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def post_raw(base: str, q: str) -> bytes:
    req = Request(f"{base}/ask", data=urlencode({"q": q}).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=10) as r:
        return r.read()


def test_smoke_against_a_fake_box(box):
    s = smoke()
    assert route(s.HANDOFF_Q).kind == "handoff" and route("a" * (MAX_INPUT_CHARS + 1)).kind == "too_long"
    assert route(QUESTION).kind == "question"
    problems = (s.check_health(box, SHAS) + s.check_home(box)
                + s.check_fixed(box, s.HANDOFF_Q, HANDOFF_TEXT, "handoff")
                + s.check_fixed(box, "a" * (MAX_INPUT_CHARS + 1), TOO_LONG_TEXT, "too long")
                + s.check_evidence(box, FB["units"][0]) + s.check_static(box)
                + s.check_results(box, FACTS) + s.check_head(box))
    assert problems == []
    assert Box.asks == {}  # the fixed lines take no ask: no model call
    got, answered = s.check_ask(box, QUESTION)
    assert got == [] and answered is True
    assert s.check_burst(box, QUESTION) == []
    # the ask, 2 more under the limit, the 429, then the forged 429
    assert Box.forged == [s.FORGED] and sum(Box.asks.values()) == 5
    # why the smoke reads the visible text: Jinja escapes the apostrophe and NEXT_STEP's address is a link
    raw = post_raw(box, s.HANDOFF_Q).decode()
    assert HANDOFF_TEXT not in raw and "&#39;" in raw and NEXT_STEP not in raw
    lines, has_prev = s.read_page(raw.encode())
    assert HANDOFF_TEXT in lines and NEXT_STEP in lines and has_prev is False


def test_smoke_reports_a_stale_release_a_wrong_csp_and_a_bare_page(box):
    s = smoke()
    assert s.check_health(box, {**SHAS, "git_sha": "other"}) == [f"health: git_sha is {'g' * 40!r}, want 'other'"]
    Box.health = {"ok": True, **SHAS}
    assert s.check_health(box, SHAS) == ["health: no budget state"]
    Box.csp = None
    assert any("CSP" in p for p in s.check_home(box))
    Box.csp = CSP.replace("style-src 'self'", "script-src 'self'; style-src 'self'")
    assert any("CSP" in p for p in s.check_home(box))
    assert s.check_evidence(box, "no-such-unit") == ["evidence no-such-unit: status 404"]
    assert s.check_fixed(box, QUESTION, HANDOFF_TEXT, "handoff") == ["handoff: the page does not show the fixed line"]


def test_smoke_burst_catches_a_forgeable_or_missing_limit(box):
    s = smoke()
    Box.honor_xff = True
    assert s.check_burst(box, QUESTION) == ["forged X-Forwarded-For: status 200, want 429"]
    Box.asks, Box.limit_after, Box.honor_xff = {}, 10 ** 6, False
    assert s.check_burst(box, QUESTION, tries=5) == ["burst: no 429 after 5 asks"]
    Box.asks, Box.limit_after, Box.limited_html = {}, 1, pages.home_page()
    assert s.check_burst(box, QUESTION) == ["burst: the 429 page is not the fallback"]


def test_ask_caddy_never_compresses_the_streamed_answer():
    text = (DEPLOY / "ask.caddy").read_text(encoding="utf-8")
    assert "\t@compressible not path /ask\n\tencode @compressible zstd gzip\n" in text
    assert not re.search(r"^\s*encode zstd gzip", text, re.M)


def test_smoke_passes_an_answer_sent_in_two_parts(box):
    s = smoke()
    chunks = s.ask_chunks(box, QUESTION)[1]
    assert len(chunks) >= 2 and chunks[-1][0] - chunks[0][0] >= 1.0
    assert s.check_ask(box, "Another question about the invented depot importer?") == ([], True)


def test_smoke_finds_a_marker_split_across_two_pieces():
    s = smoke()
    page = b'<p class="wait">Reading Mike&#39;s evidence</p>'
    cut = page.index(b"Mike&#") + 3
    chunks = [(0.1, page[:cut]), (0.7, page[cut:]), (2.0, b'<ol class="answer">')]
    assert s.first_seen(chunks, s.WAIT_QUESTION) == 0.7
    assert s.first_seen(chunks, 'class="answer"') == 2.0
    assert s.first_seen(chunks, "never there") is None
    split = [(0.1, b'<ol class="ans'), (1.9, b'wer">')]
    assert s.first_seen(split, 'class="answer"') == 1.9


def test_smoke_reports_an_answer_sent_in_one_piece(box):
    Box.stream = False
    assert smoke().check_ask(box, QUESTION) == (
        ["ask: the answer came in one piece (cache hit?), so streaming wasn't checked; use a question not asked "
         "since this deploy"], True)


def test_smoke_reports_a_waiting_line_and_answer_that_arrived_together(box):
    Box.gap = 0.0
    problems, answered = smoke().check_ask(box, QUESTION)
    assert problems == ["ask: the waiting line and the answer arrived together"] and answered is True


def test_smoke_burst_stops_when_the_cache_does_not_hold_the_question(box):
    """Every ask under the limit is served from the cache only when the question came back as an answer. A fallback
    is not cached, so the next ask would call the model: the burst stops after one."""
    Box.answers = False
    assert smoke().check_burst(box, QUESTION) == [
        "burst: an ask came back without an answer, so the cache did not serve it; stopped before another model call"]
    assert len(Box.posts) == 1


def test_smoke_main_exit_codes_and_prints_no_question_or_page_text(box, monkeypatch, capsys):
    s = smoke()
    monkeypatch.setattr(s, "local_shas", lambda root=None: dict(SHAS))
    assert s.main([box + "/", "--ask", QUESTION]) == 0
    printed = capsys.readouterr().out
    assert printed.splitlines()[-1] == "smoke: ok" and QUESTION not in printed and "invented" not in printed

    Box.asks, Box.posts = {}, []
    assert s.main([box]) == 1  # never a silent skip: the rate-limit checks need --ask
    printed = capsys.readouterr().out
    assert ("FAIL burst: not run: the rate-limit and forged X-Forwarded-For checks need --ask" in printed
            and printed.splitlines()[-1] == "smoke: 1 problem(s)" and Box.asks == {} and "SKIP" not in printed)

    Box.asks, Box.answers = {}, False  # the live question comes back as the fallback: the burst cannot run free
    assert s.main([box, "--ask", QUESTION]) == 1
    printed = capsys.readouterr().out
    assert "FAIL burst: not run" in printed and QUESTION not in printed and len(Box.posts) > 0


def test_smoke_reports_a_dead_box_as_failures_not_a_traceback():
    s = smoke()
    with ThreadingHTTPServer(("127.0.0.1", 0), Box) as srv:
        dead = f"http://127.0.0.1:{srv.server_address[1]}"
    assert s.check_health(dead, SHAS) == ["health: status 0"]
    assert s.check_home(dead) and s.check_static(dead) and s.check_evidence(dead, "x")
    assert s.check_fixed(dead, s.HANDOFF_Q, HANDOFF_TEXT, "handoff") == ["handoff: status 0"]
    assert s.check_ask(dead, QUESTION) == (["ask: status 0"], False)
    assert s.check_burst(dead, QUESTION) == ["burst: status 0 after 0 asks, before any 429"]


def test_smoke_reads_the_local_shas(monkeypatch, tmp_path):
    s = smoke()
    (tmp_path / "corpus").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "corpus" / "units.jsonl").write_bytes(b"u\n")
    (tmp_path / "build" / "index.db").write_bytes(b"i")
    seen = []
    monkeypatch.setattr(s.subprocess, "check_output", lambda cmd, cwd, text: seen.append((cmd, cwd)) or "abc\n")
    assert s.local_shas(tmp_path) == {"git_sha": "abc", "corpus_sha": hashlib.sha256(b"u\n").hexdigest(),
                                      "index_sha": hashlib.sha256(b"i").hexdigest(),
                                      "template_sha": load_answer_settings().template_sha}
    assert seen == [(["git", "rev-parse", "HEAD"], tmp_path)]


def test_smoke_reports_a_missing_results_page_and_a_head_refusal(box):
    s = smoke()
    Box.results, Box.head_ok = False, False
    assert s.check_results(box, FACTS) == ["/results: status 404"]
    assert s.check_head(box) == ["HEAD /: status 405"]


def test_smoke_reports_a_results_page_that_lacks_the_local_recall():
    s = smoke()
    stale = {**FACTS, "m1_jdreq_R4_report_recall_at_10": {**FACTS["m1_jdreq_R4_report_recall_at_10"],
                                                          "display": "0.4242"}}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Box)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        Box.results = True
        assert s.check_results(base, stale) == ["/results: does not show recall@10 0.4242 from this checkout's facts"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_smoke_reports_a_missing_favicon(box):
    Box.favicon = False
    assert smoke().check_static(box) == ["/favicon.ico: status 404"]


def test_smoke_reports_a_recall_that_shows_only_on_another_row(box):
    """The live page carries the local R4 figure, but on the keyword-baseline row: the release's R4 row is not this
    checkout's, so the smoke must say so rather than find the figure elsewhere on the page."""
    kw = FACTS["m1_jdreq_KW_report_recall_at_10"]["display"]
    local = {**FACTS, "m1_jdreq_R4_report_recall_at_10": {**FACTS["m1_jdreq_R4_report_recall_at_10"], "display": kw}}
    assert kw in "\n".join(smoke().read_page(pages.results_page(FACTS).encode())[0])
    assert smoke().check_results(box, local) == [f"/results: does not show recall@10 {kw} from this checkout's facts"]
