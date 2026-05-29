#!/bin/bash
set -e
cd "$(dirname "$0")"

git add scripts/daily_coach.py index.html

git commit -m "fix: sync plan adaptatif -> Intervals.icu + corrections régressions

- sync_adaptive_plan_to_intervals() : appelée automatiquement après chaque run
  Supprime les WORKOUT futurs puis recrée avec titre exact du plan adaptatif
  (ex: RUN 49' -- Intervalles Z4). Ne touche pas aux jours passés ni Repos.

- load_theoretical_plans() Cas 2 : weekly_plans.json semaine prochaine ->
  ideal_plan=[] fallback algo semaine courante, next_week_plan=IA préservé.

- actual_activities reconstruites depuis Intervals.icu (source de vérité)
  après réception JSON IA, évite tss_estimate=None sur jours done.

- index.html : jour done sur Repos prévu -> Séance non planifiée."

git push origin main
echo ""
echo "Pushe OK. Lance maintenant :"
echo "  python3 scripts/daily_coach.py"
echo "  git add data/today.json && git commit -m 'data: today.json' && git push"
