#!/bin/bash
# Nova in die Cloud (Fly.io) bringen – einmal ausführen. Später für Updates einfach nochmal.
set -e
cd "$(dirname "$0")"
[ -f config.json ] || cp config.example.json config.json

say(){ printf "\n\033[1;36m✦ %s\033[0m\n" "$1"; }

# 1) Fly-Programm installieren
if ! command -v fly >/dev/null 2>&1 && ! command -v flyctl >/dev/null 2>&1; then
  say "Installiere das Fly-Programm (einmalig) ..."
  if command -v brew >/dev/null 2>&1; then brew install flyctl; else curl -L https://fly.io/install.sh | sh; export PATH="$HOME/.fly/bin:$PATH"; fi
fi
FLY=$(command -v fly || command -v flyctl)

# 2) Einloggen
if ! $FLY auth whoami >/dev/null 2>&1; then
  say "Browser öffnet sich: bei Fly.io einloggen ..."
  $FLY auth login
fi

# 3) App anlegen (nur beim ersten Mal)
if grep -q 'app = "APP_NAME"' fly.toml; then
  APP="nova-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 6)"
  say "Lege deine Nova-App an: $APP"
  $FLY apps create "$APP"
  sed -i '' "s/app = \"APP_NAME\"/app = \"$APP\"/" fly.toml
  $FLY volumes create nova_data --size 1 --region fra -a "$APP" -y

  KEY=$(grep -o 'sk-ant-[A-Za-z0-9_-]*' API_KEY.txt | head -1)
  if [ -z "$KEY" ]; then echo "Kein API-Key in API_KEY.txt gefunden."; exit 1; fi
  echo ""
  read -s -p "Wähl ein Passwort für Nova (mind. 8 Zeichen): " PW; echo ""
  while [ ${#PW} -lt 8 ]; do read -s -p "Zu kurz, nochmal: " PW; echo ""; done
  $FLY secrets set ANTHROPIC_API_KEY="$KEY" NOVA_PASSWORD="$PW" -a "$APP" --stage
  echo "$PW" > .cloud_pw && chmod 600 .cloud_pw
  FIRST=1
fi
APP=$(grep '^app = ' fly.toml | cut -d'"' -f2)

# 4) Hochladen
say "Lade Nova hoch (dauert 1–3 Minuten) ..."
$FLY deploy -a "$APP" --ha=false --remote-only

URL="https://$APP.fly.dev"

# 5) Beim ersten Mal: Daten vom Mac mitnehmen + Mac-Nova ausschalten
if [ "${FIRST:-0}" = "1" ]; then
  if [ -f data/state.json ]; then
    say "Nehme deinen Verlauf, Streaks und Tipps mit ..."
    sleep 8
    curl -s -X POST "$URL/api/import" -H "X-Nova-Password: $(cat .cloud_pw)" -H "Content-Type: application/json" --data-binary @data/state.json >/dev/null && echo "ok"
  fi
  rm -f .cloud_pw
  say "Schalte die Mac-Version aus (sonst ruft Nova dich doppelt an) ..."
  PL="$HOME/Library/LaunchAgents/com.nova.assistant.plist"
  launchctl unload "$PL" 2>/dev/null && rm -f "$PL" || true
  lsof -ti:7777 | xargs kill 2>/dev/null || true
fi

say "Fertig! Nova läuft jetzt hier:  $URL"
echo "  1. Auf dem Mac: Link öffnen, Passwort eingeben, als Lesezeichen speichern."
echo "  2. Auf dem iPhone: Link in Safari öffnen → Teilen → „Zum Home-Bildschirm“."
echo "  3. In der App oben auf „🔔 Anrufe an“ tippen (auf Mac und iPhone)."
open "$URL" 2>/dev/null || true
