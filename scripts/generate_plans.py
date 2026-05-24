"""generate_plans.py — Génère les plans théoriques des 2 semaines à venir via Claude IA.

À exécuter le dimanche soir (ou manuellement à tout moment).
Stocke le résultat dans data/weekly_plans.json, lu ensuite par daily_coach.py.

Usage :
    python scripts/generate_plans.py              # génère à partir d'aujourd'hui
    python scripts/generate_plans.py --dry-run    # affiche le prompt sans appeler l'API
    python scripts/generate_plans.py --force      # force la régénération même si déjà fait cette semaine
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intervals_client import IntervalsClient
from claude_client import call_claude_json, SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PLANS_FILE = DATA_DIR / "weekly_plans.json"
ATHLETE_PROFILE_PATH = DATA_DIR / "athlete_profile.json"

FR_WEEKDAYS = {
    "Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi",
    "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi",
    "Sunday": "Dimanche",
}


def week_dates(monday: date) -> list[dict]:
    """Retourne les 7 jours de la semaine avec leurs métadonnées."""
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        days.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "weekday_fr": FR_WEEKDAYS[d.strftime("%A")],
        })
    return days


def bloc_context(monday: date, profile: dict) -> dict:
    """Calcule la semaine dans le bloc et la phase à partir du profil."""
    targets: dict = profile.get("fitness_baseline", {}).get("weekly_tss_targets", {})
    phases: list = profile.get("season", {}).get("phases", [])

    # Numéro de semaine dans le bloc 4+1
    # On cherche la position de ce lundi dans la séquence de targets
    sorted_mondays = sorted(targets.keys())
    try:
        idx = sorted_mondays.index(monday.isoformat())
    except ValueError:
        idx = 0

    # Dans un cycle 4+1, la semaine de récup est à l'index 4 (0-based dans chaque groupe)
    bloc_position = (idx % 5) + 1  # 1..5
    is_recovery = (bloc_position == 5)

    # TSS cible de cette semaine et des voisines
    tss_this = targets.get(monday.isoformat(), 0)
    prev_monday = (monday - timedelta(days=7)).isoformat()
    next_monday = (monday + timedelta(days=7)).isoformat()
    tss_prev = targets.get(prev_monday, 0)
    tss_next = targets.get(next_monday, 0)

    # Phase courante
    phase_name = "Préparation générale"
    for ph in phases:
        if ph.get("start", "") <= monday.isoformat() <= ph.get("end", ""):
            phase_name = ph["name"]
            break

    return {
        "bloc_week": bloc_position,
        "is_recovery": is_recovery,
        "phase": phase_name,
        "tss_target": tss_this,
        "tss_prev_week": tss_prev,
        "tss_next_week": tss_next,
    }


def build_prompt(
    week1_monday: date,
    week2_monday: date,
    profile: dict,
    thresholds: dict,
    wellness: dict,
    recent_activities: list[dict],
) -> str:
    """Construit le prompt utilisateur pour la génération des 2 plans théoriques."""

    race_date_str = profile.get("season", {}).get("race", {}).get("date", "inconnue")
    race_name = profile.get("season", {}).get("race", {}).get("name", "Course objectif")
    weeks_to_race = (date.fromisoformat(race_date_str) - week1_monday).days // 7 if race_date_str != "inconnue" else "?"

    ctx1 = bloc_context(week1_monday, profile)
    ctx2 = bloc_context(week2_monday, profile)

    # Résumé des activités des 2 dernières semaines
    recent_summary = []
    for a in recent_activities[-14:]:
        d = a.get("start_date_local", "")[:10]
        sport = a.get("type", "?")
        dur = round((a.get("moving_time") or 0) / 60)
        dist = round((a.get("distance") or 0) / 1000, 1)
        tss = a.get("icu_training_load") or 0
        name = a.get("name", "")
        recent_summary.append(f"  {d} {sport} — {dur}' {dist}km TSS={tss:.0f} ({name})")

    # Seuils
    ftp = thresholds.get("ftp_watts", 250)
    thr_run = thresholds.get("threshold_pace_run_str", "4:53/km")
    thr_run_mps = thresholds.get("threshold_pace_run_mps", 3.41)
    css = thresholds.get("threshold_pace_swim_str", "2:00/100m")

    # Zones vélo
    def z(pct): return round(ftp * pct)
    zones_bike = f"Z1<{z(0.55)}W Z2={z(0.55)}-{z(0.75)}W Z3={z(0.75)}-{z(0.90)}W Z4={z(0.90)}-{z(1.05)}W Z5>{z(1.05)}W"

    # Zones CAP (allures en min/km)
    def pace_str(mps_factor):
        if not thr_run_mps: return "—"
        mps = thr_run_mps * mps_factor
        total_s = round(60 / mps * 10) * 6  # arrondi 10s
        return f"{total_s // 60}:{total_s % 60:02d}/km"

    zones_run = (f"Z1<{pace_str(0.77)} Z2={pace_str(0.88)}-{pace_str(0.94)} "
                 f"Z3={pace_str(0.94)}-{pace_str(1.00)} Z4={pace_str(1.00)}-{pace_str(1.06)} "
                 f"Seuil={thr_run}")

    # Préférences warmup/cooldown
    wu = profile.get("training_preferences", {}).get("warmup_cooldown", {})

    w1_days = week_dates(week1_monday)
    w2_days = week_dates(week2_monday)

    prompt = f"""# Contexte athlète

**Athlète** : {profile.get("identity", {}).get("name", "David")}, {profile.get("identity", {}).get("weight_kg", 78)} kg, amateur confirmé
**Objectif** : {race_name} le {race_date_str} ({weeks_to_race} semaines)
**Point fort** : {profile.get("performance_level", {}).get("primary_discipline_strength", "Vélo")}
**Point faible** : {profile.get("performance_level", {}).get("primary_discipline_weakness", "Natation")}
**Méthodologie** : {profile.get("weekly_structure", {}).get("methodology", "Polarisé 80/20")}

# Seuils physiologiques

- FTP vélo : {ftp} W | {zones_bike}
- Seuil CAP : {thr_run} | {zones_run}
- CSS natation : {css}
- FC repos : {thresholds.get("resting_hr_bpm", 51)} bpm | FCmax : {thresholds.get("hr_max_bpm", 190)} bpm
- LTHR vélo : {thresholds.get("lthr_bpm", 168)} bpm

# Forme du moment (PMC Coggan)

- CTL (forme chronique) : {wellness.get("ctl", 0):.1f}
- ATL (fatigue aiguë)   : {wellness.get("atl", 0):.1f}
- TSB (forme nette)     : {wellness.get("tsb", 0):.1f}

# Activités récentes (2 dernières semaines)

{chr(10).join(recent_summary) if recent_summary else "  Aucune activité récente."}

# Structure hebdomadaire fixe

- Lundi : repos
- Mardi : CAP (séance qualité)
- Mercredi : Natation (endurance + technique)
- Jeudi : Vélo home trainer (séance seuil/sweet spot)
- Vendredi : Renforcement musculaire (pas d'écha cardio)
- Samedi : repos
- Dimanche : sortie longue alternée — semaines ISO paires = vélo longue, impaires = CAP longue (ou Brick à partir de la phase spécifique)

# Échauffements / retours au calme OBLIGATOIRES

- CAP : {wu.get("warmup_run_content", "20' écha avec gammes")} | {wu.get("cooldown_run_min", 5)}' retour au calme trot Z1
- Vélo : {wu.get("warmup_bike_min", 15)}' écha progressif Z1→Z2 + 3×30s à 100rpm | {wu.get("cooldown_bike_min", 5)}' retour au calme Z1
- Natation : {wu.get("warmup_swim_m", 400)}m écha nage souple | {wu.get("cooldown_swim_m", 200)}m retour au calme
- Renforcement : AUCUN échauffement cardio

# Séances à générer

## SEMAINE 1 — du {week1_monday.isoformat()} au {(week1_monday + timedelta(days=6)).isoformat()}

- Phase : {ctx1["phase"]}
- Semaine {ctx1["bloc_week"]}/5 du bloc ({"RÉCUPÉRATION — intensité réduite, pas d'intervalles intenses" if ctx1["is_recovery"] else "CHARGE"})
- TSS cible : {ctx1["tss_target"]} (semaine précédente : {ctx1["tss_prev_week"]}, semaine suivante cible : {ctx1["tss_next_week"]})
- Semaine ISO : {week1_monday.isocalendar()[1]} ({"paire → dimanche VÉLO longue" if week1_monday.isocalendar()[1] % 2 == 0 else "impaire → dimanche CAP longue"})

Jours : {[d["date"] + " " + d["weekday_fr"] for d in w1_days]}

## SEMAINE 2 — du {week2_monday.isoformat()} au {(week2_monday + timedelta(days=6)).isoformat()}

- Phase : {ctx2["phase"]}
- Semaine {ctx2["bloc_week"]}/5 du bloc ({"RÉCUPÉRATION — intensité réduite, pas d'intervalles intenses" if ctx2["is_recovery"] else "CHARGE"})
- TSS cible : {ctx2["tss_target"]} (semaine précédente : {ctx2["tss_prev_week"]}, semaine suivante cible : {ctx2["tss_next_week"]})
- Semaine ISO : {week2_monday.isocalendar()[1]} ({"paire → dimanche VÉLO longue" if week2_monday.isocalendar()[1] % 2 == 0 else "impaire → dimanche CAP longue"})

Jours : {[d["date"] + " " + d["weekday_fr"] for d in w2_days]}

# Format de réponse

Réponds UNIQUEMENT avec ce JSON (sans markdown, sans texte) :

{{
  "generated_at": "YYYY-MM-DDTHH:MM:SS",
  "week1": {{
    "monday": "{week1_monday.isoformat()}",
    "sunday": "{(week1_monday + timedelta(days=6)).isoformat()}",
    "tss_target": {ctx1["tss_target"]},
    "bloc_week": {ctx1["bloc_week"]},
    "is_recovery": {"true" if ctx1["is_recovery"] else "false"},
    "phase": "{ctx1["phase"]}",
    "coach_note": "Note courte du coach sur l'objectif de la semaine",
    "days": [
      {{
        "date": "YYYY-MM-DD",
        "weekday": "Monday",
        "weekday_fr": "Lundi",
        "sport": "Repos|Run|Swim|VirtualRide|Ride|Strength|Brick (Bike+Run)",
        "type": "Nom court du type de séance",
        "duration_min": 0,
        "structure": "Description détaillée de la séance avec allures/watts précis",
        "zones": "Zones cibles",
        "rationale": "Justification scientifique courte",
        "tss_estimate": 0
      }}
    ]
  }},
  "week2": {{ ... même structure ... }}
}}

RÈGLES IMPORTANTES :
1. Les durées incluent l'échauffement et le retour au calme
2. Les structures doivent mentionner les allures/watts précis avec les zones vélo ({zones_bike}) et CAP ({zones_run})
3. La progression entre semaine 1 et 2 doit être visible (durée intervalles, nombre répétitions, durée sortie longue)
4. Repos et Renforcement musculaire : tss_estimate = 0
5. La natation du mercredi : 1 séance/semaine uniquement, ~50 min
6. Le renforcement du vendredi : exercices fonctionnels triathlon (gainage, squat unipodal, fentes, mollets, proprioception)
7. Adapte le volume des séances au TSS cible hebdomadaire
8. Si semaine de récupération : supprimer les intervalles intenses, réduire les durées, garder Z2 uniquement"""

    return prompt


def generate_plans(dry_run: bool = False, force: bool = False) -> dict:
    """Génère les plans théoriques des 2 semaines à venir et les stocke."""
    today = date.today()
    # Semaine 1 = semaine prochaine (lundi prochain)
    days_to_next_monday = (7 - today.weekday()) % 7 or 7
    week1_monday = today + timedelta(days=days_to_next_monday)
    week2_monday = week1_monday + timedelta(days=7)

    print(f"📅 Génération des plans IA pour :")
    print(f"   Semaine 1 : {week1_monday} → {week1_monday + timedelta(days=6)}")
    print(f"   Semaine 2 : {week2_monday} → {week2_monday + timedelta(days=6)}")

    # Vérifier si déjà généré cette semaine (sauf --force)
    if not force and PLANS_FILE.exists():
        existing = json.loads(PLANS_FILE.read_text())
        if existing.get("week1", {}).get("monday") == week1_monday.isoformat():
            print("✅ Plans déjà générés pour ces semaines. Utilise --force pour régénérer.")
            return existing

    # Charger le profil athlète
    if not ATHLETE_PROFILE_PATH.exists():
        raise FileNotFoundError(f"Profil athlète introuvable : {ATHLETE_PROFILE_PATH}")
    profile = json.loads(ATHLETE_PROFILE_PATH.read_text())

    # Données Intervals.icu
    client = IntervalsClient()
    print(f"🔗 Connexion Intervals.icu OK — athlète {client.athlete_id}")

    thresholds = client.get_thresholds()
    activities = client.activities(today - timedelta(days=21))
    wellness_data = client.wellness(today - timedelta(days=7))

    # Wellness d'aujourd'hui
    today_w = next(
        (w for w in wellness_data if w.get("id") == today.isoformat()),
        wellness_data[-1] if wellness_data else {}
    )
    ctl = today_w.get("ctl") or 0
    atl = today_w.get("atl") or 0
    wellness = {"ctl": ctl, "atl": atl, "tsb": ctl - atl}

    # Construire le prompt
    prompt = build_prompt(week1_monday, week2_monday, profile, thresholds, wellness, activities)

    if dry_run:
        print("\n" + "="*60)
        print("PROMPT (dry-run) :")
        print("="*60)
        print(prompt)
        return {}

    # Appel Claude
    print(f"\n🤖 Appel Claude Sonnet pour générer les 2 semaines...")
    result = call_claude_json(prompt)

    # Enrichir avec les métadonnées de dates complètes (par sécurité)
    from daily_coach import FR_WEEKDAYS as FR_WD
    for week_key, week_monday in [("week1", week1_monday), ("week2", week2_monday)]:
        if week_key not in result:
            continue
        days = result[week_key].get("days", [])
        for i, day in enumerate(days):
            d = week_monday + timedelta(days=i)
            day["date"] = d.isoformat()
            day["weekday"] = d.strftime("%A")
            day["weekday_fr"] = FR_WD.get(d.strftime("%A"), d.strftime("%A"))
            day.setdefault("status", "ideal")
            day.setdefault("tss_estimate", 0)

    # Ajouter les totaux
    for week_key in ["week1", "week2"]:
        if week_key not in result:
            continue
        days = result[week_key].get("days", [])
        total_min = sum(d.get("duration_min", 0) for d in days)
        total_tss = sum(d.get("tss_estimate", 0) for d in days)
        result[week_key]["total_minutes"] = total_min
        result[week_key]["total_minutes_str"] = f"{total_min // 60}h{total_min % 60:02d}" if total_min >= 60 else f"{total_min}'"
        result[week_key]["total_tss"] = total_tss

    # Sauvegarder
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLANS_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"✅ Plans sauvegardés dans {PLANS_FILE}")

    # Afficher le résumé
    for week_key, label in [("week1", "Semaine 1"), ("week2", "Semaine 2")]:
        w = result.get(week_key, {})
        print(f"\n📋 {label} ({w.get('monday', '?')}) — TSS cible {w.get('tss_target', '?')} :")
        for day in w.get("days", []):
            sport = day.get("sport", "?")
            typ = day.get("type", "")
            dur = day.get("duration_min", 0)
            tss = day.get("tss_estimate", 0)
            if sport != "Repos":
                print(f"  {day.get('weekday_fr', '?'):>8} : {sport} — {typ} ({dur}' / TSS {tss})")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Génère les plans théoriques IA des 2 prochaines semaines")
    parser.add_argument("--dry-run", action="store_true", help="Affiche le prompt sans appeler l'API")
    parser.add_argument("--force", action="store_true", help="Force la régénération même si déjà fait")
    args = parser.parse_args()

    generate_plans(dry_run=args.dry_run, force=args.force)
