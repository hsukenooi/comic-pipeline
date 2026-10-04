#!/usr/bin/env bash
# Runs the five required CI jobs from .github/workflows/ci.yml locally, in CI order:
# workspace, apps-python, lint, solutions-lint, ezship. Skips the non-required typecheck job.
# Fails fast with a non-zero exit and prints one summary line per gate. Run from the repo root.
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
Usage: scripts/gates.sh

Runs the required CI jobs (workspace, apps-python, lint, solutions-lint, ezship) and
prints one "PASS <gate> - <counts>" line per gate. Exits non-zero on the first failure.
Not run: the non-required typecheck job (see .claude/em-batch.md, Local Gates).
Needs uv, uvx, shellcheck, and node on PATH.
USAGE
  exit 0
fi

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

# gate <name> <dir> <cmd...>: run in <dir>, print the pytest summary (or last line) on success.
gate() {
  local name="$1" dir="$2"
  shift 2
  if ! (cd "$dir" && "$@") >"$LOG" 2>&1; then
    cat "$LOG"
    echo "FAIL $name (in $dir: $*)"
    exit 1
  fi
  local summary
  summary="$(grep -E '[0-9]+ passed|OK|clean|All checks passed|no findings' "$LOG" | tail -1 || true)"
  echo "PASS $name - ${summary:-ok}"
}

command -v shellcheck >/dev/null || { echo "FAIL shellcheck missing (CI installs it; run: brew install shellcheck)"; exit 1; }

# Job: workspace
gate "workspace: uv sync" . uv sync --all-packages
gate "workspace: imports" . uv run python -c "import server.main; import gixen.plugins; print('workspace imports OK')"
gate "workspace: gixen console script" . bash -c 'uv run gixen --help >/dev/null && echo "gixen console script OK"'
gate "workspace: overlay coupling" . uv run python -c "import gixen_overlay.routes; from server.main import _ensure_fresh_sync, iso_to_relative, _spawn_fallback_task; from server.db import get_bid_by_item_id; print('overlay -> gixen-cli coupling OK')"
gate "workspace: plugin.py parses" . uv run python -c "import ast; ast.parse(open('plugins/gixen-overlay/src/gixen_overlay/plugin.py').read()); print('plugin.py parses OK')"
gate "workspace: pytest gixen-cli" packages/gixen-cli uv run pytest -m "not integration"
gate "workspace: pytest locg-cli" packages/locg-cli uv run pytest
gate "workspace: pytest gixen-overlay" plugins/gixen-overlay uv run pytest

# Job: apps-python
gate "apps-python: pytest apps/ebay" apps/ebay uv run --with pytest pytest
gate "apps-python: pytest apps/fmv" apps/fmv uv run --with pytest pytest

# Job: lint
gate "lint: ruff" . uvx ruff check .

# Job: solutions-lint
gate "solutions-lint: self-test" . ./scripts/solutions-lint --self-test
gate "solutions-lint: lint" . ./scripts/solutions-lint
gate "solutions-lint: premise-check selftest" . ./scripts/premise-check --selftest

# Job: ezship (CI uses Node 20 and a clean `npm ci`, so stale deps can't hide a lockfile drift)
gate "ezship: npm ci" apps/ezship npm ci
gate "ezship: tsc" apps/ezship npx tsc --noEmit
gate "ezship: npm test" apps/ezship npm test

echo "ALL GATES PASSED"
