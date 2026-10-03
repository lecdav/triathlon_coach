"""Chargement du profil athlète — source unique : config/athlete_profile.yaml.

Utilisé par daily_coach.py et generate_theoretical_plan.py.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "athlete_profile.yaml"


def load_profile() -> dict:
    """Retourne le profil athlète sous forme de dict ({} si le fichier est absent)."""
    if not PROFILE_PATH.exists():
        return {}
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8")) or {}
