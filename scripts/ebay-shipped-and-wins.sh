#!/usr/bin/env bash
# BUI-1267: the every-3-days job (com.comics.ebay-shipped-submit) runs both
# unattended halves of the post-win workflow:
#
#   1. ebay-shipped --auto-submit   shipped orders -> EZShip (BUI-1238)
#   2. gixen record-wins            new Gixen wins -> the collection
#
# Each step runs whatever the other did, and each pings its own heartbeat
# (ebay-shipped-submit, record-wins), so one failing never hides the other.
# The script exits non-zero if either step failed.
set -u

status=0

# BUI-1385: a failing step also posts to Telegram. The healthchecks.io check
# this job pings is shared with stale jobs, so it stays down and never emails
# on a new failure. Only the TELEGRAM_* lines are read from the env file.
TELEGRAM_ENV_FILE="${TELEGRAM_ENV_FILE:-$HOME/.config/tasks-to-linear.env}"
TELEGRAM_REPORT="${TELEGRAM_REPORT:-$HOME/.claude/scripts/shared/telegram_report.py}"
LOG_FILE="${LOG_FILE:-$HOME/.comics-server/ebay-shipped-submit.log}"

alert() {
  # alert <step> <exit code> <impact> <fix>
  local line
  if [ -f "$TELEGRAM_ENV_FILE" ]; then
    while IFS= read -r line; do
      case "$line" in
        TELEGRAM_BOT_TOKEN=*|TELEGRAM_CHAT_ID=*) export "${line//\"/}" ;;
      esac
    done < "$TELEGRAM_ENV_FILE"
  fi
  if [ -f "$TELEGRAM_REPORT" ]; then
    /usr/bin/python3 "$TELEGRAM_REPORT" send-fatal "Comics shipped-submit" \
      "$1 exited $2" "$LOG_FILE" --impact "$3" --fix "$4" \
      || echo "telegram alert failed for $1" >&2
  else
    echo "telegram alert skipped: $TELEGRAM_REPORT not found" >&2
  fi
}

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ebay-shipped --auto-submit"
ebay-shipped --auto-submit || {
  rc=$?; echo "ebay-shipped exited $rc" >&2; status=1
  if [ "$rc" -eq 3 ]; then
    alert "ebay-shipped" "$rc" "Won orders stop reaching EZShip." \
      "Log in to ezbuy in the automation Chrome, then copy its cookies into the ezship config."
  else
    alert "ebay-shipped" "$rc" "Won orders stop reaching EZShip." "Read the log and rerun ebay-shipped --auto-submit."
  fi
}

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) gixen record-wins"
gixen record-wins || {
  rc=$?; echo "gixen record-wins exited $rc" >&2; status=1
  alert "gixen record-wins" "$rc" "New wins stop reaching the collection." "Run /comic:collection-add."
}

exit "$status"
