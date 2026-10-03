# Triathlon Coach — David

Coach triathlon automatique : il lit tes données **Intervals.icu** (Garmin, Strava…),
calcule ta forme, prépare tes séances avec l'aide de Claude et les envoie sur ta montre.

**Objectif : Triathlon M TriAtBain (Bain-de-Bretagne) — dimanche 2 mai 2027 — 2h43**

Dashboard : <https://lecdav.github.io/triathlon_coach/>

---

## Ce qui tourne tout seul

Trois workflows GitHub Actions, sur la branche `main` (heures de Paris, heure d'été) :

| Quand | Workflow | Ce qu'il fait | Fichiers mis à jour |
|---|---|---|---|
| Chaque nuit, ~1h | `daily_coach.yml` | Forme du jour (CTL/ATL/TSB), plan adaptatif de la semaine, message du coach, synchro des séances à venir sur Intervals.icu | `data/today.json` |
| Dimanche, ~23h | `generate_plans.yml` | L'IA ajuste les TSS cibles de la saison et génère les 2 semaines suivantes | `data/weekly_plans.json`, `data/periodization.json` |
| Lundi, ~5h | `push_workouts.yml` | Envoie les séances de la semaine sur Intervals.icu → Garmin | — |

Chacun peut être lancé à la main : GitHub → onglet **Actions** → choisir le workflow → **Run workflow**.

---

## Où modifier quoi

| Je veux… | Fichier |
|---|---|
| Changer l'objectif, les phases de la saison, les seuils, mes préférences | **`config/athlete_profile.yaml`** (source unique) |
| Changer la logique de coaching (plan, adaptation, message) | `scripts/daily_coach.py` |
| Changer la génération IA des plans du dimanche | `scripts/generate_theoretical_plan.py` |
| Changer l'affichage du dashboard | `index.html` |

**Ne pas modifier à la main** le contenu de `data/` : ces fichiers sont réécrits par les workflows.

---

## Organisation du dépôt

```
config/
  athlete_profile.yaml        ← TON profil (objectif, phases, seuils, préférences)
data/                         ← généré automatiquement, ne pas éditer
  today.json                    état du jour, lu par le dashboard
  weekly_plans.json             plans IA des 2 semaines à venir
  periodization.json            TSS cibles par semaine ajustés par l'IA
scripts/
  daily_coach.py              script principal (rapport quotidien)
  generate_theoretical_plan.py  plans IA du dimanche
  push_workouts.py            envoi des séances vers Intervals.icu / Garmin
  session_builder.py          calcul des allures, watts, durées et TSS d'une séance
  athlete_profile.py          lecture du profil
  intervals_client.py         client API Intervals.icu
  claude_client.py            client API Claude
index.html                    dashboard (GitHub Pages), lit data/today.json
.github/workflows/            les 3 automatismes ci-dessus
```

---

## Faire évoluer le projet

1. Créer une branche à partir de `main`.
2. Modifier les fichiers (voir « Où modifier quoi »).
3. Ouvrir une pull request vers `main`, puis la fusionner.
   Les workflows utilisent le nouveau code dès leur prochain passage.

Ne pas pousser directement sur `main` : les workflows y commitent chaque jour.

---

## Lancer en local

```bash
pip install -r requirements.txt
```

Créer `config/credentials.env` (ignoré par git) :

```
INTERVALS_ATHLETE_ID=i406969
INTERVALS_API_KEY=ta_clé_api
```

Pour les fonctions IA (optionnel — sans clé, plan et message restent algorithmiques) :

```bash
export ANTHROPIC_API_KEY=ta_clé_anthropic
```

```bash
python scripts/intervals_client.py                      # tester la connexion Intervals.icu
python scripts/daily_coach.py                           # rapport du jour → data/today.json
python scripts/generate_theoretical_plan.py --dry-run   # voir le prompt IA sans l'appeler
python scripts/push_workouts.py --dry-run               # voir les séances sans les envoyer
python -m http.server 8080                              # dashboard sur http://localhost:8080
```

Secrets GitHub nécessaires (Settings → Secrets and variables → Actions) :
`INTERVALS_ATHLETE_ID`, `INTERVALS_API_KEY`, `ANTHROPIC_API_KEY`.

---

## Méthodologie

- **Polarisé 80/20** (Seiler 2010) : ~80 % en Z1-Z2, ~20 % en Z4-Z5.
- **Charge** : modèle CTL/ATL/TSB (Banister/Coggan), progression ≤ +10 TSS/semaine.
- **Périodisation** : cycles 4+1 (4 semaines de charge, 1 de récupération à ~65 %),
  base → seuil/spécifique → pic → affûtage → course (TSB visé +10 à +15).
- **Semaine type** : 5 séances dont 1 renforcement musculaire (vendredi), sortie longue le dimanche.
