"""
RunHealth ingest — serveur pour le data field Garmini.

Reçoit les métriques de la montre Forerunner 165 (POST JSON sur /), détecte
chaque kilomètre parcouru, agrège les données de ce km, puis appelle l'API
Gemini pour analyser le km courant en le comparant aux km précédents et
renvoyer un retour constructif sur l'état de forme du coureur.

Routes :
  POST /          -> ingère un point de mesure JSON
  GET  /          -> { count, last, kmCount, service }
  GET  /data      -> liste complète des points reçus
  GET  /kms       -> résumés agrégés par kilomètre
  GET  /analysis  -> dernière analyse Gemini
  GET  /analyses  -> toutes les analyses Gemini
  POST /reset     -> réinitialise l'état (nouvelle course)
  GET  /health    -> { ok: true }

Variables d'environnement :
  GEMINI_API_KEY  (requis pour l'analyse)   clé Google AI Studio
  GEMINI_MODEL    (optionnel)               défaut: gemini-2.5-flash

Démarrage local :
  pip install -r requirements.txt
  python app.py            # http://localhost:5000
"""

import os
import time
import logging
import threading
from collections import deque

import requests
from flask import Flask, request, jsonify

# Logs visibles dans la console Render.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("runhealth")
log.setLevel(logging.INFO)

# Sous gunicorn (Render), rattacher nos logs aux handlers de gunicorn pour
# qu'ils apparaissent bien dans la console.
_gunicorn_logger = logging.getLogger("gunicorn.error")
if _gunicorn_logger.handlers:
    log.handlers = _gunicorn_logger.handlers
    log.setLevel(_gunicorn_logger.level)
log.propagate = False

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Pushcut : notification push (arrive iPhone verrouillé, app fermée).
# On utilise l'endpoint /notifications/<NOM> et PAS /execute : /execute
# nécessite l'Automation Server au premier plan, alors qu'une notification
# push passe par Apple et s'affiche même verrouillé.
PUSHCUT_SECRET = os.environ.get("PUSHCUT_SECRET", "zK_G6KUQHQcPfHN-g8Dyi")
PUSHCUT_NOTIFICATION = os.environ.get("PUSHCUT_NOTIFICATION", "Gemini")
PUSHCUT_URL = os.environ.get(
    "PUSHCUT_URL",
    f"https://api.pushcut.io/{PUSHCUT_SECRET}/notifications/{PUSHCUT_NOTIFICATION}",
)

MAX_POINTS = 20000
KM_METERS = 1000.0

EXPECTED_FIELDS = [
    "timerTime",
    "elapsedDistance",
    "currentHeartRate",
    "currentSpeed",
    "currentCadence",
    "energyExpenditure",
    "altitude",
    "trainingEffect",
    "currentOxygenSaturation",
    "heartRateZones",
]

# ---------------------------------------------------------------------------
# État (en mémoire, protégé par un verrou)
# ---------------------------------------------------------------------------
LOCK = threading.Lock()
DATA = deque(maxlen=MAX_POINTS)   # tous les points bruts
KM_POINTS = []                    # points du km en cours d'accumulation
KM_SUMMARIES = []                 # résumés des km terminés (avec feedback)
LAST_DISTANCE = 0.0               # dernière distance vue (détection reset)
CURRENT_KM = 0                    # nb de km complets déjà franchis


def _reset_state():
    global KM_POINTS, KM_SUMMARIES, LAST_DISTANCE, CURRENT_KM
    KM_POINTS = []
    KM_SUMMARIES = []
    LAST_DISTANCE = 0.0
    CURRENT_KM = 0


# ---------------------------------------------------------------------------
# Agrégation d'un kilomètre
# ---------------------------------------------------------------------------
def _avg(points, field):
    vals = [p[field] for p in points if p.get(field) is not None]
    return (sum(vals) / len(vals)) if vals else None


def _max(points, field):
    vals = [p[field] for p in points if p.get(field) is not None]
    return max(vals) if vals else None


def _min(points, field):
    vals = [p[field] for p in points if p.get(field) is not None]
    return min(vals) if vals else None


def _last(points, field):
    for p in reversed(points):
        if p.get(field) is not None:
            return p[field]
    return None


def _pace_str(avg_speed):
    """m/s -> 'mm:ss /km'."""
    if not avg_speed or avg_speed <= 0:
        return None
    sec = 1000.0 / avg_speed
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def summarize_km(km_number, points):
    """Construit le résumé statistique d'un kilomètre."""
    avg_speed = _avg(points, "currentSpeed")
    timer_vals = [p["timerTime"] for p in points if p.get("timerTime") is not None]
    duration_s = None
    if len(timer_vals) >= 2:
        duration_s = (max(timer_vals) - min(timer_vals)) / 1000.0

    alt_max = _max(points, "altitude")
    alt_min = _min(points, "altitude")
    elevation_gain = (alt_max - alt_min) if (alt_max is not None and alt_min is not None) else None

    return {
        "km": km_number,
        "samples": len(points),
        "durationSec": round(duration_s, 1) if duration_s is not None else None,
        "avgHeartRate": round(_avg(points, "currentHeartRate"), 1) if _avg(points, "currentHeartRate") is not None else None,
        "maxHeartRate": _max(points, "currentHeartRate"),
        "avgSpeed": round(avg_speed, 2) if avg_speed is not None else None,
        "pace": _pace_str(avg_speed),
        "avgCadence": round(_avg(points, "currentCadence"), 1) if _avg(points, "currentCadence") is not None else None,
        "avgEnergyExpenditure": round(_avg(points, "energyExpenditure"), 2) if _avg(points, "energyExpenditure") is not None else None,
        "avgSpO2": round(_avg(points, "currentOxygenSaturation"), 1) if _avg(points, "currentOxygenSaturation") is not None else None,
        "minSpO2": _min(points, "currentOxygenSaturation"),
        "trainingEffect": _last(points, "trainingEffect"),
        "elevationGain": round(elevation_gain, 1) if elevation_gain is not None else None,
        "heartRateZones": _last(points, "heartRateZones"),
        "feedback": None,          # rempli par Gemini (async)
        "feedbackStatus": "pending",
        "createdAt": time.time(),
    }


# ---------------------------------------------------------------------------
# Appel Gemini
# ---------------------------------------------------------------------------
def build_prompt(current, previous):
    lines = []
    lines.append("Tu es un coach de course à pied et physiologiste de l'effort.")
    lines.append(
        "Analyse les données du kilomètre courant et compare-les aux kilomètres "
        "précédents. Donne un retour CONSTRUCTIF et BREF (3 à 5 phrases) en français "
        "sur l'état de forme du coureur : repère une éventuelle fatigue, dérive "
        "cardiaque (HR qui monte à allure égale), baisse de SpO2, perte d'allure ou "
        "de cadence. Termine par un conseil actionnable. "
        "Ne pose JAMAIS de diagnostic médical ; reste prudent et factuel."
    )
    lines.append("")
    lines.append(f"KILOMÈTRE COURANT (#{current['km']}):")
    lines.append(_fmt_km(current))
    lines.append("")
    if previous:
        lines.append("KILOMÈTRES PRÉCÉDENTS:")
        for km in previous:
            lines.append(_fmt_km(km))
    else:
        lines.append("Aucun kilomètre précédent (c'est le premier).")
    return "\n".join(lines)


def _fmt_km(km):
    return (
        f"- km {km['km']}: durée={km.get('durationSec')}s, "
        f"HR moy={km.get('avgHeartRate')} max={km.get('maxHeartRate')} bpm, "
        f"allure={km.get('pace')} /km (vitesse {km.get('avgSpeed')} m/s), "
        f"cadence={km.get('avgCadence')} ppm, "
        f"SpO2 moy={km.get('avgSpO2')}% min={km.get('minSpO2')}%, "
        f"énergie={km.get('avgEnergyExpenditure')} kcal/min, "
        f"D+={km.get('elevationGain')} m, TE={km.get('trainingEffect')}"
    )


def call_gemini(prompt):
    """Retourne (texte, status). status = 'ok' | 'no_key' | 'error: ...'."""
    if not GEMINI_API_KEY:
        return (None, "no_key")
    try:
        resp = requests.post(
            GEMINI_URL,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        if resp.status_code != 200:
            return (None, f"error: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return (text, "ok")
    except Exception as exc:  # noqa: BLE001
        return (None, f"error: {exc}")


def send_to_pushcut(text, km_number):
    """Envoie le feedback Gemini en notification push Pushcut (POST JSON).

    Notification push -> s'affiche iPhone verrouillé et app fermée.
    Retourne (ok: bool, info: str). Le texte Gemini va dans `text`,
    le titre indique le km. `isTimeSensitive` la fait passer même en
    mode Concentration.
    """
    if not PUSHCUT_URL:
        return (False, "no_url")
    try:
        resp = requests.post(
            PUSHCUT_URL,
            headers={"Content-Type": "application/json"},
            json={
                "title": f"Garmini — km {km_number}",
                "text": text,
                # `input` est transmis au Raccourci iPhone lancé par la notif :
                # côté Raccourci on le récupère via "Entrée du raccourci".
                "input": text,
                "isTimeSensitive": True,
            },
            timeout=15,
        )
        if 200 <= resp.status_code < 300:
            return (True, f"HTTP {resp.status_code}")
        return (False, f"HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"error: {exc}")


def analyze_km_async(summary, previous_snapshot):
    """Thread : appelle Gemini puis écrit le feedback dans le résumé partagé."""
    prompt = build_prompt(summary, previous_snapshot)
    text, status = call_gemini(prompt)
    with LOCK:
        summary["feedback"] = text
        summary["feedbackStatus"] = status
    if status == "ok":
        log.info("GEMINI km %s -> %s", summary["km"], text)
        # On relaie le feedback vers Pushcut.
        ok, info = send_to_pushcut(text, summary["km"])
        with LOCK:
            summary["pushcutStatus"] = "ok" if ok else info
        if ok:
            log.info("PUSHCUT km %s -> envoyé (%s)", summary["km"], info)
        else:
            log.warning("PUSHCUT km %s échec : %s", summary["km"], info)
    else:
        log.warning("GEMINI km %s status=%s", summary["km"], status)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.post("/")
def ingest():
    global LAST_DISTANCE, CURRENT_KM, KM_POINTS

    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "JSON invalide ou corps vide"}), 400

    record = {k: payload.get(k) for k in EXPECTED_FIELDS}
    record["receivedAt"] = time.time()

    # Log du point reçu (toutes les ~10 s).
    log.info(
        "POINT  t=%sms  dist=%sm  HR=%s bpm  v=%s m/s  cad=%s  SpO2=%s%%  "
        "kcal/min=%s  alt=%sm  TE=%s",
        record.get("timerTime"),
        record.get("elapsedDistance"),
        record.get("currentHeartRate"),
        record.get("currentSpeed"),
        record.get("currentCadence"),
        record.get("currentOxygenSaturation"),
        record.get("energyExpenditure"),
        record.get("altitude"),
        record.get("trainingEffect"),
    )

    triggered = []  # (summary, previous_snapshot) à analyser hors verrou

    with LOCK:
        DATA.append(record)

        dist = record.get("elapsedDistance")
        if isinstance(dist, (int, float)):
            # Détection d'une nouvelle course (distance qui repart à zéro).
            if dist < LAST_DISTANCE - 50:
                log.info("Nouvelle course détectée (distance %sm < %sm) - reset", dist, LAST_DISTANCE)
                _reset_state()

            KM_POINTS.append(record)

            new_km = int(dist // KM_METERS)
            while new_km > CURRENT_KM:
                km_number = CURRENT_KM + 1
                summary = summarize_km(km_number, KM_POINTS)
                KM_SUMMARIES.append(summary)
                previous_snapshot = [
                    {kk: s[kk] for kk in s if kk != "feedback"}
                    for s in KM_SUMMARIES[:-1]
                ]
                triggered.append((summary, previous_snapshot))
                log.info(
                    "KM %s terminé  -> HR moy=%s max=%s  allure=%s/km  cad=%s  "
                    "SpO2 moy=%s min=%s  durée=%ss  (analyse Gemini lancée)",
                    summary["km"], summary["avgHeartRate"], summary["maxHeartRate"],
                    summary["pace"], summary["avgCadence"], summary["avgSpO2"],
                    summary["minSpO2"], summary["durationSec"],
                )
                KM_POINTS = []          # on démarre l'accumulation du km suivant
                CURRENT_KM = km_number

            LAST_DISTANCE = dist

    # Lance les analyses Gemini en tâche de fond (hors verrou, non bloquant).
    for summary, prev in triggered:
        threading.Thread(
            target=analyze_km_async, args=(summary, prev), daemon=True
        ).start()

    return jsonify({"status": "ok", "count": len(DATA), "kmCount": CURRENT_KM}), 200


@app.get("/")
def root():
    with LOCK:
        last = list(DATA)[-5:]
        return jsonify({
            "count": len(DATA),
            "kmCount": CURRENT_KM,
            "last": list(reversed(last)),
            "service": "RunHealth ingest",
        })


@app.get("/data")
def data():
    with LOCK:
        return jsonify(list(DATA))


@app.get("/kms")
def kms():
    with LOCK:
        return jsonify(KM_SUMMARIES)


@app.get("/analysis")
def analysis():
    with LOCK:
        if not KM_SUMMARIES:
            return jsonify({"message": "aucune analyse pour l'instant"}), 200
        return jsonify(KM_SUMMARIES[-1])


@app.get("/analyses")
def analyses():
    with LOCK:
        return jsonify([
            {"km": s["km"], "feedback": s["feedback"],
             "feedbackStatus": s["feedbackStatus"]}
            for s in KM_SUMMARIES
        ])


@app.post("/reset")
def reset():
    with LOCK:
        DATA.clear()
        _reset_state()
    return jsonify({"status": "reset"}), 200


@app.get("/health")
def health():
    return jsonify({"ok": True, "geminiKey": bool(GEMINI_API_KEY), "model": GEMINI_MODEL})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
