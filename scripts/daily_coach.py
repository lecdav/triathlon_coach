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

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "daily"
CACHE_DIR = ROOT / "data" / "cache"
PROFILE_PATH = ROOT / "config" / "athlete_profile.yaml"
DASHBOARD_TEMPLATE = SCRIPT_DIR / "dashboard_template.html"
DASHBOARD_OUTPUT = ROOT / "reports" / "dashboard.html"

REPORT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Chemin vers le dashboard public GitHub Pages
GITHUB_PAGES_DASHBOARD = ROOT / "index.html"


def build_github_pages_dashboard(snapshot: dict) -> Path | None:
    """Met à jour index.html (GitHub Pages) en injectant le snapshot JSON.

    Ce fichier est la version web publique du dashboard, conçue pour
    fonctionner comme page statique sur GitHub Pages.
    """
    if not GITHUB_PAGES_DASHBOARD.exists():
        print(f"⚠️  index.html absent ({GITHUB_PAGES_DASHBOARD}) — GitHub Pages non mis à jour.")
        return None
    html = GITHUB_PAGES_DASHBOARD.read_text(encoding="utf-8")
    snapshot_json = json.dumps(snapshot, indent=2, ensure_ascii=False, default=str)
    # Remplace le contenu entre les balises <script type="application/json" id="snapshot">
    import re
    pattern = r'(<script type="application/json" id="snapshot">)\s*\{[^<]*\}\s*(</script>)'
    replacement = r'\g<1>\n' + snapshot_json + r'\n\2'
    new_html, count = re.subn(pattern, replacement, html, flags=re.DOTALL)
    if count == 0:
        print("⚠️  Balise snapshot introuvable dans index.html — mise à jour ignorée.")
        return None
    GITHUB_PAGES_DASHBOARD.write_text(new_html, encoding="utf-8")
    return GITHUB_PAGES_DASHBOARD


def git_push_dashboard() -> bool:
    """Push désactivé — maintenant géré par GitHub Actions (.github/workflows/daily_coach.yml).

    Le workflow CI commit et pousse index.html automatiquement après chaque run.
    Cette fonction est conservée pour rétrocompatibilité mais ne fait plus rien.
    """
    print("ℹ️  git push local désactivé — GitHub Actions s'en charge.")
    return True


def build_dashboard_html(snapshot: dict) -> Path:
    """Injecte le snapshot JSON dans le template HTML et écrit
    reports/dashboard.html — version autonome, lisible hors-ligne et
    réutilisable comme contenu de l'artifact Cowork triathlon-dashboard.
    """
    if not DASHBOARD_TEMPLATE.exists():
        # Ne casse pas le pipeline si le template manque, mais avertit.
        print(f"⚠️  Template absent ({DASHBOARD_TEMPLATE}) — dashboard.html non généré.")
        return None
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    # ensure_ascii=False pour garder les accents/emoji lisibles dans le HTML.
    snapshot_json = json.dumps(snapshot, indent=2, ensure_ascii=False, default=str)
    html = template.replace("__SNAPSHOT_JSON__", snapshot_json)
    DASHBOARD_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_OUTPUT.write_text(html, encoding="utf-8")
    return DASHBOARD_OUTPUT


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
                            structure=f"Footing continu en Z1-Z2 ({p_run_easy}).",
                            zones="Z1-Z2 — FC <80% LTHR",
                            rationale="Volume sans intensité, ATL trop élevé.")
            else:
                item.update(sport="Run", type="VO2max — intervalles courts",
                            duration_min=max(avg_run["avg_minutes"], 45),
                            structure=f"15' échauffement + 6 à 8 × 3' à allure 5km ({p_run_z4}) "
                                      f"r=2' trot + 10' retour calme.",
                            zones=f"Z5 — FC 92-97% FCmax",
                            rationale="Bloc 80/20 : la séance dure du haut du spectre, "
                                      "stimulation VO2max (Helgerud 2007).")
        elif weekday == "wednesday":
            # 1 seule séance natation par semaine, ~50 min, le mercredi
            item.update(sport="Swim", type="Endurance + technique",
                        duration_min=50,
                        structure=f"400 échauf. + 10×100 ({p_swim_thr}) r=20s + 200 souple. "
                                  f"Total ~2000 m.",
                        zones=f"Z3-Z4 (CSS = {p_swim_thr})",
                        rationale="Séance natation hebdomadaire unique (~50 min). "
                                  "CSS bloc principal pour maintenir la technique et l'endurance spécifique.")
        elif weekday == "thursday":
            # Vélo seuil sur HT
            if deload:
                item.update(sport="VirtualRide", type="Endurance",
                            duration_min=avg_bike["avg_minutes"],
                            structure=f"Z2 continu {z2_low}-{z2_high} W (~{int(avg_bike['avg_minutes'])}').",
                            zones=f"Z2 — {z2_low}-{z2_high} W",
                            rationale="Volume aérobie pur, pas de stress neuromusculaire.")
            else:
                item.update(sport="VirtualRide", type="Seuil — sweet spot",
                            duration_min=max(avg_bike["avg_minutes"], 60),
                            structure=f"15' échauf. + 3×12' à {z4_low}-{ftp} W r=4' + 8' calme.",
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
                            structure=f"Vélo {bike_brick_min}' Z2 dont 2×8' tempo Z3 → "
                                      f"transition rapide → CAP {run_brick_min}' Z2 ({p_run_easy}).",
                            zones="Vélo Z2-Z3 / CAP Z2",
                            rationale="Brick dominical : adaptation neuromusculaire à la transition "
                                      "vélo→CAP, spécifique triathlon M (Hausswirth 2010).")
            else:
                # Phase de base : alterner sortie longue CAP et vélo selon le numéro de semaine ISO.
                # Semaine paire → longue vélo (compense le manque de volume vélo en semaine)
                # Semaine impaire → longue CAP (fondation aérobie et endurance course à pied)
                iso_week = week_monday.isocalendar()[1]
                # Comptage des TSS vélo vs CAP sur 7j pour affiner : si l'un est < 60% de l'autre, on le privilégie
                run_load_7d = sum(
                    a.get("icu_training_load", 0) for a in []  # activités non disponibles ici, fallback sur semaine ISO
                )
                if iso_week % 2 == 0:
                    item.update(sport="VirtualRide", type="Sortie longue vélo",
                                duration_min=int(long_bike_min),
                                structure=f"Vélo {int(long_bike_min)}' continu Z2 ({z2_low}-{z2_high} W). "
                                          f"Cadence 85-90 rpm, sans pic d'intensité.",
                                zones=f"Z2 — {z2_low}-{z2_high} W",
                                rationale="Sortie longue vélo dominicale (semaine paire) — volume aérobie "
                                          "spécifique triathlon, développement du moteur lipidique à vélo.")
                else:
                    item.update(sport="Run", type="Sortie longue CAP",
                                duration_min=long_run_min,
                                structure=f"{long_run_min}' continu en Z2 ({p_run_easy}). "
                                          f"Optionnel : derniers 10' à allure marathon ({p_run_thr}).",
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
    1. "done_exact" → activité du bon sport ce jour-là
    2. "done_any"   → n'importe quelle activité sportive ce jour-là (sport décalé)
    3. Jours passés sans aucune activité → "past_missed"
    4. Jour courant sans activité → "today"
    5. Futur → "todo"

    Note : on accepte qu'un athlète fasse le Run planifié un autre jour ou
    intervertisse deux séances dans la semaine — l'important est qu'une
    séance ait bien eu lieu.
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

    for item in plan:
        d = date.fromisoformat(item["date"])
        day_acts = acts_by_date.get(item["date"], [])
        planned_sport = item.get("sport") or ""
        compatible_types = SPORT_MATCH.get(planned_sport, set())

        # Activités du jour compatibles avec le sport planifié (match exact)
        exact_match = [a for a in day_acts if a.get("type") in compatible_types]
        # Toutes activités sportives du jour (match souple — sport interverti)
        any_sport = [a for a in day_acts if a.get("type") not in {"", None}
                     and a.get("icu_training_load", 0) > 0]

        if planned_sport == "Repos":
            # Repos : toujours considéré fait
            item["status"] = "done"
            item["actual_activities"] = []
        elif exact_match:
            # Bon sport, bon jour ✅
            item["status"] = "done"
            item["sport_match"] = "exact"
            item["actual_activities"] = [fmt_activity(a) for a in exact_match]
        elif any_sport and d <= today:
            # Une séance a bien été réalisée ce jour, mais sport différent du plan
            item["status"] = "done"
            item["sport_match"] = "approximate"
            item["actual_activities"] = [fmt_activity(a) for a in any_sport]
        elif d == today:
            item["status"] = "today"
            item["actual_activities"] = []
        elif d < today:
            item["status"] = "past_missed"
            item["actual_activities"] = []
        else:
            item["status"] = "todo"
            item["actual_activities"] = []

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
    elif missed_key and free_slots:
        missed = missed_key[0]   # on récupère la première séance clé manquée
        slot = free_slots[0]     # sur le premier jour de repos à venir

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

    return plan, adaptations


# ---------- Message coach motivationnel ----------

def generate_coach_message(snapshot: dict) -> str:
    """Génère un texte coach personnalisé embarqué dans le snapshot.

    Structure :
      1. Accroche sur la forme du jour
      2. Bilan du travail réalisé cette semaine (séances faites/manquées)
      3. Points forts identifiés sur les 4 dernières semaines
      4. Axes de progression + comment on va s'y prendre
      5. Positionnement dans le macro-plan (phase, semaines restantes)
    """
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

    # Profil athlète YAML
    try:
        import yaml  # type: ignore
        prof_yaml = yaml.safe_load(PROFILE_PATH.read_text()) if PROFILE_PATH.exists() else {}
    except Exception:
        prof_yaml = {}

    race = (prof_yaml or {}).get("race", {})
    race_date = None
    if race.get("date"):
        try:
            race_date = date.fromisoformat(race["date"])
        except ValueError:
            race_date = None

    # Données API
    thresholds = client.get_thresholds()
    # 90j pour le graphique PMC ; 14j suffisent pour les métriques du jour
    wellness_90d = client.wellness(today - timedelta(days=90))
    wellness = [w for w in wellness_90d
                if w.get("id", "") >= (today - timedelta(days=14)).isoformat()]
    # Récupère 42j pour CTL/profil + toute la semaine courante pour le croisement
    week_monday = today - timedelta(days=today.weekday())
    oldest_fetch = min(today - timedelta(days=42), week_monday)
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

    plan, weeks_to_race, phase = build_weekly_plan(today, form, session_profile,
                                                    thresholds, race_date)

    # Croisement plan ↔ activités réellement effectuées cette semaine
    plan = match_activities_to_plan(plan, activities)

    # Adaptation dynamique : ajuste les séances futures selon la charge réalisée
    plan_total_tss_target = sum(p.get("tss_estimate", 0) for p in plan)
    plan, week_adaptations = adapt_plan_to_week(plan, atl, ctl, plan_total_tss_target)

    # Totaux du plan hebdo (pour la ligne "Total semaine")
    plan_total_min = sum(p.get("duration_min", 0) for p in plan)
    plan_total_tss = sum(p.get("tss_estimate", 0) for p in plan)
    plan_totals = {
        "total_minutes": plan_total_min,
        "total_minutes_str": format_duration_total(plan_total_min),
        "total_tss": plan_total_tss,
    }

    week_sunday = week_monday + timedelta(days=6)

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
        "weekly_plan": plan,
        "weekly_plan_totals": plan_totals,
        "week_adaptations": week_adaptations,
        "weeks_to_race": weeks_to_race,
        "phase": phase,
        "race_name": race.get("name"),
        "race_date": race.get("date"),
    }

    # Message coach généré après le snapshot complet (a besoin du plan enrichi)
    snapshot["coach_message"] = generate_coach_message(snapshot)

    # Sortie : Markdown + JSON cache
    md = render_markdown(snapshot)
    md_path = REPORT_DIR / f"{today.isoformat()}.md"
    md_path.write_text(md)

    cache_path = CACHE_DIR / "today.json"
    cache_path.write_text(json.dumps(snapshot, indent=2, default=str))

    # Dashboard HTML autonome (snapshot embarqué) — sera poussé dans
    # l'artifact Cowork par la tâche planifiée.
    dashboard_path = build_dashboard_html(snapshot)

    # Dashboard GitHub Pages — met à jour index.html et pousse sur GitHub
    gh_pages_path = build_github_pages_dashboard(snapshot)

    print(md)
    print(f"\n---\nRapport sauvegardé : {md_path}")
    print(f"Cache JSON : {cache_path}")
    if dashboard_path:
        print(f"Dashboard Cowork : {dashboard_path}")
    if gh_pages_path:
        print(f"Dashboard GitHub Pages : {gh_pages_path}")
        git_push_dashboard()
    return snapshot


if __name__ == "__main__":
    run()
