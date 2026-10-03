"""Lecture des données Intervals.icu importées dans inputs/intervals/.

Le moteur ne fait aucun appel réseau à Intervals.icu : il lit les fichiers
produits par jobs/import_intervals.py. Cette classe expose les mêmes méthodes
de lecture que connectors.intervals_client.IntervalsClient.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from engine.paths import INTERVALS_INPUT_DIR


class IntervalsInputs:
    def __init__(self, folder: Path = INTERVALS_INPUT_DIR):
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"Aucun import Intervals.icu dans {folder}. "
                "Lance d'abord : python jobs/import_intervals.py"
            )
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.athlete_id = self.meta.get("athlete_id")
        self._thresholds = json.loads((folder / "thresholds.json").read_text(encoding="utf-8"))
        self._wellness = json.loads((folder / "wellness.json").read_text(encoding="utf-8"))
        self._activities = json.loads((folder / "activities.json").read_text(encoding="utf-8"))

    def get_thresholds(self) -> dict:
        return self._thresholds

    def wellness(self, oldest: date, newest: date | None = None) -> list[dict]:
        return _between(self._wellness, lambda w: w.get("id", ""), oldest, newest)

    def activities(self, oldest: date, newest: date | None = None) -> list[dict]:
        return _between(self._activities, lambda a: (a.get("start_date_local") or "")[:10],
                        oldest, newest)


def _between(rows: list[dict], day_of, oldest: date, newest: date | None) -> list[dict]:
    lo = oldest.isoformat()
    hi = newest.isoformat() if newest else "9999-12-31"
    return [r for r in rows if lo <= day_of(r) <= hi]
