# Triathlon Coach — David

Coach triathlon automatique : il lit tes données **Intervals.icu** (Garmin, Strava…),
calcule ta forme, prépare tes séances avec l'aide de Claude et les envoie sur ta montre.

**Objectif : Triathlon M TriAtBain (Bain-de-Bretagne) — dimanche 2 mai 2027 — 2h43**

Dashboard : <https://lecdav.github.io/triathlon_coach/>

---

## Comment c'est organisé

Le projet suit un flux en 4 étapes : **entrées → jobs → moteur → sorties**.

```
inputs/                          ① ENTRÉES
  athlete_profile.yaml             ton profil : objectif, phases, seuils, préférences (édité à la main)
  intervals/                       imports bruts Intervals.icu (non versionnés, recréés à chaque run)
  credentials.env                  tes clés API en local (non versionné)

jobs/                            ② JOBS — import / export, lancés par GitHub Actions
  import_intervals.py              Intervals.icu → inputs/intervals/
  daily.py                         import → moteur (coach) → export des séances vers Intervals.icu
  weekly_plans.py                  import → moteur (plans IA du dimanche)
  push_workouts.py                 docs/data/today.json → Intervals.icu → Garmin

engine/                          ③ MOTEUR — calculs et IA, aucun appel à Intervals.icu
  coach.py                         forme du jour, plan adaptatif, message du coach
  plans.py                         plans IA des 2 semaines à venir + périodisation
  session_builder.py               allures, watts, durées et TSS d'une séance
  athlete_profile.py               lecture du profil
  intervals_inputs.py              lecture des imports Intervals.icu
  paths.py                         emplacement de tous les fichiers

connectors/                      clients des API externes
  intervals_client.py              Intervals.icu
  claude_client.py                 Claude

docs/                            ④ SORTIES — publiées par GitHub Pages
  index.html                       le dashboard
  data/today.json                  état du jour
  data/weekly_plans.json           plans IA des 2 semaines à venir
  data/periodization.json          TSS cibles par semaine ajustés par l'IA

.github/workflows/               les 3 automatismes ci-dessous
```

---

## Ce qui tourne tout seul

Trois workflows GitHub Actions, sur la branche `main` (heures de Paris, heure d'été) :

| Quand | Workflow | Job lancé | Sorties mises à jour |
|---|---|---|---|
| Chaque nuit, ~1h | `daily_coach.yml` | `jobs/daily.py` | `docs/data/today.json` + séances à venir sur Intervals.icu |
| Dimanche, ~23h | `generate_plans.yml` | `jobs/weekly_plans.py` | `docs/data/weekly_plans.json`, `docs/data/periodization.json` |
| Lundi, ~5h | `push_workouts.yml` | `jobs/push_workouts.py` | séances de la semaine sur Intervals.icu → Garmin |

Chacun peut être lancé à la main : GitHub → onglet **Actions** → choisir le workflow → **Run workflow**.

---

## Où modifier quoi

| Je veux… | Fichier |
|---|---|
| Changer l'objectif, les phases de la saison, les seuils, mes préférences | **`inputs/athlete_profile.yaml`** |
| Changer la logique de coaching (plan, adaptation, message) | `engine/coach.py` |
| Changer la génération IA des plans du dimanche | `engine/plans.py` |
| Changer l'affichage du dashboard | `docs/index.html` |

**Ne pas modifier à la main** `docs/data/` ni `inputs/intervals/` : ils sont réécrits automatiquement.

---

## Faire évoluer le projet

1. Créer une branche à partir de `main`.
2. Modifier les fichiers (voir « Où modifier quoi »).
3. Ouvrir une pull request vers `main`, puis la fusionner.
   Les workflows utilisent le nouveau code dès leur prochain passage.

Ne pas pousser directement sur `main` : les workflows y commitent chaque jour.

---

## Lancer en local

Toutes les commandes se lancent depuis la racine du dépôt.

```bash
pip install -r requirements.txt
```

Créer `inputs/credentials.env` (ignoré par git ; l'ancien `config/credentials.env` marche encore) :

```
INTERVALS_ATHLETE_ID=i406969
INTERVALS_API_KEY=ta_clé_api
```

Pour les fonctions IA (optionnel — sans clé, plan et message restent algorithmiques) :

```bash
export ANTHROPIC_API_KEY=ta_clé_anthropic
```

```bash
python -m connectors.intervals_client           # tester la connexion Intervals.icu
python jobs/import_intervals.py                 # importer les données → inputs/intervals/
python -m engine.coach                          # moteur seul, sur le dernier import (sans réseau)
python jobs/daily.py                            # job complet : import → moteur → export Intervals.icu
python jobs/weekly_plans.py --dry-run --force   # voir les prompts IA sans appeler Claude
python jobs/push_workouts.py --dry-run          # voir les séances sans les envoyer
python -m http.server 8080 --directory docs     # dashboard sur http://localhost:8080
```

Secrets GitHub nécessaires (Settings → Secrets and variables → Actions) :
`INTERVALS_ATHLETE_ID`, `INTERVALS_API_KEY`, `ANTHROPIC_API_KEY`.

GitHub Pages doit publier le dossier `/docs` : Settings → Pages → Branch `main`, dossier `/docs`.

---

## Méthodologie

- **Polarisé 80/20** (Seiler 2010) : ~80 % en Z1-Z2, ~20 % en Z4-Z5.
- **Charge** : modèle CTL/ATL/TSB (Banister/Coggan), progression ≤ +10 TSS/semaine.
- **Périodisation** : cycles 4+1 (4 semaines de charge, 1 de récupération à ~65 %),
  base → seuil/spécifique → pic → affûtage → course (TSB visé +10 à +15).
- **Semaine type** : 5 séances dont 1 renforcement musculaire (vendredi), sortie longue le dimanche.
