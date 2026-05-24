"""claude_client.py — Wrapper pour l'API Anthropic (Claude Sonnet).

Utilisé par :
  - generate_plans.py  : génération des plans théoriques (dimanche)
  - daily_coach.py     : plan adaptatif mis à jour chaque jour

Priorité clé API :
  1. Variable d'environnement ANTHROPIC_API_KEY (GitHub Actions Secrets)
  2. Fichier config/credentials.env  (usage local, jamais commité)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = ROOT / "config" / "credentials.env"

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8192


def load_api_key() -> str:
    """Lit la clé API Anthropic."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if CREDENTIALS_FILE.exists():
        for line in CREDENTIALS_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "Clé API Anthropic introuvable. "
        "Ajoute ANTHROPIC_API_KEY dans config/credentials.env ou en variable d'environnement."
    )


SYSTEM_PROMPT = """Tu es un coach triathlon expert, spécialisé dans la préparation des triathlètes amateurs de niveau confirmé.

Tes références scientifiques principales :
- Seiler (2010) : entraînement polarisé 80/20 — 80% du volume en Z1-Z2 (sous LT1), 20% en Z4-Z5 (au-dessus de LT2), minimal en Z3
- Helgerud et al. (2007) : les intervalles aérobies haute intensité améliorent le VO2max plus que l'endurance modérée
- Issurin (2008) : périodisation par blocs — progression intra-bloc sur la durée/répétitions des séances clés, semaine de récupération à 60-65% du pic
- Coggan & Allen (2010) : modèle PMC (CTL/ATL/TSB) pour piloter la charge
- Beattie et al. (2017) : le renforcement musculaire améliore les performances en endurance
- Hausswirth (2010) : spécificité des briques vélo+CAP pour l'adaptation neuromusculaire triathlon

Principes de progression intra-bloc (4 semaines charge + 1 récup) :
- S1 (base) : volume modéré, intensités clés courtes
- S2 : +8-10% TSS, allongement des intervalles ou +1 répétition
- S3 : +8-10% TSS, volume maximal du bloc, séances clés plus longues
- S4 : +8-10% TSS (si applicable), sinon maintien — pic du bloc
- S5 (récup) : 60-65% du TSS pic, séances courtes et légères, pas d'intervalles intenses

Pour les séances de CAP :
- Échauffement systématique : 20' (10' trot Z1 progressif + 5' gammes : talons-fesses, montées genoux, foulées bondissantes + 5' accélérations progressives)
- Retour au calme : 5' trot léger Z1

Pour les séances vélo :
- Échauffement : 15' progressif Z1→Z2 (finir avec 3×30s à 100 rpm)
- Retour au calme : 5' Z1 (<55% FTP, cadence souple)

Pour la natation :
- Échauffement : 400m nage souple
- Retour au calme : 200m nage souple

Réponds UNIQUEMENT en JSON valide, sans markdown, sans texte avant ou après le JSON."""


def call_claude(prompt: str, system: str | None = None) -> str:
    """Appelle l'API Anthropic et retourne la réponse texte brute."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "Package 'anthropic' manquant. Lance : pip install anthropic --break-system-packages"
        )

    api_key = load_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system or SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def call_claude_json(prompt: str, system: str | None = None) -> Any:
    """Appelle Claude et parse la réponse en JSON. Lève une exception si invalide."""
    raw = call_claude(prompt, system)

    # Extrait le JSON même si Claude a ajouté du texte autour (défense en profondeur)
    raw = raw.strip()

    # Cherche un bloc ```json ... ``` ou ``` ... ```
    md_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if md_match:
        raw = md_match.group(1)

    # Cherche le premier { ou [ comme début de JSON
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        idx = raw.find(start_char)
        if idx != -1:
            # Trouve la dernière accolade/crochet fermant correspondant
            last_idx = raw.rfind(end_char)
            if last_idx > idx:
                raw = raw[idx:last_idx + 1]
                break

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Réponse Claude non-JSON :\n{raw[:500]}\n\nErreur : {e}")
