#!/bin/bash
# Auto-Start ausschalten
PL="$HOME/Library/LaunchAgents/com.nova.assistant.plist"
launchctl unload "$PL" 2>/dev/null && rm -f "$PL"
echo "Auto-Start ist aus. Nova läuft nicht mehr im Hintergrund."
