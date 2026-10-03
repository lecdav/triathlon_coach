"""Chargement du profil athlète — source unique : inputs/athlete_profile.yaml.

Utilisé par engine/coach.py et engine/plans.py.
"""

from __future__ import annotations

import yaml

from engine.paths import PROFILE_PATH


def load_profile() -> dict:
    """Retourne le profil athlète sous forme de dict ({} si le fichier est absent)."""
    if not PROFILE_PATH.exists():
        return {}
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8")) or {}
