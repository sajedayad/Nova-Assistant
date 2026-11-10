#!/bin/bash
# Nova startet ab jetzt automatisch, sobald du dich am Mac anmeldest.
cd "$(dirname "$0")"
DIR="$(pwd)"
if [ ! -x .venv/bin/python ]; then echo "Bitte zuerst einmal ./start.command ausführen."; exit 1; fi
PL="$HOME/Library/LaunchAgents/com.nova.assistant.plist"
mkdir -p "$HOME/Library/LaunchAgents" data
lsof -ti:7777 | xargs kill 2>/dev/null; sleep 1
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.nova.assistant</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/.venv/bin/python</string><string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/nova.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/nova.log</string>
</dict></plist>
PLIST
launchctl unload "$PL" 2>/dev/null
launchctl load "$PL"
sleep 3
open "http://localhost:7777"
echo ""
echo "✦ Fertig! Nova läuft jetzt im Hintergrund und startet bei jedem Login automatisch."
echo "✦ Öffnen: http://localhost:7777  (am besten als Lesezeichen speichern)"
echo "✦ Dieses Terminal-Fenster kannst du schließen."
