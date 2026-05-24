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
    "VirtualRide": "Ride",   # Garmin sync uniquement Ride pour les workouts vélo structurés
    "Ride": "Ride",
    "Swim": "Swim",
    "Brick (Bike+Run)": "Ride",
    "Strength": None,        # Garmin ne supporte pas les workouts structurés de gym
    "Repos": None,
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


def build_workout_name(item: dict, workout_text: str | None, thresholds: dict) -> str:
    """Génère un nom compact style 'BIKE 60' 3×12' 88-94%FTP'.

    Exemples :
      BIKE 60' 3×12' 88-94%FTP
      RUN 45' 7×3' 105%Pace
      SWIM 10×100m
      GYM 45'
      BIKE 90' Z2
    """
    sport     = item.get("sport", "")
    wtype     = item.get("type", "")
    total_min = item.get("duration_min", 0)
    prefix    = SPORT_PREFIX.get(sport, sport.upper())

    # ---- GYM / Renforcement ----
    if sport == "Strength":
        return f"GYM {total_min}'" if total_min else "GYM"

    # ---- Natation ----
    if sport == "Swim":
        return "SWIM 10×100m"

    # ---- Brick ----
    if sport == "Brick (Bike+Run)":
        bike_min = max(int(total_min * 0.8), 45)
        run_min  = max(total_min - bike_min, 15)
        return f"BRICK {bike_min}'+{run_min}' Z2"

    # ---- CAP ----
    if sport == "Run":
        if "VO2max" in wtype or "intervalles" in wtype.lower():
            return f"RUN {total_min}' 7×3' 105%Pace"
        elif "Seuil" in wtype or "tempo" in wtype.lower():
            return f"RUN {total_min}' 95-100%Pace"
        elif "Sortie longue" in wtype or "long" in wtype.lower():
            return f"RUN {total_min}' 75%Pace"
        else:
            return f"RUN {total_min}' Z2"

    # ---- Vélo ----
    if sport in ("VirtualRide", "Ride"):
        if "sweet spot" in wtype.lower() or "Seuil" in wtype:
            return f"BIKE {total_min}' 3×12' 88-94%FTP"
        elif "Sortie longue" in wtype:
            return f"BIKE {total_min}' 56-75%FTP"
        else:
            return f"BIKE {total_min}' Z2"

    # Fallback
    return f"{prefix} {total_min}'" if total_min else prefix


# ---------------------------------------------------------------------------
# Conversion d'une séance → workout_doc structuré (steps Intervals.icu)
# ---------------------------------------------------------------------------

def _run_pace(thresholds: dict, pct_low: float, pct_high: float) -> str:
    """Allure CAP en plage absolue → '5:04-5:20 pace' (format reconnu par Intervals.icu).

    pct_low  = % seuil pour l'allure rapide (borne haute de vitesse)
    pct_high = % seuil pour l'allure lente  (borne basse de vitesse)
    Ex: _run_pace(t, 1.03, 1.07) → allures encadrant 105% du seuil
    """
    mps_val = thresholds.get("threshold_pace_run_mps")
    if not mps_val or mps_val <= 0:
        return "Z2 HR"
    # allure rapide = vitesse haute = pct_low
    sec_fast = 1000.0 / (mps_val * pct_low)
    sec_slow = 1000.0 / (mps_val * pct_high)
    mf, sf = divmod(int(round(sec_fast)), 60)
    ms, ss = divmod(int(round(sec_slow)), 60)
    return f"{mf}:{sf:02d}-{ms}:{ss:02d} pace"


def _swim_pace(thresholds: dict, pct_low: float, pct_high: float) -> str:
    """Allure natation en plage absolue → '1:55-2:05/100m pace'."""
    mps_val = thresholds.get("threshold_pace_swim_mps")
    if not mps_val or mps_val <= 0:
        return "Z3 HR"
    sec_fast = 100.0 / (mps_val * pct_low)
    sec_slow = 100.0 / (mps_val * pct_high)
    mf, sf = divmod(int(round(sec_fast)), 60)
    ms, ss = divmod(int(round(sec_slow)), 60)
    return f"{mf}:{sf:02d}-{ms}:{ss:02d}/100m pace"


def build_workout_text(item: dict, thresholds: dict) -> str | None:
    """Génère le texte markdown des steps pour Intervals.icu.

    C'est la SEULE façon de créer des séances structurées via l'API events :
    Intervals.icu parse les steps depuis le champ 'description' en texte markdown.
    Le champ workout_doc doit être passé comme {} (objet vide) pour déclencher le parsing.

    Syntaxe (doc officielle) :
      - [durée] [cible]          → un step simple
      Nx                         → bloc répété N fois (ligne séparée, ligne vide avant/après)
      - [durée] [cible]          → step dans le bloc

    Cibles :
      Vélo  : 88-94%                (% FTP — Garmin OK)
      CAP   : 5:04-5:20 pace        (plage d'allure absolue, format minimal sans nom de step)
      Nata  : 1:55-2:05/100m pace   (même format)

    Durées : 10m, 30s, 1h30m, 400mtr, 2km
    """
    sport = item.get("sport", "")
    wtype = item.get("type", "")
    duration_min = item.get("duration_min", 0)

    if not sport or sport in ("Repos", "Strength") or duration_min == 0:
        return None

    lines = []

    # ---- CAP ----
    # Format minimal "- Xm A:BB-C:DD pace" sans nom de step — identique à
    # l'exemple du forum qui fonctionne dans Garmin.
    if sport == "Run":
        pace_z1    = _run_pace(thresholds, 0.80, 0.87)   # ~Z1 récup
        pace_z2    = _run_pace(thresholds, 0.83, 0.90)   # ~Z2 endurance
        pace_seuil = _run_pace(thresholds, 0.97, 1.03)   # ~seuil
        pace_vo2   = _run_pace(thresholds, 1.03, 1.08)   # ~VO2max

        # Échauffement CAP : 20 min avec gammes, retour au calme : 5 min
        if "VO2max" in wtype or "intervalles" in wtype.lower():
            bloc_min = max(duration_min - 25, 15)  # 20' écha + 5' cool
            lines = [
                f"- 10m {pace_z2}",                          # trot progressif
                f"- 5m {pace_z1}",                           # gammes (talons-fesses, montées genoux, foulées bondissantes)
                f"- 5m {pace_z2}",                           # accélérations progressives
                "",
                "7x",
                f"- 3m {pace_vo2}",
                f"- 2m {pace_z1}",
                "",
                f"- 5m {pace_z1}",                          # retour au calme 5'
            ]
        elif "Seuil" in wtype or "tempo" in wtype.lower():
            bloc_min = max(duration_min - 25, 20)
            lines = [
                f"- 10m {pace_z2}",
                f"- 5m {pace_z1}",                           # gammes
                f"- 5m {pace_z2}",                           # accélérations
                "",
                f"- {bloc_min}m {pace_seuil}",
                "",
                f"- 5m {pace_z1}",
            ]
        elif "Sortie longue" in wtype or "long" in wtype.lower():
            bloc_min = max(duration_min - 25, 30)
            lines = [
                f"- 10m {pace_z1}",
                f"- 5m {pace_z1}",                           # gammes légères
                f"- 5m {pace_z2}",
                "",
                f"- {bloc_min}m {pace_z2}",
                "",
                f"- 5m {pace_z1}",
            ]
        else:
            bloc_min = max(duration_min - 25, 20)
            lines = [
                f"- 10m {pace_z1}",
                f"- 5m {pace_z1}",                           # gammes
                f"- 5m {pace_z2}",
                "",
                f"- {bloc_min}m {pace_z2}",
                "",
                f"- 5m {pace_z1}",
            ]

    # ---- VÉLO ----
    elif sport in ("VirtualRide", "Ride"):
        # Échauffement vélo : 15 min, retour au calme : 5 min
        if "sweet spot" in wtype.lower() or "Seuil" in wtype:
            lines = [
                "- Échauffement 15m 56-75%",
                "",
                "3x",
                "- Sweet Spot 12m 88-94%",
                "- Récupération 4m 50-60%",
                "",
                "- Retour au calme 5m 50-60%",
            ]
        elif "Sortie longue" in wtype:
            bloc_min = max(duration_min - 20, 60)
            lines = [
                "- Mise en route 15m 50-65%",
                "",
                f"- Endurance {bloc_min}m 56-75%",
                "",
                "- Retour au calme 5m 50-60%",
            ]
        else:
            bloc_min = max(duration_min - 20, 30)
            lines = [
                "- Mise en route 15m 50-65%",
                "",
                f"- Endurance {bloc_min}m 56-75%",
                "",
                "- Retour au calme 5m 50-60%",
            ]

    # ---- NATATION ----
    elif sport == "Swim":
        swim_easy     = _swim_pace(thresholds, 0.80, 0.88)
        swim_interval = _swim_pace(thresholds, 1.02, 1.08)
        lines = [
            f"- 400mtr {swim_easy}",
            "",
            "10x",
            f"- 100mtr {swim_interval}",
            "- 20s",
            "",
            f"- 200mtr {swim_easy}",
        ]

    # ---- BRICK (Vélo+CAP) ----
    elif sport == "Brick (Bike+Run)":
        pace_z2  = _run_pace(thresholds, 0.83, 0.90)
        bike_min = max(int(duration_min * 0.8), 45)
        run_min  = max(duration_min - bike_min, 15)
        lines = [
            "- 10m 50-65%",
            "",
            f"- {bike_min - 10}m 56-75%",
            "",
            f"- {run_min}m {pace_z2}",
        ]

    if not lines:
        return None

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversion d'un item du plan → event Intervals.icu
# ---------------------------------------------------------------------------

def plan_item_to_event(item: dict, thresholds: dict) -> dict | None:
    """Convertit un item du plan hebdomadaire en dict event Intervals.icu.

    Les steps structurés sont encodés en texte markdown dans 'description'.
    NE PAS inclure workout_doc dans le payload : si workout_doc est absent,
    Intervals.icu parse la description, génère le workout_doc et le FIT
    structuré qu'il transmet à Garmin. Si workout_doc={} est présent,
    Intervals.icu considère les steps comme déjà fournis et ne parse pas
    la description → Garmin reçoit une séance sans structure.
    """
    sport = item.get("sport", "")
    intervals_type = SPORT_TYPE_MAP.get(sport)
    if intervals_type is None:
        return None  # Repos → pas d'event

    workout_text = build_workout_text(item, thresholds)
    workout_name = build_workout_name(item, workout_text, thresholds)

    # Description = steps structurés en markdown + notes contextuelles
    description_parts = []
    if workout_text:
        description_parts.append(workout_text)
    if item.get("structure"):
        description_parts.append(f"\n---\n{item['structure']}")
    if item.get("zones"):
        description_parts.append(f"Zones cibles : {item['zones']}")
    if item.get("rationale"):
        description_parts.append(f"💡 {item['rationale']}")
    description = "\n\n".join(description_parts)

    event: dict = {
        "start_date_local": f"{item['date']}T09:00:00",
        "category": "WORKOUT",
        "name": workout_name,
        "type": intervals_type,
        "description": description,
        "moving_time": item.get("duration_min", 0) * 60,
        "color": pick_color(item.get("type", "")),
        # workout_doc intentionnellement absent : Intervals.icu parse alors
        # la description markdown et génère le FIT structuré pour Garmin.
    }

    return event


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def push_week(client: "IntervalsClient", week_monday: date, plan: list[dict],
              thresholds: dict, do_replace: bool, dry_run: bool, label: str) -> tuple[int, int]:
    """Envoie un plan hebdomadaire vers Intervals.icu. Retourne (sent, errors)."""
    week_sunday = week_monday + timedelta(days=6)
    print(f"\n{'='*60}")
    print(f"📅 {label} — du {week_monday.isoformat()} au {week_sunday.isoformat()}")

    events_to_send = []
    for item in plan:
        ev = plan_item_to_event(item, thresholds)
        if ev:
            events_to_send.append((item, ev))

    print(f"📋 {len(events_to_send)} séances :")
    for item, ev in events_to_send:
        print(f"  • {ev['start_date_local']} [{item['weekday_fr']:>8}] {ev['name']}")

    if dry_run:
        print("🔍 Mode dry-run — aucun envoi.")
        return 0, 0

    if do_replace:
        deleted = client.delete_week_workouts(week_monday)
        if deleted:
            print(f"🗑️  {len(deleted)} séances supprimées : {deleted}")
        else:
            print("✅ Aucune séance existante à supprimer.")

    sent, errors = 0, 0
    for item, ev in events_to_send:
        try:
            result = client.create_event(ev)
            print(f"  ✅ {ev['start_date_local']} — {ev['name']} (id={result.get('id')})")
            sent += 1
        except Exception as e:
            print(f"  ❌ {ev['start_date_local']} — {ev['name']} : {e}")
            errors += 1

    return sent, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Envoie le plan des 2 semaines sur Intervals.icu")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche les séances sans les envoyer")
    parser.add_argument("--replace", action="store_true",
                        help="Supprime les séances existantes avant d'envoyer")
    parser.add_argument("--week", default=None,
                        help="Date de début de semaine (YYYY-MM-DD, lundi). Défaut = semaine courante.")
    parser.add_argument("--only-current", action="store_true",
                        help="N'envoie que la semaine en cours (pas la suivante).")
    args = parser.parse_args()

    today = date.today()
    if args.week:
        week_monday = date.fromisoformat(args.week)
    else:
        week_monday = today - timedelta(days=today.weekday())

    next_week_monday = week_monday + timedelta(days=7)

    client = IntervalsClient()
    print(f"🔗 Connexion Intervals.icu OK — athlète {client.athlete_id}")

    # Récupération du profil (race_date)
    race_date = None
    try:
        import json as _json
        athlete_json_path = ROOT / "data" / "athlete_profile.json"
        if athlete_json_path.exists():
            _ap = _json.loads(athlete_json_path.read_text())
            _rd = _ap.get("season", {}).get("race", {}).get("date")
            if _rd:
                race_date = date.fromisoformat(str(_rd))
    except Exception:
        pass
    if not race_date:
        try:
            import yaml  # type: ignore
            profile_path = ROOT / "config" / "athlete_profile.yaml"
            if profile_path.exists():
                raw = yaml.safe_load(profile_path.read_text()) or {}
                _rd = raw.get("race", {}).get("date")
                if _rd:
                    race_date = date.fromisoformat(str(_rd))
        except Exception:
            pass

    # Données API
    thresholds = client.get_thresholds()
    activities = client.activities(today - timedelta(days=42))
    wellness_data = client.wellness(today - timedelta(days=14))

    today_w = next((w for w in wellness_data if w.get("id") == today.isoformat()),
                   wellness_data[-1] if wellness_data else {})
    ctl = today_w.get("ctl") or 0
    atl = today_w.get("atl") or 0
    form = classify_form(ctl - atl)

    session_profile = average_session_profile(activities, 28)

    # Plan semaine en cours (théorique — sans match activités)
    plan_cur, weeks_to_race, phase = build_weekly_plan(
        week_monday, form, session_profile, thresholds, race_date
    )

    print(f"🏋️  Phase : {phase}")
    if weeks_to_race is not None:
        print(f"⏱️  Semaines avant course : {weeks_to_race}")

    if args.dry_run:
        print("\n🔍 Mode dry-run activé — aucun envoi ne sera effectué.")

    total_sent, total_errors = 0, 0

    s, e = push_week(client, week_monday, plan_cur, thresholds,
                     args.replace, args.dry_run, "Semaine en cours")
    total_sent += s; total_errors += e

    if not args.only_current:
        plan_next, _, _ = build_weekly_plan(
            next_week_monday, form, session_profile, thresholds, race_date
        )
        s, e = push_week(client, next_week_monday, plan_next, thresholds,
                         args.replace, args.dry_run, "Semaine suivante")
        total_sent += s; total_errors += e

    print(f"\n{'='*60}")
    print(f"🎯 Total : {total_sent} séances envoyées, {total_errors} erreurs.")
    if total_sent > 0:
        print("👉 Ouvre Intervals.icu → Calendrier pour vérifier, "
              "puis synchronise avec Garmin Connect (Settings → Connections).")


if __name__ == "__main__":
    main()
