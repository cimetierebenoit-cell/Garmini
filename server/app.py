"""
RunHealth ingest — serveur de référence pour le data field Garmini.

Reçoit les métriques envoyées par la montre Forerunner 165 (POST JSON sur /),
les stocke en mémoire et les expose en lecture.

Routes :
  POST /        -> ingère un point de mesure JSON
  GET  /        -> { "count": N, "last": [...], "service": "RunHealth ingest" }
  GET  /data    -> liste complète des points reçus
  GET  /health  -> { "ok": true }

Démarrage local :
  pip install -r requirements.txt
  python app.py            # http://localhost:5000
"""

import os
import time
from collections import deque

from flask import Flask, request, jsonify

app = Flask(__name__)

# Stockage en mémoire (anneau borné). Note : sur Render free tier, la mémoire
# est réinitialisée à chaque redémarrage/veille du service.
MAX_POINTS = 5000
DATA = deque(maxlen=MAX_POINTS)

# Champs attendus depuis la montre.
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


@app.post("/")
def ingest():
    # force=True : accepte le corps même si le Content-Type n'est pas parfait.
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "JSON invalide ou corps vide"}), 400

    record = {k: payload.get(k) for k in EXPECTED_FIELDS}
    record["receivedAt"] = time.time()  # horodatage serveur (epoch s)
    DATA.append(record)

    return jsonify({"status": "ok", "count": len(DATA)}), 200


@app.get("/")
def root():
    last = list(DATA)[-5:]
    return jsonify({
        "count": len(DATA),
        "last": list(reversed(last)),
        "service": "RunHealth ingest",
    })


@app.get("/data")
def data():
    return jsonify(list(DATA))


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Render fournit le port via la variable d'environnement PORT.
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
