"""Emplacements de tous les fichiers du projet — entrées, sorties, journaux.

inputs/   ← ce qui entre : profil athlète (saisi à la main) + imports Intervals.icu
docs/     ← ce qui sort : dashboard + données exportées (publié par GitHub Pages)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── Entrées ──────────────────────────────────────────────────────────────────
INPUTS_DIR = ROOT / "inputs"
PROFILE_PATH = INPUTS_DIR / "athlete_profile.yaml"      # profil athlète (édité à la main)
CREDENTIALS_PATH = INPUTS_DIR / "credentials.env"        # clés API locales (non versionné)
LEGACY_CREDENTIALS_PATH = ROOT / "config" / "credentials.env"  # ancien emplacement
INTERVALS_INPUT_DIR = INPUTS_DIR / "intervals"           # imports bruts (non versionnés)

# ── Sorties ──────────────────────────────────────────────────────────────────
DOCS_DIR = ROOT / "docs"                                 # racine GitHub Pages
OUTPUT_DATA_DIR = DOCS_DIR / "data"
TODAY_PATH = OUTPUT_DATA_DIR / "today.json"              # état du jour (lu par le dashboard)
WEEKLY_PLANS_PATH = OUTPUT_DATA_DIR / "weekly_plans.json"
PERIODIZATION_PATH = OUTPUT_DATA_DIR / "periodization.json"

# ── Journaux ─────────────────────────────────────────────────────────────────
LOG_DIR = ROOT / "logs"                                  # échanges Claude (non versionné)


def credentials_file() -> Path:
    """Fichier de clés API local : inputs/credentials.env, sinon l'ancien config/credentials.env."""
    if not CREDENTIALS_PATH.exists() and LEGACY_CREDENTIALS_PATH.exists():
        return LEGACY_CREDENTIALS_PATH
    return CREDENTIALS_PATH
