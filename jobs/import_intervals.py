"""Import Intervals.icu → inputs/intervals/ (seuils, wellness, activités).

Les fichiers importés ne sont pas versionnés (données de santé personnelles) :
ils sont recréés à chaque run, puis lus par le moteur (engine/).

Usage :
    python jobs/import_intervals.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.intervals_client import IntervalsClient
from engine.paths import INTERVALS_INPUT_DIR

WELLNESS_DAYS = 90                 # graphique PMC du dashboard
ACTIVITIES_MIN_DAYS = 42           # profil de séances + croisement avec le plan
FRISE_START = date(2026, 5, 4)     # début de la frise saison sur le dashboard


def import_intervals(client: IntervalsClient | None = None) -> Path:
    client = client or IntervalsClient()
    today = date.today()
    week_monday = today - timedelta(days=today.weekday())
    wellness_since = today - timedelta(days=WELLNESS_DAYS)
    activities_since = min(today - timedelta(days=ACTIVITIES_MIN_DAYS), week_monday, FRISE_START)

    print(f"⬇️  Import Intervals.icu — athlète {client.athlete_id}")
    data = {
        "thresholds.json": client.get_thresholds(),
        "wellness.json": client.wellness(wellness_since),
        "activities.json": client.activities(activities_since),
        "meta.json": {
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "athlete_id": client.athlete_id,
            "wellness_since": wellness_since.isoformat(),
            "activities_since": activities_since.isoformat(),
        },
    }

    INTERVALS_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in data.items():
        (INTERVALS_INPUT_DIR / name).write_text(
            json.dumps(content, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
    print(f"   {len(data['wellness.json'])} jours de wellness, "
          f"{len(data['activities.json'])} activités → {INTERVALS_INPUT_DIR}")
    return INTERVALS_INPUT_DIR


if __name__ == "__main__":
    import_intervals()
