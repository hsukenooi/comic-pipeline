#!/usr/bin/env bash
#
# Install (or reinstall) one checked-in LaunchAgent from scripts/launchd/.
# One command, so it can carry one allow rule and run without a prompt
# (BUI-1304). Replaces the cp / unload / load sequence in README.md.
#
# Usage: scripts/launchd/install.sh <label> [--dry-run]
#   <label>    e.g. com.comics.unpriced-rerun (a trailing .plist is accepted)
#   --dry-run  print each command instead of running it
#
# Exit codes: 0 ok, 2 usage, 3 plist missing or invalid, 4 launchctl failed.
set -euo pipefail

usage() { sed -n '7,11p' "$0" | sed 's/^# \{0,1\}//'; }

[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }
label=${1%.plist}
dry=0
if [[ $# -eq 2 ]]; then
  [[ $2 == --dry-run ]] || { usage >&2; exit 2; }
  dry=1
fi
[[ $label =~ ^com\.comics\.[a-z0-9-]+$ ]] || { echo "install: label must look like com.comics.<name>, got: $label" >&2; exit 2; }

dir="$(cd "$(dirname "$0")" && pwd)"
src="$dir/$label.plist"
dst="$HOME/Library/LaunchAgents/$label.plist"

[[ -f $src ]] || { echo "install: no plist at $src" >&2; exit 3; }
plutil -lint "$src" >/dev/null || { echo "install: $src fails plutil -lint" >&2; exit 3; }

run() {
  if [[ $dry -eq 1 ]]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

run mkdir -p "$HOME/Library/LaunchAgents"
run cp "$src" "$dst"
# An agent that was never loaded makes unload fail; that is fine.
run launchctl unload "$dst" 2>/dev/null || true
run launchctl load -w "$dst" || { echo "install: launchctl load failed for $dst" >&2; exit 4; }

if [[ $dry -eq 0 ]]; then
  launchctl list | grep -F "$label" || { echo "install: $label not listed after load" >&2; exit 4; }
fi
echo "install: $label installed at $dst"
