"""Job quotidien : import Intervals.icu → moteur → sorties → export Intervals.icu.

1. Importe les données Intervals.icu dans inputs/intervals/
2. Lance le moteur (engine/coach.py) → docs/data/today.json
3. Exporte les séances à venir du plan adaptatif vers Intervals.icu (→ Garmin)

Usage :
    python jobs/daily.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.intervals_client import IntervalsClient
from engine import coach
from jobs.import_intervals import import_intervals
from jobs.push_workouts import plan_item_to_event


def main() -> dict:
    client = IntervalsClient()
    import_intervals(client)
    snapshot = coach.run()

    # Export : pousse les séances FUTURES du plan adaptatif vers Intervals.icu
    # en remplaçant les séances déjà planifiées sur ces jours.
    try:
        sync_adaptive_plan_to_intervals(
            client, snapshot["weekly_plan"], snapshot["thresholds"], date.today()
        )
    except Exception as e:
        print(f"⚠️  Sync Intervals.icu échouée : {e}")
    return snapshot


def sync_adaptive_plan_to_intervals(
    client: "IntervalsClient",
    plan: list[dict],
    thresholds: dict,
    today: date,
) -> None:
    """Supprime et recrée les séances planifiées (WORKOUT) sur les jours futurs
    du plan adaptatif dans Intervals.icu.

    - Ne touche PAS aux jours passés (status=done/past_missed) ni à aujourd'hui.
    - Supprime uniquement les events de type WORKOUT (pas les activités réelles).
    - Le titre de la séance dans Intervals.icu correspond au champ 'type' du plan.
    """
    # Jours futurs uniquement (status=todo, pas aujourd'hui ni passé)
    future_days = [
        p for p in plan
        if p.get("status") == "todo"
        and p.get("sport") not in ("Repos", None, "Strength")
    ]

    if not future_days:
        print("ℹ️  Aucune séance future à synchroniser sur Intervals.icu.")
        return

    # Dates concernées
    future_dates = [date.fromisoformat(p["date"]) for p in future_days]
    min_date = min(future_dates)
    max_date = max(future_dates)

    print(f"\n🔄 Sync Intervals.icu : {len(future_days)} séances futures "
          f"({min_date.isoformat()} → {max_date.isoformat()})")

    # Supprime les WORKOUT existants sur ces dates uniquement
    existing_events = client.events(min_date, max_date)
    deleted = 0
    for ev in existing_events:
        ev_date_str = (ev.get("start_date_local") or "")[:10]
        if ev.get("category") == "WORKOUT" and ev_date_str >= today.isoformat():
            try:
                client.delete_event(ev["id"])
                deleted += 1
            except Exception as e:
                print(f"  ⚠️  Suppression event {ev['id']} ({ev_date_str}) échouée : {e}")

    if deleted:
        print(f"  🗑️  {deleted} séances supprimées.")

    # Crée les nouvelles séances
    sent, errors = 0, 0
    for item in future_days:
        # Nom de la séance = type du plan adaptatif (ex: "Intervalles Z4", "Sweet Spot")
        event = plan_item_to_event(item, thresholds)
        if not event:
            continue
        # Remplace le nom générique par le titre exact du plan adaptatif
        sport_prefix = {"Run": "RUN", "VirtualRide": "BIKE", "Ride": "BIKE", "Swim": "SWIM"}.get(
            item.get("sport", ""), item.get("sport", "").upper()
        )
        dur = item.get("duration_min", 0)
        event["name"] = f"{sport_prefix} {dur}' — {item.get('type', '')}"
        try:
            result = client.create_event(event)
            print(f"  ✅ {item['date']} [{item.get('weekday_fr',''):>8}] {event['name']} "
                  f"(id={result.get('id')})")
            sent += 1
        except Exception as e:
            print(f"  ❌ {item['date']} {event['name']} : {e}")
            errors += 1

    print(f"  → {sent} séances créées, {errors} erreurs.")


if __name__ == "__main__":
    main()
