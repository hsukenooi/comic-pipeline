#!/usr/bin/env bash
# BUI-172: the ONE canonical comics-server call convention for the /comic:* skills.
#
# "Resolve the comics server, health-gate it, call it, hard-fail loudly" was
# copy-pasted across the skills and drifted (BUI-151/154/157/169/170). This is
# the single definition every skill routes through so those divergences cannot
# recur. Source it, then:
#
#     source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
#     comics_resolve_server || exit 1     # sets/exports GIXEN_SERVER_URL
#     comics_health_gate     || exit 1     # /health must answer
#     comics_curl "$GIXEN_SERVER_URL/api/comics/collection/status" || exit 1
#
# Rule that matters most: a failed call NEVER yields a degraded/empty body a
# skill could mistake for a real "no results" answer — comics_curl exits
# non-zero and prints a loud diagnostic on any unreachable host or non-200.

# Resolve GIXEN_SERVER_URL. If already set, keep it. Otherwise infer from the
# hostname (the Mac Mini hosts the server; the MacBook reaches it via Tailscale).
# Unrecognised machine -> hard-fail rather than guess.
comics_resolve_server() {
  if [ -n "${GIXEN_SERVER_URL:-}" ]; then
    return 0
  fi
  local host
  host="$(hostname | tr '[:upper:]' '[:lower:]')"
  case "$host" in
    *mac-mini*|*macmini*)
      GIXEN_SERVER_URL="http://localhost:8080" ;;
    *macbook*)
      GIXEN_SERVER_URL="http://mac-mini.tail9b7fa5.ts.net:8080" ;;
    *)
      echo "GIXEN_SERVER_URL is not set and the machine ('$(hostname)') is unrecognised. Set the variable manually and confirm the server is running." >&2
      return 1 ;;
  esac
  export GIXEN_SERVER_URL
}

# Health-gate the server. /health is a static {"status":"ok"} — it proves the
# process is up but NOT that the collection store is healthy, so callers must
# still check the exit code of the actual data call (that is comics_curl's job).
comics_health_gate() {
  if [ -z "${GIXEN_SERVER_URL:-}" ]; then
    echo "comics_health_gate: GIXEN_SERVER_URL is unset — call comics_resolve_server first." >&2
    return 1
  fi
  if ! curl -sf "${GIXEN_SERVER_URL}/health" >/dev/null 2>&1; then
    echo "The Gixen server at '${GIXEN_SERVER_URL}' is not responding. Confirm the server is running before continuing." >&2
    return 1
  fi
}

# The hard-failing call wrapper. Pass any curl arguments (a URL, -X POST, -d,
# -o file, …); it forces --fail-with-body so a non-200 exits non-zero while
# still surfacing the error body on stderr, and never swallows failure into an
# empty success. Stdout carries the response body for the success path.
comics_curl() {
  local body status
  body="$(curl -sS --fail-with-body "$@")"
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "comics-server call FAILED (curl exit $status): $*" >&2
    [ -n "$body" ] && echo "$body" >&2
    return "$status"
  fi
  printf '%s' "$body"
}
