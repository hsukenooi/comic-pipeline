#!/bin/bash
# Headless /comic:grade benchmark runner (BUI-1107). Runs the orchestrator with the lean tool set
# (docs/reference/headless-grader-runs.md) and sums orchestrator + seat usage.
#
# Usage: scripts/grade-benchmark.sh <label> [model] [item ids...]
#   label  output prefix; files land in $BENCH_DIR/<label>.{json,stderr,log} and $BENCH_DIR/<label>-seats/
#   model  default claude-fable-5-1
#   ids    default: the three-listing benchmark (336816244968 267800242816 298630965109)
# Env: BENCH_DIR (default ~/comic-grader-fixtures/efficiency-loop/bench), ENV_FILE (default
#      <repo>/apps/ebay/.env, sourced if present), SEAT_ROOT (default /tmp/comic-grading).
# Runs `claude -p` from the repo that holds this script, so it loads that checkout's grade.md.
# No prompt suffix and no --allowedTools: the orchestrator must run grade-seats in the foreground
# on its own (grade.md Step 2).
set -u
LABEL=${1:?usage: grade-benchmark.sh <label> [model] [item ids...]}
MODEL=${2:-claude-fable-5-1}
shift $(( $# < 2 ? $# : 2 ))
IDS=${*:-"336816244968 267800242816 298630965109"}
REPO=$(cd "$(dirname "$0")/.." && pwd)
B=${BENCH_DIR:-$HOME/comic-grader-fixtures/efficiency-loop/bench}
SEAT_ROOT=${SEAT_ROOT:-/tmp/comic-grading}
ENV_FILE=${ENV_FILE:-$REPO/apps/ebay/.env}
mkdir -p "$B/$LABEL-seats"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
EMPTY=$B/empty-mcp.json
[ -f "$EMPTY" ] || echo '{"mcpServers":{}}' > "$EMPTY"
cd "$REPO" || exit 1
echo "start $(date +%T) model=$MODEL head=$(git rev-parse --short HEAD) ids=$IDS" > "$B/$LABEL.log"
claude -p "/comic:grade $IDS" --model "$MODEL" --tools Read,Bash,Write \
  --strict-mcp-config --mcp-config "$EMPTY" --output-format json \
  > "$B/$LABEL.json" 2> "$B/$LABEL.stderr"
echo "exit $? $(date +%T)" >> "$B/$LABEL.log"
# The orchestrator result names its work dir (<SEAT_ROOT>/run-*); collect that run's seat envelopes.
RUN=$(python3 "$REPO/scripts/grade-benchmark-sum.py" --run-dir "$B/$LABEL.json" "$SEAT_ROOT")
echo "run dir: ${RUN:-unknown}" >> "$B/$LABEL.log"
if [ -n "$RUN" ]; then find "$RUN" -maxdepth 3 -name 'seat-*.json' -exec cp {} "$B/$LABEL-seats/" \; ; fi
python3 "$REPO/scripts/grade-benchmark-sum.py" "$B/$LABEL.json" "$B/$LABEL-seats"
