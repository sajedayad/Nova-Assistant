#!/bin/bash
# Nova starten
cd "$(dirname "$0")"
[ -f config.json ] || cp config.example.json config.json
if ! grep -q "sk-ant-" API_KEY.txt 2>/dev/null; then
  echo "Kein API-Key gefunden. Ich öffne API_KEY.txt – Key einfügen, speichern, dann nochmal starten."
  open -e API_KEY.txt
  exit 1
fi
if [ ! -d .venv ]; then
  echo "Erster Start: richte Nova ein (dauert ca. 1 Minute) ..."
  python3 -m venv .venv && ./.venv/bin/pip install -q --upgrade pip && ./.venv/bin/pip install -q edge-tts || { echo "Einrichtung fehlgeschlagen."; exit 1; }
fi
# alte Nova beenden, falls sie noch läuft
lsof -ti:7777 | xargs kill 2>/dev/null; sleep 1
(sleep 3; open "http://localhost:7777") &
./.venv/bin/python server.py
