# Triathlon Coach App — David

Application de coaching triathlon personnalisée s'appuyant sur les données
Intervals.icu (qui agrège elle-même Garmin / Strava / etc.).

**Objectif : Triathlon M de Quimperlé — 30 août 2026**

---

## Vue d'ensemble du workflow

```
┌─────────────────────────────────────────────────────────────────┐
│                        CHAQUE MATIN À 6h30                      │
│                                                                  │
│   GitHub Actions (serveur distant, tourne même Mac éteint)      │
│   ┌──────────────────────────────────────────────────────┐      │
│   │ 1. Récupère les données Intervals.icu du jour         │      │
│   │ 2. Calcule CTL/ATL/TSB, génère le plan de la semaine │      │
│   │ 3. Génère le rapport Markdown + index.html            │      │
│   │ 4. Commite et pousse sur la branche main              │      │
│   └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       CHAQUE MATIN À 7h02                       │
│                                                                  │
│   Tâche planifiée Cowork (tourne sur le Mac)                    │
│   ┌──────────────────────────────────────────────────────┐      │
│   │ 1. Commite automatiquement tout travail local (WIP)   │      │
│   │ 2. Merge main → dev pour récupérer le rapport du jour │      │
│   │ 3. Rapport de ce qui a été mis à jour                 │      │
│   └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     CHAQUE LUNDI À 7h07                         │
│                                                                  │
│   Tâche planifiée Cowork (tourne sur le Mac)                    │
│   ┌──────────────────────────────────────────────────────┐      │
│   │ Envoie les 5 séances de la semaine sur Intervals.icu  │      │
│   │ → synchronisées automatiquement sur la montre Garmin  │      │
│   └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    QUAND TU VEUX FAIRE ÉVOLUER L'APP            │
│                                                                  │
│   Travail local sur la branche dev (avec Claude dans Cowork)    │
│   ┌──────────────────────────────────────────────────────┐      │
│   │ 1. Modifier template, script, profil athlète          │      │
│   │ 2. Tester en local                                    │      │
│   │ 3. Commiter sur dev                                   │      │
│   │ 4. Ouvrir une PR dev → main sur GitHub                │      │
│   │ 5. Merger → GitHub Actions utilise le nouveau code    │      │
│   └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Les deux branches

| Branche | Rôle | Qui écrit dessus |
|---------|------|-----------------|
| `main` | Production — dashboard en ligne sur GitHub Pages | GitHub Actions uniquement (commits automatiques) |
| `dev` | Développement — ton espace de travail local | Toi + Claude dans Cowork |

**Règle d'or : ne jamais pousser manuellement sur `main`.** Toutes les évolutions passent par une PR `dev → main`.

---

## GitHub Actions — le moteur de production

**Fichier :** `.github/workflows/daily_coach.yml`
**Quand :** tous les jours à 6h30 heure de Paris (même si le Mac est éteint)
**Ce qu'il fait :**

1. Checkout de la branche `main`
2. Appelle l'API Intervals.icu → récupère wellness, activités, seuils du jour
3. Calcule CTL/ATL/TSB, classifie la forme, génère le plan de la semaine
4. Produit `reports/daily/YYYY-MM-DD.md` (rapport Markdown détaillé)
5. Régénère `index.html` (dashboard GitHub Pages) depuis `dashboard_template.html`
6. Génère le message coach via l'API Claude Haiku (si `ANTHROPIC_API_KEY` configurée)
7. Commite et pousse sur `main` — disponible sur GitHub Pages dans la minute

**Secrets à configurer** dans GitHub → Settings → Secrets and variables → Actions :

| Secret | Valeur |
|--------|--------|
| `INTERVALS_ATHLETE_ID` | identifiant Intervals.icu (ex: `i406969`) |
| `INTERVALS_API_KEY` | clé API Intervals.icu |
| `ANTHROPIC_API_KEY` | clé API Anthropic (optionnel — pour le message coach IA) |

**Lancer manuellement :** GitHub → onglet Actions → "Coach Triathlon — Rapport quotidien" → Run workflow.

---

## Tâche planifiée Cowork — la sync matinale

**Quand :** tous les jours à 7h02 (32 min après le run GitHub Actions)
**Ce qu'elle fait :**

1. **Protège le travail local** : si des fichiers ont été modifiés sur `dev`, crée un commit `wip: snapshot local` automatiquement avant de toucher quoi que ce soit
2. **Merge `main` → `dev`** : récupère le nouveau rapport et le dashboard généré par GitHub Actions
3. En cas de conflit : s'arrête et affiche les fichiers en conflit sans rien écraser

Cette tâche garantit que tu trouves chaque matin les données fraîches du jour dans ton environnement local, sans jamais perdre ton travail en cours.

---

## Tâche planifiée Cowork — push des séances Garmin

**Quand :** chaque lundi à 7h07
**Ce qu'elle fait :**

1. Lit le plan de la semaine généré par `daily_coach.py`
2. Convertit chaque séance en format structuré (étapes : échauffement, intervalles, retour au calme)
3. Envoie les séances sur Intervals.icu via l'API
4. Intervals.icu les synchronise automatiquement sur Garmin Connect → ta montre

---

## Dashboard

- **GitHub Pages** : `https://lecdav.github.io/triathlon_coach/` — mis à jour chaque matin par GitHub Actions
- **Local** : ouvrir `index.html` dans un navigateur, ou via `python3 -m http.server 8080` puis `http://localhost:8080`

Le dashboard affiche (dans l'ordre) :
1. **Frise du plan de saison** — positionnement semaine par semaine jusqu'à Quimperlé, avec les phases et le volume cible
2. **Plan adaptatif de la semaine** — séances réalisées (en vert) + reste à faire ajusté selon la fatigue
3. **Plan idéal de la semaine** — plan théorique polarisé 80/20
4. **État de forme** — bannière TSB + graphique PMC sur 3 mois
5. **KPIs, charge, message coach, zones de référence**

---

## Workflow de développement (faire évoluer l'app)

```bash
# 1. S'assurer d'être sur dev
git checkout dev

# 2. Modifier les fichiers voulus
#    - scripts/dashboard_template.html  → mise en forme du dashboard
#    - scripts/daily_coach.py           → logique de coaching, plan, calculs
#    - config/athlete_profile.yaml      → profil athlète, zones, objectif

# 3. Tester en local
python3 scripts/daily_coach.py
# → régénère index.html, reports/dashboard.html, data/cache/today.json

# 4. Commiter
git add .
git commit -m "feat: description de la modification"
git push origin dev

# 5. Créer une PR sur GitHub : dev → main
# GitHub.com → ton repo → "Compare & pull request"
# → Merger la PR → GitHub Actions utilise le nouveau code dès le prochain run à 6h30
```

---

## Setup local (Mac) — première installation

### Prérequis

```bash
pip3 install -r requirements.txt
```

### Credentials Intervals.icu

Créer `config/credentials.env` (jamais commité — dans `.gitignore`) :

```
INTERVALS_ATHLETE_ID=i406969
INTERVALS_API_KEY=ta_clé_api
```

### Vérifier la connexion API

```bash
python3 scripts/intervals_client.py
```

### Lancer le script manuellement

```bash
python3 scripts/daily_coach.py
```

---

## Architecture des fichiers

```
.
├── .github/
│   └── workflows/
│       └── daily_coach.yml         # GitHub Actions — 6h30 chaque matin
├── config/
│   ├── credentials.env             # clé API Intervals.icu (gitignored)
│   └── athlete_profile.yaml        # profil : zones, objectif, préférences
├── scripts/
│   ├── intervals_client.py         # client API Intervals.icu
│   ├── daily_coach.py              # script principal : rapport + plan + dashboard
│   ├── push_workouts.py            # envoi des séances sur Garmin via Intervals.icu
│   └── dashboard_template.html     # source unique du dashboard (→ index.html et dashboard.html)
├── reports/
│   └── daily/                      # rapports Markdown quotidiens (commités par CI)
├── data/
│   └── cache/                      # cache JSON local (gitignored)
├── index.html                      # dashboard GitHub Pages (régénéré par CI)
└── requirements.txt                # dépendances Python
```

---

## Méthodologie d'entraînement

- **Polarisation 80/20** (Seiler 2010) : ~80 % Z1-Z2 endurance sous LT1, ~20 % Z4-Z5 haute intensité
- **Charge** : suivi CTL/ATL/TSB (modèle Banister/Coggan) ; ramp rate cible ≤ +5 CTL/sem
- **Volume** : 5 séances/sem dont 1 renforcement musculaire (vendredi), 1 natation (mercredi), sortie longue le dimanche
- **Périodisation** : Base → Développement → Pic → Affûtage (pic TSB +10/+20 pour la course)

---

## Instructions pour Claude (Cowork)

À chaque session de travail, Claude peut :

- **Lire** `data/cache/today.json` → état du jour (forme, plan, métriques)
- **Lire** `config/athlete_profile.yaml` → préférences et contraintes d'entraînement
- **Lire** `reports/daily/` → historique des rapports
- **Modifier** `scripts/dashboard_template.html` → faire évoluer le rendu du dashboard
- **Modifier** `scripts/daily_coach.py` → faire évoluer la logique de coaching
- **Modifier** `config/athlete_profile.yaml` → ajuster les paramètres (zones, objectif…)
- **Exécuter** `python3 scripts/daily_coach.py` → régénérer le dashboard en local

Toujours travailler sur la branche `dev`. Les modifications passent en production via une PR `dev → main`.
