# Copied from deal-terms-desk deploy/hostport.sh (branch caddy-split, 7976f1f) on 2026-11-05, unchanged.
# Sourced by push.sh. host_port URL prints "host:port": the scheme is stripped, then everything from the first "/";
# the port is the one the URL names, else 443 for https and 80 for http.
host_port() {
  local rest="${1#*://}"
  rest="${rest%%/*}"
  case "$rest" in
    *:*) printf '%s\n' "$rest" ;;
    *) case "$1" in https://*) printf '%s:443\n' "$rest" ;; *) printf '%s:80\n' "$rest" ;; esac ;;
  esac
}
