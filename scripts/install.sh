#!/usr/bin/env bash
#
# Install the comic-pipeline Python CLIs into uv-managed tool environments so
# they work from any directory with no PYTHONPATH workaround.
#
# Installs (uv-managed console scripts into ~/.local/bin — runtime model (a),
# BUI-55: the apps AND the gixen/locg package CLIs live on PATH; the uv workspace
# env is for development/server/tests only, not these user-facing commands):
#   - ebay-tools  -> ebay-fetch, ebay-sold-comps, seller-scan, comic-identify, wishlist-sellers   (apps/ebay)
#   - comic-fmv   -> comic-fmv                                   (apps/fmv)
#   - gixen-cli   -> gixen                                       (packages/gixen-cli, editable)
#   - locg        -> locg                                        (packages/locg-cli, editable)
#
# comic-fmv shells out to the `ebay-sold-comps` console script at runtime, so
# both apps must be installed for the FMV pipeline to work end to end. The
# /comic:* skills invoke `gixen` and `locg` as bare commands (U6/BUI-57).
#
# Background (BUI-27): hand-rolled wrappers were previously dropped into
# /opt/homebrew/bin with a shebang pinned to /opt/homebrew/opt/python@3.14 — an
# interpreter that does not have these modules — so they failed with
# `ModuleNotFoundError`. uv installs to ~/.local/bin, which precedes
# /opt/homebrew/bin on PATH; this script also removes the stale wrappers.
#
# Re-run after merging packages/* changes (BUI-365): a `uv tool install`ed CLI
# is a frozen copy of the source tree at install time — it does NOT pick up
# commits merged into packages/gixen-cli or packages/locg-cli afterward. After
# merging a PR that touches packages/*, re-run this script (or
# `uv tool install --force ./packages/<pkg>`) on every machine that runs these
# CLIs, including the Mac Mini. Incident: the first `gixen add` of the
# 2026-07-16 `/comic:buy` run crashed with `ModuleNotFoundError: No module
# named 'record_win_prep'` — the Mac Mini's installed `gixen` predated the
# BUI-352/353/354 merge (2026-07-14) that added that module — and the
# diagnosing agent burned time before running
# `uv tool install --force ./packages/gixen-cli` and retrying successfully.
#
# After merging overlay/server changes (gixen-cli server/, plugins/gixen-overlay),
# the Mac Mini additionally needs (BUI-377):
#   uv sync --all-packages
#   launchctl kickstart -k gui/$(id -u)/com.comics.server
# (the comics server runs via launchd out of the workspace .venv, which this
# script does NOT refresh; observed: post-merge the running server served
# pre-merge verdicts until sync + kickstart).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed. See https://docs.astral.sh/uv/ to install it." >&2
  exit 1
fi

echo "Installing ebay-tools (ebay-fetch, ebay-sold-comps, seller-scan, comic-identify, wishlist-sellers)..."
# BUI-241: --editable so the file-relative _load_dotenv() in seller_scan.py
# resolves back into the source tree (apps/ebay/.env) regardless of caller cwd;
# secrets stay out of the built wheel.
uv tool install --reinstall --editable "$REPO_ROOT/apps/ebay"

echo "Installing comic-fmv..."
uv tool install --reinstall "$REPO_ROOT/apps/fmv"

# gixen + locg are uv-workspace packages, but the /comic:* skills invoke them as
# bare console scripts on PATH. Installed --editable so the entry-point module
# resolves from the source tree: gixen's cli.py uses a file-relative load_dotenv(),
# so an editable install finds packages/gixen-cli/.env regardless of caller cwd.
echo "Installing gixen (packages/gixen-cli)..."
uv tool install --reinstall --editable "$REPO_ROOT/packages/gixen-cli"

echo "Installing locg (packages/locg-cli)..."
uv tool install --reinstall --editable "$REPO_ROOT/packages/locg-cli"

# Remove stale hand-rolled wrappers pinned to python@3.14. Only delete files we
# positively identify as the broken wrappers, never anything else on PATH.
# (The pre-merge locg install was exactly this python@3.14 wrapper.)
echo "Cleaning up stale wrappers..."
for name in comic-fmv ebay-fetch ebay-sold-comps seller-scan comic-identify wishlist-sellers gixen locg; do
  stale="/opt/homebrew/bin/$name"
  if [ -f "$stale" ] && grep -q "python@3.14" "$stale" 2>/dev/null; then
    echo "  removing stale $stale"
    rm -f "$stale"
  fi
done

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
echo
echo "Done. CLIs installed via uv into $bin_dir:"
for name in comic-fmv ebay-sold-comps ebay-fetch seller-scan comic-identify wishlist-sellers gixen locg; do
  printf '  %-16s -> %s\n' "$name" "$(command -v "$name" 2>/dev/null || echo 'NOT ON PATH — add '"$bin_dir"' to PATH')"
done
