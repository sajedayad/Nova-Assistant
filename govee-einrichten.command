#!/bin/bash
# Govee-Lichter mit Nova verbinden. Einmal ausführen.
cd "$(dirname "$0")"
APP=$(grep '^app = ' fly.toml | cut -d'"' -f2)
FLY=$(command -v fly || command -v flyctl || echo "$HOME/.fly/bin/fly")
echo ""
read -s -p "Govee API-Key (aus der E-Mail, wird nicht angezeigt): " KEY; echo ""
KEY=$(echo "$KEY" | tr -d '[:space:]')
echo "Frage deine Lampen bei Govee ab ..."
RES=$(curl -s https://openapi.api.govee.com/router/api/v1/user/devices -H "Govee-API-Key: $KEY")
if ! echo "$RES" | grep -q '"device"'; then echo "❌ Govee sagt: $RES"; exit 1; fi
echo "✓ Gefunden:"; echo "$RES" | grep -o '"deviceName":"[^"]*"' | cut -d'"' -f4 | sed 's/^/   • /'
$FLY secrets set GOVEE_API_KEY="$KEY" -a "$APP" --stage
./cloud-deploy.command
echo ""
echo "✦ Fertig! Sag Nova: \"Mach das Licht lila\" 💜"
