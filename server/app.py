"""
Serveur d'exemple pour recevoir les données de santé envoyées par
l'application Garmin Connect IQ "RunHealth".

Endpoints
---------
POST /            Reçoit un instantané JSON et l'ajoute à data.jsonl
GET  /            Page de statut (nombre de mesures, dernières reçues)
GET  /data        Renvoie toutes les mesures (filtre optionnel ?sessionId=)
GET  /sessions    Liste les sessions reçues avec le nombre de mesures
GET  /health      Sonde de disponibilité ({"status": "ok"})

⚠️ Render (offre gratuite) : le disque est éphémère. data.jsonl est effacé
à chaque redémarrage/déploiement. Pour conserver durablement les données,
ajoute un "Persistent Disk" Render ou branche une vraie base (Postgres).
"""

import json
import os
import time
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)

# Emplacement du fichier de données (surchargable via la variable DATA_FILE).
DATA_FILE = os.environ.get("DATA_FILE", "data.jsonl")

# Champs attendus depuis la montre (tous optionnels : un capteur peut renvoyer null).
EXPECTED_FIELDS = [
    "sessionId", "ts", "elapsed", "hr", "pace", "speed",
    "distance", "cadence", "stress", "spo2", "respiration",
]


def _append_record(record: dict) -> None:
    """Ajoute une ligne JSON au fichier (format JSON Lines)."""
    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_records() -> list:
    """Relit toutes les mesures stockées."""
    if not os.path.exists(DATA_FILE):
        return []
    records = []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


@app.route("/", methods=["POST"])
def receive():
    """Réception d'un instantané de santé depuis la montre Garmin."""
    payload = request.get_json(silent=True)
    if payload is None or not isinstance(payload, dict):
        return jsonify({"error": "corps JSON invalide"}), 400

    # On ne garde que les champs connus + un horodatage de réception serveur.
    record = {k: payload.get(k) for k in EXPECTED_FIELDS}
    record["received_at"] = datetime.now(timezone.utc).isoformat()

    _append_record(record)
    app.logger.info("Mesure reçue : session=%s hr=%s spo2=%s stress=%s",
                    record.get("sessionId"), record.get("hr"),
                    record.get("spo2"), record.get("stress"))

    return jsonify({"status": "ok", "stored": record}), 200


@app.route("/", methods=["GET"])
def status():
    """Petite page de statut lisible dans le navigateur."""
    records = _read_records()
    last = records[-5:] if records else []
    return jsonify({
        "service": "RunHealth ingest",
        "count": len(records),
        "last": last,
    }), 200


@app.route("/data", methods=["GET"])
def data():
    """Toutes les mesures, avec filtre optionnel ?sessionId=..."""
    records = _read_records()
    session_id = request.args.get("sessionId")
    if session_id is not None:
        records = [r for r in records if str(r.get("sessionId")) == session_id]
    return jsonify(records), 200


@app.route("/sessions", methods=["GET"])
def sessions():
    """Récapitulatif par session."""
    summary = {}
    for r in _read_records():
        sid = r.get("sessionId")
        summary[sid] = summary.get(sid, 0) + 1
    out = [{"sessionId": sid, "samples": n} for sid, n in summary.items()]
    return jsonify(out), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": time.time()}), 200


if __name__ == "__main__":
    # Développement local : python app.py  ->  http://localhost:5000
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
