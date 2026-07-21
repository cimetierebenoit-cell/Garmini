"""
RunHealth ingest — serveur pour le data field Garmini.

Reçoit les métriques de la montre Forerunner 165 (POST JSON sur /), détecte
chaque kilomètre parcouru, agrège les données de ce km, puis appelle l'API
Gemini qui renvoie 2 à 4 INDICATEURS COURTS (ex: "Fatigue légère",
"Cadence basse", "Dérive cardiaque") affichés directement sur le Data Field
de la montre. Plus aucune notification Pushcut.

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
  GEMINI_API_KEY      (requis pour l'analyse)  clé Google AI Studio
  GEMINI_MODEL        (optionnel)              défaut: gemini-2.5-flash

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

# Marqueur de version : permet de confirmer dans les logs Render QUELLE version
# tourne réellement. Si ce numéro n'apparaît pas au démarrage, le déploiement
# n'est pas à jour.
APP_VERSION = "watch-indicators-1"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

log.info("DEMARRAGE %s | model=%s", APP_VERSION, GEMINI_MODEL)

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
        "indicators": None,        # liste de textes clés Gemini (async)
        "feedback": None,          # texte brut Gemini (repli / debug)
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
        "précédents. Réponds UNIQUEMENT par 2 à 4 indicateurs de performance "
        "COURTS en français, UN PAR LIGNE, maximum 22 caractères chacun, sans "
        "puces, sans numérotation, sans phrase d'introduction. "
        "Exemples de format attendu : 'Fatigue legere', 'Cadence basse', "
        "'Derive cardiaque', 'Allure stable', 'Bonne recup', 'SpO2 en baisse', "
        "'Accelere un peu', 'Ralentis'. "
        "N'utilise PAS d'accents ni de caractères spéciaux (écran de montre). "
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


def parse_indicators(text):
    """Transforme la réponse Gemini en liste de textes clés propres.

    Nettoie puces/numéros/guillemets éventuels, ignore les lignes vides,
    tronque à 22 caractères et garde au plus 4 indicateurs.
    """
    if not text:
        return []
    indicators = []
    for line in text.splitlines():
        s = line.strip().strip("\"'`")
        # Retire une éventuelle puce ou numérotation en tête de ligne.
        s = s.lstrip("-*•").strip()
        if s[:2].rstrip(".").rstrip(")").isdigit():
            s = s.lstrip("0123456789.)").strip()
        if not s:
            continue
        indicators.append(s[:22])
        if len(indicators) >= 4:
            break
    return indicators


def analyze_km_async(summary, previous_snapshot):
    """Thread : appelle Gemini puis écrit les indicateurs dans le résumé partagé."""
    prompt = build_prompt(summary, previous_snapshot)
    text, status = call_gemini(prompt)
    indicators = parse_indicators(text) if status == "ok" else []
    with LOCK:
        summary["feedback"] = text
        summary["indicators"] = indicators
        summary["feedbackStatus"] = status
    if status == "ok":
        log.info("GEMINI km %s -> %s", summary["km"], " | ".join(indicators))
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
            {"km": s["km"], "indicators": s.get("indicators"),
             "feedback": s["feedback"], "feedbackStatus": s["feedbackStatus"]}
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
