#!/bin/bash
# Nova-Passwort neu setzen (mit Bestätigung, damit kein Tippfehler passiert)
cd "$(dirname "$0")"
APP=$(grep '^app = ' fly.toml | cut -d'"' -f2)
FLY=$(command -v fly || command -v flyctl || echo "$HOME/.fly/bin/fly")
while true; do
  read -s -p "Neues Passwort (mind. 8 Zeichen): " A; echo ""
  read -s -p "Nochmal zur Bestätigung:          " B; echo ""
  if [ "$A" != "$B" ]; then echo "❌ Die zwei sind nicht gleich, nochmal."; continue; fi
  if [ ${#A} -lt 8 ]; then echo "❌ Zu kurz, nochmal."; continue; fi
  break
done
$FLY secrets set NOVA_PASSWORD="$A" -a "$APP" && echo "" && echo "✦ Fertig! In ca. 1 Minute gilt das neue Passwort: https://$APP.fly.dev"
