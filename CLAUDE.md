# Ask Mike: rules for working in this repo

The design is `docs/superpowers/specs/2026-11-03-ask-mike-design.md`. The full private version, with the don't-print specifics, the source paths and the M0 rulings list, is `$ASK_PRIVATE_DIR/docs/design-private.md`. Read both before planning a milestone.

## This repo is public. Private material lives outside it.

- Personal data, raw jb files, eval job descriptions, visitor logs and the identity denylist live only in `$ASK_PRIVATE_DIR` (normally `~/Documents/ask-mike-private`). That folder is outside every git tree on purpose, so never move it or copy files from it in here.
- Never commit a phone number, street address, ZIP code, personal email, salary figure, application or interview count, or the name of a company Mike applied to. Before each commit, scan with the patterns in `$ASK_PRIVATE_DIR/denylist.local.txt`, and never copy those patterns into the repo.
- Don't put absolute home-directory paths in tracked files. Paths come from the environment variables `ASK_PRIVATE_DIR`, `JB_ROOT`, `DOCS_ROOT` and `MEMORY_ROOT`.
- Never import from jb. It is private, and this repo has to run for a stranger. When a jb rule is needed, such as the slop list or the PII patterns, copy it in and note where it came from.
- Every commit uses the noreply identity `Michael Fornal <253626225+MichaelFornal@users.noreply.github.com>`, which is set repo-locally. Nothing is pushed until Mike says so.

## What the AI may say

- **Honesty ledger** (`skill_depth`):
  - `none`: never claimed.
  - `familiar` and `learning`: only as "hasn't, but the closest thing is".
  - `working` and `strong`: claimable.
- **Numbers.** A number may appear only if the canonical resume or the evidence store already prints it (after Mike's review), or if it comes from a facts file with a command behind it. The don't-print list applies on top.
- **Voice.** Third person ("Mike built…"). No hype adjectives, no slop, short. No score or verdict, and never "not a fit".
- **Handoffs.** Salary, phone, address and job-search history get "that one's for Mike" plus mike@forn.al.

## Numbers in docs

Every number printed in the README, a report or the results page comes from `facts.json`, and each fact stores the command that produced it. Don't type figures by hand.

## Money

- $25/month all-in covers BOTH projects on the shared server: $7.09 server + $2.91 Deal Terms Desk model cap + **$15.00 Ask Mike model cap**.
- Evals and grading run through `claude -p` at $0, with a prompt byte-identical to the live one.
- The only planned API spend before launch is the ~40-item calibration, about $2.

## Shared server: read before planning or building M3

Deal Terms Desk stays live at deals.forn.al as a side project (Mike, 2026-11-03), on the same VM Ask Mike deploys to. Ask Mike runs **beside** `dtd.service`, never in place of it. See the spec §7 (Server, Shared Caddy) and §10.

- **Blocker: DTD's deploy overwrites the whole Caddy config.** DTD's `deploy/push.sh` (around line 154) installs its `deploy/Caddyfile` over `/etc/caddy/Caddyfile`. Left as is, every DTD deploy deletes the `ask.forn.al` site, and an Ask Mike deploy built the same way deletes deals.forn.al.
- **Fix it first, as a DTD release, before Ask Mike's first deploy:**
  - The base `/etc/caddy/Caddyfile` keeps DTD's global `log default` filter, which drops IPs, URIs and headers (Ask Mike's no-IP rule needs it too), plus `import /etc/caddy/sites/*.caddy`.
  - DTD's site block moves to `deals.caddy`. Ask Mike ships only `ask.caddy`.
  - Each push stages its own site file, runs `caddy validate` on the combined config, and only then installs and reloads.
  - Check that `audit.py`, `prune.sh` and `smoke.py` touch only their own app's directories and host.
  - Ship it through DTD's own gates, then confirm deals.forn.al still passes DTD's `deploy/smoke.py`.
- **Isolation:** Ask Mike gets its own:
  - system user `ask`
  - `/srv/ask`, `/var/lib/ask` and `/etc/ask/env` (root, 600)
  - `ask.service` on 127.0.0.1:8001 (DTD holds 8000)
  - `MemoryMax`, spend ledger and API key
- **Provisioning is additive.** Don't rewrite the sshd, journald or ufw drop-ins that DTD's provision already set.
- **M3 is done only when both smoke tests pass:** Ask Mike's and DTD's.

## Running things

- Pull or refresh the raw material: `ASK_PRIVATE_DIR=~/Documents/ask-mike-private python3 tools/pull_from_jb.py`. Add `--check` to verify it.
- Tests: `uv run pytest`.
- Export `ASK_PRIVATE_DIR` (normally `~/Documents/ask-mike-private`) before running the pipeline or committing; the code and the pre-commit scan both read it.
- The pre-commit hook (`.githooks/pre-commit`, enabled by `git config core.hooksPath .githooks`) runs `uv run --frozen python tools/scan_public.py --staged` and blocks the commit on any private pattern; it refuses to run when `ASK_PRIVATE_DIR` is unset. Run `uv run python tools/scan_public.py` to scan every tracked file.
- Export to the public corpus: `uv run python -m askmike.corpus export` (refuses while rulings or units are pending or any gate fails).
