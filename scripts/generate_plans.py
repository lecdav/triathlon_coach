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
- TSS cible : {ctx1["tss_target"]} (sem. précédente : {ctx1["tss_prev_week"]}, sem. suivante : {ctx1["tss_next_week"]})
- Jours : {[d["date"] + " " + d["weekday_fr"] for d in w1_days]}

## SEMAINE 2 — {week2_monday.isoformat()} au {(week2_monday + timedelta(days=6)).isoformat()}
- Phase : {ctx2["phase"]} | Semaine {ctx2["bloc_week"]}/5 du bloc ({"RÉCUPÉRATION" if ctx2["is_recovery"] else "CHARGE"})
- TSS cible : {ctx2["tss_target"]} (sem. précédente : {ctx2["tss_prev_week"]}, sem. suivante : {ctx2["tss_next_week"]})
- Jours : {[d["date"] + " " + d["weekday_fr"] for d in w2_days]}

# Format de réponse

Réponds UNIQUEMENT avec ce JSON (sans markdown, sans texte avant/après).

IMPORTANT : Ne calcule PAS les allures ni les watts — fournis uniquement des % d'intensité.
Les champs duration_min et tss_estimate seront calculés par l'application à partir des blocs.

{{
  "week1": {{
    "monday": "{week1_monday.isoformat()}",
    "tss_target": {ctx1["tss_target"]},
    "bloc_week": {ctx1["bloc_week"]},
    "is_recovery": {"true" if ctx1["is_recovery"] else "false"},
    "phase": "{ctx1["phase"]}",
    "coach_note": "Objectif de la semaine en 1-2 phrases",
    "days": [
      {{
        "date": "YYYY-MM-DD",
        "weekday_fr": "Lundi",
        "sport": "Repos|Run|Swim|VirtualRide|Strength",
        "type": "Nom court (ex: Intervalles Z4, Sweet spot, Endurance fondamentale)",
        "rationale": "Justification scientifique courte (1 phrase)",
        "blocks": [
          {{
            "type": "endurance|interval|recovery|strength_exercise",
            "duration_min": 20,
            "reps": 1,
            "recovery_min": 0,
            "intensity_pct": 75,
            "zone": "Z2",
            "description": "Texte libre pour renfo/gammes/exercices spéciaux"
          }}
        ]
      }}
    ]
  }},
  "week2": {{ "monday": "{week2_monday.isoformat()}", "tss_target": {ctx2["tss_target"]}, "bloc_week": {ctx2["bloc_week"]}, "is_recovery": {"true" if ctx2["is_recovery"] else "false"}, "phase": "{ctx2["phase"]}", "coach_note": "...", "days": [...] }}
}}

RÈGLES :
1. Pour Repos : blocks = [] (liste vide)
2. Pour Strength : blocks = liste d'exercices avec type="strength_exercise" et description (ex: "3×15 squats unipodaux", "4×30s gainage")
3. Pour Swim : inclure blocs warmup (400m), corps, cooldown (200m) — utiliser duration_min en équivalent temps
4. Pour Run/VirtualRide : NE PAS inclure warmup/cooldown dans les blocs — ils sont ajoutés automatiquement
5. La progression semaine 1→2 doit être visible : +1 répétition OU +2' par intervalle OU +10' sur la sortie longue
6. Si semaine de récupération : pas d'intervalles (pas de Z4/Z5), blocs endurance Z1-Z2 uniquement, durées réduites
7. Adapte le nombre/durée des blocs pour atteindre le TSS cible hebdomadaire
8. HIGH ANAEROBIC (Z5-Z6) : intègre 1 séquence d'efforts courts très intenses par semaine, sur la séance Run du mardi OU vélo du jeudi (pas les deux). Format : 3 à 6 répétitions de 20–30 secondes à 110–120% FTP (vélo) ou 105–110% seuil (run), récup complète 2–3'. Ces efforts stimulent le système anaérobie lactique et maintiennent les adaptations neuromusculaires (Laursen & Jenkins 2002). Ne pas ajouter si semaine de récupération."""

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
