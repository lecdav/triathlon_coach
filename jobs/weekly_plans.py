"""Job du dimanche : import Intervals.icu → plans IA des 2 semaines à venir.

1. Importe les données Intervals.icu dans inputs/intervals/
2. Lance le moteur (engine/plans.py) → docs/data/weekly_plans.json + periodization.json

Usage :
    python jobs/weekly_plans.py --force          # régénère même si déjà fait cette semaine
    python jobs/weekly_plans.py --dry-run        # affiche les prompts sans appeler Claude
    python jobs/weekly_plans.py --skip-import    # réutilise le dernier import
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import plans
from jobs.import_intervals import import_intervals


def main() -> dict:
    parser = argparse.ArgumentParser(description="Génère les plans théoriques IA des 2 prochaines semaines")
    parser.add_argument("--dry-run", action="store_true", help="Affiche les prompts sans appeler l'API Claude")
    parser.add_argument("--force", action="store_true", help="Force la régénération même si déjà fait")
    parser.add_argument("--skip-import", action="store_true", help="Réutilise inputs/intervals/ sans réimporter")
    args = parser.parse_args()

    if not args.skip_import:
        import_intervals()
    return plans.generate_plans(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
