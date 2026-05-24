#!/bin/bash
# commit.sh — Commite et pousse toutes les modifications locales
# Usage : ./commit.sh "message de commit"
#    ou : ./commit.sh   (message automatique avec la date)

set -e

cd "$(dirname "$0")"

# Supprime le lock s'il existe (résidu d'un processus interrompu)
[ -f .git/index.lock ] && rm -f .git/index.lock && echo "🔓 index.lock supprimé"

# Message de commit
MSG="${1:-"chore: mise à jour locale $(date '+%Y-%m-%d %H:%M')"}"

# Vérifie s'il y a des changements
if git diff --quiet && git diff --cached --quiet; then
  echo "✅ Rien à commiter — le repo est propre."
  exit 0
fi

git add -A
git commit -m "$MSG"
git push

echo "✅ Commit et push réussis : $MSG"
