#!/usr/bin/env bash
#
# scripts/deploy.sh — one-command deploy for the Mac Mini (BUI-612).
#
# Runs the full documented deploy ritual idempotently, then VERIFIES it
# actually landed: every uv-tool-installed console script's `--version`
# output and the comics server's `/health` endpoint must report the git SHA
# that is currently checked out (merged HEAD) — or this script fails loudly
# instead of leaving stale code running silently.
#
# Why this exists: "deployed code isn't merged code" has bitten this repo
# three times, each defended only by prose someone has to remember to reread
# at the worst possible moment — a post-merge context switch:
#   - BUI-365: a uv-tool-installed `gixen` predated a merged commit that
#     added record_win_prep.py -> ModuleNotFoundError on first post-merge use.
#   - BUI-455: `uv tool install --force` ALONE silently serves a STALE
#     CACHED wheel when the package's declared version is unchanged (uv
#     keys its wheel cache on name+version, not content) — `--no-cache` is
#     required to actually pick up new source. This is why every install
#     below passes BOTH --force AND --no-cache; dropping either reopens
#     BUI-455.
#   - BUI-377: the comics server runs via launchd out of the *workspace*
#     `.venv`, which `uv tool install` never touches — it needs its own
#     `uv sync --all-packages` + `launchctl kickstart` step.
#
# Two different SHA-attestation mechanisms feed the assertion below, because
# not everything below is built (or installed) the same way:
#   - apps/fmv (BUI-305), apps/ebay (BUI-314), and packages/locg-cli
#     (BUI-612) each stamp their own git SHA into the wheel at BUILD time
#     (see their hatch_build.py) and `--version` reports it.
#   - packages/gixen-cli's `gixen` console script and the comics server
#     BOTH read git HEAD directly at runtime instead (see `_git` in
#     packages/gixen-cli/cli.py and `_resolve_git_sha` in
#     packages/gixen-cli/server/main.py) rather than via a build-time
#     stamp: the comics server runs from the editable WORKSPACE `.venv`
#     (source, not a frozen wheel — see BUI-377), and `gixen` is always
#     `uv tool install --editable`ed (needed for cli.py's file-relative
#     load_dotenv() to find packages/gixen-cli/.env), which likewise makes
#     it source-backed rather than a frozen copy — and unlike hatchling,
#     setuptools' editable-install strategy has no mechanism to also
#     materialize a separately-generated build-time stamp file, so a
#     build-time approach wouldn't even be reachable there.
#
# Usage: ./scripts/deploy.sh   (run on the Mac Mini, from this checkout,
# after merging to main and pulling)
#
# Out of scope (BUI-612): unattended auto-deploy (no rollback story yet) and
# the resurrect.sh restore path (a separate later issue) — this script is
# meant to be run by a human, once, right after a merge, not on a schedule.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed. See https://docs.astral.sh/uv/ to install it." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required to parse the comics server's /health response." >&2
  exit 1
fi

# Resolve/health-gate the comics server the same canonical way every
# /comic:* skill does (BUI-172/BUI-220), rather than hand-rolling a curl
# call that could silently hit an empty host (the BUI-352 trap). Resolved
# once, up front, and reused below for both the post-kickstart readiness
# poll and the final SHA assertion.
# shellcheck source=./comics-server.sh
source "$REPO_ROOT/scripts/comics-server.sh"
comics_resolve_server || exit 1

MERGED_SHA="$(git rev-parse --short HEAD)"

# A dirty working tree makes the whole SHA assertion below meaningless: it
# only proves the checked-out COMMIT is live, not any uncommitted edits
# sitting on top of it. Refuse rather than print a falsely reassuring "OK".
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree has uncommitted changes -- refusing to deploy from a dirty tree." >&2
  echo "       Commit, stash, or discard these before running deploy.sh:" >&2
  git status --short >&2
  exit 1
fi

echo "Deploying merged HEAD: $MERGED_SHA"
echo

# --- 1. uv-tool-installed console scripts ---
# Flags/paths mirror scripts/install.sh's per-package choices (--editable
# where the package relies on a file-relative load_dotenv(), see install.sh);
# --force --no-cache replaces install.sh's --reinstall (BUI-455: plain
# --force alone can silently reinstall a stale cached wheel when the
# package's version string is unchanged — --no-cache forces a real rebuild).
echo "Installing ebay-tools (editable)..."
uv tool install --force --no-cache --editable "$REPO_ROOT/apps/ebay"

echo "Installing comic-fmv..."
uv tool install --force --no-cache "$REPO_ROOT/apps/fmv"

echo "Installing gixen (packages/gixen-cli, editable)..."
uv tool install --force --no-cache --editable "$REPO_ROOT/packages/gixen-cli"

echo "Installing locg (packages/locg-cli, editable)..."
uv tool install --force --no-cache --editable "$REPO_ROOT/packages/locg-cli"
echo

# --- 2. the comics server: editable workspace .venv, not a uv-tool wheel
# (BUI-377) — needs its own refresh + restart, launchd does not pick up a
# source change on its own. ---
echo "Syncing workspace env (packages/* + plugins/*)..."
uv sync --all-packages
echo

LAUNCHD_LABEL="com.comics.server"
echo "Restarting the comics server ($LAUNCHD_LABEL)..."
if ! launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"; then
  echo "error: launchctl kickstart failed for $LAUNCHD_LABEL." >&2
  echo "       Is this running on the Mac Mini, with the LaunchAgent loaded?" >&2
  echo "       See packages/gixen-cli/server/install.sh." >&2
  exit 1
fi
# Poll briefly for the freshly-kickstarted process to come back up, rather
# than a flat sleep — a slow-starting process shouldn't produce a false FAIL
# below just because the guess was too short.
server_up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf -m 2 "$COMICS_SERVER_URL/health" >/dev/null 2>&1; then
    server_up=1
    break
  fi
  sleep 1
done
if [ "$server_up" -eq 0 ]; then
  echo "warning: comics server did not answer /health within 10s of kickstart; checking anyway." >&2
fi
echo

# --- 3. assert every deployed component == merged HEAD, failing loudly ---
fail=0

check_cli_sha() {
  local label="$1" cmd="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "FAIL  $label: '$cmd' not found on PATH" >&2
    fail=1
    return
  fi
  local out sha
  out="$("$cmd" --version 2>&1)" || {
    echo "FAIL  $label: '$cmd --version' errored: $out" >&2
    fail=1
    return
  }
  sha="$(printf '%s' "$out" | grep -oE 'git [0-9a-f]+' | head -1 | awk '{print $2}')"
  if [ -z "$sha" ]; then
    echo "FAIL  $label: could not parse a git SHA out of: $out" >&2
    fail=1
  elif [ "$sha" != "$MERGED_SHA" ]; then
    echo "FAIL  $label: deployed git $sha != merged HEAD $MERGED_SHA ($out)" >&2
    fail=1
  else
    echo "OK    $label: git $sha"
  fi
}

check_cli_sha "gixen"      gixen
check_cli_sha "locg"       locg
check_cli_sha "comic-fmv"  comic-fmv
check_cli_sha "ebay-fetch" ebay-fetch

# COMICS_SERVER_URL was already resolved above (before the install/sync/
# kickstart steps); re-health-gate it now that the server has been restarted.
if comics_health_gate; then
  server_health="$(comics_get "$COMICS_SERVER_URL/health")" || {
    echo "FAIL  comics server: GET $COMICS_SERVER_URL/health errored" >&2
    fail=1
    server_health=""
  }
else
  echo "FAIL  comics server: unreachable at $COMICS_SERVER_URL" >&2
  fail=1
  server_health=""
fi
if [ -n "$server_health" ]; then
  server_sha="$(printf '%s' "$server_health" | jq -r '.git_sha // empty')"
  if [ -z "$server_sha" ]; then
    echo "FAIL  comics server: no git_sha in /health response: $server_health" >&2
    fail=1
  elif [ "$server_sha" != "$MERGED_SHA" ]; then
    echo "FAIL  comics server: running git $server_sha != merged HEAD $MERGED_SHA" >&2
    fail=1
  else
    echo "OK    comics server: git $server_sha"
  fi
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "DEPLOY FAILED: not every component is running merged HEAD ($MERGED_SHA)." >&2
  exit 1
fi
echo "Deploy verified: every component is running merged HEAD ($MERGED_SHA)."
