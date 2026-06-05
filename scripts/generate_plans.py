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
from session_builder import compute_session

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PLANS_FILE = DATA_DIR / "weekly_plans.json"
PERIODIZATION_FILE = DATA_DIR / "periodization.json"
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
    tss_target_w1: int = 0,
    tss_target_w2: int = 0,
) -> str:
    """Construit le prompt pour la génération des 2 plans théoriques.

    Claude ne calcule PAS les allures/watts ni les durées totales — il fournit
    uniquement des blocs structurés avec des % d'intensité.
    Le calcul réel est fait par session_builder.compute_session().
    """

    race_date_str = profile.get("season", {}).get("race", {}).get("date", "inconnue")
    race_name = profile.get("season", {}).get("race", {}).get("name", "Course objectif")
    weeks_to_race = (date.fromisoformat(race_date_str) - week1_monday).days // 7 if race_date_str != "inconnue" else "?"

    ctx1 = bloc_context(week1_monday, profile)
    ctx2 = bloc_context(week2_monday, profile)
    # TSS cibles IA (depuis periodization.json) ou fallback profil
    tss1 = tss_target_w1 or ctx1["tss_target"]
    tss2 = tss_target_w2 or ctx2["tss_target"]

    # Résumé des activités des 2 dernières semaines
    recent_summary = []
    for a in recent_activities[-14:]:
        d = a.get("start_date_local", "")[:10]
        sport = a.get("type", "?")
        dur = round((a.get("moving_time") or 0) / 60)
        tss = a.get("icu_training_load") or 0
        recent_summary.append(f"  {d} {sport} — {dur}' TSS={tss:.0f}")

    ftp = thresholds.get("ftp_watts", 250)
    thr_run = thresholds.get("threshold_pace_run_str", "4:53/km")
    css = thresholds.get("threshold_pace_swim_str", "2:00/100m")

    w1_days = week_dates(week1_monday)
    w2_days = week_dates(week2_monday)

    prompt = f"""# Contexte athlète

**Athlète** : {profile.get("identity", {}).get("name", "David")}, amateur confirmé
**Objectif** : {race_name} le {race_date_str} ({weeks_to_race} semaines)
**Point fort** : {profile.get("performance_level", {}).get("primary_discipline_strength", "Vélo")}
**Point faible** : {profile.get("performance_level", {}).get("primary_discipline_weakness", "Natation")}

# Références physiologiques (pour contexte seulement — NE PAS calculer les allures)

- FTP vélo : {ftp} W
- Seuil CAP : {thr_run}
- CSS natation : {css}

# Forme du moment (PMC Coggan)

- CTL : {wellness.get("ctl", 0):.1f} | ATL : {wellness.get("atl", 0):.1f} | TSB : {wellness.get("tsb", 0):.1f}

# Activités récentes (2 dernières semaines)

{chr(10).join(recent_summary) if recent_summary else "  Aucune activité récente."}

# Structure hebdomadaire fixe

- Lundi : Repos
- Mardi : Run (séance qualité avec intervalles)
- Mercredi : Swim (endurance + technique)
- Jeudi : VirtualRide (home trainer, séance seuil/sweet spot)
- Vendredi : Strength (renforcement musculaire, sans écha cardio)
- Samedi : Repos
- Dimanche : Sortie longue — semaine ISO {week1_monday.isocalendar()[1]} ({"paire → VirtualRide longue" if week1_monday.isocalendar()[1] % 2 == 0 else "impaire → Run longue"})

# Séances à planifier

## SEMAINE 1 — {week1_monday.isoformat()} au {(week1_monday + timedelta(days=6)).isoformat()}
- Phase : {ctx1["phase"]} | Semaine {ctx1["bloc_week"]}/5 du bloc ({"RÉCUPÉRATION" if ctx1["is_recovery"] else "CHARGE"})
- **TSS CIBLE : {tss1}** — la somme des tss_estimate de tous les jours doit être entre {int(tss1*0.95)} et {int(tss1*1.05)}
- Jours : {[d["date"] + " " + d["weekday_fr"] for d in w1_days]}

## SEMAINE 2 — {week2_monday.isoformat()} au {(week2_monday + timedelta(days=6)).isoformat()}
- Phase : {ctx2["phase"]} | Semaine {ctx2["bloc_week"]}/5 du bloc ({"RÉCUPÉRATION" if ctx2["is_recovery"] else "CHARGE"})
- **TSS CIBLE : {tss2}** — la somme des tss_estimate de tous les jours doit être entre {int(tss2*0.95)} et {int(tss2*1.05)}
- Jours : {[d["date"] + " " + d["weekday_fr"] for d in w2_days]}

Réponds UNIQUEMENT avec ce JSON (sans markdown). Pas d'allures/watts — % intensité uniquement, duration_min/tss_estimate calculés par l'app.

{{"week1":{{"monday":"{week1_monday.isoformat()}","tss_target":{tss1},"bloc_week":{ctx1["bloc_week"]},"is_recovery":{"true" if ctx1["is_recovery"] else "false"},"phase":"{ctx1["phase"]}","coach_note":"...","days":[{{"date":"YYYY-MM-DD","weekday_fr":"...","sport":"Repos|Run|Swim|VirtualRide|Strength","type":"...","rationale":"...","blocks":[{{"type":"endurance|interval|recovery|strength_exercise","duration_min":20,"reps":1,"recovery_min":0,"intensity_pct":75,"zone":"Z2","description":""}}]}}]}},"week2":{{"monday":"{week2_monday.isoformat()}","tss_target":{tss2},"bloc_week":{ctx2["bloc_week"]},"is_recovery":{"true" if ctx2["is_recovery"] else "false"},"phase":"{ctx2["phase"]}","coach_note":"...","days":[...]}}}}

Règles : Repos→blocks=[] | Strength→type="strength_exercise"+description | Swim→inclure warmup/cooldown blocs | Run/Vélo→PAS de warmup/cooldown | Récup→Z1-Z2 uniquement, pas de Z4-Z5 | Progression S1→S2 visible (+1 rep OU +2' OU +10' sortie longue) | TSS hebdo dans ±5% cible | 1 séquence high anaerobic Z5-Z6 (3-6×20-30s, récup 2-3') sur Run mardi OU Vélo jeudi par semaine (pas récup).

NATATION — règle impérative : les blocs natation sont TOUJOURS exprimés en distance (mètres), jamais en durée. Utilise "distance_m" à la place de "duration_min" pour les blocs Swim. Exemples corrects : warmup 400m, intervalles 8×100m ou 6×200m, cooldown 200m. Jamais "5 minutes à telle allure" — en piscine on programme par longueurs."""

    return prompt


def build_periodization_prompt(profile: dict, wellness: dict, week1_monday: date) -> str:
    """Prompt pour ajuster les TSS cibles de la saison selon la forme actuelle."""
    race = profile.get("season", {}).get("race", {})
    race_date_str = race.get("date", "")
    race_name = race.get("name", "Course objectif")
    phases = profile.get("season", {}).get("phases", [])
    base_targets = profile.get("fitness_baseline", {}).get("weekly_tss_targets", {})

    ctl = wellness.get("ctl", 0)
    atl = wellness.get("atl", 0)
    tsb = wellness.get("tsb", 0)

    # Semaines restantes depuis week1_monday
    remaining = {k: v for k, v in sorted(base_targets.items()) if k >= week1_monday.isoformat()}

    phases_txt = "\n".join(
        f"  {ph['name']} ({ph['start']} → {ph['end']}) : TSS cible base {ph['tss_target_weekly']}/sem"
        for ph in phases
    )
    targets_txt = "\n".join(f"  {k}: {v}" for k, v in list(remaining.items()))

    return f"""# Ajustement périodisation saison — {race_name} le {race_date_str}

## Forme actuelle
- CTL : {ctl:.1f} | ATL : {atl:.1f} | TSB : {tsb:+.1f}
- Semaine courante : {week1_monday.isoformat()}

## Phases de la saison (base profil)
{phases_txt}

## TSS cibles hebdomadaires actuels (base profil, semaines restantes)
{targets_txt}

## Ta mission
Ajuste les TSS cibles hebdomadaires pour les semaines restantes en tenant compte de :
1. La forme actuelle (CTL {ctl:.1f}) — si CTL < 40 allège les premières semaines, si CTL > 60 peut progresser plus vite
2. La progression en blocs 4+1 (4 semaines charge + 1 récup à 60-65% du pic) — respecter ce pattern
3. La phase d'affûtage finale (-40% volume) et la semaine de course (très léger)
4. Ne pas dépasser +10 TSS/semaine de progression (Gabbett 2016 — ramp rate)
5. Reste proche des valeurs de base (±15% max) sauf si la forme l'exige vraiment

Réponds UNIQUEMENT avec ce JSON :
{{
  "generated_at": "YYYY-MM-DDTHH:MM:SS",
  "ctl_at_generation": {ctl:.1f},
  "tss_by_week": {{
    "YYYY-MM-DD": 270,
    ...
  }},
  "coach_rationale": "Explication courte des ajustements principaux (2-3 phrases)"
}}

Les clés de tss_by_week sont les lundis ISO des semaines restantes : {list(remaining.keys())}"""


def generate_periodization(profile: dict, wellness: dict, week1_monday: date,
                           dry_run: bool = False) -> dict:
    """Génère/ajuste la périodisation saison via Claude et sauvegarde dans periodization.json."""
    prompt = build_periodization_prompt(profile, wellness, week1_monday)

    if dry_run:
        print("\n" + "="*60)
        print("PROMPT PÉRIODISATION (dry-run) :")
        print(prompt)
        return {}

    print("\n🤖 Appel Claude Sonnet pour ajuster la périodisation saison...")
    try:
        result = call_claude_json(prompt)
    except Exception as e:
        print(f"⚠️  Erreur génération périodisation IA ({e}) — conservation des valeurs profil.")
        # Fallback : utilise les valeurs du profil telles quelles
        base_targets = profile.get("fitness_baseline", {}).get("weekly_tss_targets", {})
        remaining = {k: v for k, v in sorted(base_targets.items())
                     if k >= week1_monday.isoformat()}
        result = {
            "generated_at": week1_monday.isoformat(),
            "ctl_at_generation": wellness.get("ctl", 0),
            "tss_by_week": remaining,
            "coach_rationale": "Valeurs de base du profil athlète (fallback).",
        }

    # Sauvegarde
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PERIODIZATION_FILE.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    print(f"✅ Périodisation sauvegardée dans {PERIODIZATION_FILE}")
    rationale = result.get("coach_rationale", "")
    if rationale:
        print(f"   → {rationale}")
    return result


def get_tss_target(week_monday: date) -> int | None:
    """Lit le TSS cible pour une semaine depuis periodization.json.

    Fallback sur athlete_profile.json si le fichier n'existe pas.
    Retourne None si introuvable.
    """
    monday_str = week_monday.isoformat()

    # 1. periodization.json (prioritaire)
    if PERIODIZATION_FILE.exists():
        try:
            data = json.loads(PERIODIZATION_FILE.read_text())
            tss = data.get("tss_by_week", {}).get(monday_str)
            if tss:
                return int(tss)
        except Exception:
            pass

    # 2. Fallback athlete_profile.json
    if ATHLETE_PROFILE_PATH.exists():
        try:
            profile = json.loads(ATHLETE_PROFILE_PATH.read_text())
            tss = profile.get("fitness_baseline", {}).get(
                "weekly_tss_targets", {}
            ).get(monday_str)
            if tss:
                return int(tss)
        except Exception:
            pass

    return None


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

    # 1. Générer/ajuster la périodisation saison
    periodization = generate_periodization(profile, wellness, week1_monday, dry_run=dry_run)
    tss1 = (periodization.get("tss_by_week", {}).get(week1_monday.isoformat())
            or get_tss_target(week1_monday)
            or bloc_context(week1_monday, profile)["tss_target"])
    tss2 = (periodization.get("tss_by_week", {}).get(week2_monday.isoformat())
            or get_tss_target(week2_monday)
            or bloc_context(week2_monday, profile)["tss_target"])
    print(f"   TSS cibles : semaine 1 = {tss1}, semaine 2 = {tss2}")

    # 2. Construire le prompt des séances avec les TSS cibles IA
    prompt = build_prompt(week1_monday, week2_monday, profile, thresholds, wellness, activities,
                          tss_target_w1=tss1, tss_target_w2=tss2)

    if dry_run:
        print("\n" + "="*60)
        print("PROMPT SÉANCES (dry-run) :")
        print("="*60)
        print(prompt)
        return {}

    # Appel Claude
    print(f"\n🤖 Appel Claude Sonnet pour générer les 2 semaines...")
    result = call_claude_json(prompt)

    # Enrichir avec les métadonnées de dates + calcul algorithmique des séances
    from daily_coach import FR_WEEKDAYS as FR_WD
    wu_profile = profile.get("training_preferences", {}).get("warmup_cooldown", {})

    for week_key, week_monday in [("week1", week1_monday), ("week2", week2_monday)]:
        if week_key not in result:
            continue
        days = result[week_key].get("days", [])
        for i, day in enumerate(days):
            # Dates
            d = week_monday + timedelta(days=i)
            day["date"] = d.isoformat()
            day["weekday"] = d.strftime("%A")
            day["weekday_fr"] = FR_WD.get(d.strftime("%A"), d.strftime("%A"))
            day.setdefault("status", "ideal")
            # Calcul algorithmique : allures, durée totale, TSS, structure
            compute_session(day, thresholds, wu_profile)

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
