"""session_builder.py — Calcul algorithmique des séances à partir des blocs IA.

Claude retourne des blocs structurés avec des pourcentages d'intensité.
Ce module calcule :
  - Les allures réelles (min/km) et watts à partir de % FTP / % seuil
  - La durée totale (warmup + blocs + récups + cooldown)
  - Le TSS estimé
  - Le texte formaté final de la séance (champ `structure`)

Utilisé par generate_plans.py et daily_coach.py après réception de la réponse Claude.
"""

from __future__ import annotations

import math
from typing import Any


# ── Constantes warmup/cooldown (utilisées si le profil n'est pas fourni) ──────

DEFAULT_WARMUP = {
    "run_min": 20,           # 10' trot Z1 + 5' gammes + 5' accélérations
    "run_content": "10' trot Z1 progressif + 5' gammes (talons-fesses, montées genoux, foulées bondissantes) + 5' accélérations progressives",
    "bike_min": 15,          # 15' progressif Z1→Z2 + 3×30s à 100rpm
    "bike_content": "15' progressif Z1→Z2 + 3×30s à 100rpm",
    "swim_m": 400,
    "swim_content": "400m nage souple",
}
DEFAULT_COOLDOWN = {
    "run_min": 5,
    "run_content": "5' trot léger Z1",
    "bike_min": 5,
    "bike_content": "5' Z1 (<55% FTP, cadence souple)",
    "swim_m": 200,
    "swim_content": "200m nage souple",
}


# ── Helpers de conversion ─────────────────────────────────────────────────────

def mps_to_minkm(mps: float) -> str:
    """Convertit m/s en chaîne min:ss/km. Ex : 3.41 → '4:53/km'."""
    if mps <= 0:
        return "—"
    total_s = round(1000 / mps)
    return f"{total_s // 60}:{total_s % 60:02d}/km"


def pace_factor_to_str(thr_mps: float, factor: float) -> str:
    """Retourne l'allure pour (thr_mps × factor) en min:ss/km."""
    return mps_to_minkm(thr_mps * factor)


def watts(ftp: int, pct: float) -> int:
    """Retourne les watts pour un % du FTP, arrondi à 5W."""
    return int(round(ftp * pct / 5) * 5)


def swim_pace_factor_to_str(css_mps: float, factor: float) -> str:
    """Retourne l'allure natation pour (css_mps × factor) en min:ss/100m."""
    if css_mps <= 0:
        return "—"
    mps = css_mps * factor
    total_s = round(100 / mps)
    return f"{total_s // 60}:{total_s % 60:02d}/100m"


# ── Calcul TSS ────────────────────────────────────────────────────────────────

def tss_run(duration_min: float, avg_intensity_factor: float) -> int:
    """TSS CAP = (durée_h × IF² × 100), IF = allure_réelle / seuil."""
    return int(round(duration_min / 60 * avg_intensity_factor ** 2 * 100))


def tss_bike(duration_min: float, avg_pct_ftp: float) -> int:
    """TSS vélo = (durée_h × NP/FTP² × 100)."""
    return int(round(duration_min / 60 * avg_pct_ftp ** 2 * 100))


def tss_swim(duration_min: float, avg_intensity_factor: float) -> int:
    """TSS natation (approximation) = (durée_h × IF² × 100)."""
    return int(round(duration_min / 60 * avg_intensity_factor ** 2 * 100))


# ── Parser de blocs IA ────────────────────────────────────────────────────────

def parse_blocks(blocks: list[dict], sport: str, thresholds: dict) -> dict:
    """Calcule durée totale, allures/watts et texte depuis les blocs IA.

    Chaque bloc IA a la forme :
      {"type": "interval|endurance|recovery|strength_exercise",
       "duration_min": 6,
       "reps": 3,           # optionnel, pour les séries
       "recovery_min": 3,   # repos entre séries
       "intensity_pct": 95, # % du seuil (run) ou % FTP (bike) ou % CSS (swim)
       "zone": "Z4",
       "description": "Texte libre court (exercice de renfo, gammes…)"}

    Retourne {"duration_min": int, "structure": str, "avg_intensity_factor": float,
              "tss_estimate": int, "zones": str}
    """
    ftp = thresholds.get("ftp_watts", 250)
    thr_run_mps = thresholds.get("threshold_pace_run_mps", 3.41)
    css_mps = thresholds.get("threshold_pace_swim_mps", 0.833)  # 2:00/100m ≈ 0.833 m/s

    total_min = 0.0
    parts = []
    weighted_if_sq_x_min = 0.0  # pour TSS
    zones_seen: set[str] = set()

    for b in blocks:
        btype = b.get("type", "endurance")
        dur = float(b.get("duration_min", 0))
        reps = int(b.get("reps", 1))
        rec = float(b.get("recovery_min", 0))
        pct = float(b.get("intensity_pct", 70)) / 100.0
        zone = b.get("zone", "")
        desc = b.get("description", "")

        if zone:
            zones_seen.add(zone)

        # Durée réelle du bloc (répétitions + récupérations)
        block_active_min = dur * reps
        block_rec_min = rec * max(0, reps - 1)
        block_total_min = block_active_min + block_rec_min
        total_min += block_total_min

        # Texte du bloc
        if sport == "Run":
            pace = pace_factor_to_str(thr_run_mps, pct)
            if reps > 1:
                rec_txt = f"récup {int(rec)}' trot Z1" if rec > 0 else ""
                parts.append(f"{reps}×{int(dur)}' à {pace} ({zone})" + (f" — {rec_txt}" if rec_txt else ""))
            else:
                parts.append(desc or f"{int(dur)}' à {pace} ({zone})")
            if_val = pct
        elif sport in ("VirtualRide", "Ride"):
            w = watts(ftp, pct)
            if reps > 1:
                rec_txt = f"récup {int(rec)}' Z1" if rec > 0 else ""
                parts.append(f"{reps}×{int(dur)}' à {w}W / {int(pct*100)}% FTP ({zone})" + (f" — {rec_txt}" if rec_txt else ""))
            else:
                parts.append(desc or f"{int(dur)}' à {w}W / {int(pct*100)}% FTP ({zone})")
            if_val = pct
        elif sport == "Swim":
            p = swim_pace_factor_to_str(css_mps, pct)
            if reps > 1:
                rec_txt = f"récup {int(rec)}' nage souple" if rec > 0 else ""
                parts.append(f"{reps}×{int(dur)}' à {p} ({zone})" + (f" — {rec_txt}" if rec_txt else ""))
            else:
                parts.append(desc or f"{int(dur)}' à {p} ({zone})")
            if_val = pct
        else:
            # Strength / Brick / autre — texte libre
            parts.append(desc or f"{int(dur)}'")
            if_val = pct

        # Contribution au TSS (activité + récup à faible intensité)
        weighted_if_sq_x_min += block_active_min * (if_val ** 2)
        weighted_if_sq_x_min += block_rec_min * (0.55 ** 2)  # récup ≈ Z1 55%

    # Durée totale + TSS
    avg_if_sq = weighted_if_sq_x_min / total_min if total_min > 0 else 0.5 ** 2
    avg_if = math.sqrt(avg_if_sq)

    if sport == "Run":
        tss = tss_run(total_min, avg_if)
    elif sport in ("VirtualRide", "Ride"):
        tss = tss_bike(total_min, avg_if)
    elif sport == "Swim":
        tss = tss_swim(total_min, avg_if)
    else:
        tss = 0

    zones_str = " + ".join(sorted(zones_seen)) if zones_seen else "—"
    structure = " | ".join(parts)

    return {
        "duration_min": int(round(total_min)),
        "structure": structure,
        "avg_intensity_factor": round(avg_if, 3),
        "tss_estimate": tss,
        "zones": zones_str,
    }


# ── Fonction principale ───────────────────────────────────────────────────────

def compute_session(day: dict, thresholds: dict, wu_profile: dict | None = None) -> dict:
    """Enrichit un jour de plan IA avec les valeurs calculées algorithmiquement.

    Entrée (champs fournis par Claude) :
      sport, type, blocks, rationale, coach_note (optionnel)

    Sortie (champs ajoutés/remplacés) :
      structure, duration_min, tss_estimate, zones

    Les jours Repos et Renforcement sont passés tels quels (tss=0).
    """
    sport = day.get("sport", "Repos")

    if sport in ("Repos", "Strength", None):
        day.setdefault("duration_min", 0)
        day.setdefault("tss_estimate", 0)
        day.setdefault("zones", "—")
        # Pour le renforcement, garde la structure textuelle libre fournie par Claude
        if sport == "Strength" and not day.get("structure"):
            blocks = day.get("blocks", [])
            day["structure"] = " | ".join(
                b.get("description", "") for b in blocks if b.get("description")
            )
        return day

    wu = wu_profile or {}
    blocks = day.get("blocks", [])

    # Warmup
    if sport == "Run":
        wu_min = wu.get("warmup_run_min", DEFAULT_WARMUP["run_min"])
        wu_txt = wu.get("warmup_run_content", DEFAULT_WARMUP["run_content"])
        cd_min = wu.get("cooldown_run_min", DEFAULT_COOLDOWN["run_min"])
        cd_txt = wu.get("cooldown_run_content", DEFAULT_COOLDOWN["run_content"])
    elif sport in ("VirtualRide", "Ride"):
        wu_min = wu.get("warmup_bike_min", DEFAULT_WARMUP["bike_min"])
        wu_txt = wu.get("warmup_bike_content", DEFAULT_WARMUP["bike_content"])
        cd_min = wu.get("cooldown_bike_min", DEFAULT_COOLDOWN["bike_min"])
        cd_txt = wu.get("cooldown_bike_content", DEFAULT_COOLDOWN["bike_content"])
    elif sport == "Swim":
        wu_min = 0   # natation : warmup en mètres, pas en minutes — géré dans les blocs
        wu_txt = ""
        cd_min = 0
        cd_txt = ""
    else:  # Brick
        wu_min = wu.get("warmup_bike_min", DEFAULT_WARMUP["bike_min"])
        wu_txt = wu.get("warmup_bike_content", DEFAULT_WARMUP["bike_content"])
        cd_min = wu.get("cooldown_run_min", DEFAULT_COOLDOWN["run_min"])
        cd_txt = wu.get("cooldown_run_content", DEFAULT_COOLDOWN["run_content"])

    # Calcul du corps de séance
    body = parse_blocks(blocks, sport, thresholds)

    # Durée totale = warmup + corps + cooldown
    total_min = wu_min + body["duration_min"] + cd_min

    # Structure textuelle complète
    parts = []
    if wu_txt:
        parts.append(f"Échauffement : {wu_txt}")
    if body["structure"]:
        parts.append(f"Corps : {body['structure']}")
    if cd_txt:
        parts.append(f"Retour au calme : {cd_txt}")
    structure = " | ".join(parts)

    # TSS recalculé sur la durée totale (warmup/cooldown à ~Z1 = 55%)
    ftp = thresholds.get("ftp_watts", 250)
    thr_run_mps = thresholds.get("threshold_pace_run_mps", 3.41)
    css_mps = thresholds.get("threshold_pace_swim_mps", 0.833)

    body_if = body["avg_intensity_factor"]
    wu_cd_if = 0.55
    total_if_sq = (
        wu_min * wu_cd_if**2
        + body["duration_min"] * body_if**2
        + cd_min * wu_cd_if**2
    ) / total_min if total_min > 0 else 0.3
    avg_if = math.sqrt(total_if_sq)

    if sport == "Run":
        tss = tss_run(total_min, avg_if)
    elif sport in ("VirtualRide", "Ride"):
        tss = tss_bike(total_min, avg_if)
    elif sport == "Swim":
        tss = tss_swim(total_min, avg_if)
    else:
        tss = body["tss_estimate"]  # Brick : TSS depuis les blocs

    day["duration_min"] = total_min
    day["structure"] = structure
    day["tss_estimate"] = tss
    day["zones"] = body["zones"]

    return day
