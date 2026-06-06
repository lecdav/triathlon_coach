"""push_workouts.py — Envoie le plan hebdomadaire sur Intervals.icu (→ Garmin Connect).

Usage :
    python scripts/push_workouts.py              # semaine en cours
    python scripts/push_workouts.py --dry-run    # affiche les séances sans les envoyer
    python scripts/push_workouts.py --replace    # supprime d'abord les séances existantes

Source du plan : data/today.json (généré par daily_coach.py).
  - weekly_plan     → plan adaptatif de la semaine en cours (tient compte du réalisé)
  - next_week_plan  → plan idéal de la semaine suivante
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

# Import minimal depuis daily_coach (plus de recalcul du plan ici)
from daily_coach import (
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
    """Génère un nom compact sans préfixe sport.

    Exemples :
      3×12' 88-94%FTP (60')
      7×3' VO2max (45')
      10×100m CSS
      Renforcement (45')
      Sortie longue Z2 (90')
    """
    sport     = item.get("sport", "")
    wtype     = item.get("type", "")
    total_min = item.get("duration_min", 0)
    dur       = f"({total_min}')" if total_min else ""

    # ---- GYM / Renforcement ----
    if sport == "Strength":
        return f"Renforcement {dur}".strip()

    # ---- Natation ----
    if sport == "Swim":
        wl = wtype.lower()
        if "technique" in wl or "mixed" in wl:
            return f"Technique + endurance {dur}".strip()
        elif "interval" in wl:
            blocks = item.get("blocks") or []
            intv = next((b for b in blocks if b.get("type") == "interval"), None)
            if intv:
                return f"{intv['reps']}×100m CSS {dur}".strip()
        return f"10×100m CSS {dur}".strip()

    # ---- Brick ----
    if sport == "Brick (Bike+Run)":
        bike_min = max(int(total_min * 0.8), 45)
        run_min  = max(total_min - bike_min, 15)
        return f"Brick {bike_min}'+{run_min}' Z2"

    # ---- CAP ----
    if sport == "Run":
        wl = wtype.lower()
        if "vo2" in wl or "interval" in wl or "intervalles" in wl:
            # Récupère les reps depuis les blocks si dispo
            blocks = item.get("blocks") or []
            intv = next((b for b in blocks if b.get("type") == "interval"), None)
            if intv:
                return f"{intv['reps']}×{intv['duration_min']}' Z{intv.get('zone','5').replace('Z','')} {dur}".strip()
            return f"Intervalles VO2max {dur}".strip()
        elif "seuil" in wl or "tempo" in wl:
            return f"Tempo 95-100%LT {dur}".strip()
        elif "long" in wl:
            return f"Sortie longue Z2 {dur}".strip()
        else:
            return f"Endurance Z2 {dur}".strip()

    # ---- Vélo ----
    if sport in ("VirtualRide", "Ride"):
        wl = wtype.lower()
        if "sweet spot" in wl or "seuil" in wl:
            return f"3×12' Sweet Spot {dur}".strip()
        elif "long" in wl:
            return f"Sortie longue Z2 {dur}".strip()
        elif "interval" in wl or "vo2" in wl:
            blocks = item.get("blocks") or []
            intv = next((b for b in blocks if b.get("type") == "interval"), None)
            if intv:
                return f"{intv['reps']}×{intv['duration_min']}' {intv.get('zone','Z5')} {dur}".strip()
            return f"Intervalles vélo {dur}".strip()
        else:
            return f"Endurance Z2 {dur}".strip()

    # Fallback
    return f"{wtype} {dur}".strip() if wtype else dur.strip()


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


def _pct_to_target(sport: str, pct: int, thresholds: dict) -> str:
    """Convertit intensity_pct d'un block IA → cible Intervals.icu.

    Vélo  : % FTP direct (ex: "88-94%")
    CAP   : allure absolue depuis % seuil (ex: "5:04-5:20 pace")
    Swim  : allure absolue /100m (ex: "1:55-2:05/100m pace")
    Autres: zone HR générique
    """
    if sport in ("VirtualRide", "Ride"):
        lo = max(pct - 3, pct - 3)
        hi = pct + 3
        return f"{lo}-{hi}%"
    elif sport == "Run":
        # pct est la % du seuil — convertir en plage d'allure ±3%
        p = pct / 100.0
        return _run_pace(thresholds, p * 0.97, p * 1.03)
    elif sport == "Swim":
        p = pct / 100.0
        return _swim_pace(thresholds, p * 0.97, p * 1.03)
    elif sport == "Brick (Bike+Run)":
        lo = pct - 3
        hi = pct + 3
        return f"{lo}-{hi}%"
    return f"Z2 HR"


def _blocks_to_steps(item: dict, thresholds: dict) -> list[str]:
    """Convertit les blocks IA d'un item en lignes markdown Intervals.icu."""
    blocks = item.get("blocks", [])
    sport  = item.get("sport", "")
    lines: list[str] = []

    for b in blocks:
        dur   = b.get("duration_min", 0)
        reps  = b.get("reps", 1)
        recov = b.get("recovery_min", 0)
        pct   = b.get("intensity_pct", 65)
        target = _pct_to_target(sport, pct, thresholds)

        if reps > 1:
            lines.append("")
            lines.append(f"{reps}x")
            lines.append(f"- {dur}m {target}")
            if recov > 0:
                recov_pct = _pct_to_target(sport, 50, thresholds)
                lines.append(f"- {recov}m {recov_pct}")
            lines.append("")
        else:
            lines.append(f"- {dur}m {target}")

    return lines


def build_workout_text(item: dict, thresholds: dict) -> str | None:
    """Génère le texte markdown des steps pour Intervals.icu.

    Priorité :
      1. blocks IA (plan adaptatif) → steps précis depuis intensity_pct
      2. Fallback générique depuis sport/type

    Syntaxe Intervals.icu :
      - Xm TARGET       → step simple
      Nx                → répétitions
      - Xm TARGET       → step dans le bloc
    Cibles : "88-94%" pour vélo, "5:04-5:20 pace" pour CAP, "1:55-2:05/100m pace" pour nata.
    """
    sport        = item.get("sport", "")
    wtype        = item.get("type", "")
    duration_min = item.get("duration_min", 0)

    if not sport or sport in ("Repos", "Strength") or duration_min == 0:
        return None

    # --- Priorité 1 : blocks IA ---
    if item.get("blocks"):
        lines = _blocks_to_steps(item, thresholds)
        # Nettoyer les doubles lignes vides en début/fin
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        if lines:
            return "\n".join(lines)

    # --- Fallback générique (plan théorique sans blocks) ---
    lines = []

    if sport == "Run":
        pace_z1    = _run_pace(thresholds, 0.80, 0.87)
        pace_z2    = _run_pace(thresholds, 0.83, 0.90)
        pace_seuil = _run_pace(thresholds, 0.97, 1.03)
        pace_vo2   = _run_pace(thresholds, 1.03, 1.08)

        if "VO2max" in wtype or "interval" in wtype.lower():
            lines = [
                f"- 10m {pace_z2}", f"- 5m {pace_z1}", f"- 5m {pace_z2}",
                "", "7x", f"- 3m {pace_vo2}", f"- 2m {pace_z1}", "",
                f"- 5m {pace_z1}",
            ]
        elif "Seuil" in wtype or "tempo" in wtype.lower():
            bloc = max(duration_min - 25, 20)
            lines = [
                f"- 10m {pace_z2}", f"- 5m {pace_z1}", f"- 5m {pace_z2}",
                "", f"- {bloc}m {pace_seuil}", "",
                f"- 5m {pace_z1}",
            ]
        elif "long" in wtype.lower():
            bloc = max(duration_min - 25, 30)
            lines = [
                f"- 10m {pace_z1}", f"- 5m {pace_z1}", f"- 5m {pace_z2}",
                "", f"- {bloc}m {pace_z2}", "",
                f"- 5m {pace_z1}",
            ]
        else:
            bloc = max(duration_min - 20, 20)
            lines = [f"- 10m {pace_z1}", "", f"- {bloc}m {pace_z2}", "", f"- 5m {pace_z1}"]

    elif sport in ("VirtualRide", "Ride"):
        if "sweet spot" in wtype.lower() or "Seuil" in wtype:
            lines = [
                "- 15m 50-65%", "",
                "3x", "- 12m 88-94%", "- 4m 50-60%", "",
                "- 5m 50-60%",
            ]
        elif "long" in wtype.lower():
            bloc = max(duration_min - 20, 60)
            lines = ["- 15m 50-65%", "", f"- {bloc}m 56-75%", "", "- 5m 50-60%"]
        else:
            bloc = max(duration_min - 20, 30)
            lines = ["- 15m 50-65%", "", f"- {bloc}m 56-75%", "", "- 5m 50-60%"]

    elif sport == "Swim":
        swim_easy     = _swim_pace(thresholds, 0.80, 0.88)
        swim_interval = _swim_pace(thresholds, 1.02, 1.08)
        lines = [
            f"- 400mtr {swim_easy}", "",
            "10x", f"- 100mtr {swim_interval}", "- 20s", "",
            f"- 200mtr {swim_easy}",
        ]

    elif sport == "Brick (Bike+Run)":
        pace_z2  = _run_pace(thresholds, 0.83, 0.90)
        bike_min = max(int(duration_min * 0.8), 45)
        run_min  = max(duration_min - bike_min, 15)
        lines = ["- 10m 50-65%", "", f"- {bike_min - 10}m 56-75%", "", f"- {run_min}m {pace_z2}"]

    return "\n".join(lines) if lines else None


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
        # Ne pusher que les séances à venir (pas celles déjà réalisées)
        if item.get("status") in ("done", "done_weekly", "exact"):
            continue
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

    # -----------------------------------------------------------------------
    # Lecture du plan depuis data/today.json (généré par daily_coach.py)
    # -----------------------------------------------------------------------
    today_json_path = ROOT / "data" / "today.json"
    if not today_json_path.exists():
        print("❌ data/today.json introuvable — lance d'abord daily_coach.py")
        sys.exit(1)

    coach_data = json.loads(today_json_path.read_text())

    # Vérifier que le json est bien de la semaine courante
    stored_monday = coach_data.get("week_monday")
    if stored_monday and stored_monday != week_monday.isoformat():
        print(f"⚠️  today.json est daté de la semaine du {stored_monday}, "
              f"pas de la semaine demandée ({week_monday.isoformat()}).")
        print("   Lance daily_coach.py pour regénérer, ou utilise --week pour cibler une semaine précise.")

    plan_cur = coach_data.get("weekly_plan", [])
    plan_next = coach_data.get("next_week_plan", [])
    thresholds = coach_data.get("thresholds", {})
    phase = coach_data.get("phase", "—")
    weeks_to_race = coach_data.get("weeks_to_race")
    ia_source = coach_data.get("ia_adaptive_source", "—")

    print(f"🏋️  Phase : {phase}  |  Source plan adaptatif : {ia_source}")
    if weeks_to_race is not None:
        print(f"⏱️  Semaines avant course : {weeks_to_race}")

    if not plan_cur:
        print("❌ weekly_plan vide dans today.json — relance daily_coach.py")
        sys.exit(1)

    if args.dry_run:
        print("\n🔍 Mode dry-run activé — aucun envoi ne sera effectué.")

    total_sent, total_errors = 0, 0

    s, e = push_week(client, week_monday, plan_cur, thresholds,
                     args.replace, args.dry_run, "Semaine en cours (plan adaptatif)")
    total_sent += s; total_errors += e

    if not args.only_current:
        if not plan_next:
            print("⚠️  next_week_plan absent de today.json — semaine suivante ignorée.")
        else:
            s, e = push_week(client, next_week_monday, plan_next, thresholds,
                             args.replace, args.dry_run, "Semaine suivante (plan idéal)")
            total_sent += s; total_errors += e

    print(f"\n{'='*60}")
    print(f"🎯 Total : {total_sent} séances envoyées, {total_errors} erreurs.")
    if total_sent > 0:
        print("👉 Ouvre Intervals.icu → Calendrier pour vérifier, "
              "puis synchronise avec Garmin Connect (Settings → Connections).")


if __name__ == "__main__":
    main()
