#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$REPO_DIR/.venv"
SERVER_DIR="$HOME/.gixen-server"
PLIST="$HOME/Library/LaunchAgents/com.gixen.server.plist"

echo "==> Creating $SERVER_DIR"
mkdir -p "$SERVER_DIR"
chmod 700 "$SERVER_DIR"

if [ ! -f "$SERVER_DIR/.env" ]; then
  echo "==> Creating $SERVER_DIR/.env (fill in credentials)"
  cat > "$SERVER_DIR/.env" <<ENV
GIXEN_USERNAME=your_username_here
GIXEN_PASSWORD=your_password_here
DB_PATH=$HOME/.gixen-server/db.sqlite
GIXEN_SYNC_ENABLED=true
GIXEN_SYNC_INTERVAL=600
ENV
  chmod 600 "$SERVER_DIR/.env"
  echo "    Edit $SERVER_DIR/.env before starting the server."
fi

echo "==> Creating Python venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

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
    <string>$REPO_DIR</string>
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

echo ""
echo "Done. Server starting on port 8080."
echo "Logs: $SERVER_DIR/server.log"
echo "      $SERVER_DIR/server.error.log"
echo ""
echo "Test: curl http://localhost:8080/health"
