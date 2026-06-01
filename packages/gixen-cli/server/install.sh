#!/usr/bin/env bash
set -euo pipefail

# U11/BUI-60: deploy the gixen server from the comic-pipeline MONOREPO workspace.
#
# server/ now lives at packages/gixen-cli/server/, so the workspace root is three
# levels up. The deploy uses `uv sync --all-packages` rather than a per-package
# `pip install -r requirements.txt`, so the shared .venv includes gixen-cli AND
# the gixen-overlay plugin AND locg. This closes the pre-merge gap: plugin
# discovery is via importlib.metadata entry-points (the `gixen.plugins` group),
# which reads INSTALLED dist metadata — so the overlay must be *installed*
# (dist-info present), not merely importable. A bare gixen-cli venv would boot
# the server without the /comics tab.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$SCRIPT_DIR")"               # packages/gixen-cli
MONOREPO_ROOT="$(cd "$PKG_DIR/../.." && pwd)"    # monorepo root (uv workspace)
VENV="$MONOREPO_ROOT/.venv"                      # shared workspace venv
SERVER_DIR="$HOME/.gixen-server"
PLIST="$HOME/Library/LaunchAgents/com.gixen.server.plist"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed. See https://docs.astral.sh/uv/ to install it." >&2
  exit 1
fi

echo "==> Creating $SERVER_DIR"
mkdir -p "$SERVER_DIR"
chmod 700 "$SERVER_DIR"

if [ ! -f "$SERVER_DIR/.env" ]; then
  echo "==> Creating $SERVER_DIR/.env (fill in credentials)"
  cat > "$SERVER_DIR/.env" <<ENV
GIXEN_USERNAME=your_username_here
GIXEN_PASSWORD=your_password_here
DB_PATH=$HOME/.gixen-server/db.sqlite
ENV
  chmod 600 "$SERVER_DIR/.env"
  echo "    Edit $SERVER_DIR/.env before starting the server."
fi

echo "==> Syncing uv workspace (gixen-cli + gixen-overlay + locg) at $MONOREPO_ROOT"
( cd "$MONOREPO_ROOT" && uv sync --all-packages )

echo "==> Writing LaunchAgent plist to $PLIST"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gixen.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/uvicorn</string>
        <string>server.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8080</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PKG_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ENV_FILE</key>
        <string>$SERVER_DIR/.env</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SERVER_DIR/server.log</string>
    <key>StandardErrorPath</key>
    <string>$SERVER_DIR/server.error.log</string>
</dict>
</plist>
PLIST

echo "==> Loading LaunchAgent"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
# `load -w` registers the job but on modern macOS does not reliably (re)start the
# process when reloading in-place — RunAtLoad can be skipped, leaving the job
# "loaded but not running". Force a fresh start so the deploy actually serves.
launchctl kickstart -k "gui/$(id -u)/com.gixen.server" 2>/dev/null || true

echo ""
echo "Done. Server starting on port 8080 from the monorepo workspace venv."
echo "  venv:    $VENV"
echo "  cwd:     $PKG_DIR"
echo "  env:     $SERVER_DIR/.env"
echo "Logs: $SERVER_DIR/server.log"
echo "      $SERVER_DIR/server.error.log"
echo ""
echo "Test: curl http://localhost:8080/health && curl -s http://localhost:8080/comics | head"
