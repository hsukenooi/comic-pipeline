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

load_telegram_env() {
  local line
  if [ -f "$TELEGRAM_ENV_FILE" ]; then
    while IFS= read -r line; do
      case "$line" in
        TELEGRAM_BOT_TOKEN=*|TELEGRAM_CHAT_ID=*) export "${line//\"/}" ;;
      esac
    done < "$TELEGRAM_ENV_FILE"
  fi
}

alert() {
  # alert <step> <exit code> <impact> <fix>
  load_telegram_env
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
rw_out="$(gixen record-wins)"; rc=$?
printf '%s\n' "$rw_out"
if [ "$rc" -ne 0 ]; then
  echo "gixen record-wins exited $rc" >&2; status=1
  alert "gixen record-wins" "$rc" "New wins stop reaching the collection." "Run /comic:collection-add."
else
  # A held win is a clean exit, so alert() never sees it. Without this, held
  # wins only raise a Mac Mini notification and can sit unrecorded for weeks,
  # hiding owned books from the /comic:buy collection check (BUI-1385).
  held="$(printf '%s\n' "$rw_out" | sed -n 's/^record-wins:.* \([0-9][0-9]*\) need review.*/\1/p' | head -1)"
  # BUI-1387: a record-win self-collision holds its item and exits 0.
  collided="$(printf '%s\n' "$rw_out" | sed -n 's/^record-wins: \([0-9][0-9]*\) held (self-collision).*/\1/p' | head -1)"
  held=$(( ${held:-0} + ${collided:-0} ))
  if [ "$held" -gt 0 ]; then
    load_telegram_env
    if [ -f "$TELEGRAM_REPORT" ]; then
      /usr/bin/python3 "$TELEGRAM_REPORT" add-item ACTION "Comics record-wins" \
        "$held won auction(s) are held for review and not in the collection yet." \
        --impact "The collection check will not see them, so a buy run can bid on books you own." \
        --fix "Run /comic:collection-add." --log "$LOG_FILE" --now \
        || echo "telegram held-wins alert failed" >&2
    fi
  fi
fi

exit "$status"
