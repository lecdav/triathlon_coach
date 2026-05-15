"""Client minimaliste pour l'API Intervals.icu.

Doc API : https://intervals.icu/api-docs.html
Auth : HTTP Basic — username = "API_KEY" (litéral), password = la clé API.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.env"
API_BASE = "https://intervals.icu/api/v1"


def load_credentials() -> tuple[str, str]:
    """Lit athlete_id et api_key.

    Priorité :
      1. Variables d'environnement INTERVALS_ATHLETE_ID / INTERVALS_API_KEY
         (utilisées par GitHub Actions via les Secrets du repo)
      2. Fichier config/credentials.env (usage local, jamais commité)
    """
    # 1. Variables d'environnement (GitHub Actions Secrets)
    env_id = os.environ.get("INTERVALS_ATHLETE_ID")
    env_key = os.environ.get("INTERVALS_API_KEY")
    if env_id and env_key:
        return env_id, env_key

    # 2. Fichier local credentials.env
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Credentials introuvables : ni variables d'environnement "
            f"INTERVALS_ATHLETE_ID / INTERVALS_API_KEY, ni fichier {CREDENTIALS_FILE}."
        )
    creds: dict[str, str] = {}
    for line in CREDENTIALS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip()] = v.strip()
    try:
        return creds["INTERVALS_ATHLETE_ID"], creds["INTERVALS_API_KEY"]
    except KeyError as e:
        raise KeyError(f"Clé manquante dans credentials.env : {e}") from e


class IntervalsClient:
    def __init__(self, athlete_id: str | None = None, api_key: str | None = None):
        if athlete_id is None or api_key is None:
            athlete_id, api_key = load_credentials()
        self.athlete_id = athlete_id
        self.session = requests.Session()
        self.session.auth = ("API_KEY", api_key)
        self.session.headers.update({"Accept": "application/json"})

    # ---- Helpers ----
    def _get(self, path: str, **params) -> Any:
        url = f"{API_BASE}{path}"
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # ---- Endpoints ----
    def athlete(self) -> dict:
        """Profil athlète (FTP, seuils, zones, etc.)."""
        return self._get(f"/athlete/{self.athlete_id}")

    def activities(self, oldest: date, newest: date | None = None) -> list[dict]:
        """Liste des activités entre oldest et newest (incl.)."""
        if newest is None:
            newest = date.today()
        return self._get(
            f"/athlete/{self.athlete_id}/activities",
            oldest=oldest.isoformat(),
            newest=newest.isoformat(),
        )

    def activity_detail(self, activity_id: str) -> dict:
        return self._get(f"/activity/{activity_id}")

    def wellness(self, oldest: date, newest: date | None = None) -> list[dict]:
        """Données wellness (sommeil, HRV, FC repos, charge perçue, etc.)."""
        if newest is None:
            newest = date.today()
        return self._get(
            f"/athlete/{self.athlete_id}/wellness",
            oldest=oldest.isoformat(),
            newest=newest.isoformat(),
        )

    def events(self, oldest: date, newest: date) -> list[dict]:
        """Événements du calendrier (séances planifiées, courses, notes)."""
        return self._get(
            f"/athlete/{self.athlete_id}/events",
            oldest=oldest.isoformat(),
            newest=newest.isoformat(),
        )

    def fitness(self, oldest: date, newest: date | None = None) -> list[dict]:
        """Évolution CTL/ATL/TSB jour par jour."""
        if newest is None:
            newest = date.today()
        # endpoint wellness contient déjà ctl/atl, mais on peut aussi récupérer fitness séparément
        wellness_data = self.wellness(oldest, newest)
        return [
            {
                "date": w.get("id"),
                "ctl": w.get("ctl"),
                "atl": w.get("atl"),
                "tsb": (w.get("ctl") or 0) - (w.get("atl") or 0),
                "ramp_rate": w.get("rampRate"),
            }
            for w in wellness_data
        ]

    # ---- Seuils par sport ----
    def get_thresholds(self) -> dict:
        """Récupère FTP, threshold pace CAP/Nat, LTHR, FC max depuis sportSettings.

        Retourne un dict :
          {
            "ftp_watts": int,
            "threshold_pace_run_mps": float,
            "threshold_pace_run_str": "4:55/km",
            "threshold_pace_swim_mps": float,
            "threshold_pace_swim_str": "2:00/100m",
            "lthr": int,
            "hr_max": int,
            "resting_hr": int,
            "weight_kg": float,
            "power_zones_pct": [...],
            "hr_zones_run": [...],
            "pace_zones_run_pct": [...],
          }
        """
        prof = self.athlete()
        out: dict = {
            "resting_hr": prof.get("icu_resting_hr"),
            "weight_kg": prof.get("icu_weight"),
        }
        for s in prof.get("sportSettings", []):
            types = set(s.get("types", []))
            if "Ride" in types:
                out["ftp_watts"] = s.get("ftp")
                out["power_zones_pct"] = s.get("power_zones")
                out["hr_zones_bike"] = s.get("hr_zones")
                out.setdefault("lthr", s.get("lthr"))
                out.setdefault("hr_max", s.get("max_hr"))
            if "Run" in types:
                tp = s.get("threshold_pace")  # m/s
                out["threshold_pace_run_mps"] = tp
                out["threshold_pace_run_str"] = pace_mps_to_minkm(tp) if tp else None
                out["pace_zones_run_pct"] = s.get("pace_zones")
                out["hr_zones_run"] = s.get("hr_zones")
                out["lthr"] = s.get("lthr") or out.get("lthr")
                out["hr_max"] = s.get("max_hr") or out.get("hr_max")
            if "Swim" in types:
                tp = s.get("threshold_pace")  # m/s
                out["threshold_pace_swim_mps"] = tp
                out["threshold_pace_swim_str"] = pace_mps_to_per100m(tp) if tp else None
        return out


# ---- Conversions de pace ----
def pace_mps_to_minkm(mps: float) -> str:
    """Convertit m/s en min:sec/km (CAP)."""
    if not mps or mps <= 0:
        return "n/a"
    sec_per_km = 1000.0 / mps
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def pace_mps_to_per100m(mps: float) -> str:
    """Convertit m/s en min:sec/100m (Natation)."""
    if not mps or mps <= 0:
        return "n/a"
    sec_per_100m = 100.0 / mps
    m, s = divmod(int(round(sec_per_100m)), 60)
    return f"{m}:{s:02d}/100m"


def smoke_test() -> None:
    """Petit test de connexion : récupère le profil et les 7 derniers jours."""
    client = IntervalsClient()
    print(f"Connexion OK pour athlète {client.athlete_id}")
    profile = client.athlete()
    th = client.get_thresholds()
    print(f"  Nom         : {profile.get('name')}")
    print(f"  Poids       : {th.get('weight_kg')} kg")
    print(f"  FTP vélo    : {th.get('ftp_watts')} W")
    print(f"  Seuil CAP   : {th.get('threshold_pace_run_str')}  ({th.get('threshold_pace_run_mps'):.2f} m/s)")
    print(f"  Seuil Nat   : {th.get('threshold_pace_swim_str')}  ({th.get('threshold_pace_swim_mps'):.2f} m/s)")
    print(f"  FC seuil    : {th.get('lthr')} bpm")
    print(f"  FC max      : {th.get('hr_max')} bpm")
    print(f"  FC repos    : {th.get('resting_hr')} bpm")
    today = date.today()
    acts = client.activities(today - timedelta(days=7))
    print(f"  Activités sur 7 jours : {len(acts)}")
    for a in acts[:5]:
        print(f"    - {a.get('start_date_local', '')[:10]} {a.get('type')} {a.get('name')}")


if __name__ == "__main__":
    smoke_test()
