#!/usr/bin/env bash
#
# Install the comic-pipeline Python CLIs into uv-managed tool environments so
# they work from any directory with no PYTHONPATH workaround.
#
# Installs:
#   - ebay-tools  -> ebay-fetch, ebay-sold-comps, seller-scan   (apps/ebay)
#   - comic-fmv   -> comic-fmv                                   (apps/fmv)
#
# comic-fmv shells out to the `ebay-sold-comps` console script at runtime, so
# both apps must be installed for the FMV pipeline to work end to end.
#
# Background (BUI-27): hand-rolled wrappers were previously dropped into
# /opt/homebrew/bin with a shebang pinned to /opt/homebrew/opt/python@3.14 — an
# interpreter that does not have these modules — so they failed with
# `ModuleNotFoundError`. uv installs to ~/.local/bin, which precedes
# /opt/homebrew/bin on PATH; this script also removes the stale wrappers.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed. See https://docs.astral.sh/uv/ to install it." >&2
  exit 1
fi

echo "Installing ebay-tools (ebay-fetch, ebay-sold-comps, seller-scan)..."
uv tool install --reinstall "$REPO_ROOT/apps/ebay"

echo "Installing comic-fmv..."
uv tool install --reinstall "$REPO_ROOT/apps/fmv"

# Remove stale hand-rolled wrappers pinned to python@3.14. Only delete files we
# positively identify as the broken wrappers, never anything else on PATH.
echo "Cleaning up stale wrappers..."
for name in comic-fmv ebay-fetch ebay-sold-comps seller-scan; do
  stale="/opt/homebrew/bin/$name"
  if [ -f "$stale" ] && grep -q "python@3.14" "$stale" 2>/dev/null; then
    echo "  removing stale $stale"
    rm -f "$stale"
  fi
done

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
echo
echo "Done. CLIs installed via uv into $bin_dir:"
for name in comic-fmv ebay-sold-comps ebay-fetch seller-scan; do
  printf '  %-16s -> %s\n' "$name" "$(command -v "$name" 2>/dev/null || echo 'NOT ON PATH — add '"$bin_dir"' to PATH')"
done
