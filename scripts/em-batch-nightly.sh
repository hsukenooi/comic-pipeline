#!/bin/bash
# Nightly, fully unattended /em-batch over the open `comics` tickets (BUI-972).
#
# Fired by the LaunchAgent com.comics.em-batch-nightly (scripts/launchd/) at
# 01:00 on the Mac Mini. One run: pick tickets -> fresh detached worktree ->
# `claude -p "/em-batch mode:autonomous ..."` -> save the run's summary to the
# run dir. Nothing is pushed to the user: the Linear tickets carry the per-ticket
# closing comments, and this log plus summary.md carry the run record (a fatal
# error still raises a macOS notification). The model run does the engineering
# (implement, review, CI, merge, deploy, close); this file only frames it.
#
# If the EM's last message is not the summary (it ended its turn waiting on a
# background command; headless claude resumes it only for subagent completions),
# the wrapper resumes the same session with a nudge, up to MAX_RESUMES times.
# To finish a run by hand later: scripts/em-batch-nightly.sh --resume-run <run id>
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
RESUME_RUN=""   # --resume-run YYYY-MM-DD_HHMM: pick up an earlier run whose EM stopped early
MAX_RESUMES=2
while [ $# -gt 0 ]; do
  case "$1" in
    --resume-run) RESUME_RUN="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1 ;;
    --tickets) shift; TICKETS_OVERRIDE="${1:-}" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

for f in "$KNOBS"; do
  if [ -f "$f" ]; then set -a; source "$f"; set +a; fi
done
CAP="${EM_BATCH_NIGHTLY_CAP:-8}"
BUDGET="${EM_BATCH_NIGHTLY_BUDGET_USD:-80}"
TIMEOUT="${EM_BATCH_NIGHTLY_TIMEOUT:-6h}"
MODEL="${EM_BATCH_NIGHTLY_MODEL:-}"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"$TITLE\"" >/dev/null 2>&1; }

fatal() {
  local msg="$1"
  echo "FATAL: $msg" >&2
  notify "$msg"
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
[ -n "$RESUME_RUN" ] || mkdir -p "$RUN_DIR"   # a resume reuses its own run dir
if [ -n "$RESUME_RUN" ]; then
  RUN_DIR="$STATE_DIR/runs/$RESUME_RUN"
  [ -f "$RUN_DIR/tickets.txt" ] || fatal "no run to resume at $RUN_DIR"
  TICKETS="$(cat "$RUN_DIR/tickets.txt")"
  echo "resuming run $RESUME_RUN"
elif [ -n "$TICKETS_OVERRIDE" ]; then
  TICKETS="$TICKETS_OVERRIDE"
else
  TICKETS="$(python3 "$REPO/scripts/em-batch-nightly-select.py" --cap "$CAP" | tr '\n' ' ' | sed 's/ *$//')" \
    || fatal "ticket selection failed (see $LOG_PATH)"
fi
echo "tickets: ${TICKETS:-<none>}"
printf '%s\n' "$TICKETS" > "$RUN_DIR/tickets.txt"
if [ -z "$TICKETS" ]; then
  echo "nothing to pick up; exiting"
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
- A hold (a ticket that needs the user) = a Linear comment naming the one decision + \`linear issue update ID -a hsukenooi\`, state unchanged. Never set Blocked; that state is the user's own.
- In handoff.md record every agent's usage from its completion notification per ticket: model, output tokens, cache-read tokens, tool uses, duration, review depth run. Tokens, never dollars.
- Headless: only a subagent's completion resumes you. A background Bash command, a Monitor, or a CI watch never wakes you, so never end your turn while waiting on one. Wait in the foreground: a normal Bash call such as \`gh pr checks N --watch\` with a long tool timeout, repeated as needed.
- Your FINAL message is the user summary defined in the skill's mode:autonomous section (20 lines max, written for the person who uses the pipeline, not a code reader). It is saved as summary.md in the run dir and its first line is the wrapper's completion check; nothing is pushed to the user, who reads the Linear tickets, so put the per-ticket outcome in each ticket's closing comment."
printf '%s\n' "$PROMPT" > "$RUN_DIR/prompt.md"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--- dry run: prompt follows ---"; cat "$RUN_DIR/prompt.md"; echo "--- dry run: no model run ---"
  exit 0
fi

# --- Model run -----------------------------------------------------------------------
base_flags=(--permission-mode auto
  --permission-prompts none
  --settings "$REPO/.claude/settings.local.json"
  --add-dir "$REPO" --add-dir "$STATE_DIR"
  --autocompact auto
  --output-format json
  --max-budget-usd "$BUDGET")
[ -n "$MODEL" ] && base_flags+=(--model "$MODEL")

case "$TIMEOUT" in
  *h) TIMEOUT_SECS=$(( ${TIMEOUT%h} * 3600 )) ;;
  *m) TIMEOUT_SECS=$(( ${TIMEOUT%m} * 60 )) ;;
  *)  TIMEOUT_SECS=$(( ${TIMEOUT%s} )) ;;
esac

# run_model OUT [claude args...]: one headless segment, bounded by what is left of TIMEOUT.
run_model() {
  local out="$1"; shift
  local left=$(( TIMEOUT_SECS - ($(date +%s) - started) ))
  [ "$left" -gt 60 ] || return 124
  ( cd "$RUN_WT" && gtimeout "$left" "$CLAUDE" "$@" "${base_flags[@]}" ) > "$out" 2>> "$RUN_DIR/stderr.log"
}

# The contract for "finished": the result starts with the summary's first line.
summary_done() {
  python3 - "$1" <<'EOPY'
import json, re, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if re.match(r"\s*\d+ tickets? picked", doc.get("result", "")) else 1)
EOPY
}

session_of() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("session_id",""))' "$1" 2>/dev/null
}

NUDGE="Resumed by scripts/em-batch-nightly.sh: your last message was not the final user summary. In headless mode only a subagent's completion resumes you; a background Bash command, a Monitor, or a CI watch never does, so never end your turn while waiting on one. Wait in the foreground (a normal Bash \`gh pr checks N --watch\` call with a long timeout, repeated). Re-read $RUN_DIR/handoff.md and wave-plan.md, verify every PR and ticket's current state with gh and linear rather than trusting the notes, then finish the batch: merge what is green, close, deploy, compound if warranted, and end with the FINAL summary in the required shape (first line: <n> tickets picked, <d> done, <h> held, <s> skipped)."

started=$(date +%s)
attempt=0
if [ -n "$RESUME_RUN" ]; then
  RESULT_FILE="$(ls -t "$RUN_DIR"/result*.json 2>/dev/null | head -1)"
  [ -n "$RESULT_FILE" ] || fatal "no result json in $RUN_DIR to resume from"
  mstatus=0
else
  RESULT_FILE="$RUN_DIR/result.json"
  run_model "$RESULT_FILE" -p "$PROMPT"
  mstatus=$?
fi
while [ "$mstatus" -eq 0 ] && [ "$attempt" -lt "$MAX_RESUMES" ] && ! summary_done "$RESULT_FILE"; do
  SESSION="$(session_of "$RESULT_FILE")"
  [ -n "$SESSION" ] || break
  attempt=$(( attempt + 1 ))
  echo "EM stopped without the summary; resuming session $SESSION (attempt $attempt of $MAX_RESUMES)"
  RESULT_FILE="$RUN_DIR/result.resume$attempt.json"
  run_model "$RESULT_FILE" --resume "$SESSION" -p "$NUDGE"
  mstatus=$?
done
mins=$(( ($(date +%s) - started) / 60 ))
echo "model run exited $mstatus after ${mins}m ($attempt resumes)"

SUMMARY="$(python3 - "$RESULT_FILE" "$RUN_DIR" <<'EOPY'
import glob, json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print(doc.get("result", "").strip())
# modelUsage in a resumed segment is cumulative for the session (checked
# 2026-09-22: the first segment lines carried over unchanged), so the last file
# is the run total; only the turn counts add up. Tokens, not dollars: the runs
# draw on a subscription, so a dollar figure is not money spent. No apostrophes in here: bash 3.2 cannot
# parse a quote inside a heredoc inside a command substitution.
turns = 0
for f in sorted(glob.glob(sys.argv[2] + "/result*.json")):
    try:
        turns += json.load(open(f)).get("num_turns", 0)
    except Exception:
        pass
mu = doc.get("modelUsage", {}) or {}
out = sum(v.get("outputTokens", 0) for v in mu.values())
rd = sum(v.get("cacheReadInputTokens", 0) for v in mu.values())
wr = sum(v.get("cacheCreationInputTokens", 0) + v.get("inputTokens", 0) for v in mu.values())
per = ", ".join("%s %dk out / %.1fM read" % (m.split("-")[1], v.get("outputTokens", 0) / 1000, v.get("cacheReadInputTokens", 0) / 1e6) for m, v in mu.items())
print("\nUsage: %dk output, %.1fM cache read, %.2fM cache write tokens, %s turns (%s)." % (out / 1000, rd / 1e6, wr / 1e6, turns, per))
EOPY
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
printf '%s\n' "$SUMMARY"
echo "summary saved to $RUN_DIR/summary.md"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') exit 0 ==="
exit 0
