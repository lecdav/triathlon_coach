"""push_workouts.py — Envoie le plan hebdomadaire sur Intervals.icu (→ Garmin Connect).

Usage :
    python scripts/push_workouts.py              # semaine en cours
    python scripts/push_workouts.py --dry-run    # affiche les séances sans les envoyer
    python scripts/push_workouts.py --replace    # supprime d'abord les séances existantes

Le script réutilise build_weekly_plan() de daily_coach.py pour construire le plan,
puis convertit chaque séance en événement Intervals.icu structuré (avec workout_doc / steps).
Intervals.icu synchronise automatiquement avec Garmin Connect si la connexion est activée.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intervals_client import IntervalsClient, pace_mps_to_minkm, pace_mps_to_per100m

# Import du plan depuis daily_coach — on réutilise la même logique
from daily_coach import (
    build_weekly_plan,
    classify_form,
    weekly_load,
    average_session_profile,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Correspondances sport → type Intervals.icu
# ---------------------------------------------------------------------------
SPORT_TYPE_MAP: dict[str, str] = {
    "Run": "Run",
    "VirtualRide": "VirtualRide",
    "Ride": "Ride",
    "Swim": "Swim",
    "Brick (Bike+Run)": "VirtualRide",  # on envoie la partie vélo en principal
    "Strength": "WeightTraining",
    "Repos": None,  # pas d'event pour les jours de repos
}

# Couleurs par type de séance (code couleur Intervals.icu)
WORKOUT_COLORS: dict[str, str] = {
    "VO2max": "red",
    "Seuil": "orange",
    "sweet spot": "orange",
    "Endurance": "blue",
    "Sortie longue": "blue",
    "Spécifique": "purple",
    "Brick": "purple",
    "Renforcement": "green",
    "Récupération": "gray",
}


def pick_color(workout_type: str) -> str:
    for key, color in WORKOUT_COLORS.items():
        if key.lower() in workout_type.lower():
            return color
    return "blue"


# ---------------------------------------------------------------------------
# Génération du nom compact de séance
# ---------------------------------------------------------------------------

SPORT_PREFIX: dict[str, str] = {
    "Run":              "RUN",
    "VirtualRide":      "BIKE",
    "Ride":             "BIKE",
    "Swim":             "SWIM",
    "Brick (Bike+Run)": "BRICK",
    "Strength":         "GYM",
}


def build_workout_name(item: dict, workout_doc: dict | None, thresholds: dict) -> str:
    """Génère un nom compact style 'BIKE 60' 3×12' 88-94%FTP'.

    Exemples :
      BIKE 60' 3×12' 88-94%FTP
      RUN 45' 7×3' Z5
      SWIM 3×600m
      GYM 45'
      BIKE 90' Z2
    """
    sport     = item.get("sport", "")
    wtype     = item.get("type", "")
    total_min = item.get("duration_min", 0)
    prefix    = SPORT_PREFIX.get(sport, sport.upper())
    ftp       = thresholds.get("ftp_watts") or 230

    # ---- Natation : distance totale ou structure de blocs ----
    if sport == "Swim" and workout_doc:
        steps = workout_doc.get("steps", [])
        # Cherche le step intervalle principal
        main = next((s for s in steps if s.get("type") == "interval"), None)
        if main and main.get("distance") and main.get("reps"):
            dist_m = main["distance"] * main["reps"]
            # + warmup + cooldown
            for s in steps:
                if s.get("type") in ("warmup", "cooldown") and s.get("distance"):
                    dist_m += s["distance"]
            return f"SWIM {main['reps']}×{main['distance']}m"
        elif total_min:
            return f"SWIM {total_min}'"
        return "SWIM"

    # ---- GYM / Renforcement ----
    if sport == "Strength":
        return f"GYM {total_min}'" if total_min else "GYM"

    # ---- Brick ----
    if sport == "Brick (Bike+Run)" and workout_doc:
        steps = workout_doc.get("steps", [])
        bike_step = next((s for s in steps if s.get("type") == "interval"), None)
        bike_min  = round(bike_step["duration"] / 60) if bike_step else 0
        run_min   = total_min - bike_min - 10 if bike_min else 0
        return f"BRICK {bike_min}'+{run_min}' Z2"

    # ---- Vélo / CAP : cherche le bloc intervalles principal ----
    if workout_doc:
        steps = workout_doc.get("steps", [])
        main = next((s for s in steps if s.get("type") == "interval"), None)
        if main and main.get("reps", 1) > 1:
            reps     = main["reps"]
            dur_s    = main.get("duration", 0)
            dur_min  = round(dur_s / 60) if dur_s >= 60 else None
            dur_str  = f"{dur_min}'" if dur_min else f"{dur_s}''"

            # Cible : puissance (vélo), %LT (cap) ou zone générique
            power = main.get("power")
            if power and ftp:
                pct_lo = round(power["min"] / ftp * 100)
                pct_hi = round(power["max"] / ftp * 100)
                target = f"{pct_lo}-{pct_hi}%FTP"
            elif main.get("pace_pct_lt"):
                target = f"{main['pace_pct_lt']}%LT"
            elif main.get("zone"):
                target = f"Z{main['zone']}"
            else:
                target = ""

            interval_part = f"{reps}×{dur_str} {target}".strip()
            return f"{prefix} {total_min}' {interval_part}"

        # Pas d'intervalles répétés → sortie longue / endurance
        main_dur = next(
            (s for s in steps if s.get("type") == "interval"), None
        )
        if main_dur:
            zone = main_dur.get("zone")
            power = main_dur.get("power")
            pct_lt = main_dur.get("pace_pct_lt")
            if power and ftp:
                pct_lo = round(power["min"] / ftp * 100)
                pct_hi = round(power["max"] / ftp * 100)
                return f"{prefix} {total_min}' {pct_lo}-{pct_hi}%FTP"
            elif pct_lt:
                return f"{prefix} {total_min}' {pct_lt}%LT"
            elif zone:
                return f"{prefix} {total_min}' Z{zone}"

    # Fallback
    return f"{prefix} {total_min}'" if total_min else prefix


# ---------------------------------------------------------------------------
# Conversion d'une séance → workout_doc structuré (steps Intervals.icu)
# ---------------------------------------------------------------------------

def build_workout_doc(item: dict, thresholds: dict) -> dict | None:
    """Construit un workout_doc avec steps pour Intervals.icu.

    Format workout_doc :
    {
      "steps": [
        {"type": "warmup",   "duration": 900,  "power": {"min": 0.56, "max": 0.75}},
        {"type": "interval", "duration": 180,  "power": {"min": 0.91, "max": 1.05},
         "reps": 6},
        {"type": "rest",     "duration": 120},
        {"type": "cooldown", "duration": 600},
      ]
    }
    Les durées sont en secondes.
    Pour la CAP : on peut utiliser pace (m/s) ou target_pace_str.
    Pour la natation : même logique.
    """
    sport = item.get("sport", "")
    wtype = item.get("type", "")
    duration_min = item.get("duration_min", 0)
    ftp = thresholds.get("ftp_watts", 230)
    lthr = thresholds.get("lthr", 160)

    if not sport or sport == "Repos" or duration_min == 0:
        return None

    steps = []

    # ---- CAP ----
    if sport == "Run":
        thr_mps = thresholds.get("threshold_pace_run_mps")
        if "VO2max" in wtype or "intervalles" in wtype.lower():
            # 15' échauf + 7 × 3' @ ~105%LT (Z5) r=2' + 10' retour
            steps = [
                {"type": "warmup",   "duration": 900,  "zone": 2},
                {"type": "interval", "duration": 180,  "zone": 5, "reps": 7,
                 "restDuration": 120, "restZone": 1, "pace_pct_lt": 105},
                {"type": "cooldown", "duration": 600,  "zone": 1},
            ]
        elif "Seuil" in wtype or "tempo" in wtype.lower():
            # Seuil lactate ~95-100%LT
            steps = [
                {"type": "warmup",   "duration": 600,  "zone": 2},
                {"type": "interval", "duration": max((duration_min - 20) * 60, 1200), "zone": 4,
                 "pace_pct_lt": 98},
                {"type": "cooldown", "duration": 600,  "zone": 1},
            ]
        elif "Sortie longue" in wtype or "long" in wtype.lower():
            # Longue sortie ~75%LT (Z2)
            steps = [
                {"type": "warmup",   "duration": 600,  "zone": 1},
                {"type": "interval", "duration": max((duration_min - 20) * 60, 1800), "zone": 2,
                 "pace_pct_lt": 75},
                {"type": "cooldown", "duration": 600,  "zone": 1},
            ]
        else:
            # Endurance souple / récup ~70%LT (Z2)
            steps = [
                {"type": "warmup",   "duration": 300,  "zone": 1},
                {"type": "interval", "duration": max((duration_min - 10) * 60, 600), "zone": 2,
                 "pace_pct_lt": 70},
                {"type": "cooldown", "duration": 300,  "zone": 1},
            ]

    # ---- VÉLO ----
    elif sport in ("VirtualRide", "Ride"):
        if "sweet spot" in wtype.lower() or "Seuil" in wtype:
            # 15' échauf + 3×12' sweet spot r=4' + 8' calme
            steps = [
                {"type": "warmup",   "duration": 900,
                 "power": {"min": round(ftp * 0.56), "max": round(ftp * 0.75)}},
                {"type": "interval", "duration": 720,
                 "power": {"min": round(ftp * 0.88), "max": round(ftp * 0.94)},
                 "reps": 3,
                 "restDuration": 240,
                 "restPower": {"min": round(ftp * 0.50), "max": round(ftp * 0.60)}},
                {"type": "cooldown", "duration": 480,
                 "power": {"min": round(ftp * 0.50), "max": round(ftp * 0.60)}},
            ]
        elif "Sortie longue" in wtype:
            steps = [
                {"type": "warmup",   "duration": 600,
                 "power": {"min": round(ftp * 0.50), "max": round(ftp * 0.65)}},
                {"type": "interval", "duration": max((duration_min - 20) * 60, 3600),
                 "power": {"min": round(ftp * 0.56), "max": round(ftp * 0.75)}},
                {"type": "cooldown", "duration": 600,
                 "power": {"min": round(ftp * 0.50), "max": round(ftp * 0.60)}},
            ]
        else:
            # Endurance Z2 générique
            steps = [
                {"type": "warmup",   "duration": 600,
                 "power": {"min": round(ftp * 0.50), "max": round(ftp * 0.65)}},
                {"type": "interval", "duration": max((duration_min - 15) * 60, 1800),
                 "power": {"min": round(ftp * 0.56), "max": round(ftp * 0.75)}},
                {"type": "cooldown", "duration": 300,
                 "power": {"min": round(ftp * 0.50), "max": round(ftp * 0.60)}},
            ]

    # ---- NATATION ----
    elif sport == "Swim":
        # 400 échauf + 10×100 @ CSS r=20s + 200 cool
        steps = [
            {"type": "warmup",   "distance": 400,  "zone": 2},
            {"type": "interval", "distance": 100,  "zone": 4, "reps": 10,
             "restDuration": 20},
            {"type": "cooldown", "distance": 200,  "zone": 1},
        ]

    # ---- BRICK (Vélo+CAP) ----
    elif sport == "Brick (Bike+Run)":
        bike_min = max(int(duration_min * 0.8), 45)
        run_min = max(duration_min - bike_min, 15)
        steps = [
            {"type": "warmup",   "duration": 600,
             "power": {"min": round(ftp * 0.50), "max": round(ftp * 0.65)}},
            {"type": "interval", "duration": (bike_min - 10) * 60,
             "power": {"min": round(ftp * 0.56), "max": round(ftp * 0.75)}},
            {"type": "cooldown", "duration": run_min * 60, "zone": 2,
             "note": f"Transition → CAP {run_min}' Z2"},
        ]

    # ---- RENFORCEMENT ----
    elif sport == "Strength":
        # Pas de steps cardio, juste la description textuelle suffit
        return None

    if not steps:
        return None

    return {"steps": steps}


# ---------------------------------------------------------------------------
# Conversion d'un item du plan → event Intervals.icu
# ---------------------------------------------------------------------------

def plan_item_to_event(item: dict, thresholds: dict) -> dict | None:
    """Convertit un item du plan hebdomadaire en dict event Intervals.icu."""
    sport = item.get("sport", "")
    intervals_type = SPORT_TYPE_MAP.get(sport)
    if intervals_type is None:
        return None  # Repos → pas d'event

    workout_doc = build_workout_doc(item, thresholds)
    workout_name = build_workout_name(item, workout_doc, thresholds)
    description = item.get("structure", "")
    if item.get("zones"):
        description += f"\n\nZones cibles : {item['zones']}"
    if item.get("rationale"):
        description += f"\n\n💡 {item['rationale']}"

    event: dict = {
        "start_date_local": f"{item['date']}T09:00:00",
        "category": "WORKOUT",
        "name": workout_name,
        "type": intervals_type,
        "description": description,
        "moving_time": item.get("duration_min", 0) * 60,
        "color": pick_color(item.get("type", "")),
    }

    if workout_doc:
        event["workout_doc"] = workout_doc

    return event


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Envoie le plan hebdomadaire sur Intervals.icu")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche les séances sans les envoyer")
    parser.add_argument("--replace", action="store_true",
                        help="Supprime les séances existantes de la semaine avant d'envoyer")
    parser.add_argument("--week", default=None,
                        help="Date de début de semaine (YYYY-MM-DD, lundi). Défaut = semaine courante.")
    args = parser.parse_args()

    today = date.today()
    if args.week:
        week_monday = date.fromisoformat(args.week)
    else:
        week_monday = today - timedelta(days=today.weekday())

    print(f"📅 Semaine du {week_monday.isoformat()} au {(week_monday + timedelta(days=6)).isoformat()}")

    client = IntervalsClient()
    print(f"🔗 Connexion Intervals.icu OK — athlète {client.athlete_id}")

    # Récupération du profil YAML (race_date, etc.)
    profile_path = ROOT / "config" / "athlete_profile.yaml"
    try:
        import yaml  # type: ignore
        athlete_profile_raw = yaml.safe_load(profile_path.read_text()) if profile_path.exists() else {}
    except Exception:
        athlete_profile_raw = {}

    race_date = None
    race_cfg = (athlete_profile_raw or {}).get("race", {})
    if race_cfg.get("date"):
        try:
            race_date = date.fromisoformat(str(race_cfg["date"]))
        except ValueError:
            pass

    # Récupération des données API
    thresholds = client.get_thresholds()
    activities = client.activities(today - timedelta(days=42))
    wellness_data = client.wellness(today - timedelta(days=14))

    # Forme du jour (TSB)
    today_w = next((w for w in wellness_data if w.get("id") == today.isoformat()),
                   wellness_data[-1] if wellness_data else {})
    ctl = today_w.get("ctl") or 0
    atl = today_w.get("atl") or 0
    tsb = ctl - atl
    form = classify_form(tsb)

    session_profile = average_session_profile(activities, 28)

    # Construction du plan
    plan, weeks_to_race, phase = build_weekly_plan(
        week_monday, form, session_profile, thresholds, race_date
    )

    print(f"🏋️  Phase : {phase}")
    if weeks_to_race is not None:
        print(f"⏱️  Semaines avant course : {weeks_to_race}")
    print()

    # Conversion en events
    events_to_send = []
    for item in plan:
        ev = plan_item_to_event(item, thresholds)
        if ev:
            events_to_send.append((item, ev))

    print(f"📋 {len(events_to_send)} séances à envoyer :\n")
    for item, ev in events_to_send:
        print(f"  • {ev['start_date_local']} [{item['weekday_fr']:>8}] {ev['name']}")

    if args.dry_run:
        print("\n🔍 Mode dry-run — aucun envoi effectué.")
        print("\nExemple de payload JSON pour la 1re séance :")
        print(json.dumps(events_to_send[0][1], indent=2, ensure_ascii=False))
        return

    # Suppression préalable si --replace
    if args.replace:
        deleted = client.delete_week_workouts(week_monday)
        if deleted:
            print(f"\n🗑️  {len(deleted)} séances existantes supprimées : {deleted}")
        else:
            print("\n✅ Aucune séance existante à supprimer.")

    # Envoi
    print()
    sent, errors = 0, 0
    for item, ev in events_to_send:
        try:
            result = client.create_event(ev)
            print(f"  ✅ {ev['start_date_local']} — {ev['name']} (id={result.get('id')})")
            sent += 1
        except Exception as e:
            print(f"  ❌ {ev['start_date_local']} — {ev['name']} : {e}")
            errors += 1

    print(f"\n🎯 Résumé : {sent} séances envoyées, {errors} erreurs.")
    if sent > 0:
        print("👉 Ouvre Intervals.icu → Calendrier pour vérifier, "
              "puis synchronise avec Garmin Connect (Settings → Connections).")


if __name__ == "__main__":
    main()
