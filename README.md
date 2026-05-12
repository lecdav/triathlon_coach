# Triathlon Coach App — David

Application de coaching triathlon personnalisée s'appuyant sur les données
Intervals.icu (qui agrège elle-même Garmin / Strava / etc.).

## Objectif principal

**Triathlon M de Quimperlé — 30 août 2026** (16 semaines de prépa depuis le 8 mai 2026).

## Architecture

```
.
├── config/
│   ├── credentials.env       # clé API Intervals.icu (gitignored)
│   └── athlete_profile.yaml  # profil, capteurs, zones, objectif, méthode
├── scripts/
│   ├── intervals_client.py   # client API minimaliste
│   ├── daily_coach.py        # rapport quotidien (matin)
│   └── weekly_review.py      # bilan + plan (lundi matin)
├── reports/
│   ├── daily/                # synthese_AAAA-MM-JJ.md + detail_AAAA-MM-JJ.html
│   └── weekly/               # bilan_S{n}.md + plan_S{n+1}.md
└── data/
    └── cache/                # cache local des activités (réduire appels API)
```

## Méthodologie

- **Périodisation** : Base (S1-6) → Développement (S7-10) → Spécifique (S11-13) → Affûtage (S14-16)
- **Polarisation 80/20** (Seiler) : 80 % travail sous seuil, 20 % au-dessus, peu de tempo
- **Métriques** : puissance vélo (FTP, NP, TSS via puissance), allure CAP + FC, CSS natation
- **Charge** : suivi CTL/ATL/TSB ; ramp rate ≤ +5 TSS/sem en base, plus prudent en spécifique

## Tâches planifiées

- **Quotidienne** (~6h30) : `daily_coach.py` → séance du jour + verdict fatigue + 1 page synthèse + rapport HTML détaillé
- **Hebdomadaire** (lundi 7h) : `weekly_review.py` → bilan S-1 + ajustement plan S+1

## Premier setup

1. Créer une clé API sur intervals.icu → la coller dans `config/credentials.env`
2. Ajouter `intervals.icu` à l'allowlist réseau de Cowork (Settings → Capabilities)
3. Lancer `python3 scripts/intervals_client.py` pour vérifier la connexion
4. Compléter `config/athlete_profile.yaml` (poids, FCmax si non détectée)
