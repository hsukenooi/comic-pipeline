#!/usr/bin/env bash
# BUI-1299: shared launcher for headless `claude -p` grading seats. SOURCE this file:
#   source "$REPO/scripts/headless-seat.sh"
#   headless_seat <system-prompt-file> <max-turns> [mcp-config] [extra claude args...] < job.txt > out.json
# Sourcing defines functions only; it never cd's the caller's shell. headless_seat runs claude
# in a subshell whose cwd is $SEAT_CWD (default /tmp/comic-grading-seat, outside any repo), so
# no project CLAUDE.md / MEMORY.md loads, and with user settings (hooks, enabled plugins) off.
# Flags mirrored in apps/ebay/src/grade_seats.py (SEAT_SUPPRESS_FLAGS); a test pins parity.
# Model: $SEAT_MODEL (default claude-fable-5-1). Needs no write under ~/.claude.

SEAT_SUPPRESS_FLAGS=(--setting-sources local --settings '{"disableAllHooks":true}')

headless_seat() {
  local prompt="$1" turns="$2" mcp="${3:-}"
  shift 2
  [ $# -gt 0 ] && shift
  local cwd="${SEAT_CWD:-/tmp/comic-grading-seat}"
  mkdir -p "$cwd" || return 1
  if [ -z "$mcp" ]; then
    mcp="$cwd/empty-mcp.json"
    echo '{"mcpServers":{}}' > "$mcp"
  fi
  (
    cd "$cwd" || exit 1
    exec claude -p --model "${SEAT_MODEL:-claude-fable-5-1}" \
      --system-prompt-file "$prompt" \
      --tools Read,Bash --strict-mcp-config --mcp-config "$mcp" \
      "${SEAT_SUPPRESS_FLAGS[@]}" \
      --max-turns "$turns" --output-format json "$@"
  )
}
