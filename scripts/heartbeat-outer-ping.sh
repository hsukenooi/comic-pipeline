#!/usr/bin/env bash
# BUI-672: the outer layer of the BUI-602/624 job-heartbeat watchdog — a
# dead-man's-switch, not another poller.
#
# Everything the watchdog does (docs/reference/job-heartbeat-contract.md) is a
# PULL: something must ask GET /api/comics/health/heartbeats for a stale job
# to be noticed. A launchd job on the Mac Mini that merely checks the endpoint
# and logs locally would fail green in exactly the scenario that matters most:
# the Mini asleep, launchd dead, or the comics server down — nobody local is
# left to read the log. So this script does not raise ITS OWN alarm; it feeds
# a healthchecks.io check that alarms on SILENCE. The three outcomes:
#
#   healthy == true   -> ping success        ("all good, still breathing")
#   healthy == false  -> ping /fail          (immediate alarm, names the jobs)
#   anything else      -> ping NOTHING       (env unset, fetch failed, parse
#                         (non-200, curl error,   failed, script bug — every one
#                          script bug, ...)        of these must read as silence)
#
# healthchecks.io's alarm lives off this machine and fires on absence, so a
# dead Mini, a dead launchd, a dead comics server, or a broken copy of this
# very script all trip it the same way a missed ping would. That is the whole
# argument for this shape over a local poller — see the ticket and
# docs/reference/heartbeat-outer-ping-scheduling.md.
#
# Scheduling, healthchecks.io setup, and the launchd plist recipe are
# documented in docs/reference/heartbeat-outer-ping-scheduling.md — this file
# is deliberately silent on cadence so the two cannot drift apart in prose.
set -euo pipefail

# Per-call network timeouts (seconds). Overridable for manual testing; a
# launchd job that hangs forever stops pinging and never says why, so every
# network call below is bounded explicitly rather than trusting a caller's
# default.
HEARTBEAT_FETCH_MAX_TIME="${HEARTBEAT_FETCH_MAX_TIME:-15}"
HEARTBEAT_PING_MAX_TIME="${HEARTBEAT_PING_MAX_TIME:-10}"

# --- Guard 1: no ping URL, no ping. Fail closed. ---------------------------
# A healthchecks.io ping URL is a bearer secret (anyone with it can fake this
# job's heartbeat), so it is read from the environment only — never hardcoded
# here, never committed anywhere in this repo. If it is missing, the correct
# behavior is silence: exit non-zero without contacting healthchecks.io at
# all, so the check that already exists (or will exist) alarms on the
# resulting gap instead of being told "all good" by a mis-deployed job.
if [ -z "${HEARTBEAT_OUTER_PING_URL:-}" ]; then
  echo "heartbeat-outer-ping: HEARTBEAT_OUTER_PING_URL is unset or empty — refusing to ping anything. This is fail-closed on purpose: healthchecks.io will alarm on the silence, which is the correct alarm for a mis-deployed job." >&2
  exit 1
fi
# Strip a trailing slash so appending "/fail" below can never produce "//fail".
HEARTBEAT_OUTER_PING_URL="${HEARTBEAT_OUTER_PING_URL%/}"

# --- Resolve scripts/comics-api next to this script ------------------------
# Resolve through symlinks the same way scripts/comics-api resolves
# comics-server.sh, so this still works if this script is ever symlinked onto
# PATH. Portable to macOS's bash 3.2 (no `readlink -f`).
_resolve_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [ -h "$src" ]; do
    local dir
    dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    case "$src" in
      /*) ;;
      *) src="$dir/$src" ;;
    esac
  done
  cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(_resolve_script_dir)"
COMICS_API="$SCRIPT_DIR/comics-api"

if [ ! -x "$COMICS_API" ]; then
  echo "heartbeat-outer-ping: cannot find or execute $COMICS_API — sending no ping. Is the repo checkout intact?" >&2
  exit 1
fi

# --- Fetch the heartbeat report ---------------------------------------------
# Prefer scripts/comics-api over a bare `curl "$COMICS_SERVER_URL/..."`: a
# bare curl silently hits an empty host when COMICS_SERVER_URL is unset (the
# BUI-352 trap), whereas comics-api resolves COMICS_SERVER_URL itself (or
# hard-fails if the machine is unrecognised and the var is unset), health-gates
# the server via GET /health, and only prints the response body to stdout on a
# genuine 2xx (scripts/comics-server.sh's comics_curl uses
# `--fail-with-body`). Any unreachable host, non-200, or curl error is a
# non-zero exit with nothing usable on stdout — exactly the "no success ping"
# outcome this script needs.
#
# The /health gate cannot mask an unhealthy heartbeat report: /health is a
# separate, static `{"status":"ok"}` endpoint that only proves the process is
# up. It says nothing about job heartbeats, so a server that is UP but whose
# heartbeat report says `healthy: false` still passes the gate, comics-api
# still returns 200 with that body, and this script still sees healthy=false
# below. The gate can only turn an unreachable server into "no ping" (correct)
# — it can never turn an unhealthy report into a false success ping.
#
# `set +e` around this one call: under `set -e`, `report_json=$(...)` on a
# failing command substitution would abort the script immediately, before we
# get a chance to read $fetch_status and decide "no ping" deliberately.
# comics-api prints its own loud diagnostic to stderr on failure (unreachable
# server, non-200, unset COMICS_SERVER_URL) — that is left to flow straight
# through to this script's own stderr (launchd's StandardErrorPath), so no
# extra capture/replay plumbing is needed here.
set +e
report_json="$(COMICS_CURL_MAX_TIME="$HEARTBEAT_FETCH_MAX_TIME" "$COMICS_API" GET /api/comics/health/heartbeats)"
fetch_status=$?
set -e

if [ "$fetch_status" -ne 0 ] || [ -z "$report_json" ]; then
  echo "heartbeat-outer-ping: fetching /api/comics/health/heartbeats failed (comics-api exit $fetch_status) — sending no ping. healthchecks.io will alarm on the silence." >&2
  exit 1
fi

# --- Parse the report --------------------------------------------------------
# Ping success ONLY when `healthy` is exactly boolean true. Any parse
# failure, missing field, or non-boolean value must fall through to "no
# ping" — never default to healthy. `pending_instrumentation` jobs already
# make the server's own `healthy` field false (see heartbeat_report() in
# gixen_overlay/db.py), so this script inherits that: pending_instrumentation
# is never treated as health here either.
if ! command -v python3 >/dev/null 2>&1; then
  echo "heartbeat-outer-ping: python3 is not available to parse the heartbeat report — sending no ping." >&2
  exit 1
fi

set +e
parsed="$(printf '%s' "$report_json" | python3 -c '
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("STATUS=PARSE_ERROR")
    sys.exit(0)

healthy = data.get("healthy")
if healthy is True:
    print("STATUS=HEALTHY")
elif healthy is False:
    def csv(key):
        vals = data.get(key) or []
        if not isinstance(vals, list):
            vals = []
        return ",".join(str(v) for v in vals)

    print("STATUS=UNHEALTHY")
    print("STALE=" + csv("stale_jobs"))
    print("NEVER=" + csv("never_seen_jobs"))
    print("PENDING=" + csv("pending_instrumentation_jobs"))
else:
    print("STATUS=PARSE_ERROR")
')"
parse_status=$?
set -e

if [ "$parse_status" -ne 0 ] || [ -z "$parsed" ]; then
  echo "heartbeat-outer-ping: could not parse the heartbeat report — sending no ping." >&2
  exit 1
fi

status="PARSE_ERROR"
stale_jobs=""
never_jobs=""
pending_jobs=""
while IFS='=' read -r key value; do
  case "$key" in
    STATUS) status="$value" ;;
    STALE) stale_jobs="$value" ;;
    NEVER) never_jobs="$value" ;;
    PENDING) pending_jobs="$value" ;;
  esac
done <<PARSED
$parsed
PARSED

# --- Act on the verdict ------------------------------------------------------
case "$status" in
  HEALTHY)
    # Success ping ONLY on this branch, and only after the checks above have
    # all passed. If the ping itself cannot be delivered, no success signal
    # reaches healthchecks.io and the resulting silence is the correct alarm
    # — so this still exits non-zero rather than claiming success it could not
    # actually send.
    if curl -fsS --max-time "$HEARTBEAT_PING_MAX_TIME" "$HEARTBEAT_OUTER_PING_URL" >/dev/null 2>&1; then
      echo "heartbeat-outer-ping: heartbeats report healthy=true — pinged success."
      exit 0
    fi
    echo "heartbeat-outer-ping: heartbeats report healthy=true but the success ping to healthchecks.io failed — no ping was delivered, so healthchecks.io alarms on the resulting silence instead of a false green." >&2
    exit 1
    ;;
  UNHEALTHY)
    fail_body="stale_jobs=${stale_jobs}; never_seen_jobs=${never_jobs}; pending_instrumentation_jobs=${pending_jobs}"
    echo "heartbeat-outer-ping: heartbeats report healthy=false — ${fail_body}" >&2
    # Best-effort: whether or not this delivers, the script still exits
    # non-zero below. If it fails to deliver too, the resulting silence
    # alarms anyway — belt and suspenders, not a required step.
    curl -fsS --max-time "$HEARTBEAT_PING_MAX_TIME" \
      --data-urlencode "message=${fail_body}" \
      "${HEARTBEAT_OUTER_PING_URL}/fail" >/dev/null 2>&1 || \
      echo "heartbeat-outer-ping: the /fail ping itself could not be delivered — exiting non-zero regardless; the resulting silence alarms too." >&2
    exit 1
    ;;
  *)
    echo "heartbeat-outer-ping: heartbeats report parsed as neither healthy=true nor healthy=false (status=${status}) — sending no ping." >&2
    exit 1
    ;;
esac
