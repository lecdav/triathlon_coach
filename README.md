# Triathlon Coach App — David

Application de coaching triathlon personnalisée s'appuyant sur les données
Intervals.icu (qui agrège elle-même Garmin / Strava / etc.).

## Objectif principal

**Triathlon M de Quimperlé — 30 août 2026**

---

## Architecture

```
.
├── .github/
│   └── workflows/
│       └── daily_coach.yml     # GitHub Actions — exécution automatique chaque matin à 6h30
├── config/
│   ├── credentials.env         # clé API Intervals.icu (gitignored, jamais commité)
│   └── athlete_profile.yaml   # profil athlète : zones, objectif, préférences d'entraînement
├── scripts/
│   ├── intervals_client.py     # client API Intervals.icu (lit credentials env ou fichier)
│   ├── daily_coach.py          # script principal : rapport + plan + dashboard
│   └── dashboard_template.html # template HTML (thème sombre) — source unique pour index.html et dashboard.html
├── reports/
│   └── daily/                  # rapports Markdown quotidiens YYYY-MM-DD.md (commités par CI)
├── data/
│   └── cache/                  # cache JSON local (gitignored)
├── index.html                  # dashboard GitHub Pages (régénéré à chaque run CI)
└── requirements.txt            # dépendances Python (requests, pyyaml)
```

---

## Méthodologie d'entraînement

- **Polarisation 80/20** (Seiler 2010) : ~80 % Z1-Z2 endurance sous LT1, ~20 % Z4-Z5 haute intensité, peu de Z3
- **Charge** : suivi CTL/ATL/TSB (modèle Banister/Coggan) ; ramp rate cible ≤ +5 CTL/sem
- **Volume** : 5 séances max/sem dont 1 renforcement musculaire (vendredi)
- **Périodisation** : Base → Développement → Spécifique → Affûtage (pic TSB +10/+20 pour la course)

---

## Exécution automatique — GitHub Actions

Le script tourne chaque matin à **6h30 heure de Paris** via GitHub Actions,
indépendamment de l'état du Mac.

**Workflow** : `.github/workflows/daily_coach.yml`

À chaque exécution :
1. Appelle l'API Intervals.icu pour récupérer les données du jour (wellness, activités, seuils)
2. Calcule CTL/ATL/TSB, classifie la forme, génère le plan de la semaine
3. Produit `reports/daily/YYYY-MM-DD.md` (rapport Markdown détaillé)
4. Régénère `index.html` depuis `dashboard_template.html` avec le snapshot du jour embarqué
5. Commite et pousse `index.html` + le rapport sur GitHub

**Secrets à configurer** dans GitHub → Settings → Secrets and variables → Actions :

| Secret | Valeur |
|---|---|
| `INTERVALS_ATHLETE_ID` | identifiant Intervals.icu (ex: `i406969`) |
| `INTERVALS_API_KEY` | clé API Intervals.icu |

**Lancer manuellement** : GitHub → onglet Actions → "Coach Triathlon — Rapport quotidien" → Run workflow.

---

## Dashboard

- **GitHub Pages** : `https://lecdav.github.io/triathlon_coach/` — mis à jour chaque matin par le CI
- **Cowork artifact** : artifact `triathlon-dashboard` dans Claude Cowork — rafraîchi par la tâche planifiée locale

Les deux sont générés depuis le même `dashboard_template.html` (thème sombre, message coach, graphique PMC, plan hebdo).

---

## Setup local (Mac)

### Prérequis

```bash
pip install -r requirements.txt
```

### Credentials

Créer `config/credentials.env` (jamais commité) :

```
INTERVALS_ATHLETE_ID=i406969
INTERVALS_API_KEY=ta_clé_api
```

### Vérifier la connexion API

```bash
python3 scripts/intervals_client.py
```

### Lancer le rapport manuellement

```bash
python3 scripts/daily_coach.py
```

Produit :
- `reports/daily/YYYY-MM-DD.md` — rapport Markdown
- `data/cache/today.json` — snapshot JSON
- `reports/dashboard.html` — dashboard HTML autonome (pour l'artifact Cowork)
- `index.html` — dashboard GitHub Pages

### Modifier le profil athlète

Éditer `config/athlete_profile.yaml` pour ajuster les zones, l'objectif, les préférences
(nombre de séances, jour de repos, etc.). Les seuils (FTP, CSS, LTHR) sont synchronisés
automatiquement depuis Intervals.icu à chaque run.

---

## Workflow de développement

```bash
# Modifier le code localement
# Tester : python3 scripts/daily_coach.py

git add .
git commit -m "feat: description de la modification"
git push
# → GitHub Actions utilisera le nouveau code dès le prochain run à 6h30
```

---

## Instructions pour Claude (Cowork)

Claude est configuré comme coach triathlon quotidien dans ce projet.
À chaque session, Claude peut :

- **Lire** `data/cache/today.json` pour connaître l'état du jour (forme, plan, métriques)
- **Lire** `config/athlete_profile.yaml` pour les préférences et contraintes d'entraînement
- **Lire** `reports/daily/` pour l'historique des rapports
- **Modifier** `config/athlete_profile.yaml` pour ajuster les paramètres (ex: nombre de séances, jour de muscu)
- **Modifier** `scripts/dashboard_template.html` pour faire évoluer le rendu du dashboard
- **Modifier** `scripts/daily_coach.py` pour faire évoluer la logique de coaching
- **Exécuter** `python3 scripts/daily_coach.py` pour régénérer le rapport et le dashboard
- **Mettre à jour** l'artifact Cowork `triathlon-dashboard` via `mcp__cowork__update_artifact`

La tâche planifiée `triathlon-daily-coach` dans Cowork exécute automatiquement
ces étapes chaque matin (si le Mac est ouvert). GitHub Actions prend le relais
si le Mac est éteint ou en veille.
