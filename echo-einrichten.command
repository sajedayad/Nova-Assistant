#!/bin/bash
# Echo mit Nova verbinden (Voice Monkey). Einmal ausführen.
cd "$(dirname "$0")"
APP=$(grep '^app = ' fly.toml | cut -d'"' -f2)
FLY=$(command -v fly || command -v flyctl || echo "$HOME/.fly/bin/fly")
echo ""
echo "Du brauchst aus app.voicemonkey.io:"
echo "  1. deinen API-Token   (Dashboard → API / Token)"
echo "  2. die Device-ID deines Speakers (z. B. 'nova')"
echo ""
read -s -p "API-Token (wird nicht angezeigt): " TOK; echo ""
read -p "Device-ID: " DEV
TOK=$(echo "$TOK" | tr -d '[:space:]'); DEV=$(echo "$DEV" | tr -d '[:space:]')
if [ -z "$TOK" ] || [ -z "$DEV" ]; then echo "❌ Beides wird gebraucht."; exit 1; fi
echo "Prüfe Token bei Voice Monkey ..."
CHECK=$(curl -s "https://api-v3.voicemonkey.io/usage?token=$TOK")
if echo "$CHECK" | grep -qi "invalid"; then echo "❌ Voice Monkey sagt: Token ungültig. Bist du auf app.voicemonkey.io mit dem richtigen Konto eingeloggt?"; exit 1; fi
echo "✓ Token ok"
$FLY secrets set VOICEMONKEY_TOKEN="$TOK" VOICEMONKEY_DEVICE="$DEV" -a "$APP" --stage
echo ""
echo "✦ Gespeichert. Jetzt lade ich Nova neu hoch ..."
./cloud-deploy.command
echo ""
echo "✦ Fertig! Sag Nova in der App:  \"Teste den Echo\""
