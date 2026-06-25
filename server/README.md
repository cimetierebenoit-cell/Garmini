# RunHealth ingest — serveur de référence

Reçoit les métriques du data field Garmini (Forerunner 165) et les expose en lecture.

## Routes

| Méthode | Route      | Description                                                        |
|---------|------------|--------------------------------------------------------------------|
| POST    | `/`        | Ingère un point de mesure JSON (corps = JSON de la montre)         |
| GET     | `/`        | `{ "count": N, "last": [...], "service": "RunHealth ingest" }`     |
| GET     | `/data`    | Liste complète des points reçus                                    |
| GET     | `/health`  | `{ "ok": true }`                                                   |

## Format JSON attendu (POST /)

```json
{
  "timerTime": 123000,
  "elapsedDistance": 540.2,
  "currentHeartRate": 152,
  "currentSpeed": 3.4,
  "currentCadence": 168,
  "energyExpenditure": 11.2,
  "altitude": 87.5,
  "trainingEffect": 2.7,
  "currentOxygenSaturation": 97,
  "heartRateZones": [93, 121, 140, 159, 178, 197]
}
```

Les champs absents/non disponibles sont envoyés à `null` par la montre.
Le serveur ajoute `receivedAt` (epoch secondes) à chaque point.

## Lancer en local

```bash
cd server
pip install -r requirements.txt
python app.py        # http://localhost:5000
```

Test rapide :

```bash
curl -X POST http://localhost:5000/ \
  -H "Content-Type: application/json" \
  -d '{"currentHeartRate":152,"currentSpeed":3.4}'

curl http://localhost:5000/data
```

## Déployer sur Render

1. Pousser ce dossier `server/` dans le dépôt Git lié à Render.
2. Service type **Web Service**, environnement **Python 3**.
3. Build command : `pip install -r requirements.txt`
4. Start command : `gunicorn app:app` (ou laisse le `Procfile`).

> Note : le stockage est en mémoire. Sur le plan gratuit Render, le service se met
> en veille et perd les données. Pour conserver l'historique, branche une base
> (SQLite sur disque persistant, Postgres Render, etc.).
