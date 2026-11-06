#!/usr/bin/env bash
# Ship HEAD to Ask Mike on the box it shares with Deal Terms Desk. Safe to rerun: a release or index bundle already on
# the box is kept, and an interrupted run resumes. Needs ASK_HOST, the ssh target (for example root@<host>), and
# ASK_PRIVATE_DIR (the scan and the gate config read it). BASE defaults to https://ask.forn.al.
# Ported from deal-terms-desk deploy/push.sh (branch caddy-split, 7976f1f) on 2026-11-05. Ask Mike installs only its
# own release, index bundle, gate config, unit and site file (/etc/caddy/sites/ask.caddy). It reads the box's base
# Caddyfile to validate the combined config, and never writes the base or another app's site file.
set -euo pipefail
: "${ASK_HOST:?set ASK_HOST to the ssh target, for example root@<host>}"
: "${ASK_PRIVATE_DIR:?set ASK_PRIVATE_DIR to the private folder; the scan and the gate config need it}"
BASE="${ASK_BASE:-https://ask.forn.al}"
cd "$(git rev-parse --show-toplevel)"
# Sourced first, so a missing helper fails the push before anything ships.
. deploy/hostport.sh

dirty() { [ -n "$(git status --porcelain)" ]; }
if dirty; then
  echo "refusing to deploy: the working tree is dirty; commit or stash first" >&2
  exit 1
fi

# Release gates: CI's checks plus the ones that need the private folder.
uv run pytest -q
uv run python -m askmike.evals facts --check
uv run python -m askmike.evals facts-m2 --check
uv run python -m askmike.evals facts-m3 --check
uv run python tools/scan_public.py
uv run python -m askmike.facts.copycheck README.md docs/m1/REPORT.md docs/m2/REPORT.md

# The gate config (the answer gates' private patterns), built in the private folder. A stale one, whose private inputs
# changed since it was built, is refused before anything ships.
PRIV="$(uv run python -c 'from askmike.private import private_dir; print(private_dir())')"
GATE="$PRIV/deploy/gate_config.json"
if ! uv run python -c 'import sys; from askmike.answer.gates import bundle_stale; sys.exit(1 if bundle_stale(sys.argv[1], sys.argv[2]) else 0)' "$GATE" "$PRIV"; then
  echo "refusing to deploy: the gate config is missing or stale; rebuild it: uv run python -m askmike.answer gate-config" >&2
  exit 1
fi

# The retrieval index the release serves, built from this commit's corpus (build/ is not tracked).
uv run python -m askmike.retrieval index
if dirty; then
  echo "refusing to deploy: the release gates changed tracked files; review and commit them first" >&2
  exit 1
fi

sha256() { uv run python -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"; }
SHA="$(git rev-parse HEAD)"
INDEX_SHA="$(sha256 build/index.db)"
CORPUS_SHA="$(sha256 corpus/units.jsonl)"
TEMPLATE_SHA="$(uv run python -c 'from askmike.answer.settings import load_answer_settings; print(load_answer_settings().template_sha)')"
REL="/srv/ask/releases/$SHA"
BUN="/srv/ask/bundles/$INDEX_SHA"
# Run a command in the release directory as the service user, with /etc/ask/env loaded (it is root-only, so it is
# sourced as root and the values are passed through sudo). The sourcing's stderr is discarded, so no parse message
# can ever echo a value; the lines themselves are checked first, below.
LOAD_ENV="set -a; . /etc/ask/env 2>/dev/null; set +a"
AS_ASK="sudo -E -u ask env HOME=/var/lib/ask"

# The env file holds the API key: refuse unless it is root-owned and mode 600.
if [ "$(ssh "$ASK_HOST" "stat -c '%U %a' /etc/ask/env" 2>/dev/null || true)" != "root 600" ]; then
  echo "refusing to deploy: /etc/ask/env must exist, owned by root, mode 600." >&2
  echo "  fix: write it from deploy/env.example, then: ssh $ASK_HOST 'chown root:root /etc/ask/env && chmod 600 /etc/ask/env'" >&2
  exit 1
fi

# Sourcing runs the file as shell: a hand-typed `ANTHROPIC_API_KEY= sk-ant-...` would run the key as a command, and
# the shell's "command not found" would print it here. So before the file is ever sourced, every line that is not
# blank or a comment must be NAME=value with no whitespace. The box reports only the numbers of the lines that are
# not (cut drops the content there), and the refusal prints only those numbers.
ENV_LINES="$(ssh "$ASK_HOST" 'test -r /etc/ask/env && { grep -nvE "^([A-Z_][A-Z0-9_]*=[^[:space:]]*|[[:space:]]*(#.*)?)\$" /etc/ask/env | cut -d: -f1 | tr "\n" " "; echo end; }' 2>/dev/null || true)"
case "$ENV_LINES" in
  end) ;;
  *" end")
    echo "refusing to deploy: /etc/ask/env line(s) ${ENV_LINES% end} are not NAME=value with no whitespace; fix them on" \
      "the box (no value is printed here)" >&2
    exit 1 ;;
  *)
    echo "refusing to deploy: could not check the lines of /etc/ask/env" >&2
    exit 1 ;;
esac

# The caps the box enforces (/etc/ask/env, read as root) must be the caps the facts publish (facts.json, computed
# from deploy/budget.json). Checked before anything ships.
caps_match() {  # $1 = "<ASK_MONTH_CAP_USD>|<ASK_DAY_CAP_USD>" as the box has them
  uv run python - "$1" <<'CAPS'
import json, sys
facts = json.load(open("facts.json"))
box = (sys.argv[1].split("|") + ["", ""])[:2]
bad = []
for env, key, raw in (("ASK_MONTH_CAP_USD", "m3_model_cap_usd", box[0]), ("ASK_DAY_CAP_USD", "m3_day_cap_usd", box[1])):
    fact = facts.get(key)
    want, raw = (fact.get("value") if isinstance(fact, dict) else None), raw.strip()
    if want is None:
        bad.append(f"facts.json has no {key} (fill in deploy/budget.json, then run: uv run python -m askmike.evals facts-m3)")
    elif not raw:
        bad.append(f"/etc/ask/env does not set {env}; facts.json {key}={want}")
    else:
        try:
            have = float(raw)
        except ValueError:
            bad.append(f"/etc/ask/env {env}={raw!r} is not a number; facts.json {key}={want}")
            continue
        if not abs(have - want) <= 1e-6:
            bad.append(f"/etc/ask/env {env}={raw} but facts.json {key}={want}")
for b in bad:
    print(f"refusing to deploy: the box would enforce a cap the facts do not publish: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
CAPS
}
BOX_CAPS="$(ssh "$ASK_HOST" "$LOAD_ENV; printf '%s|%s' \"\${ASK_MONTH_CAP_USD:-}\" \"\${ASK_DAY_CAP_USD:-}\"" || true)"
if ! caps_match "$BOX_CAPS"; then
  echo "  fix: set ASK_MONTH_CAP_USD and ASK_DAY_CAP_USD in /etc/ask/env to the published caps, or correct" \
    "deploy/budget.json and rerun facts-m3" >&2
  exit 1
fi

# The settings a release runs must be this commit's: the models, effort, max_tokens and template_sha from
# askmike/answer/settings.json (load_answer_settings), and the caps from facts.json (m3_model_cap_usd,
# m3_day_cap_usd). Used before the swap on the new release's own `config-check`.
config_match() {  # $1 = config-check's output on the box, $2 = what each problem is prefixed with
  uv run python - "$1" "$2" <<'CONFIG'
import json, sys
from askmike.answer.settings import load_answer_settings
raw, prefix = sys.argv[1], sys.argv[2]
try:
    box = json.loads(raw.strip().splitlines()[-1])
except (ValueError, IndexError):
    box = None
if not isinstance(box, dict):
    print(f"{prefix}: could not read the release's config-check output from the box: {raw[-300:]!r}", file=sys.stderr)
    sys.exit(1)
s = load_answer_settings()
bad = [f"the box runs {field}={box.get(field)!r} but this commit's settings have {field}={want!r}"
       for field, want in (("models", s.model), ("effort", s.effort), ("max_tokens", s.max_tokens),
                           ("template_sha", s.template_sha)) if box.get(field) != want]
facts = json.load(open("facts.json"))
for field, key in (("month_cap_usd", "m3_model_cap_usd"), ("day_cap_usd", "m3_day_cap_usd")):
    fact, have = facts.get(key), box.get(field)
    want = fact.get("value") if isinstance(fact, dict) else None
    if want is None:
        bad.append(f"facts.json has no {key} (run: uv run python -m askmike.evals facts-m3)")
    elif isinstance(have, bool) or not isinstance(have, (int, float)) or not abs(have - want) <= 1e-6:
        bad.append(f"the box runs {field}={have!r} but facts.json {key}={want}")
for b in bad:
    print(f"{prefix}: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
CONFIG
}

# Code: the committed tree only, never the working tree. .complete marks a finished release. Bytecode is compiled
# here: the release is read-only to the service (ProtectSystem=strict), so Python could never cache it later. The
# static fallback page Caddy serves while the app is down (site/dist/fallback.html) is built here as root, by the
# release's own code, so a release never changes once it is complete.
if ! ssh "$ASK_HOST" test -f "$REL/.complete"; then
  ssh "$ASK_HOST" "rm -rf '$REL' && mkdir -p '$REL'"
  git archive --format=tar HEAD | ssh "$ASK_HOST" "tar -x --no-same-owner -C '$REL'"
  ssh "$ASK_HOST" "cd '$REL' && echo $SHA > GIT_SHA && UV_PYTHON_PREFERENCE=only-system UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev \
  && .venv/bin/python -m askmike.service site --out site/dist && touch .complete"
fi

# The new release's own settings and caps, read as the service would read them (its env, its user, its directory),
# before anything live moves. A config-check that fails prints nothing on stdout, and that is refused too.
NEW_CFG="$(ssh "$ASK_HOST" "cd '$REL' && { $LOAD_ENV; $AS_ASK .venv/bin/python -m askmike.service config-check; }" || true)"
if ! config_match "$NEW_CFG" "refusing to switch"; then
  echo "  fix: commit the settings this box should run, or set ASK_MONTH_CAP_USD and ASK_DAY_CAP_USD in /etc/ask/env" \
    "to the m3 facts; the live release is untouched" >&2
  exit 1
fi

# Index: only when the box lacks this exact file. Resumable, then checked against its sha256.
if ! ssh "$ASK_HOST" test -f "$BUN/index.db"; then
  ssh "$ASK_HOST" "mkdir -p '$BUN'"
  rsync -a --partial build/index.db "$ASK_HOST:$BUN/index.db.part"
  ssh "$ASK_HOST" "cd '$BUN' && echo '$INDEX_SHA  index.db.part' | sha256sum -c --quiet - && mv index.db.part index.db"
fi
ssh "$ASK_HOST" "ln -sfn '$BUN/index.db' '$REL/index.db'"

# rsync -a keeps the sender's owner, and macOS's openrsync has no --chown, so ownership is set on the box: the
# release (code, venv, fallback page) and the bundle belong to root, and the service user can only read them.
ssh "$ASK_HOST" "chown -R root:root '$REL' '$BUN'"

# The embedding model, downloaded once into the service's cache, as the service user.
ssh "$ASK_HOST" "cd '$REL' && { $LOAD_ENV; $AS_ASK .venv/bin/python -c 'from askmike.retrieval.models import Embedder; Embedder().embed_query(\"warm up\")'; }"

# Several apps share this box's Caddy: /etc/caddy/Caddyfile is the base (Deal Terms Desk owns it) and each app installs
# only its own /etc/caddy/sites/<app>.caddy. Ask Mike never writes the base, so validate_site builds its temp tree on
# the box from the box's live base: its import line rewritten to the temp sites dir, every current site file except
# <name>.caddy, and the candidate. It refuses when the base does not import the sites dir (Deal Terms Desk's Caddy
# split is not live), since then the site would have nowhere to go.
validate_site() {  # $1 = candidate site file on the box, $2 = its name (the file it will be installed as, minus .caddy)
  ssh "$ASK_HOST" "bash -s -- '$1' '$2'" <<'VALIDATE'
set -eu
cand="$1"; name="$2"
base=/etc/caddy/Caddyfile
import_line='import /etc/caddy/sites/*.caddy'
if ! grep -qxF "$import_line" "$base"; then
  echo "refusing: $base is missing or does not import /etc/caddy/sites/*.caddy; Deal Terms Desk's Caddy split must be live first" >&2
  exit 1
fi
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir "$T/sites"
sed "s#^import /etc/caddy/sites/\*\.caddy\$#import $T/sites/*.caddy#" "$base" > "$T/Caddyfile"
grep -qxF "import $T/sites/*.caddy" "$T/Caddyfile" || { echo "refusing: the import line was not rewritten" >&2; exit 1; }
for f in /etc/caddy/sites/*.caddy; do
  [ -e "$f" ] || continue
  [ "$(basename "$f")" = "$name.caddy" ] || cp "$f" "$T/sites/"
done
cp "$cand" "$T/sites/$name.caddy"
caddy validate --adapter caddyfile --config "$T/Caddyfile"
VALIDATE
}

# Validate the new config before touching anything live: a bad site file or unit leaves the old release serving.
# The unit is verified with its path pointed at this release, since `current` does not move yet.
if ! validate_site "$REL/deploy/ask.caddy" ask \
  || ! ssh "$ASK_HOST" "T=\$(mktemp -d) && sed 's#/srv/ask/current#$REL#g' '$REL/deploy/ask.service' > \"\$T/ask.service\" \
  && systemd-analyze verify \"\$T/ask.service\"; r=\$?; rm -rf \"\$T\"; exit \$r"; then
  echo "refusing to switch: the new Caddy site or ask.service failed validation; the live release is untouched" >&2
  exit 1
fi

# The gate config goes to /etc/ask/gate_config.json (root:ask, 640), never into a release. It travels on ssh's stdin
# to a temp file on the box, is installed from there, and the temp file is removed. Its content is never printed.
ssh "$ASK_HOST" 'T=$(mktemp) && trap "rm -f \"\$T\"" EXIT && cat > "$T" && install -o root -g ask -m 640 "$T" /etc/ask/gate_config.json' < "$GATE"

# Switch: atomic symlink swap, Ask Mike's site file and unit from the release, reload Caddy, restart the service. The
# previous release is kept for the revert.
# NONE marks "no symlink" (the first push). An ssh failure is not that: it refuses here, before the swap, with the
# live release still serving, instead of letting a later failure uninstall the live release as if it were a first one.
PREV="$(ssh "$ASK_HOST" 'if [ -L /srv/ask/current ]; then readlink /srv/ask/current; else echo NONE; fi')" \
  || { echo "refusing to switch: could not read /srv/ask/current" >&2; exit 1; }
[ "$PREV" = NONE ] && PREV=""
install_release() {  # $1 = release directory on the box
  ssh "$ASK_HOST" "ln -sfn '$1' /srv/ask/current.next && mv -T /srv/ask/current.next /srv/ask/current \
  && install -m 644 '$1/deploy/ask.caddy' /etc/caddy/sites/ask.caddy \
  && install -m 644 '$1/deploy/ask.service' /etc/systemd/system/ask.service \
  && systemctl daemon-reload && systemctl enable --now ask && systemctl reload caddy && systemctl restart ask"
}
# A first release that fails leaves the box as it was before the push: the service stopped and disabled (not
# restarting forever), its site file and unit removed, and Caddy reloaded, which restores the config that was live
# before. `current` is removed too, so the next push has no failed release to revert to.
uninstall_first() {
  ssh "$ASK_HOST" "systemctl disable --now ask; rm -f /etc/caddy/sites/ask.caddy /etc/systemd/system/ask.service \
  /srv/ask/current && systemctl daemon-reload && systemctl reload caddy"
}
# Leave the new release: swap back to the previous one, reinstall its site file and unit and restart, then fail. With
# no previous release, uninstall this one instead.
revert() {  # $1 = why
  if [ -n "$PREV" ]; then
    echo "$1; reverting to the previous release ${PREV##*/}" >&2
    install_release "$PREV" || echo "the revert itself failed; fix by hand: point /srv/ask/current at $PREV, copy its" \
      "deploy/ask.caddy and deploy/ask.service into place, then reload caddy and restart ask" >&2
    echo "reverted from $SHA to ${PREV##*/}; the push failed" >&2
  else
    echo "$1, and there is no previous release to revert to; removing this first release's service and site file" >&2
    uninstall_first || echo "the cleanup itself failed; fix by hand: systemctl disable --now ask, remove" \
      "/etc/caddy/sites/ask.caddy and /etc/systemd/system/ask.service, then systemctl daemon-reload and reload caddy" >&2
    echo "the first release $SHA was removed; the push failed" >&2
  fi
  exit 1
}
# Any step after the swap can fail (site file, unit, daemon-reload, enable, reload, restart): `current` has moved by
# then, so a failure takes the revert, not a bare exit.
if ! install_release "$REL"; then
  revert "installing release $SHA failed"
fi

# The box must now serve exactly this code, corpus, index and answer template.
matches() {
  uv run python - "$1" "$SHA" "$CORPUS_SHA" "$INDEX_SHA" "$TEMPLATE_SHA" <<'PY'
import json, sys
try:
    h = json.loads(sys.argv[1])
except ValueError:
    sys.exit(1)
if not isinstance(h, dict):
    sys.exit(1)
sys.exit(0 if [h.get("git_sha"), h.get("corpus_sha"), h.get("index_sha"), h.get("template_sha")] == sys.argv[2:]
         else 1)
PY
}
# Pin the host to the box's own address for the poll: the operator's DNS cache can lag new records for up to the
# zone's negative TTL (an NXDOMAIN seen before the records existed), while TLS, Caddy and the service are still
# checked through the public HTTPS path. host_port comes from deploy/hostport.sh, sourced at the top. While the app
# is down Caddy answers with the static fallback page and an error status, which curl -f turns into '{}'.
BOX_IP="$(ssh "$ASK_HOST" "hostname -I" | tr -s ' \t' '\n\n' | grep -m1 '\.' || true)"
PIN=()
if [ -n "$BOX_IP" ]; then
  PIN=(--resolve "$(host_port "$BASE"):$BOX_IP")
else
  echo "warning: could not find the box's IPv4 address; polling health through the operator's DNS, which may lag new records" >&2
fi
live=""
for _ in $(seq 60); do
  HEALTH="$(curl -fsS --max-time 10 ${PIN[@]+"${PIN[@]}"} "$BASE/health" || echo '{}')"
  if matches "$HEALTH"; then live=1; break; fi
  sleep 2
done
if [ -z "$live" ]; then
  revert "release $SHA never reported healthy"
fi
echo "deployed $SHA"

# Disk: keep the live release, two rollback targets and the bundles they link to (deploy/prune.sh, from this
# release). PREV, the release that was live before this push, is kept as one of the two. A failed prune leaves
# the deploy standing.
ssh "$ASK_HOST" "bash '$REL/deploy/prune.sh' /srv/ask '$PREV'" || echo "prune failed; the release is live; check /srv/ask by hand" >&2
