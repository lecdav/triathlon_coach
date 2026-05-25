"""Coach quotidien triathlon — analyse + plan hebdomadaire.

Lit les données Intervals.icu (wellness, activités, profil, calendrier),
calcule la forme du jour (CTL/ATL/TSB, ramp rate, HRV, sommeil),
puis produit :
  1) un rapport Markdown détaillé dans reports/daily/YYYY-MM-DD.md
  2) un snapshot JSON dans data/cache/today.json (alimente l'artifact)

Méthodologie :
  - Polarisée 80/20 (Seiler) : ~80% Z1-Z2 endurance, ~20% Z4-Z5 haute intensité,
    peu/pas de zone tempo (Z3) sauf en bloc spécifique pré-compétition.
  - Forme = TSB (Coggan PMC) : entraînement prioritaire si TSB ≥ 0,
    récup obligatoire si TSB < -15, modulation au milieu.
  - Volume calibré sur la distribution réelle des 4 dernières semaines de l'athlète.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Permet d'importer le client quel que soit le cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))
from intervals_client import IntervalsClient, pace_mps_to_minkm, pace_mps_to_per100m
from session_builder import compute_session

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "daily"
CACHE_DIR = ROOT / "data" / "cache"
PROFILE_PATH = ROOT / "config" / "athlete_profile.yaml"
ATHLETE_PROFILE_PATH = ROOT / "data" / "athlete_profile.json"
WEEKLY_PLANS_PATH = ROOT / "data" / "weekly_plans.json"
# data/today.json est la seule sortie versionnée — chargée par index.html via fetch()

REPORT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Fichier de données public (chargé par index.html via fetch)
DATA_DIR = ROOT / "data"
TODAY_JSON_PUBLIC = DATA_DIR / "today.json"


def build_github_pages_dashboard(snapshot: dict) -> Path | None:
    """Écrit data/today.json — chargé par index.html via fetch() au runtime.

    index.html est désormais statique (code uniquement, pas de données embarquées).
    GitHub Actions pousse uniquement data/today.json chaque matin.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TODAY_JSON_PUBLIC.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8"
    )
    return TODAY_JSON_PUBLIC


def git_push_dashboard() -> bool:
    """Push désactivé — maintenant géré par GitHub Actions (.github/workflows/daily_coach.yml).

    Le workflow CI commit et pousse index.html automatiquement après chaque run.
    Cette fonction est conservée pour rétrocompatibilité mais ne fait plus rien.
    """
    print("ℹ️  git push local désactivé — GitHub Actions s'en charge.")
    return True




# ---------- Helpers présentation ----------

FR_WEEKDAYS = {
    "Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi",
    "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi",
    "Sunday": "Dimanche",
}

SPORT_INFO = {
    "Run": ("🏃", "CAP"),
    "VirtualRide": ("🚴", "Vélo HT"),
    "Ride": ("🚴", "Vélo"),
    "Swim": ("🏊", "Natation"),
    "Brick (Bike+Run)": ("🚴+🏃", "Brick"),
    "Strength": ("💪", "Muscu"),
    "Repos": ("💤", "Repos"),
}

# Intensity Factor par type de séance (ordre = priorité de matching)
# TSS = (durée_h) * IF² * 100   (Coggan)
TSS_IF_BY_TYPE = [
    ("récupération", 0.50),
    ("vo2max", 0.85),
    ("sweet spot", 0.88),
    ("seuil", 0.90),
    ("tempo", 0.82),
    ("spécifique triathlon", 0.78),
    ("sortie longue", 0.72),
    ("endurance + technique", 0.68),
    ("endurance souple", 0.65),
    ("endurance", 0.70),
    ("technique + tolérance", 0.62),
    ("technique", 0.55),
]


def estimate_tss(sport: str, type_str: str, duration_min: int) -> int:
    """Estime le TSS d'une séance via durée × IF² × 100 (Coggan).

    L'IF est inféré du libellé de la séance (VO2max, sweet spot, etc.).
    Pour la natation et le vélo HT, le calcul reste équivalent (sTSS / TSS).
    """
    if sport in ("Repos", "Strength") or duration_min <= 0:
        return 0
    type_lower = (type_str or "").lower()
    if_val = 0.70  # défaut endurance
    for key, v in TSS_IF_BY_TYPE:
        if key in type_lower:
            if_val = v
            break
    return round((duration_min / 60) * (if_val ** 2) * 100)


def format_seance(item: dict) -> str:
    """Libellé compact pour la colonne 'Séance' : emoji + discipline + intent."""
    sport = item.get("sport") or ""
    type_str = item.get("type", "") or ""
    if sport == "Repos":
        return "💤 Repos total"
    emoji, sport_fr = SPORT_INFO.get(sport, ("", sport))
    # Simplifie : on garde la partie avant le tiret (s'il y en a un)
    type_simple = type_str.split("—")[0].strip()
    if not type_simple:
        return f"{emoji} {sport_fr}".strip()
    return f"{emoji} {sport_fr} {type_simple.lower()}".strip()


def format_duration_total(total_min: int) -> str:
    """'5h40' — durée arrondie aux 5 min les plus proches."""
    if total_min <= 0:
        return "0h00"
    rounded = round(total_min / 5) * 5
    h, m = divmod(rounded, 60)
    return f"~{h}h{m:02d}"


# ---------- Forme du jour ----------

def classify_form(tsb: float) -> dict:
    """État de forme à partir du TSB (Training Stress Balance, Coggan PMC).

    Refs : Coggan & Allen, "Training and Racing with a Power Meter".
    """
    if tsb < -20:
        return {"label": "Fatigue aigüe", "code": "FATIGUE_AIGUE",
                "color": "red", "emoji": "🟥",
                "guidance": "Récupération obligatoire — risque de surentraînement."}
    if tsb < -10:
        return {"label": "Fatigué", "code": "FATIGUE",
                "color": "orange", "emoji": "🟧",
                "guidance": "Volume seulement, pas d'intensité aujourd'hui."}
    if tsb < 5:
        return {"label": "Neutre / productif", "code": "NEUTRE",
                "color": "green", "emoji": "🟩",
                "guidance": "Bonne fenêtre pour entraînement de qualité (seuil/endurance)."}
    if tsb < 15:
        return {"label": "Frais", "code": "FRAIS",
                "color": "green", "emoji": "🟩",
                "guidance": "Excellente fenêtre — séance clé recommandée (VO2max ou seuil)."}
    return {"label": "Trop frais / détraîné", "code": "DETRAINE",
            "color": "orange", "emoji": "🟦",
            "guidance": "Vous décrochez la charge — augmentez progressivement."}


def hrv_status(hrv_recent: list[float]) -> str | None:
    """Tendance HRV simple : compare aujourd'hui vs moyenne 7j."""
    if not hrv_recent or hrv_recent[-1] is None:
        return None
    today = hrv_recent[-1]
    history = [v for v in hrv_recent[:-1] if v is not None]
    if len(history) < 4:
        return None
    avg = sum(history) / len(history)
    if today < 0.92 * avg:
        return "HRV nettement basse (-{:.0f}%) — signal de fatigue parasympathique.".format(
            (1 - today / avg) * 100)
    if today > 1.08 * avg:
        return "HRV élevée (+{:.0f}%) — bonne récup, profitez-en.".format(
            (today / avg - 1) * 100)
    return "HRV stable autour de la moyenne 7j ({:.0f} ms).".format(avg)


# ---------- Analyse charges ----------

def weekly_load(activities: list[dict], days: int) -> dict:
    """Charge totale et distribution par sport sur les N derniers jours."""
    out = {"total_load": 0, "total_minutes": 0, "by_sport": defaultdict(
        lambda: {"count": 0, "minutes": 0, "load": 0, "distance_km": 0}
    )}
    cutoff = date.today() - timedelta(days=days)
    for a in activities:
        d_str = a.get("start_date_local", "")[:10]
        if not d_str:
            continue
        try:
            d = datetime.fromisoformat(d_str).date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        sport = a.get("type", "Other")
        load = a.get("icu_training_load") or 0
        minutes = (a.get("moving_time") or 0) / 60
        dist_km = (a.get("distance") or 0) / 1000
        out["total_load"] += load
        out["total_minutes"] += minutes
        out["by_sport"][sport]["count"] += 1
        out["by_sport"][sport]["minutes"] += minutes
        out["by_sport"][sport]["load"] += load
        out["by_sport"][sport]["distance_km"] += dist_km
    out["by_sport"] = dict(out["by_sport"])
    return out


def average_session_profile(activities: list[dict], days: int = 28) -> dict:
    """Profil moyen d'une séance par discipline (volume/durée habituels)."""
    cutoff = date.today() - timedelta(days=days)
    grouped = defaultdict(list)
    for a in activities:
        d_str = a.get("start_date_local", "")[:10]
        if not d_str:
            continue
        try:
            d = datetime.fromisoformat(d_str).date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        grouped[a.get("type", "Other")].append(a)
    out = {}
    for sport, lst in grouped.items():
        if not lst:
            continue
        durs = [(a.get("moving_time") or 0) / 60 for a in lst]
        dists = [(a.get("distance") or 0) / 1000 for a in lst]
        loads = [a.get("icu_training_load") or 0 for a in lst]
        out[sport] = {
            "count_4w": len(lst),
            "per_week": round(len(lst) / (days / 7), 1),
            "avg_minutes": round(sum(durs) / len(durs)),
            "avg_distance_km": round(sum(dists) / len(dists), 1),
            "avg_load": round(sum(loads) / len(loads)),
        }
    return out


# ---------- Plan hebdomadaire ----------

def build_weekly_plan(today: date, form: dict, profile: dict, thresholds: dict,
                      race_date: date | None) -> list[dict]:
    """Construit le plan de la semaine ISO courante (lundi→dimanche).

    Logique :
      - Lundi = repos (selon athlete_profile)
      - Samedi = repos
      - Dimanche = sortie longue vélo ou CAP (alternée par semaine ISO)
      - Max 5 séances/sem dont 1 séance renforcement musculaire (vendredi)
      - 80% endurance Z1-Z2, 20% intensité (1 séance VO2/seuil + 1 seuil vélo)
      - Volume calé sur les moyennes 4 semaines de l'athlète
    """
    avg_run = profile.get("Run", {"avg_minutes": 35, "avg_distance_km": 6})
    avg_bike = profile.get("VirtualRide", profile.get("Ride", {"avg_minutes": 60, "avg_distance_km": 30}))
    avg_swim = profile.get("Swim", {"avg_minutes": 40, "avg_distance_km": 1.7})

    weeks_to_race = None
    phase = "Préparation générale"
    if race_date:
        weeks_to_race = (race_date - today).days // 7
        if weeks_to_race <= 1:
            phase = "Affûtage / course"
        elif weeks_to_race <= 4:
            phase = "Spécifique compétition"
        elif weeks_to_race <= 10:
            phase = "Spécifique triathlon M (build)"
        else:
            phase = "Préparation générale (base aérobie)"

    # Modulation selon forme
    intensity_today_ok = form["code"] in ("NEUTRE", "FRAIS")
    deload = form["code"] in ("FATIGUE_AIGUE", "FATIGUE")

    # Pace targets
    p_run_thr = thresholds.get("threshold_pace_run_str", "4:55/km")
    p_run_easy = pace_pct(thresholds.get("threshold_pace_run_mps"), 0.85)  # ~ Z2 endurance
    p_run_z4 = pace_pct(thresholds.get("threshold_pace_run_mps"), 0.97)
    p_swim_easy = pace_per100_pct(thresholds.get("threshold_pace_swim_mps"), 0.85)
    p_swim_thr = thresholds.get("threshold_pace_swim_str", "2:00/100m")
    ftp = thresholds.get("ftp_watts", 230)
    z2_low, z2_high = round(ftp * 0.56), round(ftp * 0.75)
    z4_low, z4_high = round(ftp * 0.91), round(ftp * 1.05)

    plan = []
    # Le plan couvre la semaine ISO courante : lundi → dimanche.
    # today.weekday() : 0=lundi … 6=dimanche
    week_monday = today - timedelta(days=today.weekday())
    for i in range(7):
        d = week_monday + timedelta(days=i)
        weekday_en = d.strftime("%A")
        weekday = weekday_en.lower()
        # Position relative à aujourd'hui (négatif = passé, 0 = aujourd'hui, positif = futur)
        rel_day = (d - today).days
        item = {"date": d.isoformat(),
                "weekday": weekday_en,
                "weekday_fr": FR_WEEKDAYS.get(weekday_en, weekday_en),
                "rel_day": rel_day,
                "sport": None, "type": None, "duration_min": 0,
                "structure": "", "zones": "", "rationale": "",
                "tss_estimate": 0,
                "status": "todo"}  # sera enrichi par match_activities_to_plan()

        if weekday == "monday":
            item.update(sport="Repos", type="Récupération",
                        structure="Mobilité 15-20 min, étirements doux, marche.",
                        rationale="Jour de repos défini dans ton profil — supercompensation.")
        elif weekday == "tuesday":
            # Séance de qualité CAP (intervalles VO2max si frais, sinon Z2)
            if deload:
                item.update(sport="Run", type="Endurance souple",
                            duration_min=avg_run["avg_minutes"],
                            structure=f"20' échauffement : 10' trot Z1 + 5' gammes légères + 5' Z2 → "
                                      f"footing continu en Z2 ({p_run_easy}) → "
                                      f"5' retour au calme marche/trot Z1.",
                            zones="Z1-Z2 — FC <80% LTHR",
                            rationale="Volume sans intensité, ATL trop élevé.")
            else:
                item.update(sport="Run", type="VO2max — intervalles courts",
                            duration_min=max(avg_run["avg_minutes"], 45),
                            structure=f"20' échauffement : 10' trot Z1 progressif + 5' gammes (talons-fesses, montées genoux, foulées bondissantes) + 5' accélérations progressives → "
                                      f"6 à 8 × 3' à allure 5km ({p_run_z4}) r=2' trot → "
                                      f"5' retour au calme trot léger.",
                            zones=f"Z5 — FC 92-97% FCmax",
                            rationale="Bloc 80/20 : la séance dure du haut du spectre, "
                                      "stimulation VO2max (Helgerud 2007).")
        elif weekday == "wednesday":
            # 1 seule séance natation par semaine, ~50 min, le mercredi
            item.update(sport="Swim", type="Endurance + technique",
                        duration_min=50,
                        structure=f"400 m échauffement nage souple (50 crawl + 50 dos alternés) → "
                                  f"10×100 m ({p_swim_thr}) r=20s → "
                                  f"200 m retour au calme nage souple. Total ~2000 m.",
                        zones=f"Z3-Z4 (CSS = {p_swim_thr})",
                        rationale="Séance natation hebdomadaire unique (~50 min). "
                                  "CSS bloc principal pour maintenir la technique et l'endurance spécifique.")
        elif weekday == "thursday":
            # Vélo seuil sur HT
            if deload:
                item.update(sport="VirtualRide", type="Endurance",
                            duration_min=avg_bike["avg_minutes"],
                            structure=f"15' échauffement progressif Z1→Z2 (cadence libre) → "
                                      f"Z2 continu {z2_low}-{z2_high} W (~{max(int(avg_bike['avg_minutes']) - 20, 20)}') → "
                                      f"5' retour au calme Z1 (<{z2_low} W).",
                            zones=f"Z2 — {z2_low}-{z2_high} W",
                            rationale="Volume aérobie pur, pas de stress neuromusculaire.")
            else:
                item.update(sport="VirtualRide", type="Seuil — sweet spot",
                            duration_min=max(avg_bike["avg_minutes"], 60),
                            structure=f"15' échauffement progressif Z1→Z2 (cadence libre, finir avec 3×30s à 100 rpm) → "
                                      f"3×12' à {z4_low}-{ftp} W r=4' Z1 → "
                                      f"5' retour au calme Z1 (<{z2_low} W, cadence souple).",
                            zones=f"Z4 (sweet spot 88-94% FTP)",
                            rationale="Travail seuil = pilier triathlon M. "
                                      "Sweet spot = bon ratio stimulus/coût (Seiler).")
        elif weekday == "friday":
            # Renforcement musculaire — 1 séance obligatoire/sem (profil athlete_profile)
            # Plasmé le vendredi : charge faible, prépare le week-end sans créer de fatigue cardio
            item.update(sport="Strength", type="Renforcement musculaire",
                        duration_min=45,
                        structure="Gainage 3×45s · Squats 3×12 · Fentes marchées 3×12/jambe · "
                                  "Hip hinge/RDL 3×10 · Élastiques épaules 2×15 · "
                                  "Planche latérale 2×30s/côté. Récup 60-90s entre séries.",
                        zones="Force-endurance — pas de cardio",
                        rationale="Renforcement musculaire 1×/sem : réduit le risque blessure et améliore l'économie "
                                  "de course (Beattie 2017, Blagrove 2018). Vendredi = charge cardio nulle, "
                                  "laisse le week-end libre pour les séances aérobies longues.")
        elif weekday == "saturday":
            # Repos le samedi
            item.update(sport="Repos", type="Récupération",
                        structure="Repos complet. Mobilité douce, étirements, marche légère si souhaité.",
                        rationale="Jour de repos samedi — récupération avant la sortie longue / brick du dimanche.")
        elif weekday == "sunday":
            long_run_min = int(max(avg_run["avg_minutes"] * 1.6, 60))
            long_bike_min = max(avg_bike["avg_minutes"] * 1.5, 90)
            bike_brick_min = max(avg_bike["avg_minutes"], 75)
            run_brick_min = 20

            if weeks_to_race is not None and weeks_to_race <= 10:
                # Phase build/spécifique : brick vélo+CAP
                item.update(sport="Brick (Bike+Run)", type="Spécifique triathlon",
                            duration_min=bike_brick_min + run_brick_min,
                            structure=f"15' échauffement vélo Z1→Z2 → "
                                      f"vélo {bike_brick_min - 15}' Z2 dont 2×8' tempo Z3 → "
                                      f"transition rapide → "
                                      f"CAP {run_brick_min}' Z2 ({p_run_easy}) → "
                                      f"5' marche retour au calme.",
                            zones="Vélo Z2-Z3 / CAP Z2",
                            rationale="Brick dominical : adaptation neuromusculaire à la transition "
                                      "vélo→CAP, spécifique triathlon M (Hausswirth 2010).")
            else:
                # Phase de base : alterner sortie longue CAP et vélo selon le numéro de semaine ISO.
                iso_week = week_monday.isocalendar()[1]
                if iso_week % 2 == 0:
                    item.update(sport="VirtualRide", type="Sortie longue vélo",
                                duration_min=int(long_bike_min),
                                structure=f"15' échauffement progressif Z1→Z2 → "
                                          f"vélo {int(long_bike_min) - 20}' continu Z2 ({z2_low}-{z2_high} W, cadence 85-90 rpm) → "
                                          f"5' retour au calme Z1 (<{z2_low} W).",
                                zones=f"Z2 — {z2_low}-{z2_high} W",
                                rationale="Sortie longue vélo dominicale (semaine paire) — volume aérobie "
                                          "spécifique triathlon, développement du moteur lipidique à vélo.")
                else:
                    item.update(sport="Run", type="Sortie longue CAP",
                                duration_min=long_run_min,
                                structure=f"20' échauffement : 10' trot Z1 + 5' gammes (talons-fesses, montées genoux, foulées bondissantes) + 5' Z2 → "
                                          f"{long_run_min - 25}' en Z2 ({p_run_easy}) → "
                                          f"optionnel : 5' à allure marathon ({p_run_thr}) → "
                                          f"5' retour au calme trot Z1.",
                                zones="Z2 — FC 75-85% LTHR",
                                rationale="Sortie longue CAP dominicale (semaine impaire) — fondation aérobie, "
                                          "oxydation lipidique (Seiler). Unique sortie longue de la semaine.")
        # Estimation TSS uniformément après le remplissage de l'item
        item["tss_estimate"] = estimate_tss(item["sport"], item["type"], item["duration_min"])
        plan.append(item)

    return plan, weeks_to_race, phase


# ---------- Croisement plan ↔ activités réalisées ----------

SPORT_MATCH = {
    # plan sport → types d'activités Intervals.icu compatibles
    "Run": {"Run", "TrailRun", "Walk"},
    "VirtualRide": {"VirtualRide", "Ride", "GravelRide", "MountainBikeRide"},
    "Ride": {"Ride", "VirtualRide", "GravelRide"},
    "Swim": {"Swim", "OpenWaterSwim"},
    "Brick (Bike+Run)": {"VirtualRide", "Ride", "Run", "GravelRide"},
    "Strength": {"WeightTraining", "Workout", "Crossfit", "Yoga", "Pilates"},
    "Repos": set(),
}


def match_activities_to_plan(plan: list[dict], activities: list[dict]) -> list[dict]:
    """Croise les activités téléchargées avec le plan de la semaine.

    Règles de matching (par priorité) :
    1. "done_exact"  → activité du bon sport ce jour-là
    2. "done_any"    → n'importe quelle activité sportive ce jour-là (sport interverti)
    3. "done_weekly" → le sport planifié a été fait un autre jour de la semaine (séance déplacée)
    4. Jours passés sans aucune activité → "past_missed"
    5. Jour courant sans activité → "today"
    6. Futur → "todo"

    Le pass "weekly" (règle 3) évite de marquer comme manquée une séance simplement
    déplacée d'un jour : si Pool Swim est fait le mardi alors que le plan le met mercredi,
    le mercredi est marqué "done" (déplacé) plutôt que "past_missed".
    """
    today = date.today()

    # Index activités par date (toutes disciplines)
    acts_by_date: dict[str, list[dict]] = defaultdict(list)
    for a in activities:
        d_str = a.get("start_date_local", "")[:10]
        if d_str:
            acts_by_date[d_str].append(a)

    def fmt_activity(a: dict) -> dict:
        return {
            "name": a.get("name"),
            "type": a.get("type"),
            "duration_min": round((a.get("moving_time") or 0) / 60),
            "distance_km": round((a.get("distance") or 0) / 1000, 1),
            "tss": a.get("icu_training_load"),
        }

    # --- Pass 1a : matching exact par date (bon sport, bon jour) ---
    # On marque d'abord les matches exacts, et on note les activités consommées.
    exact_used: set = set()  # clés (name, type) des activités déjà matchées en exact

    for item in plan:
        d = date.fromisoformat(item["date"])
        day_acts = acts_by_date.get(item["date"], [])
        planned_sport = item.get("sport") or ""
        compatible_types = SPORT_MATCH.get(planned_sport, set())

        if planned_sport == "Repos":
            item["status"] = "done" if d <= today else "todo"
            item["actual_activities"] = []
            continue

        exact_match = [a for a in day_acts if a.get("type") in compatible_types]
        if exact_match:
            item["status"] = "done"
            item["sport_match"] = "exact"
            item["actual_activities"] = [fmt_activity(a) for a in exact_match]
            for a in exact_match:
                exact_used.add((a.get("name"), a.get("type")))
        else:
            # Statut temporaire — résolu dans les passes suivantes
            item["status"] = "_pending"
            item["actual_activities"] = []

    # --- Pass 1b : matching hebdomadaire optimal (même sport, autre jour) ---
    # Pour chaque activité passée de la semaine non encore matchée (exact_used),
    # on cherche le jour planifié du même sport le plus proche temporellement.
    # On résout le problème comme une affectation gloutonne triée par distance minimale,
    # ce qui évite qu'une activité "vole" un meilleur slot plus tard dans la semaine.
    week_start = date.fromisoformat(plan[0]["date"])
    week_end = date.fromisoformat(plan[-1]["date"])
    displaced_used: set = set()

    # Construire la liste des activités semaine non encore consommées
    week_acts_unmatched = []
    for a in activities:
        a_date_str = a.get("start_date_local", "")[:10]
        if not a_date_str:
            continue
        a_date = date.fromisoformat(a_date_str)
        if not (week_start <= a_date <= min(today, week_end)):
            continue
        key = (a.get("name"), a.get("type"))
        if key in exact_used:
            continue
        if a.get("icu_training_load", 0) <= 0:
            continue
        week_acts_unmatched.append(a)

    # Construire les paires (item_plan_pending, activité_compatible, distance_jours)
    candidates = []
    for item in plan:
        if item.get("status") != "_pending":
            continue
        d = date.fromisoformat(item["date"])
        if d > today:
            continue
        planned_sport = item.get("sport") or ""
        compatible_types = SPORT_MATCH.get(planned_sport, set())
        for a in week_acts_unmatched:
            if a.get("type") not in compatible_types:
                continue
            a_date = date.fromisoformat(a.get("start_date_local", "")[:10])
            if a_date.isoformat() == item["date"]:
                continue  # même jour déjà traité
            dist = abs((a_date - d).days)
            candidates.append((dist, id(item), id(a), item, a))

    # Trier par distance croissante et attribuer goulûment
    candidates.sort(key=lambda x: x[0])
    assigned_items: set = set()
    for dist, iid, aid, item, a in candidates:
        if iid in assigned_items:
            continue
        key = (a.get("name"), a.get("type"))
        if key in displaced_used:
            continue
        a_date_str = a.get("start_date_local", "")[:10]
        item["status"] = "done"
        item["sport_match"] = "displaced"
        item["actual_activities"] = [fmt_activity(a)]
        item["displacement_note"] = f"Séance réalisée le {a_date_str} (déplacée)"
        displaced_used.add(key)
        assigned_items.add(iid)

    # --- Pass 1c : matching approximate (n'importe quelle activité ce jour-là) ---
    # Dernier recours pour les jours passés sans match sport : on prend toute séance sportive.
    all_used = exact_used | displaced_used
    for item in plan:
        if item.get("status") != "_pending":
            continue
        d = date.fromisoformat(item["date"])
        day_acts = acts_by_date.get(item["date"], [])
        any_sport = [a for a in day_acts
                     if a.get("type") not in {"", None}
                     and a.get("icu_training_load", 0) > 0
                     and (a.get("name"), a.get("type")) not in all_used]

        if any_sport and d <= today:
            item["status"] = "done"
            item["sport_match"] = "approximate"
            item["actual_activities"] = [fmt_activity(a) for a in any_sport]
        elif d == today:
            item["status"] = "today"
        elif d < today:
            item["status"] = "past_missed"
        else:
            item["status"] = "todo"

    return plan


def pace_pct(mps: float | None, pct: float) -> str:
    if not mps:
        return "n/a"
    return pace_mps_to_minkm(mps * pct)


def pace_per100_pct(mps: float | None, pct: float) -> str:
    if not mps:
        return "n/a"
    return pace_mps_to_per100m(mps * pct)


# ---------- Adaptation dynamique du plan ----------

# Séances considérées "clés" — prioritaires pour récupération si manquées
KEY_SESSION_TYPES = {"VO2max", "Seuil", "Spécifique triathlon", "Sortie longue"}

def adapt_plan_to_week(plan: list[dict], atl: float, ctl: float,
                       tss_target_week: int) -> list[dict]:
    """Adapte les séances futures de la semaine en fonction de ce qui a déjà été réalisé.

    Règles (par ordre de priorité) :
    1. Surcharge hebdo (TSS réalisé > 80% cible) → passer les séances restantes en Z2 endurance
    2. Fatigue accumulée (ATL > CTL + 10) → remplacer toute intensité par endurance
    3. Séance clé manquée (❌ passé) → la récupérer sur le prochain jour libre (Repos futur)
    4. Séance déjà faite aujourd'hui → marquer comme done, laisser le lendemain inchangé
    """
    today = date.today()

    # --- Calcul de la charge réelle accumulée cette semaine ---
    tss_done = sum(
        sum(a.get("tss", 0) or 0 for a in item.get("actual_activities", []))
        for item in plan
        if item.get("status") == "done" and item.get("sport") != "Repos"
    )
    overload = tss_done >= tss_target_week * 0.80
    fatigue_spike = atl > ctl + 10

    # --- Identification des séances futures et des jours de repos disponibles ---
    future_items = [p for p in plan
                    if date.fromisoformat(p["date"]) > today
                    and p.get("status") == "todo"]
    free_slots = [p for p in future_items if p.get("sport") == "Repos"]

    # --- Séances clés manquées (passées et non réalisées) ---
    missed_key = [
        p for p in plan
        if p.get("status") == "past_missed"
        and p.get("sport") not in (None, "Repos")
        and any(kw.lower() in (p.get("type") or "").lower() for kw in KEY_SESSION_TYPES)
    ]

    adaptations = []  # log des ajustements pour affichage dans le dashboard

    # --- Règle 1 & 2 : surcharge ou pic de fatigue → tout en endurance ---
    if overload or fatigue_spike:
        reason = (
            f"TSS réalisé ({tss_done:.0f}) ≥ 80% de la cible hebdo ({tss_target_week})"
            if overload else
            f"Pic de fatigue (ATL {atl:.1f} > CTL {ctl:.1f} + 10)"
        )
        for item in future_items:
            if item.get("sport") in ("Repos", None):
                continue
            sport = item["sport"]
            original_type = item.get("type", "")
            # Ne pas toucher les séances déjà légères
            if any(kw in (original_type or "").lower()
                   for kw in ("récupération", "endurance souple", "sortie longue")):
                continue
            item["type"] = "Endurance souple"
            item["zones"] = "Z1-Z2 — FC <80% LTHR"
            item["structure"] = (
                f"{item.get('duration_min', 40)}' continu très léger Z1-Z2. "
                f"Effort conversationnel, aucune intensité."
            )
            item["adaptation"] = f"⚠️ Adapté : {reason}. Séance originale : {original_type}."
            item["tss_estimate"] = estimate_tss(sport, "Endurance souple", item.get("duration_min", 40))
        if overload or fatigue_spike:
            adaptations.append(f"⚠️ {reason} → séances futures allégées en Z1-Z2.")

    # --- Règle 3 : récupérer une séance clé manquée sur le prochain jour libre ---
    # Seulement si le TSS projeté reste dans la cible hebdomadaire (±15%)
    elif missed_key and free_slots:
        missed = missed_key[0]
        slot = free_slots[0]
        tss_todo = sum(
            p.get("tss_estimate", 0) for p in future_items
            if p.get("sport") not in ("Repos", None)
        )
        tss_projected = tss_done + tss_todo + missed.get("tss_estimate", 0)
        if tss_projected <= tss_target_week * 1.10:
            slot["sport"] = missed["sport"]
            slot["type"] = missed["type"]
            slot["duration_min"] = missed["duration_min"]
            slot["structure"] = missed["structure"]
            slot["zones"] = missed["zones"]
            slot["tss_estimate"] = missed["tss_estimate"]
            slot["adaptation"] = (
                f"🔄 Récupération de la séance manquée du {missed['weekday_fr']} "
                f"({missed['type']})."
            )
            adaptations.append(
                f"🔄 Séance '{missed['type']}' du {missed['weekday_fr']} manquée → "
                f"reportée au {slot['weekday_fr']}."
            )
        else:
            adaptations.append(
                f"⏭️ Séance '{missed['type']}' du {missed['weekday_fr']} manquée — "
                f"non récupérée (TSS projeté {tss_projected:.0f} dépasserait la cible {tss_target_week})."
            )

    return plan, adaptations


# ---------- Chargement du plan théorique IA ----------

def load_theoretical_plans(today: date) -> tuple[list[dict], list[dict], dict, dict]:
    """Charge les plans théoriques depuis data/weekly_plans.json.

    Retourne (ideal_week_plan, next_week_plan, ideal_totals, next_totals).
    Si le fichier n'existe pas ou est obsolète, retourne des listes vides.

    Logique de correspondance :
    - generate_plans.py est exécuté le dimanche soir et génère les plans des
      2 semaines SUIVANTES (week1 = lundi prochain, week2 = lundi dans 14j).
    - daily_coach.py tourne toute la semaine : il doit accepter week1 comme
      plan de la semaine en cours dès que week1.monday == lundi de cette semaine.
    - Le dimanche (dernier jour de la semaine passée), week1.monday est déjà
      le lundi suivant → on l'accepte aussi comme plan "en cours" pour que le
      dashboard soit déjà à jour le dimanche soir.
    """
    if not WEEKLY_PLANS_PATH.exists():
        return [], [], {}, {}

    try:
        data = json.loads(WEEKLY_PLANS_PATH.read_text())
    except Exception:
        return [], [], {}, {}

    week_monday = today - timedelta(days=today.weekday())  # lundi de cette semaine
    next_monday = week_monday + timedelta(days=7)           # lundi de la semaine prochaine

    def make_totals(w: dict) -> dict:
        return {
            "total_minutes": w.get("total_minutes", 0),
            "total_minutes_str": w.get("total_minutes_str", ""),
            "total_tss": w.get("total_tss", 0),
        }

    # Extraire les 2 entrées du fichier
    w1 = data.get("week1", {})
    w2 = data.get("week2", {})
    w1_monday = w1.get("monday", "")
    w2_monday = w2.get("monday", "")

    ideal_plan: list[dict] = []
    next_plan: list[dict] = []
    ideal_totals: dict = {}
    next_totals: dict = {}

    # Cas 1 : fichier aligné sur la semaine courante (généré lundi ou en cours de semaine)
    if w1_monday == week_monday.isoformat():
        ideal_plan = w1.get("days", [])
        ideal_totals = make_totals(w1)
        if w2_monday == next_monday.isoformat():
            next_plan = w2.get("days", [])
            next_totals = make_totals(w2)

    # Cas 2 : fichier généré le dimanche — week1 correspond déjà à la semaine prochaine.
    # On prend week1 comme plan "de la semaine courante" (qui commence demain lundi)
    # et week2 comme plan de la semaine suivante.
    elif w1_monday == next_monday.isoformat():
        ideal_plan = w1.get("days", [])
        ideal_totals = make_totals(w1)
        next_plan = w2.get("days", [])
        next_totals = make_totals(w2)
        print(f"ℹ️  Plans IA chargés en avance (générés ce dimanche) : "
              f"semaine 1 = {w1_monday}, semaine 2 = {w2_monday}")

    # Cas 3 : fichier périmé (semaines passées) → fallback algo
    else:
        print(f"⚠️  Plans IA périmés (week1={w1_monday}, today={today}) — fallback algorithmique.")

    return ideal_plan, next_plan, ideal_totals, next_totals


# ---------- Plan adaptatif IA ----------

def generate_adaptive_plan_ia(
    today: date,
    ideal_plan: list[dict],
    activities: list[dict],
    form: dict,
    thresholds: dict,
    profile: dict,
    snapshot_meta: dict,
) -> tuple[list[dict], list[str]]:
    """Génère le plan adaptatif de la semaine via Claude Sonnet.

    Prend en entrée le plan théorique + les activités déjà réalisées cette semaine
    et produit un plan mis à jour avec les ajustements nécessaires.

    Retourne (plan_adaptatif, adaptations_messages).
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            from claude_client import load_api_key
            api_key = load_api_key()
        except Exception:
            pass

    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY non disponible — fallback plan adaptatif algorithmique.")
        return [], []

    # Activités de la semaine en cours
    week_monday = today - timedelta(days=today.weekday())
    week_sunday = week_monday + timedelta(days=6)
    week_acts = [
        a for a in activities
        if week_monday.isoformat() <= a.get("start_date_local", "")[:10] <= week_sunday.isoformat()
    ]

    acts_summary = []
    for a in week_acts:
        d = a.get("start_date_local", "")[:10]
        sport = a.get("type", "?")
        dur = round((a.get("moving_time") or 0) / 60)
        dist = round((a.get("distance") or 0) / 1000, 1)
        tss = a.get("icu_training_load") or 0
        name = a.get("name", "")
        acts_summary.append(f"  {d} ({sport}) — {dur}' {dist}km TSS={tss:.0f} \"{name}\"")

    # Plan théorique simplifié
    plan_summary = []
    for day in ideal_plan:
        sport = day.get("sport", "Repos")
        typ = day.get("type", "")
        dur = day.get("duration_min", 0)
        tss = day.get("tss_estimate", 0)
        wfr = day.get("weekday_fr", "")
        d = day.get("date", "")
        if sport != "Repos":
            plan_summary.append(f"  {d} {wfr}: {sport} — {typ} ({dur}' / TSS {tss})")
        else:
            plan_summary.append(f"  {d} {wfr}: Repos")

    ftp = thresholds.get("ftp_watts", 250)
    thr_run = thresholds.get("threshold_pace_run_str", "4:53/km")
    tsb = snapshot_meta.get("tsb", 0)
    ctl = snapshot_meta.get("ctl", 0)
    atl = snapshot_meta.get("atl", 0)

    prompt = f"""# Plan adaptatif — semaine du {week_monday.isoformat()} au {week_sunday.isoformat()}

## Forme du jour ({today.isoformat()})
CTL : {ctl:.1f} | ATL : {atl:.1f} | TSB : {tsb:+.1f}
Forme : {form.get("label", "?")} — {form.get("guidance", "")}

## Plan théorique de référence
{chr(10).join(plan_summary)}

## Activités réalisées cette semaine
{chr(10).join(acts_summary) if acts_summary else "  Aucune."}

## Mission
Produis le plan adaptatif complet (lundi → dimanche).

- Jours PASSÉS avec activité → status="done", remplis actual_activities
- Jours PASSÉS sans activité → status="past_missed", blocks=[]
- AUJOURD'HUI ({today.isoformat()}) → status="today", adapte selon TSB {tsb:+.1f}
- Jours FUTURS → status="todo", ajuste si retard/avance TSS

IMPORTANT : Ne calcule PAS les allures ni les watts. Fournis uniquement des blocs avec % d'intensité.
Les durées et allures réelles seront calculées automatiquement.

Réponds UNIQUEMENT avec ce JSON :

{{
  "adaptations": ["Note d'adaptation courte si nécessaire"],
  "days": [
    {{
      "date": "YYYY-MM-DD",
      "weekday_fr": "Lundi",
      "sport": "Repos|Run|Swim|VirtualRide|Strength",
      "type": "Nom court",
      "rationale": "Justification courte",
      "adaptation": "Note si séance modifiée vs plan théorique (sinon null)",
      "status": "done|past_missed|today|todo",
      "actual_activities": [],
      "blocks": [
        {{
          "type": "endurance|interval|recovery|strength_exercise",
          "duration_min": 20,
          "reps": 1,
          "recovery_min": 0,
          "intensity_pct": 75,
          "zone": "Z2",
          "description": "Texte libre pour renfo/exercices"
        }}
      ]
    }}
  ]
}}

Pour status="done" : actual_activities = [{{"name":"...", "type":"Run/Ride/etc", "duration_min":X, "distance_km":X, "tss":X}}]
Pour status="done"/"past_missed" : blocks peut être vide (séance déjà passée)
Pour Run/VirtualRide : NE PAS inclure warmup/cooldown dans les blocs (ajoutés automatiquement)
Pour Repos : blocks = []"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        from claude_client import SYSTEM_PROMPT
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Parse JSON
        import re
        md_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
        if md_match:
            raw = md_match.group(1)
        idx = raw.find("{")
        if idx >= 0:
            raw = raw[idx: raw.rfind("}") + 1]
        result = json.loads(raw)

        adaptive_days = result.get("days", [])
        adaptations = result.get("adaptations", [])

        # Récupère le profil warmup/cooldown depuis le profil athlète
        wu_profile = profile.get("training_preferences", {}).get("warmup_cooldown", {})

        # Corriger les dates + calcul algorithmique allures/durée/TSS
        for i, day in enumerate(adaptive_days):
            d_obj = week_monday + timedelta(days=i)
            day["date"] = d_obj.isoformat()
            day["weekday"] = d_obj.strftime("%A")
            day["weekday_fr"] = FR_WEEKDAYS.get(d_obj.strftime("%A"), d_obj.strftime("%A"))
            day.setdefault("actual_activities", [])
            day.setdefault("adaptation", None)
            # Pour les jours futurs/aujourd'hui : calcul de structure/durée/TSS
            if day.get("status") not in ("done", "past_missed"):
                compute_session(day, thresholds, wu_profile)

        print(f"✅  Plan adaptatif généré par Claude Sonnet ({len(adaptive_days)} jours).")
        return adaptive_days, adaptations

    except Exception as e:
        print(f"⚠️  Erreur génération plan adaptatif IA ({e}) — fallback algorithmique.")
        return [], []


# ---------- Message coach motivationnel ----------

def _build_coach_prompt(snapshot: dict) -> str:
    """Construit le prompt envoyé à Claude Haiku pour générer le message coach."""
    m = snapshot["metrics"]
    form = snapshot["form"]
    plan = snapshot["weekly_plan"]
    th = snapshot["thresholds"]
    sp = snapshot["session_profile"]
    load_7 = snapshot["load_7d"]
    load_28 = snapshot["load_28d"]

    done = [p for p in plan if p.get("status") == "done" and p.get("sport") not in ("Repos", None)]
    missed = [p for p in plan if p.get("status") == "past_missed"]
    today_session = next((p for p in plan if p.get("status") == "today"), None)
    tss_done = sum(
        sum(a.get("tss", 0) or 0 for a in p.get("actual_activities", []))
        for p in done
    )

    done_str = ", ".join(
        f"{p['weekday_fr']} ({p.get('sport','')} — {p.get('type','')})" for p in done
    ) or "aucune"
    missed_str = ", ".join(
        f"{p['weekday_fr']} ({p.get('type','')})" for p in missed
    ) or "aucune"
    today_str = (
        f"{today_session.get('sport','')} — {today_session.get('type','')} "
        f"({today_session.get('duration_min',0)} min) : {today_session.get('structure','')}"
        if today_session else "repos"
    )
    disciplines = list(sp.keys())

    return f"""Tu es le coach triathlon de David (38 ans, {th.get('weight_kg', 78)} kg).
Tu parles directement à David, en français, avec le ton chaleureux et direct d'un vrai coach sportif.
Tu t'appuies sur les données réelles ci-dessous pour personnaliser ton message.

DONNÉES DU JOUR ({snapshot['today']}) :
- Forme : {form['label']} (TSB {m['tsb']:+.1f} — {form['guidance']})
- CTL {m['ctl']:.1f} / ATL {m['atl']:.1f} / Ramp rate {m['ramp_rate']:+.1f} CTL/sem
- Charge 7j : {round(load_7['total_load'])} TSS / {round(load_7['total_minutes'])} min
- Charge moy 28j : {round(load_28['total_load']/4)} TSS/sem
- FC repos : {m.get('resting_hr', '—')} bpm

SEMAINE EN COURS :
- Séances réalisées : {done_str} ({tss_done} TSS)
- Séances manquées : {missed_str}
- Séance du jour : {today_str}

PROFIL & OBJECTIF :
- Course : {snapshot.get('race_name')} dans {snapshot.get('weeks_to_race')} semaines ({snapshot.get('race_date')})
- Phase : {snapshot['phase']}
- Disciplines actives (28j) : {', '.join(disciplines)}
- FTP vélo : {th.get('ftp_watts')} W | Seuil CAP : {th.get('threshold_pace_run_str')} | CSS nat : {th.get('threshold_pace_swim_str')}

INSTRUCTIONS :
Rédige un message coach de 15 à 20 lignes maximum structuré ainsi :
1. Une accroche percutante sur la forme du jour (1-2 phrases)
2. Bilan honnête de la semaine en cours (séances faites, éventuelles séances manquées)
3. 2-3 points forts concrets basés sur les données (pas de généralités)
4. 2-3 axes de progression prioritaires avec des conseils précis et actionnables
5. Positionnement dans le macro-plan : où on en est, ce qui reste à faire, l'objectif à court terme

Utilise **gras** pour les titres de section. Sois direct, motivant, sans être creux.
Ne répète pas les chiffres bruts déjà affichés dans le dashboard — interprète-les."""


def generate_coach_message(snapshot: dict) -> str:
    """Génère le message coach via Claude Haiku (API Anthropic) si disponible,
    sinon fallback sur la version Python statique.

    La clé ANTHROPIC_API_KEY est lue depuis les variables d'environnement
    (GitHub Actions Secret ou export local).
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            prompt = _build_coach_prompt(snapshot)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            message = response.content[0].text.strip()
            print("✅  Message coach généré par Claude Haiku (API Anthropic).")
            return message
        except Exception as e:
            print(f"⚠️  API Anthropic indisponible ({e}) — fallback sur génération Python.")

    # --- Fallback Python statique ---
    m = snapshot["metrics"]
    form = snapshot["form"]
    plan = snapshot["weekly_plan"]
    phase = snapshot["phase"]
    weeks_to_race = snapshot.get("weeks_to_race")
    race_name = snapshot.get("race_name", "ta course objectif")
    ctl = m["ctl"]
    atl = m["atl"]
    tsb = m["tsb"]
    ramp = m["ramp_rate"]
    load_7 = snapshot["load_7d"]
    load_28 = snapshot["load_28d"]
    sp = snapshot["session_profile"]
    th = snapshot["thresholds"]
    ftp = th.get("ftp_watts", 230)

    # --- Séances réalisées / manquées cette semaine ---
    done_sessions = [p for p in plan if p.get("status") == "done" and p.get("sport") != "Repos"]
    missed_sessions = [p for p in plan if p.get("status") == "past_missed"]
    total_sport = [p for p in plan if p.get("sport") not in ("Repos", None, "Strength")]
    nb_done = len(done_sessions)
    nb_missed = len(missed_sessions)
    tss_done = sum(
        sum(a.get("tss", 0) or 0 for a in p.get("actual_activities", []))
        for p in done_sessions
    )

    # --- Sports présents dans le profil 4 semaines ---
    has_run = "Run" in sp
    has_bike = "VirtualRide" in sp or "Ride" in sp
    has_swim = "Swim" in sp
    run_sessions_pw = sp.get("Run", {}).get("per_week_float", sp.get("Run", {}).get("per_week", 0))
    swim_sessions_pw = sp.get("Swim", {}).get("per_week_float", sp.get("Swim", {}).get("per_week", 0))

    lines = []

    # 1. Accroche forme du jour
    if form["code"] == "FRAIS":
        lines.append(
            f"David, tu arrives à cette séance avec un TSB de {tsb:+.1f} — tu es frais, bien récupéré. "
            f"C'est exactement la fenêtre qu'on cherche pour placer une séance clé. Profites-en !"
        )
    elif form["code"] == "NEUTRE":
        lines.append(
            f"TSB à {tsb:+.1f} — tu es dans une zone neutre, ni surchargé ni sous-entraîné. "
            f"C'est le quotidien du triathlete qui construit sérieusement. On continue."
        )
    elif form["code"] == "FATIGUE":
        lines.append(
            f"TSB à {tsb:+.1f} — la fatigue s'accumule, c'est normal dans une phase de charge. "
            f"Ça signifie que le travail est là. Aujourd'hui on gère intelligemment, pas à fond."
        )
    else:
        lines.append(
            f"TSB à {tsb:+.1f} — attention, la fatigue est marquée cette semaine. "
            f"On reste disciplinés : c'est la récupération qui transforme l'entraînement en progrès."
        )
    lines.append("")

    # 2. Bilan de la semaine en cours
    bilan_parts = []
    if nb_done > 0:
        disciplines_done = list({p["sport"] for p in done_sessions})
        bilan_parts.append(
            f"Cette semaine, tu as déjà coché {nb_done} séance{'s' if nb_done > 1 else ''} "
            f"({', '.join(disciplines_done)}) pour environ {tss_done} TSS réalisés."
        )
    if nb_missed > 0:
        missed_names = [p.get("weekday_fr", "") for p in missed_sessions]
        bilan_parts.append(
            f"{'Une séance' if nb_missed == 1 else str(nb_missed) + ' séances'} "
            f"{'a été manquée' if nb_missed == 1 else 'ont été manquées'} "
            f"({', '.join(missed_names)}) — ça arrive, l'important c'est la régularité sur les semaines, pas la perfection sur chaque jour."
        )
    if bilan_parts:
        lines.append(" ".join(bilan_parts))
        lines.append("")

    # 3. Points forts
    points_forts = []
    tss_28_per_week = load_28["total_load"] / 4 if load_28["total_load"] else 0
    if tss_28_per_week >= 180:
        points_forts.append(
            f"ta capacité à encaisser un volume solide ({round(tss_28_per_week)} TSS/sem en moyenne sur 28j)"
        )
    if ctl >= 35:
        points_forts.append(
            f"ta CTL à {ctl:.1f} qui reflète un niveau de forme construit semaine après semaine — tu n'es pas là par hasard"
        )
    if abs(ramp) < 3:
        points_forts.append(
            f"ta progression maîtrisée (ramp rate {ramp:+.1f} CTL/sem) : tu sais doser l'effort sans te cramer"
        )
    if has_bike and has_run and has_swim:
        points_forts.append(
            "ta régularité sur les trois disciplines — rare pour un amateur, c'est ce qui fait la différence en triathlon"
        )

    if points_forts:
        lines.append("**Tes points forts :**")
        for pf in points_forts:
            lines.append(f"— {pf.capitalize()}.")
        lines.append("")

    # 4. Axes de progression
    axes = []
    run_pw = float(str(sp.get("Run", {}).get("per_week", "0")).replace("/sem", "").strip()) if has_run else 0
    swim_pw = float(str(sp.get("Swim", {}).get("per_week", "0")).replace("/sem", "").strip()) if has_swim else 0

    if swim_pw < 1.5:
        axes.append((
            "Densifier la natation",
            f"Tu tournes à ~{sp.get('Swim', {}).get('per_week', '1')} séance nat/sem. "
            f"Sur un Triathlon M avec 1500m de nage, c'est la discipline où on grignote le plus de temps "
            f"sans coût physique élevé. On vise 2 séances/sem en build. "
            f"La CSS cible reste {th.get('threshold_pace_swim_str', '2:00/100m')} — les répétitions de 100m à CSS sont ta priorité."
        ))
    if run_pw < 2.5 and has_run:
        axes.append((
            "Solidifier le running",
            f"La CAP est ta discipline qui supporte le plus de blessures si on monte trop vite. "
            f"On va augmenter progressivement la fréquence des sorties courtes en Z1-Z2 "
            f"avant d'ajouter du volume. Allure cible endurance : autour de {th.get('threshold_pace_run_str', '4:55/km')} −20%."
        ))
    if ctl < 45:
        axes.append((
            "Construire la base CTL",
            f"Avec une CTL à {ctl:.1f}, il y a de la marge pour progresser d'ici la course. "
            f"L'objectif est d'atteindre CTL ≥ 55-60 au pic de charge, puis d'affûter. "
            f"On y va méthodiquement : +3 à 4 CTL/sem max pour rester dans la zone verte."
        ))
    axes.append((
        "Renforcement musculaire",
        "La séance de muscu du vendredi n'est pas du remplissage — c'est un pilier. "
        "Squats, fentes, hip hinge : ce sont les fondations qui protègent tes genoux sur le run "
        "et ta puissance sur le vélo (Beattie 2017). Ne la saute pas !"
    ))

    if axes:
        lines.append("**Axes de progression :**")
        for titre, explication in axes:
            lines.append(f"**{titre}** — {explication}")
            lines.append("")

    # 5. Macro-plan
    if weeks_to_race is not None:
        if weeks_to_race > 12:
            macro = (
                f"On est à {weeks_to_race} semaines de {race_name}. "
                f"Phase de **{phase}** : l'heure est à construire le moteur aérobie, pas à se presser. "
                f"Chaque séance Z2 que tu poses maintenant sera du carburant en août. "
                f"Le pic de charge est prévu dans ~{max(weeks_to_race - 6, 3)} semaines — on a le temps de faire les choses bien."
            )
        elif weeks_to_race > 6:
            macro = (
                f"{weeks_to_race} semaines avant {race_name}. "
                f"On entre progressivement en **{phase}**. "
                f"Les séances spécifiques (bricks, seuil vélo, allures de course) vont prendre plus de place. "
                f"Le volume global reste stable — on travaille la qualité et la spécificité."
            )
        elif weeks_to_race > 2:
            macro = (
                f"Plus que {weeks_to_race} semaines avant {race_name} ! "
                f"**{phase}** : on réduit le volume, on maintient quelques piques d'intensité, "
                f"le corps va supercompenser. Fais confiance au travail déjà accompli — il est là."
            )
        else:
            macro = (
                f"C'est la semaine de {race_name}. Repos, activation légère, confiance. "
                f"L'entraînement est fait. Tu es prêt."
            )
        lines.append(f"**Là où on en est :** {macro}")
        lines.append("")

    return "\n".join(lines).strip()


# ---------- Rapport Markdown ----------

def render_markdown(snapshot: dict) -> str:
    s = snapshot
    today = s["today"]
    form = s["form"]
    th = s["thresholds"]
    metrics = s["metrics"]
    load_7 = s["load_7d"]
    load_28 = s["load_28d"]
    plan = s["weekly_plan"]
    phase = s["phase"]
    weeks_to_race = s["weeks_to_race"]

    md = []
    md.append(f"# Coach quotidien — {today}")
    md.append("")
    md.append(f"**État du jour : {form['emoji']} {form['label']}** — TSB = {metrics['tsb']:+.1f}")
    md.append(f"_{form['guidance']}_")
    md.append("")

    md.append("## 1. État de forme (Banister / PMC)")
    md.append("")
    md.append(f"| Indicateur | Valeur | Interprétation |")
    md.append(f"|---|---|---|")
    md.append(f"| **CTL** (forme, charge chronique 42j) | {metrics['ctl']:.1f} | "
              f"Capacité d'absorption d'entraînement |")
    md.append(f"| **ATL** (fatigue, charge aigüe 7j) | {metrics['atl']:.1f} | "
              f"Coût de la dernière semaine |")
    md.append(f"| **TSB** (forme = CTL−ATL) | {metrics['tsb']:+.1f} | "
              f"{form['label']} |")
    md.append(f"| **Ramp rate** | {metrics['ramp_rate']:+.1f} CTL/sem | "
              f"{'Sain (<5)' if abs(metrics['ramp_rate']) < 5 else 'Risque blessure si >7 (Gabbett)'} |")
    rhr = metrics.get("resting_hr")
    if rhr:
        md.append(f"| **FC repos** | {rhr} bpm | Référence {th.get('resting_hr', 56)} bpm |")
    if metrics.get("hrv_status"):
        md.append(f"| **HRV** | — | {metrics['hrv_status']} |")
    sleep_h = metrics.get("sleep_hours")
    if sleep_h:
        md.append(f"| **Sommeil** | {sleep_h:.1f} h | {'OK' if sleep_h >= 7 else 'Sous la cible 7h+'} |")
    md.append("")

    md.append("## 2. Charge & contexte")
    md.append("")
    md.append(f"- **Phase de prépa** : {phase}")
    if weeks_to_race is not None:
        md.append(f"- **Course objectif** : {s['race_name']} dans **{weeks_to_race} semaines** ({s['race_date']})")
    md.append(f"- **Charge 7j** : {load_7['total_load']:.0f} TSS | {load_7['total_minutes']:.0f} min")
    md.append(f"- **Charge 28j moyenne / sem** : {load_28['total_load']/4:.0f} TSS | "
              f"{load_28['total_minutes']/4:.0f} min")
    md.append("")
    md.append("**Distribution moyenne 4 semaines** (calibre du plan ci-dessous) :")
    md.append("")
    md.append("| Discipline | Séances/sem | Durée moy | Distance moy | TSS moy |")
    md.append("|---|---|---|---|---|")
    for sport, p in s["session_profile"].items():
        md.append(f"| {sport} | {p['per_week']} | {p['avg_minutes']} min | "
                  f"{p['avg_distance_km']} km | {p['avg_load']} |")
    md.append("")

    STATUS_ICON = {
        "done": "✅",
        "today": "⏳",
        "todo": "⬜",
        "past_missed": "❌",
    }

    md.append("## 3. Plan de la semaine (calibré 80/20 polarisé)")
    md.append("")
    week_monday = s.get("week_monday", "")
    week_sunday = s.get("week_sunday", "")
    md.append(f"_Semaine du {week_monday} au {week_sunday}. "
              f"✅ Réalisé · ⏳ Aujourd'hui · ⬜ À venir · ❌ Manqué_")
    md.append("")
    md.append("| Statut | Jour | Séance | Réalisé / Prévu | Intensité | TSS |")
    md.append("|---|---|---|---|---|---|")
    total_min = 0
    total_tss = 0
    done_tss = 0
    for p in plan:
        status = p.get("status", "todo")
        icon = STATUS_ICON.get(status, "⬜")
        jour = f"**{p.get('weekday_fr', p['weekday'])} {p['date'][5:]}**"
        seance = format_seance(p)
        intensite = p.get("zones") or "—"

        actual = p.get("actual_activities", [])
        if actual:
            # Affiche le résumé réel
            act = actual[0]
            real_dur = act.get("duration_min", 0)
            real_dist = act.get("distance_km", 0)
            real_tss = act.get("tss") or 0
            contenu = f"_{act.get('name', act.get('type', '?'))}_ — {real_dur}' / {real_dist} km"
            tss_str = str(real_tss) if real_tss else "—"
            total_tss += real_tss
            done_tss += real_tss
        else:
            # Affiche la prescription planifiée
            contenu = p.get("structure") or "—"
            if p.get("duration_min", 0) > 0:
                contenu = f"({p['duration_min']}') {contenu}"
            tss = p.get("tss_estimate", 0)
            tss_str = str(tss) if tss > 0 else "—"
            total_tss += tss

        total_min += p.get("duration_min", 0)
        md.append(f"| {icon} | {jour} | {seance} | {contenu} | {intensite} | {tss_str} |")

    md.append(f"| | **Total semaine** | **{format_duration_total(total_min)}** | "
              f"TSS réalisé : {done_tss} | | **~{total_tss}** |")
    md.append("")

    md.append("## 4. Justifications par séance")
    md.append("")
    for p in plan:
        if not p.get("rationale"):
            continue
        md.append(f"- **{p.get('weekday_fr', p['weekday'])} {p['date'][5:]} — "
                  f"{format_seance(p)}** : {p['rationale']}")
    md.append("")

    md.append("## 5. Zones de référence (synchronisées)")
    md.append("")
    md.append(f"- **FTP vélo** : {th.get('ftp_watts')} W")
    md.append(f"- **Allure seuil CAP** : {th.get('threshold_pace_run_str')}")
    md.append(f"- **CSS natation** : {th.get('threshold_pace_swim_str')}")
    md.append(f"- **LTHR / FCmax** : {th.get('lthr')} / {th.get('hr_max')} bpm")
    md.append("")
    md.append("## 6. Notes scientifiques")
    md.append("")
    md.append("- Distribution **80/20 polarisée** (Seiler 2010, Stöggl 2014) : majoritairement "
              "Z1-Z2 (sous LT1), 15-20% en Z4-Z5 (sopra LT2), ~0% en Z3 hors blocs spécifiques.")
    md.append("- **Sweet spot** (88-94% FTP) : meilleur ratio gain CTL/coût ATL pour un athlète "
              "amateur 6-10 h/sem (Seiler).")
    md.append("- **TSB** = forme prédictive : pic course visé entre +5 et +20 (Coggan).")
    md.append("- **Ramp rate** > +5-7 CTL/sem associé à risque blessure accru (Gabbett 2016).")
    md.append("")

    return "\n".join(md)


# ---------- Main ----------

def run() -> dict:
    client = IntervalsClient()
    today = date.today()

    # Profil athlète YAML (legacy)
    try:
        import yaml  # type: ignore
        prof_yaml = yaml.safe_load(PROFILE_PATH.read_text()) if PROFILE_PATH.exists() else {}
    except Exception:
        prof_yaml = {}

    # Profil athlète JSON (source de vérité principale)
    athlete_profile: dict = {}
    try:
        if ATHLETE_PROFILE_PATH.exists():
            athlete_profile = json.loads(ATHLETE_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        athlete_profile = {}

    # Date de course : priorité au profil JSON, fallback YAML
    race_date = None
    _race_json = (athlete_profile.get("season") or {}).get("race", {})
    _race_yaml = (prof_yaml or {}).get("race", {})
    _race_date_str = _race_json.get("date") or _race_yaml.get("date")
    if _race_date_str:
        try:
            race_date = date.fromisoformat(_race_date_str)
        except ValueError:
            race_date = None

    # Données API
    thresholds = client.get_thresholds()
    # 90j pour le graphique PMC ; 14j suffisent pour les métriques du jour
    wellness_90d = client.wellness(today - timedelta(days=90))
    wellness = [w for w in wellness_90d
                if w.get("id", "") >= (today - timedelta(days=14)).isoformat()]
    # Récupère les activités depuis le début de saison (pour la frise) + 42j pour profil/croisement
    week_monday = today - timedelta(days=today.weekday())
    season_start = date(2026, 5, 4)  # lundi de la semaine du 5 mai = début frise saison
    oldest_fetch = min(today - timedelta(days=42), week_monday, season_start)
    activities = client.activities(oldest_fetch)

    # Métriques du jour
    today_w = next((w for w in wellness if w.get("id") == today.isoformat()),
                   wellness[-1] if wellness else {})
    ctl = today_w.get("ctl") or 0
    atl = today_w.get("atl") or 0
    tsb = ctl - atl
    ramp = today_w.get("rampRate") or 0
    sleep_secs = today_w.get("sleepSecs")
    sleep_h = (sleep_secs / 3600) if sleep_secs else None
    rhr = today_w.get("restingHR")
    hrv_recent = [w.get("hrv") for w in wellness[-7:]]
    hrv_msg = hrv_status(hrv_recent)

    form = classify_form(tsb)

    metrics = {
        "ctl": ctl, "atl": atl, "tsb": tsb, "ramp_rate": ramp,
        "resting_hr": rhr, "sleep_hours": sleep_h, "hrv_status": hrv_msg,
    }

    load_7 = weekly_load(activities, 7)
    load_28 = weekly_load(activities, 28)
    session_profile = average_session_profile(activities, 28)

    # ── Plan théorique idéal ────────────────────────────────────────────────
    # Source prioritaire : data/weekly_plans.json (généré par IA le dimanche).
    # Fallback : génération algorithmique si le fichier est absent ou obsolète.
    next_week_monday = week_monday + timedelta(days=7)
    next_week_sunday = next_week_monday + timedelta(days=6)

    ideal_week_plan, next_week_plan, ideal_week_plan_totals, next_week_plan_totals = \
        load_theoretical_plans(today)

    ia_plans_loaded = bool(ideal_week_plan)

    if not ia_plans_loaded:
        print("ℹ️  Aucun plan IA trouvé — fallback génération algorithmique.")
        ideal_plan_raw, weeks_to_race, phase = build_weekly_plan(today, form, session_profile,
                                                                  thresholds, race_date)
        ideal_week_plan = [{**p, "status": "ideal", "generated_by_ia": False} for p in ideal_plan_raw]
        ideal_plan_total_min = sum(p.get("duration_min", 0) for p in ideal_week_plan)
        ideal_plan_total_tss = sum(p.get("tss_estimate", 0) for p in ideal_week_plan)
        ideal_week_plan_totals = {
            "total_minutes": ideal_plan_total_min,
            "total_minutes_str": format_duration_total(ideal_plan_total_min),
            "total_tss": ideal_plan_total_tss,
        }

        next_week_plan_raw, _, _ = build_weekly_plan(
            next_week_monday, form, session_profile, thresholds, race_date
        )
        next_week_plan = [{**p, "status": "ideal", "generated_by_ia": False} for p in next_week_plan_raw]
        next_plan_total_min = sum(p.get("duration_min", 0) for p in next_week_plan)
        next_plan_total_tss = sum(p.get("tss_estimate", 0) for p in next_week_plan)
        next_week_plan_totals = {
            "total_minutes": next_plan_total_min,
            "total_minutes_str": format_duration_total(next_plan_total_min),
            "total_tss": next_plan_total_tss,
        }
        ia_plans_source = "algo"
    else:
        print("✅  Plans théoriques IA chargés depuis data/weekly_plans.json.")
        # Recalcule weeks_to_race / phase depuis le profil
        _, weeks_to_race, phase = build_weekly_plan(today, form, session_profile,
                                                     thresholds, race_date)
        # Marque chaque séance comme générée par IA
        ideal_week_plan = [{**p, "generated_by_ia": True} for p in ideal_week_plan]
        next_week_plan  = [{**p, "generated_by_ia": True} for p in next_week_plan]
        ia_plans_source = "ia"

    # ── Plan adaptatif (semaine en cours) — généré par IA chaque jour ────────
    # Tente la génération IA ; fallback algo si API indisponible.
    snapshot_meta = {"ctl": ctl, "atl": atl, "tsb": tsb}
    ia_adaptive_days, week_adaptations = generate_adaptive_plan_ia(
        today, ideal_week_plan, activities, form, thresholds, athlete_profile, snapshot_meta
    )

    if ia_adaptive_days:
        plan = [{**p, "generated_by_ia": True} for p in ia_adaptive_days]
        ia_adaptive_source = "ia"
    else:
        # Fallback algorithmique
        ideal_plan_raw_for_adapt = [
            {**p, "status": "todo"} for p in ideal_week_plan
        ] if ia_plans_loaded else list(ideal_plan_raw)  # type: ignore[possibly-undefined]
        plan = match_activities_to_plan(ideal_plan_raw_for_adapt, activities)
        plan_total_tss_target = sum(p.get("tss_estimate", 0) for p in plan)
        plan, week_adaptations = adapt_plan_to_week(plan, atl, ctl, plan_total_tss_target)
        plan = [{**p, "generated_by_ia": False} for p in plan]
        ia_adaptive_source = "algo"

    # Totaux du plan adaptatif
    plan_total_min = sum(p.get("duration_min", 0) for p in plan)
    plan_total_tss = sum(p.get("tss_estimate", 0) for p in plan)
    plan_totals = {
        "total_minutes": plan_total_min,
        "total_minutes_str": format_duration_total(plan_total_min),
        "total_tss": plan_total_tss,
    }

    week_sunday = week_monday + timedelta(days=6)

    # TSS réalisé par semaine depuis le début de saison (pour la frise)
    season_weekly_tss: dict[str, float] = {}
    _s = season_start
    while _s <= today:
        _week_end = _s + timedelta(days=6)
        _tss = sum(
            (a.get("icu_training_load") or 0)
            for a in activities
            if _s.isoformat() <= a.get("start_date_local", "")[:10] <= _week_end.isoformat()
        )
        # Semaine en cours : TSS partiel (pas terminée)
        season_weekly_tss[_s.isoformat()] = round(_tss, 1)
        _s += timedelta(days=7)

    # Historique PMC 90j pour le graphique CTL/ATL/TSB
    pmc_history = [
        {
            "date": w["id"],
            "ctl": round(w["ctl"], 2) if w.get("ctl") is not None else None,
            "atl": round(w["atl"], 2) if w.get("atl") is not None else None,
            "tsb": round(w["ctl"] - w["atl"], 2)
                   if w.get("ctl") is not None and w.get("atl") is not None else None,
        }
        for w in wellness_90d
        if w.get("ctl") is not None
    ]

    snapshot = {
        "today": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "week_monday": week_monday.isoformat(),
        "week_sunday": week_sunday.isoformat(),
        "form": form,
        "metrics": metrics,
        "thresholds": thresholds,
        "pmc_history": pmc_history,
        "load_7d": {"total_load": load_7["total_load"], "total_minutes": load_7["total_minutes"],
                    "by_sport": load_7["by_sport"]},
        "load_28d": {"total_load": load_28["total_load"], "total_minutes": load_28["total_minutes"],
                     "by_sport": load_28["by_sport"]},
        "session_profile": session_profile,
        # Plan théorique idéal (statique, référence de semaine)
        "ideal_week_plan": ideal_week_plan,
        "ideal_week_plan_totals": ideal_week_plan_totals,
        "next_week_monday": next_week_monday.isoformat(),
        "next_week_sunday": next_week_sunday.isoformat(),
        "next_week_plan": next_week_plan,
        "next_week_plan_totals": next_week_plan_totals,
        # Plan adaptatif (semaine en cours, avec suivi activités réelles)
        "weekly_plan": plan,
        "weekly_plan_totals": plan_totals,
        "week_adaptations": week_adaptations,
        "ia_plans_source": ia_plans_source,
        "ia_adaptive_source": ia_adaptive_source,
        "weeks_to_race": weeks_to_race,
        "phase": phase,
        "race_name": _race_json.get("name") or _race_yaml.get("name"),
        "race_date": _race_date_str,
        "season_weekly_tss": season_weekly_tss,
        "athlete_profile": athlete_profile,
    }

    # Message coach généré après le snapshot complet (a besoin du plan enrichi)
    snapshot["coach_message"] = generate_coach_message(snapshot)

    # Sortie : data/today.json (public, chargé par index.html) + cache local
    today_json_path = build_github_pages_dashboard(snapshot)

    cache_path = CACHE_DIR / "today.json"
    cache_path.write_text(json.dumps(snapshot, indent=2, default=str))

    print(f"Données du jour : {today_json_path}")
    print(f"Cache local     : {cache_path}")
    return snapshot


if __name__ == "__main__":
    run()
