# RunHealth ingest — serveur Garmini + analyse Gemini

Reçoit les métriques du data field Garmini (Forerunner 165), détecte **chaque
kilomètre parcouru**, agrège ses données et appelle l'**API Gemini** pour
produire un retour constructif sur l'état de forme du coureur, en comparant le
km courant aux km précédents.

## Routes

| Méthode | Route        | Description                                                        |
|---------|--------------|--------------------------------------------------------------------|
| POST    | `/`          | Ingère un point de mesure JSON (corps = JSON de la montre)         |
| GET     | `/`          | `{ count, kmCount, last, service }`                                |
| GET     | `/data`      | Liste complète des points reçus                                    |
| GET     | `/kms`       | Résumés statistiques agrégés par kilomètre                         |
| GET     | `/analysis`  | Dernière analyse Gemini (km le plus récent)                        |
| GET     | `/analyses`  | Toutes les analyses Gemini                                         |
| POST    | `/reset`     | Réinitialise l'état (nouvelle course)                              |
| GET     | `/health`    | `{ ok, geminiKey, model }`                                         |

## Analyse Gemini

À chaque borne de 1000 m franchie, le serveur agrège le km (HR moy/max, allure,
cadence, SpO2 moy/min, énergie, dénivelé, durée, Training Effect) et lance — en
tâche de fond, sans bloquer la montre — un appel à `gemini-2.5-flash` qui compare
ce km aux précédents et renvoie 3 à 5 phrases de feedback. Le retour est ensuite
lisible sur `/analysis` et `/analyses`.

> ⚠️ Le feedback est informatif et non médical : il ne pose aucun diagnostic.

### Configuration (variables d'environnement)

| Variable         | Requis | Défaut             | Rôle                                  |
|------------------|--------|--------------------|---------------------------------------|
| `GEMINI_API_KEY` | oui    | —                  | Clé Google AI Studio                  |
| `GEMINI_MODEL`   | non    | `gemini-2.5-flash` | Modèle Gemini utilisé                 |

Sans `GEMINI_API_KEY`, l'ingestion fonctionne mais `feedbackStatus` vaut `no_key`.

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
export GEMINI_API_KEY="ta_cle"     # Windows: set GEMINI_API_KEY=ta_cle
python app.py                      # http://localhost:5000
```

Test rapide (simule la traversée d'1 km pour déclencher Gemini) :

```bash
curl -X POST http://localhost:5000/ -H "Content-Type: application/json" \
  -d '{"timerTime":0,"elapsedDistance":200,"currentHeartRate":150,"currentSpeed":3.3,"currentCadence":168,"currentOxygenSaturation":98}'
curl -X POST http://localhost:5000/ -H "Content-Type: application/json" \
  -d '{"timerTime":300000,"elapsedDistance":1010,"currentHeartRate":158,"currentSpeed":3.1,"currentCadence":166,"currentOxygenSaturation":96}'

curl http://localhost:5000/analysis     # le feedback Gemini du km 1
```

## Déployer sur Render

1. Pousser ce dossier `server/` dans le dépôt Git lié à Render.
2. Service type **Web Service**, environnement **Python 3**.
3. **Root Directory** : `server` (sinon gunicorn ne trouve pas `app`).
4. Build command : `pip install -r requirements.txt`
5. Start command : `gunicorn app:app` (ou laisse le `Procfile`).
6. **Environment** : ajouter la variable `GEMINI_API_KEY`.

> Note : le stockage est en mémoire. Sur le plan gratuit Render, le service se met
> en veille et perd les données. Pour conserver l'historique, branche une base
> (SQLite sur disque persistant, Postgres Render, etc.).
