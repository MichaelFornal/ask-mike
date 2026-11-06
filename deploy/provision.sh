#!/usr/bin/env bash
# One-time, idempotent setup of Ask Mike on the box Deal Terms Desk already provisioned (Ubuntu 24.04 LTS). Additive
# only: it adds the service user `ask` and Ask Mike's own directories. It installs no package and changes nothing
# Deal Terms Desk set up: not the firewall, the ssh or journal settings, Caddy, nor any of DTD's files. Run as root
# from the repo root:
#   ssh "$ASK_HOST" 'bash -s' < deploy/provision.sh
# Then write /etc/ask/env (see deploy/env.example; owner root:root, mode 600) and run deploy/push.sh.
# Adapted from deal-terms-desk deploy/provision.sh (branch caddy-split, 7976f1f) on 2026-11-05: only its user and
# directory steps, with Ask Mike's names.
set -euo pipefail

# Deal Terms Desk's Caddy split must be live: each app's site in its own file under /etc/caddy/sites, imported by the
# base Caddyfile. Before it, ask.caddy would have nowhere to go but the base, which Ask Mike never writes.
if [ ! -d /etc/caddy/sites ] || ! grep -qxF 'import /etc/caddy/sites/*.caddy' /etc/caddy/Caddyfile; then
  echo "refusing to provision: /etc/caddy/sites is missing or the base Caddyfile does not import" \
    "/etc/caddy/sites/*.caddy; ship Deal Terms Desk's Caddy split release first" >&2
  exit 1
fi

# What deploy/push.sh runs on the box. Deal Terms Desk's provision installed each one; this only checks.
for tool in caddy uv rsync sqlite3 sudo systemd-analyze; do
  if ! command -v "$tool" >/dev/null; then
    echo "refusing to provision: $tool is not installed; Deal Terms Desk's provision installs it" >&2
    exit 1
  fi
done

# The service user and its directories: the releases and index bundles (root's; the service only reads them), the
# service's state (the spend ledger, the answer cache, the visitor log, the embedding model) and /etc/ask, which holds
# the env file (root, 600) and the gate config (root:ask, 640).
id ask >/dev/null 2>&1 || useradd --system --home-dir /var/lib/ask --shell /usr/sbin/nologin ask
install -d -o root -g root -m 755 /srv/ask /srv/ask/releases /srv/ask/bundles
install -d -o ask -g ask -m 750 /var/lib/ask /var/lib/ask/fastembed /var/lib/ask/huggingface
install -d -o root -g ask -m 750 /etc/ask

echo "provisioned Ask Mike; next: write /etc/ask/env (owner root, mode 600), then run deploy/push.sh"
