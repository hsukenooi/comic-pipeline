#!/usr/bin/env bash
# BUI-172: the ONE canonical comics-server call convention for the /comic:* skills.
#
# "Resolve the comics server, health-gate it, call it, hard-fail loudly" was
# copy-pasted across the skills and drifted (BUI-151/154/157/169/170). This is
# the single definition every skill routes through so those divergences cannot
# recur. Source it, then:
#
#     source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
#     comics_resolve_server || exit 1     # sets/exports COMICS_SERVER_URL (+ GIXEN_SERVER_URL alias)
#     comics_health_gate     || exit 1     # /health must answer
#     comics_curl "$COMICS_SERVER_URL/api/comics/collection/status" || exit 1
#
# Rule that matters most: a failed call NEVER yields a degraded/empty body a
# skill could mistake for a real "no results" answer — comics_curl exits
# non-zero and prints a loud diagnostic on any unreachable host or non-200.
#
# BUI-220: the canonical env var is COMICS_SERVER_URL (this is the comics
# server, not the Gixen bidding service). GIXEN_SERVER_URL is a deprecated alias
# still accepted as a preset. comics_resolve_server exports BOTH (same value) so
# any skill that still references either keeps working during the migration.

# Resolve the comics server URL. A preset (COMICS_SERVER_URL preferred, else the
# deprecated GIXEN_SERVER_URL) wins; otherwise infer from the hostname (the Mac
# Mini hosts the server; the MacBook reaches it via Tailscale). Unrecognised
# machine -> hard-fail rather than guess. Exports BOTH COMICS_SERVER_URL and the
# GIXEN_SERVER_URL alias to the resolved value.
comics_resolve_server() {
  local url=""
  if [ -n "${COMICS_SERVER_URL:-}" ]; then
    url="$COMICS_SERVER_URL"
  elif [ -n "${GIXEN_SERVER_URL:-}" ]; then
    url="$GIXEN_SERVER_URL"
    echo "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL" >&2
  fi
  if [ -z "$url" ]; then
    local host
    host="$(hostname | tr '[:upper:]' '[:lower:]')"
    case "$host" in
      *mac-mini*|*macmini*)
        url="http://localhost:8080" ;;
      *macbook*)
        url="http://mac-mini.tail9b7fa5.ts.net:8080" ;;
      *)
        echo "COMICS_SERVER_URL is not set and the machine ('$(hostname)') is unrecognised. Set the variable manually and confirm the server is running." >&2
        return 1 ;;
    esac
  fi
  COMICS_SERVER_URL="$url"
  GIXEN_SERVER_URL="$url"
  export COMICS_SERVER_URL GIXEN_SERVER_URL
}

# Health-gate the server. /health is a static {"status":"ok"} — it proves the
# process is up but NOT that the collection store is healthy, so callers must
# still check the exit code of the actual data call (that is comics_curl's job).
comics_health_gate() {
  if [ -z "${COMICS_SERVER_URL:-}" ]; then
    echo "comics_health_gate: COMICS_SERVER_URL is unset — call comics_resolve_server first." >&2
    return 1
  fi
  if ! curl -sf "${COMICS_SERVER_URL}/health" >/dev/null 2>&1; then
    echo "The comics server at '${COMICS_SERVER_URL}' is not responding. Confirm the server is running before continuing." >&2
    return 1
  fi
}

# The hard-failing call wrapper. Pass any curl arguments (a URL, -X POST, -d,
# -o file, …); it forces --fail-with-body so a non-200 exits non-zero while
# still surfacing the error body on stderr, and never swallows failure into an
# empty success. Stdout carries the response body for the success path.
# BUI-186: --max-time bounds a hung/half-open connection so a stalled server
# can't block a skill step forever (a hang is as silent as a swallowed error).
COMICS_CURL_MAX_TIME="${COMICS_CURL_MAX_TIME:-30}"
comics_curl() {
  local body status
  body="$(curl -sS --fail-with-body --max-time "$COMICS_CURL_MAX_TIME" "$@")"
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "comics-server call FAILED (curl exit $status): $*" >&2
    [ -n "$body" ] && echo "$body" >&2
    return "$status"
  fi
  printf '%s' "$body"
}

# BUI-186: thin GET/POST aliases over comics_curl so a skill never hand-rolls a
# raw curl (where the 2>/dev/null / "|| echo" swallow crept in). Both inherit
# comics_curl's fail-loud + --max-time behavior.
#   comics_get  "$COMICS_SERVER_URL/api/comics/collection/status"
#   comics_post "$COMICS_SERVER_URL/api/comics/wish-list" -H 'Content-Type: application/json' -d "$json"
comics_get() {
  comics_curl "$@"
}
comics_post() {
  comics_curl -X POST "$@"
}
