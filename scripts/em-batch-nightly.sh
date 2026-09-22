#!/bin/bash
# Nightly, fully unattended /em-batch over the open `comics` tickets (BUI-972).
#
# Fired by the LaunchAgent com.comics.em-batch-nightly (scripts/launchd/) at
# 01:00 on the Mac Mini. One run: pick tickets -> fresh detached worktree ->
# `claude -p "/em-batch mode:autonomous ..."` -> post the run's summary to
# Telegram (Telegram only, by request; the daily note is not touched). The model
# run does the engineering (implement, review, CI, merge, deploy, close); this
# file only frames it and reports.
#
# Run it now (does not wait for 01:00):
#   launchctl kickstart -k "gui/$(id -u)/com.comics.em-batch-nightly"
# Or by hand:
#   scripts/em-batch-nightly.sh                 # real run
#   scripts/em-batch-nightly.sh --dry-run       # selector + preflight + prompt, no model
#   scripts/em-batch-nightly.sh --tickets "BUI-956 BUI-957"   # override the selector
#
# Knobs (env, or ~/.config/em-batch-nightly.env):
#   EM_BATCH_NIGHTLY_CAP=8            tickets per night (em-batch caps a batch at ~12)
#   EM_BATCH_NIGHTLY_BUDGET_USD=80    --max-budget-usd for the model run
#   EM_BATCH_NIGHTLY_TIMEOUT=6h       wall clock for the model run (gtimeout syntax)
#   EM_BATCH_NIGHTLY_MODEL=           optional --model override (default: the CLI default)
#
# Permissions: the run loads the user-authored allow rules from the shared
# checkout's .claude/settings.local.json (gitignored, so a fresh worktree has
# none) via --settings, and runs with --permission-prompts none: anything not
# covered is DENIED, never left waiting. A denied step surfaces in the summary
# as a held ticket. The wrapper never edits that file (em-batch §6: never
# self-widen).

set -uo pipefail

REPO="/Users/hsukenooi/Projects/comic-pipeline"          # shared checkout: settings + deploy target
RUN_WT="/Users/hsukenooi/Projects/comic-pipeline-nightly" # the EM's own detached worktree
STATE_DIR="/Users/hsukenooi/.local/state/em-batch-nightly"
LOCK_DIR="$STATE_DIR/lock"
SHARED_DIR="/Users/hsukenooi/.claude/scripts/shared"    # telegram_report.py
ENVFILE="/Users/hsukenooi/.config/tasks-to-linear.env"  # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
KNOBS="/Users/hsukenooi/.config/em-batch-nightly.env"
CLAUDE="/Users/hsukenooi/.local/bin/claude"
TITLE="Nightly comics run"
LOG_PATH="/Users/hsukenooi/Library/Logs/em-batch-nightly.log"

# launchd hands a job a near-empty PATH. claude lives in ~/.local/bin;
# uv/gh/linear/gtimeout in /opt/homebrew/bin.
export PATH="/Users/hsukenooi/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Downgrade the linear-guard Done gate from a prompt to a reminder (BUI-878);
# the skill's own Done-when gate still applies inside the run.
export LINEAR_GUARD_UNATTENDED=1
# Headless claude keeps the process alive for in-flight background subagents only
# up to this ceiling (default 10 min), then kills them and returns the EM's last
# message as the "summary". A wave of ticket agents runs far longer than that, so
# raise it past the run's own wall clock; gtimeout below stays the real bound.
# (2026-09-22 12:23 run: three agents killed at 10 min, $13 for nothing.)
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=86400000

DRY_RUN=0
TICKETS_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --tickets) shift; TICKETS_OVERRIDE="${1:-}" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

for f in "$ENVFILE" "$KNOBS"; do
  if [ -f "$f" ]; then set -a; source "$f"; set +a; fi
done
CAP="${EM_BATCH_NIGHTLY_CAP:-8}"
BUDGET="${EM_BATCH_NIGHTLY_BUDGET_USD:-80}"
TIMEOUT="${EM_BATCH_NIGHTLY_TIMEOUT:-6h}"
MODEL="${EM_BATCH_NIGHTLY_MODEL:-}"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"$TITLE\"" >/dev/null 2>&1; }

# Telegram, falling back to a macOS notification inside telegram_report.py.
tg_send() {  # $1 = message
  python3 - "$1" "$TITLE" "$LOG_PATH" "$SHARED_DIR" <<'PY'
import sys
from pathlib import Path
msg, title, log, shared = sys.argv[1:5]
sys.path.insert(0, shared)
try:
    import telegram_report
except Exception as e:  # shared helper missing: say so on stderr, keep going
    print("warning: telegram_report unavailable (%s)" % e, file=sys.stderr)
    sys.exit(1)
first = msg.strip().splitlines()[0] if msg.strip() else title
sys.exit(telegram_report.send(msg, summary=first[:200], title=title, log=Path(log), spool=False))
PY
}

fatal() {
  local msg="$1"
  echo "FATAL: $msg" >&2
  python3 "$SHARED_DIR/telegram_report.py" send-fatal "$TITLE" "$msg" "$LOG_PATH" || notify "$msg"
  cleanup
  exit 1
}

cleanup() {
  if [ -d "$RUN_WT" ]; then
    git -C "$REPO" worktree remove -f -f "$RUN_WT" >/dev/null 2>&1 || rm -rf "$RUN_WT"
  fi
  git -C "$REPO" worktree prune >/dev/null 2>&1
  rm -rf "$LOCK_DIR"
}

echo "=== $(date '+%Y-%m-%d %H:%M:%S') $TITLE starting (cap=$CAP budget=\$$BUDGET timeout=$TIMEOUT dry_run=$DRY_RUN) ==="
mkdir -p "$STATE_DIR"

# --- Lock: one run at a time -------------------------------------------------
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  oldpid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "another run (pid $oldpid) is still going; exiting"
    exit 0
  fi
  echo "stale lock (pid ${oldpid:-?}); taking it over"
  rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR" || exit 1
fi
echo $$ > "$LOCK_DIR/pid"
trap cleanup EXIT

# --- Preflight ------------------------------------------------------------------
[ -x "$CLAUDE" ] || fatal "claude not found at $CLAUDE"
[ -r "$REPO/.claude/settings.local.json" ] || fatal "no $REPO/.claude/settings.local.json (the allow rules live there)"
gh auth status >/dev/null 2>&1 || fatal "gh is not logged in"
git -C "$REPO" fetch -q origin || fatal "git fetch failed"
if [ -d "$RUN_WT" ]; then
  echo "leftover run worktree found; removing it"
  git -C "$REPO" worktree remove -f -f "$RUN_WT" >/dev/null 2>&1 || rm -rf "$RUN_WT"
  git -C "$REPO" worktree prune
fi

# --- Select tickets -------------------------------------------------------------
RUN_DIR="$STATE_DIR/runs/$(date '+%Y-%m-%d_%H%M')"
mkdir -p "$RUN_DIR"
if [ -n "$TICKETS_OVERRIDE" ]; then
  TICKETS="$TICKETS_OVERRIDE"
else
  TICKETS="$(python3 "$REPO/scripts/em-batch-nightly-select.py" --cap "$CAP" | tr '\n' ' ' | sed 's/ *$//')" \
    || fatal "ticket selection failed (see $LOG_PATH)"
fi
echo "tickets: ${TICKETS:-<none>}"
printf '%s\n' "$TICKETS" > "$RUN_DIR/tickets.txt"
if [ -z "$TICKETS" ]; then
  echo "nothing to pick up; exiting"
  [ "$DRY_RUN" -eq 1 ] || tg_send "$TITLE, $(date '+%a %d %b'): nothing to pick up. No unassigned comics tickets in Today, Soon, or Someday."
  exit 0
fi

# --- Worktree ---------------------------------------------------------------------
git -C "$REPO" worktree add -q --detach "$RUN_WT" origin/main || fatal "could not create $RUN_WT"
echo "worktree: $RUN_WT at $(git -C "$RUN_WT" rev-parse --short HEAD)"

# --- Prompt ------------------------------------------------------------------------
PROMPT="/em-batch mode:autonomous $TICKETS

Run context from scripts/em-batch-nightly.sh (BUI-972):
- Your cwd is a fresh detached worktree at origin/main. Do every git write from it or from agent worktrees, never in the shared checkout.
- Shared checkout, for deploy only: $REPO (see the profile's Deploy model for the on-main-and-clean rule).
- Run dir for wave-plan.md and handoff.md: $RUN_DIR
- Budget: about \$$BUDGET and $TIMEOUT of wall clock. Prefer finishing fewer tickets cleanly over starting all of them.
- Your FINAL message is the user summary defined in the skill's mode:autonomous section (20 lines max, written for the person who uses the pipeline, not a code reader). It is posted to Telegram as-is."
printf '%s\n' "$PROMPT" > "$RUN_DIR/prompt.md"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--- dry run: prompt follows ---"; cat "$RUN_DIR/prompt.md"; echo "--- dry run: no model run ---"
  exit 0
fi

# --- Model run -----------------------------------------------------------------------
model_cmd=(gtimeout "$TIMEOUT" "$CLAUDE" -p "$PROMPT"
  --permission-mode auto
  --permission-prompts none
  --settings "$REPO/.claude/settings.local.json"
  --add-dir "$REPO" --add-dir "$STATE_DIR"
  --autocompact auto
  --output-format json
  --max-budget-usd "$BUDGET")
[ -n "$MODEL" ] && model_cmd+=(--model "$MODEL")

started=$(date +%s)
( cd "$RUN_WT" && "${model_cmd[@]}" ) > "$RUN_DIR/result.json" 2> "$RUN_DIR/stderr.log"
mstatus=$?
mins=$(( ($(date +%s) - started) / 60 ))
echo "model run exited $mstatus after ${mins}m"

SUMMARY="$(python3 - "$RUN_DIR/result.json" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print(doc.get("result", "").strip())
print("\nCost: $%.2f, %s turns." % (doc.get("total_cost_usd", 0.0), doc.get("num_turns", "?")))
PY
)"
if [ -z "$SUMMARY" ]; then
  if [ "$mstatus" -eq 124 ]; then
    fatal "Model run hit the $TIMEOUT wall clock with no summary. Check open PRs and In Progress tickets by hand. Log: $LOG_PATH, run dir: $RUN_DIR"
  fi
  fatal "Model run exited $mstatus with no summary. Check open PRs and In Progress tickets by hand. Log: $LOG_PATH, run dir: $RUN_DIR"
fi
[ "$mstatus" -eq 0 ] || SUMMARY="$SUMMARY
(The run exited with status $mstatus; the summary above may be partial. Run dir: $RUN_DIR)"
printf '%s\n' "$SUMMARY" > "$RUN_DIR/summary.md"

# --- Report ------------------------------------------------------------------------
tg_send "$TITLE, $(date '+%a %d %b'):
$SUMMARY"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') exit 0 ==="
exit 0
