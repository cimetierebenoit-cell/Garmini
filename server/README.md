# RunHealth ingest — serveur Garmini + analyse Gemini

Reçoit les métriques du data field Garmini (Forerunner 165), détecte **chaque
kilomètre parcouru**, agrège ses données et appelle l'**API Gemini** pour
produire 2 à 4 **indicateurs de performance courts** (ex: `Fatigue legere`,
`Cadence basse`, `Derive cardiaque`) affichés directement sur le Data Field
de la montre, en comparant le km courant aux km précédents.

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
ce km aux précédents et renvoie 2 à 4 indicateurs courts (`indicators`, max
22 caractères chacun, sans accents pour l'écran de la montre). Le retour est
ensuite lisible sur `/analysis` et `/analyses` ; la montre les affiche un par
ligne sous l'en-tête `km N`.

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

curl http://localhost:5000/analysis     # les indicateurs Gemini du km 1
```

## Installer le Data Field sur la montre (Forerunner 165)

### 1. Compiler le `.prg`

Prérequis (une seule fois) : [Connect IQ SDK](https://developer.garmin.com/connect-iq/sdk/)
via le **SDK Manager** (télécharger aussi le device `fr165`), VS Code avec
l'extension **Monkey C**, et une clé développeur
(`Monkey C: Generate a Developer Key`).

Ensuite, dans VS Code avec le dossier `Garmini/` ouvert :
1. Palette de commandes (`Ctrl+Shift+P`) → **Monkey C: Build for Device**.
2. Device : `fr165` — le `.prg` est généré dans `Garmini/bin/Garmini.prg`.

(Un `.prg` déjà compilé existe dans `Garmini/bin/` si le code n'a pas changé.)

### 2. Copier sur la montre

1. Brancher la Forerunner 165 en **USB** au PC (elle apparaît dans
   l'Explorateur Windows comme périphérique multimédia).
2. Copier `Garmini/bin/Garmini.prg` dans `Internal Storage/GARMIN/Apps/`.
3. Éjecter et débrancher : la montre installe le champ au redémarrage de
   l'écran d'activité.

### 3. Ajouter le champ à l'écran de course

Sur la montre : **Réglages → Activités et applications → Course →
Écrans de données** → choisir un écran (ou en ajouter un) → sélectionner un
emplacement → **Champs Connect IQ → Garmini**.

Astuce : mettre Garmini seul sur un écran 1 champ pour laisser la place aux
indicateurs.

### 4. Conditions pour que ça marche en course

- Le téléphone doit être **à proximité avec Garmin Connect ouvert en
  arrière-plan** : c'est lui qui relaie les requêtes HTTP de la montre vers le
  serveur Render.
- Le serveur Render doit être déployé et accessible (l'URL est codée dans
  `GarminiView.mc` : `SERVER_URL`). Le premier affichage peut prendre 30-60 s
  le temps du réveil du serveur (plan gratuit Render).
- Les indicateurs du km 1 apparaissent après le premier kilomètre franchi ;
  en attendant, le champ affiche `en attente km 1` + la FC.

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
