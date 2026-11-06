#!/usr/bin/env bash
# Copied from deal-terms-desk deploy/prune.sh (branch caddy-split, 7976f1f) on 2026-11-05. Changes: the default
# root is Ask Mike's, the file each release links from its bundle is index.db (the retrieval index), and the
# root's variable is TREE (the public-repo scan flags DTD's name for it).
# Free disk on the box: keep the release `current` points to, two rollback targets and every bundle a kept release
# links to; remove the rest, including partial uploads. The rollback targets are the release that was live before
# this push (PREV, from push.sh) when given, then the most recently completed others. Safe to rerun. Runs on the box
# as root after a good push: bash prune.sh [root=/srv/ask] [prev release; empty means none].
set -euo pipefail
shopt -s nullglob
TREE="$(cd "${1:-/srv/ask}" && pwd -P)"
PREV_ARG="${2:-}"
KEEP_OLDER=2

refuse() {
  echo "refusing to prune: $1; nothing was removed" >&2
  exit 1
}

# Every path below is canonical, so a root, releases/ or bundles/ reached through a symlink still compares equal.
RELS="$(cd "$TREE/releases" 2>/dev/null && pwd -P || true)"
BUNS="$(cd "$TREE/bundles" 2>/dev/null && pwd -P || true)"
if [ -z "$RELS" ] || [ -z "$BUNS" ]; then
  refuse "$TREE/releases or $TREE/bundles is not a directory"
fi

CUR="$(readlink -f "$TREE/current" 2>/dev/null || true)"
if [ -z "$CUR" ] || [ ! -d "$CUR" ]; then
  refuse "$TREE/current does not point at a release"
fi
if [ "$(dirname "$CUR")" != "$RELS" ]; then
  refuse "$TREE/current points at $CUR, which is not a release in $RELS"
fi

in_list() {  # in_list <path> [list...]: is the path one of the list
  local want="$1" have
  shift
  for have in "$@"; do
    if [ "$have" = "$want" ]; then return 0; fi
  done
  return 1
}

# What stays is decided in full before anything is removed.
keep=("$CUR")
if [ -n "$PREV_ARG" ]; then
  prev="$(readlink -f "$PREV_ARG" 2>/dev/null || true)"
  if [ -n "$prev" ] && [ -d "$prev" ] && [ "$(dirname "$prev")" = "$RELS" ] && [ "$prev" != "$CUR" ]; then
    keep+=("$prev")
  fi
fi
markers=("$RELS"/*/.complete)
if [ "${#markers[@]}" -gt 0 ]; then
  for marker in $(ls -1t "${markers[@]}"); do  # newest first; release names are shas, so no spaces
    rel="${marker%/.complete}"
    if in_list "$rel" "${keep[@]}"; then continue; fi
    if [ "${#keep[@]}" -gt "$KEEP_OLDER" ]; then break; fi
    keep+=("$rel")
  done
fi

used=()
for rel in "${keep[@]}"; do
  db=""
  if [ -e "$rel/index.db" ] || [ -L "$rel/index.db" ]; then
    db="$(readlink -f "$rel/index.db" 2>/dev/null || true)"
  fi
  if [ -n "$db" ]; then
    if [ "$(dirname "$(dirname "$db")")" != "$BUNS" ]; then
      refuse "release ${rel##*/} links its index.db to $db, which is not a bundle in $BUNS"
    fi
    used+=("$(dirname "$db")")
  fi
done

for rel in "$RELS"/*/; do
  rel="${rel%/}"
  if ! in_list "$rel" "${keep[@]}"; then
    rm -rf "$rel"
    echo "removed release ${rel##*/}"
  fi
done
for bun in "$BUNS"/*/; do
  bun="${bun%/}"
  if ! in_list "$bun" ${used[@]+"${used[@]}"}; then
    rm -rf "$bun"
    echo "removed bundle ${bun##*/}"
  fi
done
