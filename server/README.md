# Serveur RunHealth (Flask) — réception des données Garmin

Reçoit les instantanés de santé envoyés par la montre et les stocke dans `data.jsonl`.

## Endpoints

| Méthode | Route | Rôle |
|---------|-------|------|
| `POST` | `/` | Reçoit un instantané JSON (depuis la montre) et l'enregistre |
| `GET` | `/` | Statut : nombre de mesures + 5 dernières |
| `GET` | `/data` | Toutes les mesures (filtre `?sessionId=...`) |
| `GET` | `/sessions` | Nombre de mesures par session |
| `GET` | `/health` | Sonde de disponibilité |

## Lancer en local

```bash
cd server
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

Test rapide :

```bash
curl -X POST http://localhost:5000/ \
  -H "Content-Type: application/json" \
  -d '{"sessionId":1,"ts":1750000180,"hr":142,"spo2":97,"stress":34,"respiration":28.5}'

curl http://localhost:5000/data
```

## Déployer sur Render

Deux options :

**A. Via `render.yaml` (recommandé)** — pousse le code sur GitHub, puis dans Render : *New → Blueprint*, sélectionne le dépôt. Le service `garmini` se crée tout seul.

**B. Manuellement** — *New → Web Service*, puis renseigne :

- **Root Directory** : `server` (si le dépôt contient aussi le dossier Garmin)
- **Build Command** : `pip install -r requirements.txt`
- **Start Command** : `gunicorn app:app --bind 0.0.0.0:$PORT`
- **Plan** : Free

L'URL publique (ex. `https://garmini.onrender.com`) correspond à la constante `URL` dans `source/HealthUploader.mc` côté montre. Le service écoute `POST /`, donc rien à changer.

## ⚠️ Persistance des données

Sur l'offre **gratuite**, le disque de Render est **éphémère** : `data.jsonl` est effacé à chaque redéploiement ou redémarrage (et les instances free se mettent en veille après ~15 min d'inactivité → cold start au réveil).

Pour conserver les données durablement :
- ajouter un **Persistent Disk** Render et pointer `DATA_FILE` dessus (ex. `/var/data/data.jsonl`), ou
- brancher une base **Postgres** (Render en propose une gratuite) à la place du fichier.
