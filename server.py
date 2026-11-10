#!/usr/bin/env python3
"""Nova – eine persönliche KI-Assistentin mit Stimme, Routine und Smart-Home.

Startet einen kleinen Server auf http://localhost:7777
- Gehirn: Claude Haiku (über die Anthropic API)
- Stimme: Microsoft "Seraphina" (über edge-tts, kostenlos)
- Routine: Lernblöcke, Gym, Kochen, Schlafenszeit, Essens-Check, Arbeitsschichten
"""
import asyncio
import hashlib
import hmac
import secrets
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("NOVA_DATA") or os.path.join(BASE, "data")
CLOUD = bool(os.environ.get("FLY_APP_NAME"))
PASSWORD = os.environ.get("NOVA_PASSWORD", "")
PUBLIC_URL = os.environ.get("NOVA_PUBLIC_URL") or (f"https://{os.environ['FLY_APP_NAME']}.fly.dev" if CLOUD else "")
AUDIO = os.path.join(DATA, "audio")
STATE_FILE = os.path.join(DATA, "state.json")
os.makedirs(AUDIO, exist_ok=True)

with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

FAKE_AI = os.environ.get("NOVA_FAKE_AI") == "1"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
NAME = CFG["user_name"]
NOVA = CFG["assistant_name"]
CALL = CFG.get("call_name") or NAME  # so spricht Nova ihn an


def load_key():
    for fn in ("API_KEY.txt", ".env"):
        p = os.path.join(BASE, fn)
        if os.path.exists(p):
            txt = open(p, encoding="utf-8").read()
            m = re.search(r"sk-ant-[A-Za-z0-9_\-]+", txt)
            if m:
                return m.group(0)
    return os.environ.get("ANTHROPIC_API_KEY", "")


API_KEY = load_key()

# ───────────────────────────── State ─────────────────────────────
LOCK = threading.RLock()
DEFAULT_STATE = {
    "history": [], "facts": [], "shifts": [], "fired": [], "meals": [],
    "last_meal": None, "last_meal_nag": None, "snooze": {}, "reminders": [],
    "pending": None, "stats": [], "events_log": [], "phrase": None, "overrides": {},
}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            s = json.load(open(STATE_FILE, encoding="utf-8"))
            for k, v in DEFAULT_STATE.items():
                s.setdefault(k, v)
            return s
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATE))


S = load_state()


def save():
    with LOCK:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(S, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)


def now():
    return dt.datetime.now().replace(microsecond=0)


def at(date, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(date, dt.time(h, m))


def iso(d):
    return d.isoformat(timespec="minutes") if d else None


def parse(s):
    return dt.datetime.fromisoformat(s) if s else None


def hm(d):
    return d.strftime("%H:%M")


# ───────────────────────────── Plan ─────────────────────────────
def overlaps(a1, a2, b1, b2):
    return a1 < b2 and b1 < a2


def events_for(date):
    """Wochenplan + Schichten für ein Datum. Lernen/Gym/Kochen, die mit einer Schicht kollidieren, fallen weg."""
    evs = []
    with LOCK:
        ov = S.get("overrides", {}).get(date.isoformat())
    for e in (ov if ov is not None else CFG["week"].get(DAYS[date.weekday()], [])):
        evs.append({**e, "s": at(date, e["start"]), "e": at(date, e["end"])})
    shifts = []
    with LOCK:
        for sh in S["shifts"]:
            if sh["date"] == date.isoformat():
                s, e = at(date, sh["start"]), at(date, sh["end"])
                if e <= s:
                    e += dt.timedelta(days=1)
                shifts.append({"start": sh["start"], "end": sh["end"], "type": "work",
                               "title": sh.get("label") or "Arbeit", "s": s, "e": e})
    keep = []
    for ev in evs:
        if ev["type"] in ("study", "gym", "cook") and any(overlaps(ev["s"], ev["e"], w["s"], w["e"]) for w in shifts):
            continue
        keep.append(ev)
    keep += shifts
    keep.sort(key=lambda x: x["s"])
    return keep


def wake_time(date):
    evs = events_for(date)
    default = at(date, CFG["default_wake"])
    if not evs:
        return default
    w = evs[0]["s"] - dt.timedelta(minutes=CFG["morning_buffer_min"])
    return min(w, default)


def bedtime_before(date):
    """Schlafenszeit, damit man vor `date` genug Schlaf bekommt."""
    return wake_time(date) - dt.timedelta(hours=CFG["sleep_hours"])


def plan_text(date):
    evs = events_for(date)
    if not evs:
        return "frei"
    return "; ".join(f"{e['start']}–{e['end']} {e['title']}" for e in evs)


# ───────────────────────────── Voice ─────────────────────────────
ARABIC = re.compile(r"([\u0600-\u06FF]+(?:\s+[\u0600-\u06FF]+)*)")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def tts(text):
    clean = EMOJI.sub("", text).replace("*", "").strip()
    for wrong, right in CFG.get("pronunciation", {}).items():
        clean = re.sub(rf"\b{re.escape(wrong)}\b", right, clean, flags=re.IGNORECASE)
    if not clean:
        return None
    name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}.mp3"
    path = os.path.join(AUDIO, name)
    # Arabische Wörter (z. B. حبيبي) separat sprechen lassen, damit die Stimme sie arabisch ausspricht
    parts = [p.strip() for p in ARABIC.split(clean) if p and p.strip(" ,.!?")]
    try:
        import edge_tts
        if os.environ.get("NOVA_CAFILE"):
            import ssl
            import edge_tts.communicate as _c
            _c._SSL_CTX = ssl.create_default_context(cafile=os.environ["NOVA_CAFILE"])

        async def one(t):
            data = b""
            async for chunk in edge_tts.Communicate(t, CFG["voice"], rate="+4%").stream():
                if chunk["type"] == "audio":
                    data += chunk["data"]
            return data

        async def safe(t):
            for attempt in (t, t + ".", t + " ..."):
                try:
                    d = await one(attempt)
                    if d:
                        return d
                except Exception:
                    pass
            return b""

        async def go():
            out = [await safe(t) for t in parts]
            if not any(out):  # Notfall: alles am Stück, ohne arabische Schrift
                out = [await one(ARABIC.sub(CALL, clean))]
            return out

        with open(path, "wb") as f:
            for chunk in asyncio.run(go()):
                f.write(chunk)
        return "/audio/" + name
    except Exception as e:
        print("[Stimme] Fehler:", e)
        return None


FILLER_TEXTS = ["Hmm.", "Mhm, warte kurz.", "Okay, Moment.", "Hmm, lass mich kurz überlegen.",
                "Ah, okay.", "Ja, warte.", "Mhm.", "Okay, okay."]
FILLERS = []


def make_fillers():
    d = os.path.join(AUDIO, "fillers")
    os.makedirs(d, exist_ok=True)
    for i, t in enumerate(FILLER_TEXTS):
        p = os.path.join(d, f"f{i}.mp3")
        if not os.path.exists(p) or os.path.getsize(p) < 500:
            u = tts(t)
            if u:
                os.replace(os.path.join(AUDIO, os.path.basename(u)), p)
        if os.path.exists(p):
            FILLERS.append(f"/audio/fillers/f{i}.mp3")
    print(f"[Stimme] {len(FILLERS)} Füllwörter bereit")


def cleanup_audio():
    cutoff = time.time() - 6 * 3600
    for fn in os.listdir(ECHO_DIR):
        try:
            if os.path.getmtime(os.path.join(ECHO_DIR, fn)) < time.time() - 86400:
                os.remove(os.path.join(ECHO_DIR, fn))
        except OSError:
            pass
    for fn in os.listdir(AUDIO):
        p = os.path.join(AUDIO, fn)
        if os.path.isdir(p):
            continue
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


# ───────────────────────────── Outbox (Nachrichten an die App) ─────────────────────────────
OUTBOX = []
OUT_ID = [int(time.time())]
LAST_POLL = [0.0]


def mac_notify(text, audio):
    if sys.platform != "darwin":
        return
    safe = EMOJI.sub("", text).replace('"', "'")[:180]
    subprocess.Popen(["osascript", "-e", f'display notification "{safe}" with title "{NOVA}" sound name "Glass"'])
    if audio:
        subprocess.Popen(["afplay", os.path.join(AUDIO, os.path.basename(audio))])


# ───────────────────────────── iPhone/Browser-Benachrichtigungen (Web Push) ─────────────────────────────
VAPID_FILE = os.path.join(DATA, "vapid.pem")
VAPID_PUB = [""]


def vapid_setup():
    try:
        from py_vapid import Vapid01
        from cryptography.hazmat.primitives import serialization
        import base64
        if os.path.exists(VAPID_FILE):
            v = Vapid01.from_file(VAPID_FILE)
        else:
            v = Vapid01()
            v.generate_keys()
            v.save_key(VAPID_FILE)
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        VAPID_PUB[0] = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    except Exception as e:
        print("[Push] nicht verfügbar:", e)


def web_push(title, body, tag="nova"):
    if not VAPID_PUB[0]:
        return
    with LOCK:
        subs = list(S.get("push_subs", []))
    if not subs:
        return

    def go():
        from pywebpush import webpush, WebPushException
        dead = []
        for sub in subs:
            try:
                webpush(sub, json.dumps({"title": title, "body": body, "tag": tag}, ensure_ascii=False),
                        vapid_private_key=VAPID_FILE, vapid_claims={"sub": PUBLIC_URL or "mailto:nova@example.com"}, ttl=3600)
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                print("[Push] Fehler:", code, str(e)[:200])
                if code in (403, 404, 410):
                    dead.append(sub["endpoint"])
            except Exception as e:
                print("[Push] Fehler:", repr(e)[:200])
        if dead:
            with LOCK:
                S["push_subs"] = [x for x in S.get("push_subs", []) if x["endpoint"] not in dead]
                save()
    threading.Thread(target=go, daemon=True).start()


# ───────────────────────────── Echo (über Voice Monkey) ─────────────────────────────
ECHO_TOKEN = os.environ.get("VOICEMONKEY_TOKEN", "").strip()
ECHO_DEVICE = os.environ.get("VOICEMONKEY_DEVICE", "").strip()
ECHO_DIR = os.path.join(DATA, "echo")
os.makedirs(ECHO_DIR, exist_ok=True)


def echo_audio_url(local_mp3):
    """Echo braucht 44,1 kHz MP3 unter einer öffentlichen https-Adresse."""
    if not (local_mp3 and PUBLIC_URL and os.path.exists(local_mp3)):
        return None
    name = secrets.token_hex(12) + ".mp3"
    out = os.path.join(ECHO_DIR, name)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", local_mp3, "-ar", "44100", "-ac", "1", "-b:a", "64k", out],
                       check=True, timeout=30)
        return f"{PUBLIC_URL}/echo/{name}"
    except Exception as e:
        print("[Echo] Audio-Umwandlung fehlgeschlagen:", e)
        return None


def echo_announce(text, audio=None, force=False):
    if not (ECHO_TOKEN and ECHO_DEVICE):
        return "Echo ist noch nicht eingerichtet."
    today = dt.date.today().isoformat()
    cap = CFG.get("echo", {}).get("max_per_day", 6)
    with LOCK:
        cnt = S.setdefault("echo_count", {})
        if not force and cnt.get(today, 0) >= cap:
            return "Tageslimit für den Echo erreicht."
        S["echo_count"] = {today: cnt.get(today, 0) + 1}
        save()

    def go():
        local = os.path.join(AUDIO, os.path.basename(audio)) if audio else None
        if not local:
            u = tts(text)
            local = os.path.join(AUDIO, os.path.basename(u)) if u else None
        url = echo_audio_url(local)
        body = {"token": ECHO_TOKEN, "device": ECHO_DEVICE}
        if url:
            body["audio"] = url
        else:
            body.update({"speech": EMOJI.sub("", text), "language": "de-DE"})
        try:
            req = urllib.request.Request("https://api-v3.voicemonkey.io/announce", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                print("[Echo] gesendet:", r.status, flush=True)
        except urllib.error.HTTPError as e:
            print("[Echo] Fehler:", e.code, e.read().decode(errors="ignore")[:200], flush=True)
        except Exception as e:
            print("[Echo] Fehler:", repr(e)[:200], flush=True)
    threading.Thread(target=go, daemon=True).start()
    return "Okay, ich spreche gleich über deinen Echo."


# ───────────────────────────── Govee-Lichter ─────────────────────────────
GOVEE_KEY = os.environ.get("GOVEE_API_KEY", "").strip()
GOVEE_URL = "https://openapi.api.govee.com/router/api/v1"
LIGHTS = []                     # [{sku, device, name, caps:set}]
LIGHT_LOCK = threading.Lock()
LIGHT_SAVED = {"state": None, "timer": None}

MOODS = {   # Farbe (RGB) oder Weißton (Kelvin), Helligkeit %, danach zurück?
    "talk":  {"rgb": (90, 210, 255), "bri": 75, "restore": True},
    "study": {"k": 5600, "bri": 100, "restore": False},
    "gym":   {"rgb": (255, 80, 20), "bri": 100, "restore": True},
    "nag":   {"rgb": (255, 0, 0), "bri": 100, "restore": True, "flash": True},
    "meal":  {"rgb": (60, 255, 140), "bri": 70, "restore": True},
    "bed":   {"k": 2200, "bri": 12, "restore": False},
    "brief": {"k": 4200, "bri": 100, "restore": False},
}
KIND_MOOD = {"gym": "gym", "nag": "nag", "meal": "meal", "bed": "bed", "brief": "brief",
             "break": "talk", "done": "talk", "report": "talk", "weekly": "talk", "reminder": "talk"}


def govee(method, path, body=None):
    req = urllib.request.Request(GOVEE_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Govee-API-Key": GOVEE_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def lights_setup():
    if not GOVEE_KEY:
        return
    try:
        want = [n.lower() for n in CFG.get("lights", {}).get("devices", [])]
        for d in govee("GET", "/user/devices").get("data", []):
            caps = {c.get("instance") for c in d.get("capabilities", [])}
            if "powerSwitch" not in caps:
                continue
            name = d.get("deviceName") or d.get("sku")
            if want and not any(w in name.lower() for w in want):
                continue
            LIGHTS.append({"sku": d["sku"], "device": d["device"], "name": name, "caps": caps})
        print(f"[Licht] {len(LIGHTS)} Govee-Lampen: " + ", ".join(l["name"] for l in LIGHTS), flush=True)
    except Exception as e:
        print("[Licht] Govee nicht erreichbar:", repr(e)[:200], flush=True)


def light_cmd(l, ctype, inst, val):
    try:
        govee("POST", "/device/control", {"requestId": uuid.uuid4().hex, "payload": {
            "sku": l["sku"], "device": l["device"], "capability": {"type": ctype, "instance": inst, "value": val}}})
    except Exception as e:
        print(f"[Licht] {l['name']} {inst}: {repr(e)[:150]}", flush=True)


def light_set(l, power=None, rgb=None, k=None, bri=None):
    if power is not None and l.get("on") != power:
        light_cmd(l, "devices.capabilities.on_off", "powerSwitch", 1 if power else 0)
        l["on"] = power
    if power is False:
        return
    if bri is not None and "brightness" in l["caps"]:
        light_cmd(l, "devices.capabilities.range", "brightness", max(1, min(100, int(bri))))
    if rgb is not None and "colorRgb" in l["caps"]:
        light_cmd(l, "devices.capabilities.color_setting", "colorRgb", (rgb[0] << 16) + (rgb[1] << 8) + rgb[2])
    elif (k is not None or rgb is not None) and "colorTemperatureK" in l["caps"]:
        if k is None:  # nur-weiße Lampe: Farbe grob in warm/kalt übersetzen
            r, g, b = rgb
            k = 2200 if r > b + 60 else (6500 if b > r + 60 else 4000)
        light_cmd(l, "devices.capabilities.color_setting", "colorTemperatureK", int(k))


def light_read(l):
    try:
        caps = govee("POST", "/device/state", {"requestId": uuid.uuid4().hex,
                                               "payload": {"sku": l["sku"], "device": l["device"]}})["payload"]["capabilities"]
        st = {c["instance"]: c.get("state", {}).get("value") for c in caps}
        l["on"] = st.get("powerSwitch") == 1
        return {"power": st.get("powerSwitch") == 1, "bri": st.get("brightness"),
                "rgb": st.get("colorRgb"), "k": st.get("colorTemperatureK")}
    except Exception:
        return None


def light_restore():
    with LIGHT_LOCK:
        saved, LIGHT_SAVED["state"], LIGHT_SAVED["timer"] = LIGHT_SAVED["state"], None, None
    for l, st in (saved or []):
        if not st:
            continue
        if not st["power"]:
            light_set(l, power=False)
            continue
        rgb = st["rgb"]
        rgb = ((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255) if isinstance(rgb, int) and rgb > 0 and not st["k"] else None
        light_set(l, bri=st["bri"], rgb=rgb, k=st["k"] or None)


def audio_seconds(audio, text):
    try:
        p = os.path.join(AUDIO, os.path.basename(audio))
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return float(out)
    except Exception:
        return max(2.0, len(text) / 13)


def light_mood(mood, seconds=5.0):
    """Licht passend zu Nova. Läuft im Hintergrund, blockiert nichts."""
    if not LIGHTS or mood not in MOODS or not CFG.get("lights", {}).get("enabled", True):
        return
    m = MOODS[mood]

    def go():
        with LIGHT_LOCK:
            pending = LIGHT_SAVED["timer"]
            if pending:
                pending.cancel()
            need_read = m["restore"] and LIGHT_SAVED["state"] is None
        if need_read:
            with LIGHT_LOCK:
                LIGHT_SAVED["state"] = [(l, light_read(l)) for l in LIGHTS]
        if not m["restore"]:
            with LIGHT_LOCK:
                LIGHT_SAVED["state"] = None
        for l in LIGHTS:
            light_set(l, power=True, rgb=m.get("rgb"), k=m.get("k"), bri=m["bri"])
        if m.get("flash"):
            time.sleep(0.8)
            for l in LIGHTS:
                light_set(l, bri=10)
            time.sleep(0.6)
            for l in LIGHTS:
                light_set(l, bri=100)
        if m["restore"]:
            t = threading.Timer(seconds + 1.5, light_restore)
            t.daemon = True
            with LIGHT_LOCK:
                LIGHT_SAVED["timer"] = t
            t.start()
    threading.Thread(target=go, daemon=True).start()


COLOR_NAMES = {"rot": (255, 0, 0), "grün": (0, 255, 60), "blau": (0, 80, 255), "lila": (150, 50, 255), "pink": (255, 60, 180),
               "orange": (255, 110, 0), "gelb": (255, 210, 0), "türkis": (0, 220, 200), "weiß": None, "nova": (90, 210, 255)}


PUSH_TITLES = {"study": "📞 Nova ruft an · Lernzeit", "gym": "📞 Nova ruft an · Gym", "nag": "📞 Nova ruft nochmal an",
               "meal": "🍽️ Nova · Essen", "bed": "🌙 Nova · Schlafenszeit", "weekly": "🗓️ Nova · Neue Woche",
               "report": "📊 Nova · Wochenbericht", "brief": "☀️ Nova · Guten Morgen", "reminder": "⏰ Nova · Erinnerung",
               "break": "☕ Nova · Pause", "done": "✅ Nova"}


def push(kind, text, needs_ack=False, speak=True):
    audio = tts(text) if speak else None
    with LOCK:
        OUT_ID[0] += 1
        OUTBOX.append({"id": OUT_ID[0], "kind": kind, "text": text, "audio": audio,
                       "needs_ack": needs_ack, "time": hm(now())})
        del OUTBOX[:-50]
        S["events_log"].append(f"{now().strftime('%a %H:%M')} [{kind}] {text}")
        S["events_log"] = S["events_log"][-12:]
        save()
    # App nicht offen? Dann direkt über den Mac sprechen + Mitteilung zeigen
    if time.time() - LAST_POLL[0] > 30 and not CLOUD:
        mac_notify(text, audio)
    web_push(PUSH_TITLES.get(kind, NOVA), text, tag="call" if needs_ack else kind)
    if needs_ack or kind in CFG.get("echo", {}).get("kinds", []):
        echo_announce(text, audio)
    mood = "nag" if kind == "nag" else ("study" if kind == "study" and needs_ack else KIND_MOOD.get(kind, "talk"))
    light_mood(mood, audio_seconds(audio, text) if audio else 5)
    print(f"[{NOVA}] ({kind}) {text}", flush=True)


# ───────────────────────────── Sprüche (kosten nichts, kein AI-Aufruf) ─────────────────────────────
T = {
    "study_start": [
        "Yo {n}, Lernzeit! {title} bis {end}. Handy weg, Laptop auf. Bist du dabei?",
        "{n}, es ist {now}. {title} startet jetzt. Ich will ein 'bin dabei' hören.",
        "Okay {n}, Fokus-Modus. {title} bis {end}. Los geht's, du schaffst das.",
        "Lernblock startet, {n}. {title} bis {end}. Keine Ausreden heute, ja?",
    ],
    "study_break": [
        "{focus} Minuten geschafft, stark! {brk} Minuten Pause. Kurz aufstehen, Wasser trinken.",
        "Pause! {brk} Minuten. Nicht ins Handy versinken, Digga.",
        "Nice, Block geschafft. {brk} Minuten Pause, dann geht's weiter.",
    ],
    "study_resume": [
        "Pause vorbei, {n}. Weiter geht's!",
        "Okay, zurück an den Schreibtisch. Nächste Runde.",
        "Pause ist um. Komm, noch eine Runde.",
    ],
    "study_end": [
        "Lernblock fertig! Ehrlich stark, {n}.",
        "Geschafft für diesen Block. Respekt, Ehrenmann.",
        "Das war's für den Block. Läuft bei dir!",
    ],
    "gym_start": [
        "{n}, Gym-Zeit! {title}. Tasche packen, ab geht's. Dein Gym Log ist ready. Bist du unterwegs?",
        "Zeit für {title}. 45 Minuten, dann bist du durch. Keine Ausreden!",
        "Gym ruft, {n}. {title}. Ich will hören, dass du losgehst.",
    ],
    "cook": [
        "{n}, Zeit zu kochen. Was gibt's heute? Am besten was mit viel Protein.",
        "Kochzeit! Selbst kochen, nicht bestellen, okay?",
        "Hunger? Egal, jetzt wird gekocht. Sag mir, was du machst.",
    ],
    "nag": [
        "{n}? Ich hab noch kein 'bin dabei' gehört. {title} läuft schon.",
        "Hallo? {title}, schon vergessen? Komm schon.",
        "Ich frag nochmal: Bist du bei {title} dabei oder nicht?",
    ],
    "nag_final": [
        "Okay, ich trag {title} als geschwänzt ein. Nächstes Mal ziehst du's durch, ja?",
        "Na gut, {title} ist für heute weg. Ich merk mir das aber.",
    ],
    "meal": [
        "{n}, du hast seit {h} Stunden nichts gegessen. Iss was, am besten mit Protein.",
        "Essenszeit! Seit {h} Stunden nichts. Was isst du?",
        "Kurzer Check: Hast du gegessen? Laut mir nicht seit {h} Stunden.",
    ],
    "bed_warn": [
        "{n}, morgen {first_txt} um {first}. Damit du {sleep} Stunden schläfst, musst du um {bed} im Bett sein. Noch {warn} Minuten.",
        "Kleine Erinnerung: Morgen {first_txt} um {first}. Um {bed} ist Schlafenszeit, also noch {warn} Minuten.",
    ],
    "bed_now": [
        "Okay {n}, Schlafenszeit! Morgen {first_txt} um {first}. Handy weg, Licht aus. Gute Nacht!",
        "Es ist {bed}. Ab ins Bett, {n}. Morgen wartet {first_txt} um {first}.",
    ],
    "weekly": [
        "Neue Woche, {n}! Wann arbeitest du diese Woche? Sag mir einfach Tage und Uhrzeiten.",
    ],
}

PHRASES = [
    ("Ich hab keinen Bock", "Ich hab keine Lust"),
    ("Safe", "Sicher, auf jeden Fall"),
    ("Läuft bei dir", "Bei dir läuft's gut, du bist erfolgreich"),
    ("Ich bin raus", "Ich mach nicht mehr mit / ich gehe"),
    ("Das ist mir zu wild", "Das ist mir zu krass / zu viel"),
    ("Ehrenmann", "Jemand, der was Nettes oder Korrektes gemacht hat"),
    ("Lowkey", "Ein bisschen, heimlich (aus dem Englischen)"),
    ("Gönn dir", "Mach ruhig, du hast es dir verdient"),
    ("Das ist cringe", "Das ist peinlich"),
    ("Kein Plan", "Keine Ahnung"),
    ("Was geht?", "Wie geht's? / Was machst du?"),
    ("Passt schon", "Ist okay, kein Problem"),
    ("Ich feier das", "Ich find das richtig gut"),
    ("Sag bloß", "Echt jetzt? (überrascht)"),
    ("Hast du Zeit für 'nen Kaffee?", "Lockere Art, jemanden in der Uni anzusprechen"),
]


def say(kind, **kw):
    kw.setdefault("n", CALL)
    kw.setdefault("now", hm(now()))
    return random.choice(T[kind]).format(**kw)


# ───────────────────────────── Scheduler ─────────────────────────────
def fired(key):
    with LOCK:
        return key in S["fired"]


def mark(key):
    with LOCK:
        S["fired"].append(key)
        cutoff = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        S["fired"] = [k for k in S["fired"] if k[:10] >= cutoff]
        save()


def once(key, when, window_min, fn):
    n = now()
    if when <= n < when + dt.timedelta(minutes=window_min) and not fired(key):
        mark(key)
        fn()


def type_name(t):
    return {"study": "Lernen", "gym": "Gym", "cook": "Kochen", "uni": "Uni", "work": "Arbeit"}.get(t, t)


def tick():
    n = now()
    today = n.date()
    tomorrow = today + dt.timedelta(days=1)
    d = today.isoformat()
    evs = events_for(today)

    # Morgen-Briefing zur Aufstehzeit
    wake = wake_time(today)
    once(f"{d}-brief", wake, 90, lambda: morning_brief(today))

    for ev in evs:
        k = f"{d}-{ev['start']}-{ev['type']}"
        title, end = ev["title"], ev["end"]
        if ev["type"] == "study":
            skipped = k in S.get("skipped", [])
            def start_study(ev=ev, k=k):
                push("study", say("study_start", title=ev["title"], end=ev["end"]), needs_ack=True)
                set_pending(k, ev)
            once(k + "-start", ev["s"], 10, start_study)
            focus, brk = CFG["study_focus_min"], CFG["study_break_min"]
            i = 1
            while True:
                b = ev["s"] + dt.timedelta(minutes=(focus + brk) * (i - 1) + focus)
                r = b + dt.timedelta(minutes=brk)
                if r >= ev["e"] or skipped:
                    break
                once(f"{k}-break{i}", b, 5, lambda: push("break", say("study_break", focus=focus, brk=brk)))
                once(f"{k}-resume{i}", r, 5, lambda: push("study", say("study_resume")))
                i += 1
            if not skipped:
                once(k + "-end", ev["e"], 10, lambda: push("done", say("study_end")))
        elif ev["type"] == "gym":
            def start_gym(ev=ev, k=k):
                push("gym", say("gym_start", title=ev["title"]), needs_ack=True)
                set_pending(k, ev)
            once(k + "-start", ev["s"], 10, start_gym)
        elif ev["type"] == "cook":
            def cook():
                with LOCK:
                    S["last_meal_nag"] = iso(now())
                push("meal", say("cook"))
            once(k + "-start", ev["s"], 10, cook)

    nag_check()
    meal_check(n, wake)

    # Schlafenszeit (vor heute – falls nach Mitternacht – und vor morgen)
    for target in (today, tomorrow):
        bed = bedtime_before(target)
        tevs = events_for(target)
        first = tevs[0] if tevs else None
        info = dict(first_txt=(type_name(first["type"]) if first else "nichts Festes"),
                    first=(first["start"] if first else CFG["default_wake"]),
                    bed=hm(bed), sleep=CFG["sleep_hours"], warn=CFG["bedtime_warning_min"])
        tk = target.isoformat()
        once(f"{tk}-bedwarn", bed - dt.timedelta(minutes=CFG["bedtime_warning_min"]), 20,
             lambda info=info: push("bed", say("bed_warn", **info)))
        once(f"{tk}-bednow", bed, 30, lambda info=info: push("bed", say("bed_now", **info)))

    # Wöchentlicher Arbeits-Check
    wc = CFG["weekly_checkin"]
    if DAYS[today.weekday()] == wc["day"]:
        once(f"{d}-weekly", at(today, wc["time"]), 180, lambda: push("weekly", say("weekly")))

    # Wochenbericht
    wr = CFG.get("weekly_report", {"day": "sun", "time": "18:00"})
    if DAYS[today.weekday()] == wr["day"]:
        once(f"{d}-report", at(today, wr["time"]), 180, weekly_report)

    # Eigene Erinnerungen
    with LOCK:
        due = [r for r in S["reminders"] if not r.get("done") and parse(r["at"]) <= n]
        for r in due:
            r["done"] = True
        S["reminders"] = [r for r in S["reminders"] if not r.get("done")]
    for r in due:
        push("reminder", f"Erinnerung: {r['text']}")


def set_pending(key, ev):
    with LOCK:
        S["pending"] = {"key": key, "title": ev["title"], "type": ev["type"],
                        "minutes": int((ev["e"] - ev["s"]).total_seconds() // 60),
                        "next_nag": iso(now() + dt.timedelta(minutes=CFG["nag_every_min"])), "nags": 0}
        save()


def resolve_pending(status):
    with LOCK:
        p = S["pending"]
        if not p:
            return None
        S["stats"].append({"date": p["key"][:10], "title": p["title"], "type": p["type"], "status": status,
                           "minutes": p.get("minutes", 0)})
        S["stats"] = S["stats"][-300:]
        if status != "erledigt":
            S.setdefault("skipped", []).append(p["key"])
            S["skipped"] = S["skipped"][-30:]
        S["pending"] = None
        save()
        return p


def nag_check():
    with LOCK:
        p = S["pending"]
        if not p or now() < parse(p["next_nag"]):
            return
        p["nags"] += 1
        final = p["nags"] > CFG["max_nags"]
        p["next_nag"] = iso(now() + dt.timedelta(minutes=CFG["nag_every_min"]))
        title = p["title"]
        save()
    if final:
        resolve_pending("geschwänzt")
        push("nag", say("nag_final", title=title))
    else:
        push("nag", say("nag", title=title), needs_ack=True)


def meal_check(n, wake):
    bed = bedtime_before(n.date() + dt.timedelta(days=1))
    if not (wake <= n < bed - dt.timedelta(minutes=45)):
        return
    with LOCK:
        last = max(parse(S["last_meal"]) or wake, wake)
        sn = parse(S["snooze"].get("meal"))
        lastnag = parse(S["last_meal_nag"])
    if sn and n < sn:
        return
    gap = (n - last).total_seconds() / 3600
    if gap >= CFG["meal_gap_hours"] and (not lastnag or (n - lastnag).total_seconds() > 90 * 60):
        with LOCK:
            S["last_meal_nag"] = iso(n)
            save()
        push("meal", say("meal", h=int(gap)))


def morning_brief(today):
    evs = events_for(today)
    first_phrase = phrase_of_day()
    if evs:
        parts = ", ".join(f"{e['start']} {e['title']}" for e in evs[:5])
        plan = f"Heute: {parts}."
    else:
        plan = "Heute ist nichts geplant. Chill, aber nicht den ganzen Tag 😏"
    push("brief", f"Guten Morgen, {CALL}! Es ist {DAY_DE[today.weekday()]}. {plan} "
                  f"Spruch des Tages: '{first_phrase[0]}', heißt so viel wie: {first_phrase[1]}.")


def phrase_of_day():
    today = dt.date.today().isoformat()
    with LOCK:
        if S["phrase"] and S["phrase"].get("date") == today:
            return S["phrase"]["p"], S["phrase"]["m"]
    p, m = PHRASES[dt.date.today().toordinal() % len(PHRASES)]
    if API_KEY and not FAKE_AI:
        try:
            r = claude_raw(
                [{"type": "text", "text": "Du bringst einem 20-jährigen Studenten in Deutschland Jugendsprache bei. "
                                          "Antworte NUR mit JSON: {\"p\": \"Ausdruck\", \"m\": \"kurze Bedeutung + wann man es sagt\"}"}],
                [{"role": "user", "content": "Gib mir einen nützlichen, aktuellen Ausdruck, den Studis wirklich sagen. "
                                             f"Nicht: {', '.join(x[0] for x in PHRASES[:6])}. Zufallszahl: {random.randint(1, 9999)}"}],
                tools=False, max_tokens=800)
            txt = "".join(b.get("text", "") for b in r.get("content", []))
            j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            p, m = j["p"], j["m"]
        except Exception as e:
            print("[Spruch] Fallback:", e)
    with LOCK:
        S["phrase"] = {"date": today, "p": p, "m": m}
        save()
    return p, m


def week_stats(end=None):
    end = end or dt.date.today()
    start = end - dt.timedelta(days=6)
    with LOCK:
        st = [x for x in S["stats"] if start.isoformat() <= x["date"] <= end.isoformat()]
        meals = [m for m in S["meals"] if start.isoformat() <= m["time"][:10] <= end.isoformat()]
    study_done = [x for x in st if x["type"] == "study" and x["status"] == "erledigt"]
    study_skip = [x for x in st if x["type"] == "study" and x["status"] != "erledigt"]
    gym_done = [x for x in st if x["type"] == "gym" and x["status"] == "erledigt"]
    days = {}
    for m in meals:
        days.setdefault(m["time"][:10], [0, 0])
        days[m["time"][:10]][0] += m.get("kcal") or 0
        days[m["time"][:10]][1] += m.get("protein") or 0
    return {
        "study_hours": round(sum(x.get("minutes", 0) for x in study_done) / 60, 1),
        "study_done": len(study_done), "study_skipped": len(study_skip),
        "gym_done": len(gym_done), "gym_goal": 3,
        "protein_avg": round(sum(v[1] for v in days.values()) / len(days)) if days else None,
        "kcal_avg": round(sum(v[0] for v in days.values()) / len(days)) if days else None,
        "meal_days": len(days), "streak": study_streak(),
    }


def study_streak():
    with LOCK:
        done_days = {x["date"] for x in S["stats"] if x["type"] == "study" and x["status"] == "erledigt"}
    streak, day = 0, dt.date.today()
    for _ in range(90):
        planned = any(e["type"] == "study" for e in events_for(day))
        if day.isoformat() in done_days:
            streak += 1
        elif planned and day != dt.date.today():
            break
        day -= dt.timedelta(days=1)
    return streak


def weekly_report():
    w = week_stats()
    facts = (f"Lernen: {w['study_hours']} Stunden, {w['study_done']} Blöcke erledigt, {w['study_skipped']} geschwänzt. "
             f"Gym: {w['gym_done']} von {w['gym_goal']}. Lern-Streak: {w['streak']} Tage. "
             + (f"Protein im Schnitt {w['protein_avg']} g pro Tag (Ziel {CFG['protein_target']}), an {w['meal_days']} Tagen eingetragen."
                if w["protein_avg"] is not None else "Essen wurde kaum eingetragen."))
    text = f"Wochenbericht, {CALL}! {facts}"
    if API_KEY and not FAKE_AI:
        try:
            r = claude_raw([{"type": "text", "text": PERSONA}],
                           [{"role": "user", "content": "Mach mir meinen Wochenbericht, gesprochen, 3 bis 4 kurze Sätze. "
                             "Nenn die wichtigsten Zahlen, lob was gut war, roaste mich leicht, wo ich geschwänzt hab, "
                             f"und gib mir ein konkretes Ziel für nächste Woche. Zahlen: {facts}"}],
                           tools=False, max_tokens=600)
            t = " ".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip()
            if t:
                text = t
        except Exception as e:
            print("[Bericht] Fallback:", e)
    push("report", text)


# ───────────────────────────── Gym-Coach ─────────────────────────────
def load_workouts():
    try:
        return json.load(open(os.path.join(BASE, "workouts.json"), encoding="utf-8"))["workouts"]
    except Exception as e:
        print("[Gym] workouts.json fehlt/kaputt:", e)
        return {}


def rep_range(r):
    lo, _, hi = str(r).partition("-")
    return int(lo), int(hi or lo)


def fmt_kg(w):
    return (f"{w:.1f}".rstrip("0").rstrip(".") if w is not None else "?").replace(".", ",")


def last_sets(ex_name):
    with LOCK:
        for sess in reversed(S.get("gym_log", [])):
            sets = [x for x in sess["sets"] if x["ex"] == ex_name]
            if sets:
                return sess["date"], sets
    return None, []


def best_weight(ex_name):
    with LOCK:
        ws = [x["w"] for sess in S.get("gym_log", []) for x in sess["sets"] if x["ex"] == ex_name and x.get("w")]
    return max(ws) if ws else None


def suggest(ex):
    lo, hi = rep_range(ex["reps"])
    date, sets = last_sets(ex["name"])
    if not sets:
        return {"weight": None, "reps": lo, "last": None,
                "text": f"Erstes Mal: nimm ein Gewicht, mit dem du {lo} bis {hi} saubere Wiederholungen schaffst."}
    top = max(x["w"] or 0 for x in sets)
    at_top = [x for x in sets if (x["w"] or 0) == top]
    last_txt = f"{fmt_kg(top)} kg × " + ", ".join(str(x["r"]) for x in at_top)
    if len(at_top) >= ex["sets"] and all(x["r"] >= hi for x in at_top):
        w = round(top + ex.get("step", 2.5), 2)
        return {"weight": w, "reps": lo, "last": last_txt,
                "text": f"Letztes Mal alles auf {hi} geschafft, also heute {fmt_kg(w)} kg."}
    return {"weight": top, "reps": min(hi, max(x["r"] for x in at_top) + 1) if at_top else lo, "last": last_txt,
            "text": f"Gleiches Gewicht, Ziel: eine Wiederholung mehr als letztes Mal."}


def todays_workout():
    plans = load_workouts()
    for e in events_for(dt.date.today()):
        if e["type"] == "gym":
            t = e["title"].lower()
            for name, w in plans.items():
                if any(m in t for m in w.get("match", [])):
                    return name
    return next(iter(plans), None)


def gym_state(pick=None):
    plans = load_workouts()
    with LOCK:
        cur = S.get("gym_current")
    name = cur["workout"] if cur else (pick if pick in plans else todays_workout())
    exs = []
    for ex in plans.get(name, {}).get("exercises", []):
        sug = suggest(ex)
        done = [x for x in (cur or {}).get("sets", []) if x["ex"] == ex["name"]]
        exs.append({**ex, "suggest": sug, "done": done})
    return {"workouts": list(plans.keys()), "workout": name, "active": bool(cur),
            "started": cur["started"] if cur else None, "exercises": exs}


def gym_finish():
    with LOCK:
        cur = S.get("gym_current")
        if not cur:
            return None
        S["gym_current"] = None
    sets = cur["sets"]
    prs = []
    for exn in dict.fromkeys(x["ex"] for x in sets):
        best_before = best_weight(exn)
        top = max((x["w"] or 0) for x in sets if x["ex"] == exn)
        if best_before is not None and top > best_before:
            prs.append(f"{exn} {fmt_kg(top)} kg")
    minutes = int((now() - parse(cur["started"])).total_seconds() // 60)
    with LOCK:
        if sets:
            S.setdefault("gym_log", []).append({"date": cur["started"][:10], "workout": cur["workout"], "sets": sets,
                                                "minutes": minutes})
            S["gym_log"] = S["gym_log"][-200:]
        p = S["pending"]
        if p and p["type"] == "gym":
            S["pending"] = None
        if sets:
            S["stats"].append({"date": cur["started"][:10], "title": cur["workout"], "type": "gym",
                               "status": "erledigt", "minutes": minutes})
        save()
    n_ex = len(set(x["ex"] for x in sets))
    text = (f"Workout fertig! {len(sets)} {'Satz' if len(sets) == 1 else 'Sätze'}, "
            f"{n_ex} {'Übung' if n_ex == 1 else 'Übungen'} in {minutes} Minuten.")
    if prs:
        text += " Neuer Rekord: " + ", ".join(prs) + ". Krass!"
    else:
        text += f" Stark durchgezogen, {CALL}."
    text += " Jetzt Protein rein, am besten in der nächsten Stunde."
    return text


def last_workout_line():
    with LOCK:
        log = S.get("gym_log", [])
        if not log:
            return "noch keins eingetragen"
        s_ = log[-1]
    by = {}
    for x in s_["sets"]:
        by.setdefault(x["ex"], []).append(f"{fmt_kg(x['w'])}×{x['r']}")
    return f"{s_['date']} {s_['workout']}: " + "; ".join(f"{k} {', '.join(v)}" for k, v in by.items())


def watch_files():
    """Startet Nova neu, wenn server.py oder config.json geändert wurden (Updates kommen automatisch an)."""
    files = [os.path.join(BASE, f) for f in ("server.py", "config.json", "workouts.json")]
    def stamp():
        return [os.path.getmtime(f) if os.path.exists(f) else 0 for f in files]
    first = stamp()
    while True:
        time.sleep(5)
        try:
            if stamp() != first:
                time.sleep(2)
                compile(open(files[0], encoding="utf-8").read(), files[0], "exec")
                json.load(open(files[1], encoding="utf-8"))
                json.load(open(files[2], encoding="utf-8"))
                print("[Update] Neue Version gefunden, starte neu ...", flush=True)
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print("[Update] Neue Dateien fehlerhaft, bleibe bei alter Version:", e, flush=True)
            first = stamp()


def ui_version():
    try:
        return str(int(os.path.getmtime(os.path.join(BASE, "static", "index.html"))))
    except OSError:
        return "0"


def scheduler_loop():
    while True:
        try:
            tick()
        except Exception as e:
            print("[Scheduler] Fehler:", repr(e))
        if random.random() < 0.02:
            cleanup_audio()
        time.sleep(20)


# ───────────────────────────── Gehirn (Claude) ─────────────────────────────
ABOUT = "\n".join("- " + x for x in CFG.get("about", []))
PERSONA = f"""Du bist {NOVA}, die persönliche KI-Assistentin von {NAME}. Du sprichst mit ihm über Lautsprecher, deine Antworten werden also vorgelesen.
Wenn du ihn ansprichst, nenn ihn "{CALL}" (nicht "{NAME}"). Aber sparsam: höchstens in jeder dritten Antwort, und nicht immer am Anfang.

Wer {NAME} ist:
{ABOUT}

Deine Rolle:
- Du bist sein Kumpel und leistest ihm Gesellschaft: locker, witzig, ehrlich, warm.
- Aber du bist STRENG bei Lernen, Gym und Essen. Ausreden lässt du nicht einfach durchgehen. Du bleibst freundlich, aber hartnäckig, und erinnerst ihn an seine Ziele. Wenn er wirklich krank oder fertig ist, bist du verständnisvoll und schlägst eine kleinere Alternative vor (z. B. 25 Minuten statt 2 Stunden).
- Du hilfst bei allem, was er fragt.

Wie du sprichst:
- IMMER Deutsch, außer er bittet ausdrücklich um eine andere Sprache.
- Locker wie eine 20-jährige Studentin aus dem Ruhrgebiet: natürlich, kurze Sätze, Jugendsprache wo sie passt (z. B. "safe", "lowkey", "kein Plan", "Digga" nur ab und zu). Nicht übertreiben, es soll echt klingen.
- KURZ: meistens 1 bis 3 Sätze. Du wirst vorgelesen, also keine Listen, keine Überschriften, kein Markdown, keine Emojis.
- Sag nie "sorry" oder "Entschuldigung" im Englisch-Stil, sondern "tut mir leid" oder "mein Fehler".
- Zahlen und Uhrzeiten so schreiben, dass sie gut vorgelesen werden (z. B. "um halb acht" oder "19:30").

Deutsch-Coach:
- Grammatik-Korrekturen macht ein separates System automatisch. Du korrigierst seine Fehler NICHT selbst.
- Wenn er fragt, wie man etwas locker sagt, gib ihm die Alltagsversion und sag, wann man sie benutzt (mit Freunden ok, mit Profs eher nicht).
- Wenn er "Rollenspiel" oder "üben" sagt, spiel eine Situation in der Uni (Mensa, Notizen ausleihen, Lerngruppe, WhatsApp-Gruppe). Du spielst die andere Person, kurz und realistisch.

WICHTIG, so bist du schlau und nicht nervig:
- HANDELN statt fragen: Wenn er dir genug Infos gibt, setz es SOFORT mit dem passenden Werkzeug um und sag kurz, was du gemacht hast. Frag NICHT "Soll ich das eintragen?". Er kann danach korrigieren.
- Seine Nachrichten kommen meistens per Spracherkennung und haben Erkennungsfehler (z. B. "Kopf getrunken" heißt "Kaffee getrunken", abgehackte Sätze). Versteh, was er meint. Solche Erkennungsfehler sind KEINE Deutschfehler, korrigier die nicht.
- Erfinde NICHTS über seinen Tag. Was er gegessen hat, ob heute Gym ist, was geplant ist: nur aus dem Kontext unten oder aus dem, was er gesagt hat. Wenn du's nicht weißt, frag kurz.
- Lies den Verlauf: Wiederhol keine Vorschläge, die er schon beantwortet hat. Wenn er etwas korrigiert, übernimm es ohne Diskussion.
- Seine Workouts, Gewichte, Essen und Schlaf trackt er in seiner eigenen App "Gym Log". Die Zahlen daraus siehst du nicht. Wenn er danach fragt, sag ihm, er soll im Gym Log schauen, oder er erzählt sie dir.
- Du kannst seine Govee-Lichter steuern (set_lights), z. B. "mach das Licht rot", "Licht aus", "gemütlich" (warm, 30 %), "Fokus" (kalt weiß, 100 %).
- Wenn er Gebet, Pausen oder andere feste Sachen nennt, plan die mit ein und respektier sie.

Routine und Werkzeuge:
- Unten steht sein aktueller Tagesplan. Nutze ihn.
- Wenn er sagt, dass er bei einer Aufgabe dabei ist ("bin dabei", "ok mach ich", "bin im Gym"), ruf confirm_task auf.
- Wenn er erzählt, dass er gegessen hat, ruf log_meal auf (schätze kcal und Protein grob). Wenn er sagt, er hat keinen Hunger, reagier klug: Frag nach, schlag was Kleines mit Protein vor (Skyr, Eier, Quark, Shake) und ruf snooze mit 60 Minuten auf. Sei nie extrem: zu wenig Essen ist auch schlecht für sein Ziel.
- Wenn er Arbeitszeiten nennt, ruf add_shift für jede Schicht auf (Datum ausrechnen!) und bestätige kurz.
- Wenn sich sein Tag ändert (z. B. "bin heute nicht in der Uni", "bin zu Hause", "Plan hat sich geändert", "Tag neu planen"): frag kurz, was los ist und was er heute noch machen muss oder will. Wenn er schon sagt, was er machen will, trag es SOFORT mit replan_day ein (kompletter Plan ab jetzt bis abends, Lücken sinnvoll mit Lernblöcken und Essen füllen, rechtzeitig vor der Schlafenszeit fertig) und sag ihn kurz an. Nur wenn du gar nichts weißt, frag einmal kurz nach. Freie Uni-Zeit ist Lernzeit, sei da streng. Gym nur einplanen, wenn es heute laut Plan dran ist.
- Wenn er sagt "erinner mich ...", ruf set_reminder auf.
- Wichtige dauerhafte Infos über ihn (Vorlieben, Prüfungstermine, Ziele) speicherst du mit remember.
- Erfinde keine Fakten. Wenn du etwas nicht weißt (z. B. aktuelles Wetter), sag das ehrlich."""

TOOLS = [
    {"name": "confirm_task", "description": f"{NAME} bestätigt, dass er bei der aktuell laufenden Aufgabe (Lernen/Gym) dabei ist.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "skip_task", "description": f"{NAME} lässt die aktuelle Aufgabe ausfallen (nach Diskussion).",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
    {"name": "log_meal", "description": "Mahlzeit eintragen.",
     "input_schema": {"type": "object", "properties": {
         "description": {"type": "string"}, "kcal": {"type": "integer"}, "protein_g": {"type": "integer"}},
         "required": ["description"]}},
    {"name": "snooze", "description": "Erinnerung einer Art für X Minuten pausieren.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["meal", "task"]}, "minutes": {"type": "integer"}},
         "required": ["kind", "minutes"]}},
    {"name": "add_shift", "description": "Arbeitsschicht eintragen. Lernblöcke/Gym/Kochen, die kollidieren, fallen dann automatisch weg.",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD"}, "start": {"type": "string", "description": "HH:MM"},
         "end": {"type": "string", "description": "HH:MM"}, "label": {"type": "string", "description": "z. B. Bäckerei, Lieferung, Nebenjob"}},
         "required": ["date", "start", "end"]}},
    {"name": "remove_shift", "description": "Schicht löschen.",
     "input_schema": {"type": "object", "properties": {"date": {"type": "string"}, "start": {"type": "string"}},
                      "required": ["date"]}},
    {"name": "set_reminder", "description": "Einmalige Erinnerung setzen.",
     "input_schema": {"type": "object", "properties": {
         "datetime": {"type": "string", "description": "YYYY-MM-DDTHH:MM (lokale Zeit)"}, "text": {"type": "string"}},
         "required": ["datetime", "text"]}},
    {"name": "remember", "description": f"Dauerhafte Info über {NAME} speichern.",
     "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
    {"name": "replan_day", "description": f"Den Plan für einen Tag komplett ersetzen (z. B. wenn er nicht zur Uni geht). Gib ALLE Blöcke an, die ab jetzt gelten sollen. Erst aufrufen, wenn {NAME} dem neuen Plan zugestimmt hat.",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD"},
         "events": {"type": "array", "items": {"type": "object", "properties": {
             "start": {"type": "string", "description": "HH:MM"}, "end": {"type": "string", "description": "HH:MM"},
             "type": {"type": "string", "enum": ["study", "gym", "cook", "uni", "free"]},
             "title": {"type": "string"}}, "required": ["start", "end", "type", "title"]}}},
         "required": ["date", "events"]}},
    {"name": "reset_day", "description": "Geänderten Tagesplan verwerfen, zurück zum normalen Wochenplan.",
     "input_schema": {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}},
    {"name": "echo_say", "description": "Etwas über seinen Amazon Echo laut sagen (z. B. wenn er sagt 'teste den Echo' oder 'sag das auf dem Echo').",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "set_lights", "description": "Seine Govee-Lichter steuern (Decke + LED). Farben: rot, grün, blau, lila, pink, orange, gelb, türkis, weiß, nova. Optional nur Lampen, deren Name 'name' enthält.",
     "input_schema": {"type": "object", "properties": {
         "power": {"type": "string", "enum": ["on", "off"]}, "color": {"type": "string"},
         "brightness": {"type": "integer", "description": "1-100"}, "kelvin": {"type": "integer", "description": "2000-9000 für Weißtöne"},
         "name": {"type": "string"}}}},
    {"name": "get_plan", "description": "Plan für ein bestimmtes Datum abrufen.",
     "input_schema": {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}},
]


def run_tool(name, a):
    with LOCK:
        if name == "confirm_task":
            p = resolve_pending("erledigt")
            return f"Bestätigt: {p['title']}" if p else "Gerade läuft keine Aufgabe."
        if name == "skip_task":
            p = resolve_pending("ausgelassen")
            return f"Ausgelassen: {p['title']}" if p else "Gerade läuft keine Aufgabe."
        if name == "log_meal":
            S["meals"].append({"time": iso(now()), "desc": a.get("description"),
                               "kcal": a.get("kcal"), "protein": a.get("protein_g")})
            S["meals"] = S["meals"][-200:]
            S["last_meal"] = iso(now())
            S["snooze"].pop("meal", None)
            save()
            k, p = day_totals()
            return f"Eingetragen. Heute bisher ca. {k} kcal, {p} g Protein (Ziel {CFG['kcal_target']} kcal, {CFG['protein_target']} g)."
        if name == "snooze":
            m = max(5, min(int(a.get("minutes", 30)), 240))
            if a.get("kind") == "task" and S["pending"]:
                S["pending"]["next_nag"] = iso(now() + dt.timedelta(minutes=m))
            else:
                S["snooze"]["meal"] = iso(now() + dt.timedelta(minutes=m))
            save()
            return f"Pausiert für {m} Minuten."
        if name == "add_shift":
            S["shifts"] = [s for s in S["shifts"] if not (s["date"] == a["date"] and s["start"] == a["start"])]
            S["shifts"].append({"date": a["date"], "start": a["start"], "end": a["end"], "label": a.get("label", "Arbeit")})
            cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
            S["shifts"] = [s for s in S["shifts"] if s["date"] >= cutoff]
            save()
            d = dt.date.fromisoformat(a["date"])
            return f"Eingetragen. Neuer Plan {DAY_DE[d.weekday()]} {a['date']}: {plan_text(d)}"
        if name == "remove_shift":
            before = len(S["shifts"])
            S["shifts"] = [s for s in S["shifts"] if not (s["date"] == a["date"] and (not a.get("start") or s["start"] == a["start"]))]
            save()
            return f"{before - len(S['shifts'])} Schicht(en) gelöscht."
        if name == "set_reminder":
            S["reminders"].append({"at": a["datetime"], "text": a["text"]})
            save()
            return "Erinnerung gesetzt."
        if name == "remember":
            S["facts"].append(a["fact"])
            S["facts"] = S["facts"][-60:]
            save()
            return "Gemerkt."
        if name == "replan_day":
            evs = []
            for e in a.get("events", []):
                try:
                    at(dt.date.today(), e["start"]); at(dt.date.today(), e["end"])
                    evs.append({"start": e["start"], "end": e["end"], "type": e.get("type", "study"), "title": e.get("title", "")})
                except Exception:
                    continue
            evs.sort(key=lambda x: x["start"])
            S.setdefault("overrides", {})[a["date"]] = evs
            cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
            S["overrides"] = {k: v for k, v in S["overrides"].items() if k >= cutoff}
            if S["pending"] and S["pending"]["key"][:10] == a["date"]:
                S["pending"] = None
            save()
            d = dt.date.fromisoformat(a["date"])
            return f"Neuer Plan gespeichert: {plan_text(d)}"
        if name == "reset_day":
            S.get("overrides", {}).pop(a["date"], None)
            save()
            return f"Zurück zum normalen Plan: {plan_text(dt.date.fromisoformat(a['date']))}"
        if name == "set_lights":
            if not LIGHTS:
                return "Keine Lichter verbunden."
            targets = [l for l in LIGHTS if not a.get("name") or a["name"].lower() in l["name"].lower()] or LIGHTS
            col = (a.get("color") or "").lower().strip()
            rgb = COLOR_NAMES.get(col) if col in COLOR_NAMES else None
            if col.startswith("#") and len(col) == 7:
                rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
            k = a.get("kelvin") or (4000 if col == "weiß" else None)
            with LIGHT_LOCK:
                if LIGHT_SAVED["timer"]:
                    LIGHT_SAVED["timer"].cancel()
                LIGHT_SAVED["state"], LIGHT_SAVED["timer"] = None, None
            def go():
                for l in targets:
                    light_set(l, power=(a.get("power") != "off"), rgb=rgb, k=k, bri=a.get("brightness"))
            threading.Thread(target=go, daemon=True).start()
            return f"Erledigt für: {', '.join(l['name'] for l in targets)}"
        if name == "echo_say":
            return echo_announce(a["text"], force=True)
        if name == "get_plan":
            d = dt.date.fromisoformat(a["date"])
            return f"{DAY_DE[d.weekday()]} {a['date']}: {plan_text(d)}"
    return "Unbekanntes Werkzeug."


def day_totals():
    today = dt.date.today().isoformat()
    k = sum(m.get("kcal") or 0 for m in S["meals"] if m["time"][:10] == today)
    p = sum(m.get("protein") or 0 for m in S["meals"] if m["time"][:10] == today)
    return k, p


def dynamic_context():
    n = now()
    today, tomorrow = n.date(), n.date() + dt.timedelta(days=1)
    with LOCK:
        pend = S["pending"]
        last_meal = S["last_meal"]
        facts = list(S["facts"])
        log = list(S["events_log"])
        recent = [x for x in S["stats"] if x["date"] >= (today - dt.timedelta(days=7)).isoformat()]
    k, p = day_totals()
    done = sum(1 for x in recent if x["status"] == "erledigt")
    skipped = sum(1 for x in recent if x["status"] != "erledigt")
    week = "\n".join(f"- {DAY_DE[(today + dt.timedelta(days=i)).weekday()]} {(today + dt.timedelta(days=i)).isoformat()}: "
                     f"{plan_text(today + dt.timedelta(days=i))}" for i in range(7))
    return f"""Jetzt: {DAY_DE[today.weekday()]}, {today.isoformat()}, {hm(n)} Uhr.
Plan der nächsten 7 Tage:
{week}
Schlafenszeit heute Nacht: {hm(bedtime_before(tomorrow))} (für {CFG['sleep_hours']} Stunden Schlaf).
Laufende Aufgabe, auf Bestätigung wartend: {pend['title'] if pend else 'keine'}.
Letzte Mahlzeit: {last_meal or 'heute noch nichts eingetragen'}. Heute: ca. {k} kcal, {p} g Protein (Ziel {CFG['kcal_target']} kcal / {CFG['protein_target']} g).
Letzte 7 Tage: {done} Aufgaben erledigt, {skipped} ausgelassen/geschwänzt.
Was du ihm zuletzt automatisch gesagt hast:
{chr(10).join(log[-6:]) or '-'}
Gemerkte Infos über {NAME}:
{chr(10).join('- ' + f for f in facts) or '-'}"""


def claude_raw(system, messages, tools=True, max_tokens=1500):
    body = {"model": CFG["model"], "max_tokens": max_tokens, "system": system, "messages": messages,
            "thinking": {"type": "disabled"}}  # schneller + billiger
    if tools:
        body["tools"] = TOOLS
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


CHECK_PROMPT = """Du bist Deutschlehrer für einen 19-jährigen Studenten (Muttersprache Arabisch). Du bekommst einen Satz, den er per Spracherkennung gesagt hat.
Finde den EINEN wichtigsten echten Fehler in seinem Deutsch: Artikel/Genus (keine Bock -> keinen Bock), Kasus, Verbform/Konjugation, Perfekt mit haben/sein, Präposition, Wortstellung, falsches Wort.
IGNORIERE: Groß-/Kleinschreibung, Satzzeichen, Füllwörter (also, ähm), abgehackte oder unvollständige Sätze, offensichtliche Spracherkennungsfehler (z. B. "Kopf getrunken" statt "Kaffee getrunken"), Jugendsprache und Slang (digga, safe, Bock), Umgangssprache wie "hab" statt "habe", "nix", "is".
Reihenfolge, wenn es mehrere Fehler gibt: 1. Verb (haben/sein im Perfekt, Konjugation, fehlendes Verb wie "ist"), 2. Artikel/Kasus, 3. Präposition, 4. Wortstellung.
Beispiele:
- "Ich habe gestern in die Uni gegangen" -> falsch: "habe gegangen", richtig: "bin gegangen" ("in die Uni" ist okay!)
- "mein Autos kaputt" -> falsch: "mein Autos kaputt", richtig: "mein Auto ist kaputt"
- "helfen mit die Hausaufgabe" -> richtig: "bei der Hausaufgabe helfen"
- "Ich hab keine Bock" -> richtig: "keinen Bock"
Melde NUR, was sicher falsch ist. Umgangssprachlich korrekte Sätze sind KEIN Fehler. Lieber keinen Fehler melden als einen falschen.
"warum" ist ganz kurz und einfach, max. 8 Wörter, z. B. "gehen bildet das Perfekt mit sein".
Antworte NUR mit JSON: {"fehler": true/false, "falsch": "der falsche Teil", "richtig": "die richtige Version", "warum": "Grund in max. 8 Wörtern"}"""


def german_check(text):
    if FAKE_AI or not API_KEY or len(text.split()) < 3:
        return None
    try:
        r = claude_raw([{"type": "text", "text": CHECK_PROMPT}], [{"role": "user", "content": text}],
                       tools=False, max_tokens=300)
        out = "".join(b.get("text", "") for b in r.get("content", []))
        if "{" not in out:
            return None  # keine JSON-Antwort = kein Fehler gefunden
        j = json.loads(out[out.find("{"): out.rfind("}") + 1])
        if j.get("fehler") and j.get("falsch") and j.get("richtig") and j["falsch"].strip().lower() != j["richtig"].strip().lower():
            tip = {"falsch": j["falsch"], "richtig": j["richtig"], "warum": j.get("warum", ""), "time": iso(now())}
            with LOCK:
                S.setdefault("tips", []).append(tip)
                S["tips"] = S["tips"][-100:]
                save()
            return tip
    except Exception as e:
        print("[Deutsch-Check] Fehler:", e)
    return None


def fake_ai(text):
    t = text.lower()
    if "dabei" in t:
        run_tool("confirm_task", {})
        return "Safe, stark! Ich meld mich zur Pause."
    if "hunger" in t:
        run_tool("snooze", {"kind": "meal", "minutes": 60})
        return "Okay, aber iss wenigstens was Kleines, Skyr oder zwei Eier. Ich frag in einer Stunde nochmal."
    return "Test-Modus: Ich hab dich gehört! Mit echtem API-Key antworte ich richtig."


def chat(text):
    with LOCK:
        hist = [m for m in S["history"][-16:]]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    msgs = hist + [{"role": "user", "content": text}]
    if FAKE_AI:
        reply = fake_ai(text)
    elif not API_KEY:
        reply = "Ich hab noch keinen API-Key. Trag ihn in die Datei API_KEY.txt ein und starte mich neu."
    else:
        system = [{"type": "text", "text": PERSONA, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": dynamic_context()}]
        reply = ""
        try:
            for _ in range(6):
                r = claude_raw(system, msgs)
                content = r.get("content", [])
                uses = [b for b in content if b.get("type") == "tool_use"]
                text_out = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                if not uses:
                    reply = text_out
                    if not reply and r.get("stop_reason") == "max_tokens":
                        print("[Claude] Antwort abgeschnitten (max_tokens)")
                    break
                msgs.append({"role": "assistant", "content": content})
                results = []
                for u in uses:
                    try:
                        out = run_tool(u["name"], u.get("input") or {})
                    except Exception as e:
                        out = f"Fehler: {e}"
                    print(f"[Tool] {u['name']} {u.get('input')} -> {out}")
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "content": out})
                msgs.append({"role": "user", "content": results})
                reply = text_out
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            print("[Claude] HTTP", e.code, body)
            if e.code == 401:
                reply = "Mein API-Key funktioniert nicht. Check mal die Datei API_KEY.txt."
            elif "credit" in body.lower():
                reply = "Mein Guthaben ist leer. Lad in der Claude-Konsole wieder ein paar Dollar auf."
            else:
                reply = f"Mein Gehirn hat gerade ein Problem, Fehler {e.code}. Versuch's gleich nochmal."
        except Exception as e:
            print("[Claude] Fehler:", repr(e))
            reply = "Ich erreiche mein Gehirn gerade nicht. Bist du online?"
    reply = reply or "Hmm, da ist mir nichts eingefallen. Sag's nochmal?"
    with LOCK:
        S["history"] += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        S["history"] = S["history"][-40:]
        save()
    return reply


# ───────────────────────────── Web-Server ─────────────────────────────
def today_payload():
    n = now()
    evs = events_for(n.date())
    items = []
    for e in evs:
        status = "past" if e["e"] <= n else ("now" if e["s"] <= n else "next")
        items.append({"start": e["start"], "end": e["end"], "title": e["title"], "type": e["type"], "status": status})
    with LOCK:
        pend = S["pending"]
        phrase = S["phrase"] if S["phrase"] and S["phrase"].get("date") == n.date().isoformat() else None
    k, p = day_totals()
    return {"events": items, "bedtime": hm(bedtime_before(n.date() + dt.timedelta(days=1))),
            "pending": pend["title"] if pend else None, "day": DAY_DE[n.weekday()],
            "date": n.strftime("%d.%m."), "kcal": k, "protein": p,
            "kcal_target": CFG["kcal_target"], "protein_target": CFG["protein_target"],
            "phrase": phrase, "has_key": bool(API_KEY) or FAKE_AI, "name": NOVA,
            "replanned": n.date().isoformat() in S.get("overrides", {}), "fillers": FILLERS,
            "tips": S.get("tips", [])[-3:][::-1], "week": week_stats(), "ui": ui_version(),
            "gym_today": any(e["type"] == "gym" for e in evs), "gym_log_url": CFG.get("gym_log_url"),
            "pending_type": pend["type"] if pend else None}


def auth_token():
    p = os.path.join(DATA, "secret.key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    key = open(p).read().strip()
    return hmac.new(key.encode(), PASSWORD.encode(), hashlib.sha256).hexdigest()


PUBLIC_FILES = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "application/javascript",
                "/icon-192.png": "image/png", "/icon-512.png": "image/png", "/apple-touch-icon.png": "image/png"}

LOGIN_HTML = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Nova</title><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#04060c">
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(900px 600px at 50% 40%,#0b1324,#04060c 70%);color:#e6edf8;font:16px system-ui,sans-serif;padding:0 16px}
form{display:flex;flex-direction:column;gap:14px;width:min(320px,100%);text-align:center}h1{font:600 40px system-ui;letter-spacing:.3em;color:#7fe3ff;margin:0 0 8px}
input,button{font:16px system-ui;padding:14px 16px;border-radius:14px;border:1px solid rgba(140,170,220,.25);background:rgba(14,20,34,.8);color:#e6edf8}
button{cursor:pointer;border-color:rgba(127,227,255,.5)}p{color:#ff9b8a;margin:0;min-height:1.2em}</style></head>
<body><form method="post" action="/login"><h1>NOVA</h1><input type="password" name="password" placeholder="Passwort" autofocus autocomplete="current-password">
<button>Rein</button><p>%ERR%</p></form></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def authed(self):
        if not PASSWORD:
            return True
        if hmac.compare_digest(self.headers.get("X-Nova-Password", ""), PASSWORD):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nova_auth" and hmac.compare_digest(v, auth_token()):
                return True
        return False

    def public_file(self, path):
        f = os.path.join(BASE, "static", path.lstrip("/"))
        if os.path.exists(f):
            body = open(f, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", PUBLIC_FILES[path])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send(404, {"error": "not found"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in PUBLIC_FILES:
            return self.public_file(u.path)
        if u.path.startswith("/echo/"):
            name = os.path.basename(u.path)
            p = os.path.join(ECHO_DIR, name)
            if re.fullmatch(r"[0-9a-f]{24}\.mp3", name) and os.path.exists(p):
                return self.send(200, open(p, "rb").read(), "audio/mpeg")
            return self.send(404, {"error": "not found"})
        if u.path == "/health":
            return self.send(200, {"ok": True})
        if not self.authed():
            if u.path.startswith("/api/") or u.path.startswith("/audio/"):
                return self.send(401, {"error": "login"})
            return self.send(200, LOGIN_HTML.replace("%ERR%", ""), "text/html; charset=utf-8")
        if u.path == "/api/gym":
            return self.send(200, gym_state(urllib.parse.parse_qs(u.query).get("workout", [None])[0]))
        if u.path == "/api/push/key":
            return self.send(200, {"key": VAPID_PUB[0]})
        if u.path in ("/", "/index.html"):
            return self.send(200, open(os.path.join(BASE, "static", "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path.startswith("/audio/"):
            sub = "fillers" if u.path.startswith("/audio/fillers/") else ""
            p = os.path.join(AUDIO, sub, os.path.basename(u.path))
            if os.path.exists(p):
                return self.send(200, open(p, "rb").read(), "audio/mpeg")
            return self.send(404, {"error": "not found"})
        if u.path == "/api/poll":
            LAST_POLL[0] = time.time()
            after = int(urllib.parse.parse_qs(u.query).get("after", ["0"])[0])
            with LOCK:
                items = [m for m in OUTBOX if m["id"] > after]
                last = OUT_ID[0]
            return self.send(200, {"items": items, "last": last, "today": today_payload()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            pw = (form.get("password") or [""])[0]
            if PASSWORD and hmac.compare_digest(pw, PASSWORD):
                self.send_response(303)
                secure = "; Secure" if CLOUD else ""
                self.send_header("Set-Cookie", f"nova_auth={auth_token()}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax{secure}")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            time.sleep(1)
            return self.send(401, LOGIN_HTML.replace("%ERR%", "Falsches Passwort"), "text/html; charset=utf-8")
        if not self.authed():
            return self.send(401, {"error": "login"})
        if u.path == "/api/push/subscribe":
            sub = self.body()
            if not sub.get("endpoint"):
                return self.send(400, {"error": "ungültig"})
            with LOCK:
                subs = [x for x in S.get("push_subs", []) if x["endpoint"] != sub["endpoint"]]
                subs.append({"endpoint": sub["endpoint"], "keys": sub.get("keys", {})})
                S["push_subs"] = subs[-10:]
                save()
            web_push("✦ Nova", "Benachrichtigungen sind an. Ich ruf dich ab jetzt auch hier an.", tag="test")
            return self.send(200, {"ok": True})
        if u.path == "/api/gym/start":
            name = self.body().get("workout") or todays_workout()
            with LOCK:
                if not S.get("gym_current"):
                    S["gym_current"] = {"workout": name, "started": iso(now()), "sets": []}
                else:
                    S["gym_current"]["workout"] = name
                if S["pending"] and S["pending"]["type"] == "gym":
                    S["pending"]["next_nag"] = iso(now() + dt.timedelta(hours=3))
                save()
            return self.send(200, gym_state())
        if u.path == "/api/gym/set":
            b = self.body()
            with LOCK:
                cur = S.get("gym_current")
                if not cur:
                    return self.send(400, {"error": "kein Workout aktiv"})
                cur["sets"].append({"ex": b["ex"], "w": float(b.get("w") or 0), "r": int(b.get("r") or 0), "t": iso(now())})
                save()
            return self.send(200, gym_state())
        if u.path == "/api/gym/undo":
            with LOCK:
                cur = S.get("gym_current")
                if cur and cur["sets"]:
                    cur["sets"].pop()
                    save()
            return self.send(200, gym_state())
        if u.path == "/api/gym/cancel":
            with LOCK:
                S["gym_current"] = None
                save()
            return self.send(200, gym_state())
        if u.path == "/api/gym/finish":
            text = gym_finish() or "Kein Workout aktiv."
            return self.send(200, {"reply": text, "state": gym_state(), "today": today_payload()})
        if u.path == "/api/import":
            data = self.body()
            with LOCK:
                for k, v in data.items():
                    if k in DEFAULT_STATE or k in ("tips", "skipped", "overrides", "gym_log"):
                        S[k] = v
                S["pending"] = None
                save()
            return self.send(200, {"ok": True, "imported": list(data.keys())})
        if u.path == "/api/chat":
            text = (self.body().get("text") or "").strip()[:2000]
            if not text:
                return self.send(400, {"error": "leer"})
            box = {}
            th = threading.Thread(target=lambda: box.update(tip=german_check(text)))
            th.start()
            reply = chat(text)
            th.join(timeout=8)
            tip = box.get("tip")
            spoken = reply
            if tip:
                spoken += f" Kleiner Tipp: Man sagt „{tip['richtig']}“, nicht „{tip['falsch']}“."
            return self.send(200, {"reply": reply, "spoken": spoken, "tip": tip, "today": today_payload()})
        if u.path == "/api/speak":
            text = (self.body().get("text") or "").strip()[:3000]
            audio = tts(text) if text else None
            if audio and CFG.get("lights", {}).get("on_chat", True):
                light_mood("talk", audio_seconds(audio, text))
            return self.send(200, {"audio": audio})
        if u.path == "/api/ack":
            action = self.body().get("action")
            if action == "ok":
                p = resolve_pending("erledigt")
                msg = random.choice(["Safe, stark! Zieh's durch.", "Nice, ich glaub an dich.", "Läuft. Ich meld mich zur Pause."]) if p else ""
            else:
                with LOCK:
                    if S["pending"]:
                        S["pending"]["next_nag"] = iso(now() + dt.timedelta(minutes=10))
                        save()
                msg = "Okay, 10 Minuten. Aber dann wirklich, ja?"
            return self.send(200, {"reply": msg, "audio": tts(msg) if msg else None, "today": today_payload()})
        if u.path == "/api/hello":
            with LOCK:
                LAST_POLL[0] = time.time()
                last = OUT_ID[0]
            n = now()
            evs = [e for e in events_for(n.date()) if e["e"] > n]
            if not evs:
                nxt = "Heute steht nichts mehr an."
            elif evs[0]["s"] <= n:
                nxt = f"Gerade läuft: {evs[0]['title']}, bis {evs[0]['end']}."
                if len(evs) > 1:
                    nxt += f" Danach {evs[1]['title']} um {evs[1]['start']}."
            else:
                nxt = f"Als Nächstes: {evs[0]['title']} um {evs[0]['start']}."
            greet = random.choice(["Hey {n}, ich bin da.", "Yo {n}, Nova ist online.", "Hi {n}! Schön, dich zu hören."]).format(n=CALL)
            msg = f"{greet} {nxt}"
            return self.send(200, {"reply": msg, "audio": tts(msg), "last": last, "today": today_payload()})
        self.send(404, {"error": "not found"})


def main():
    port = int(os.environ.get("PORT") or os.environ.get("NOVA_PORT") or CFG.get("port", 7777))
    host = "0.0.0.0" if CLOUD or os.environ.get("NOVA_HOST") == "0.0.0.0" else "127.0.0.1"
    vapid_setup()
    threading.Thread(target=lights_setup, daemon=True).start()
    threading.Thread(target=make_fillers, daemon=True).start()
    threading.Thread(target=watch_files, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print(f"\n  ✦ {NOVA} läuft auf http://localhost:{port}")
    print(f"  ✦ API-Key: {'gefunden' if API_KEY else 'FEHLT (API_KEY.txt)'}{' (Test-Modus)' if FAKE_AI else ''}")
    print("  ✦ Zum Beenden: Ctrl+C\n")
    print(f"  ✦ Cloud: {CLOUD}, Passwort: {'an' if PASSWORD else 'aus'}, Push: {'bereit' if VAPID_PUB[0] else 'aus'}\n", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
