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
    if sport == "Repos" or duration_min <= 0:
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
      - Dimanche = sortie longue
      - 80% endurance Z1-Z2, 20% intensité (1 séance VO2/seuil + 1 fartlek/intervalles)
      - Brick (vélo+CAP) le samedi
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
            if "Swim" in profile:
                item.update(sport="Swim", type="Endurance + technique",
                            duration_min=avg_swim["avg_minutes"],
                            structure=f"400 échauf. + 8×100 ({p_swim_thr}) r=20s + 200 souple. "
                                      f"Total ~{avg_swim['avg_distance_km']*1000:.0f} m.",
                            zones=f"Z3-Z4 (CSS = {p_swim_thr})",
                            rationale="Densifier la fréquence natation : tu es à 1 séance/sem "
                                      "vs 2-3 cibles. CSS bloc principal.")
            else:
                item.update(sport="Swim", type="Endurance",
                            duration_min=40,
                            structure=f"30 min nage continue confortable ({p_swim_easy}).",
                            zones="Z2",
                            rationale="Construire le volume natation.")
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
            # Récup active OU 2e nat
            item.update(sport="Swim", type="Technique + tolérance lactate",
                        duration_min=40,
                        structure="400 éducatifs + 6×50 vite/50 souple + 200 retour calme.",
                        zones="Z1 + sprints",
                        rationale="2e séance natation pour atteindre 2/sem. "
                                  "Charge basse — vendredi = pré-week-end.")
        elif weekday == "saturday":
            # Brick : vélo + CAP enchaînés
            bike_min = max(avg_bike["avg_minutes"], 75)
            run_min = 20
            item.update(sport="Brick (Bike+Run)", type="Spécifique triathlon",
                        duration_min=bike_min + run_min,
                        structure=f"Vélo {bike_min}' Z2 dont 2×8' tempo Z3 → "
                                  f"transition rapide → CAP {run_min}' Z2 ({p_run_easy}).",
                        zones=f"Vélo Z2-Z3 / CAP Z2",
                        rationale="Brick hebdo obligatoire pour adapter la transition "
                                  "vélo→CAP (jambes lourdes). 1 brick/sem en build.")
        elif weekday == "sunday":
            # Sortie longue
            long_min = max(avg_run["avg_minutes"] * 1.6, 60)
            item.update(sport="Run", type="Sortie longue",
                        duration_min=int(long_min),
                        structure=f"{int(long_min)}' continu en Z2 ({p_run_easy}). "
                                  f"Optionnel : derniers 10' à allure marathon.",
                        zones="Z2 — FC 75-85% LTHR",
                        rationale="Sortie longue dimanche (profil) — "
                                  "fondation aérobie, oxydation lipidique.")
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
    "Repos": set(),
}


def match_activities_to_plan(plan: list[dict], activities: list[dict]) -> list[dict]:
    """Croise les activités téléchargées avec le plan de la semaine.

    Règles :
    - "done" : une activité du bon sport a été enregistrée le même jour
    - "today" : c'est le jour courant, pas encore d'activité correspondante
    - "todo" : jour futur sans activité
    - "past_missed" : jour passé sans activité (sport non Repos)
    Les activités réelles sont embarquées dans `actual_activities`.
    """
    today = date.today()
    # Index activités par date
    acts_by_date: dict[str, list[dict]] = defaultdict(list)
    for a in activities:
        d_str = a.get("start_date_local", "")[:10]
        if d_str:
            acts_by_date[d_str].append(a)

    for item in plan:
        d = date.fromisoformat(item["date"])
        day_acts = acts_by_date.get(item["date"], [])
        planned_sport = item.get("sport") or ""
        compatible_types = SPORT_MATCH.get(planned_sport, set())

        # Activités du jour compatibles avec le sport planifié
        matching = [a for a in day_acts if a.get("type") in compatible_types]

        if planned_sport == "Repos":
            # Repos : toujours considéré fait (sauf si activité intensive ce jour)
            heavy = [a for a in day_acts if a.get("icu_training_load", 0) > 30]
            item["status"] = "done" if not heavy else "done"  # repos = ok
            item["actual_activities"] = []
        elif matching:
            item["status"] = "done"
            item["actual_activities"] = [
                {
                    "name": a.get("name"),
                    "type": a.get("type"),
                    "duration_min": round((a.get("moving_time") or 0) / 60),
                    "distance_km": round((a.get("distance") or 0) / 1000, 1),
                    "tss": a.get("icu_training_load"),
                }
                for a in matching
            ]
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
    wellness = client.wellness(today - timedelta(days=14))
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

    # Totaux du plan hebdo (pour la ligne "Total semaine")
    plan_total_min = sum(p.get("duration_min", 0) for p in plan)
    plan_total_tss = sum(p.get("tss_estimate", 0) for p in plan)
    plan_totals = {
        "total_minutes": plan_total_min,
        "total_minutes_str": format_duration_total(plan_total_min),
        "total_tss": plan_total_tss,
    }

    week_sunday = week_monday + timedelta(days=6)

    snapshot = {
        "today": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "week_monday": week_monday.isoformat(),
        "week_sunday": week_sunday.isoformat(),
        "form": form,
        "metrics": metrics,
        "thresholds": thresholds,
        "load_7d": {"total_load": load_7["total_load"], "total_minutes": load_7["total_minutes"],
                    "by_sport": load_7["by_sport"]},
        "load_28d": {"total_load": load_28["total_load"], "total_minutes": load_28["total_minutes"],
                     "by_sport": load_28["by_sport"]},
        "session_profile": session_profile,
        "weekly_plan": plan,
        "weekly_plan_totals": plan_totals,
        "weeks_to_race": weeks_to_race,
        "phase": phase,
        "race_name": race.get("name"),
        "race_date": race.get("date"),
    }

    # Sortie : Markdown + JSON cache
    md = render_markdown(snapshot)
    md_path = REPORT_DIR / f"{today.isoformat()}.md"
    md_path.write_text(md)

    cache_path = CACHE_DIR / "today.json"
    cache_path.write_text(json.dumps(snapshot, indent=2, default=str))

    # Dashboard HTML autonome (snapshot embarqué) — sera poussé dans
    # l'artifact Cowork par la tâche planifiée.
    dashboard_path = build_dashboard_html(snapshot)

    print(md)
    print(f"\n---\nRapport sauvegardé : {md_path}")
    print(f"Cache JSON : {cache_path}")
    if dashboard_path:
        print(f"Dashboard HTML : {dashboard_path}")
    return snapshot


if __name__ == "__main__":
    run()
