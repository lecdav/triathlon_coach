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
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = ROOT / "config" / "credentials.env"
LOG_DIR = ROOT / "logs"

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

Tes références scientifiques principales (toutes issues de revues Q1 à peer-reviewing) :
- Seiler & Kjerland (2006, Scand J Med Sci Sports, Q1) : distribution d'intensité chez les athlètes d'endurance élites — 80% du volume en Z1-Z2 (sous LT1), 20% en Z4-Z5 (au-dessus de LT2), minimal en Z3
- Stöggl & Sperlich (2014, Frontiers in Physiology, Q1) : RCT sur 48 athlètes — l'entraînement polarisé produit les plus grandes améliorations de VO2peak et de performance en endurance vs seuil, HIIT ou haut volume
- Helgerud et al. (2007, Med Sci Sports Exerc, Q1) : les intervalles aérobies haute intensité (4×4 min à 90-95% FCmax) améliorent le VO2max plus que l'endurance modérée
- Morton, Fitz-Clarke & Banister (1990, J Appl Physiol, Q1) : modèle impulsion-réponse CTL/ATL/TSB — la performance = fitness (charge chronique 42j) moins fatigue (charge aiguë 7j)
- Beattie et al. (2017, J Strength Cond Res, Q1) : 40 semaines de renforcement musculaire améliorent l'économie de course et VVO2max chez les coureurs d'endurance
- Vleck, Bürgi & Bentley (2006, Int J Sports Med, Q1) : les performances en natation, vélo et CAP contribuent différemment au résultat global en triathlon olympique ; la spécificité des briques vélo+CAP est déterminante pour l'adaptation neuromusculaire à la transition

Principes de progression intra-bloc (4 semaines charge + 1 récup) :
- S1 (base) : volume modéré, intensités clés courtes
- S2 : +8-10% TSS, allongement des intervalles ou +1 répétition
- S3 : +8-10% TSS, volume maximal du bloc, séances clés plus longues
- S4 : +8-10% TSS (si applicable), sinon maintien — pic du bloc
- S5 (récup) : 60-65% du TSS pic, séances courtes et légères, pas d'intervalles intenses

Pour les séances de CAP :
- Échauffement systématique : 20' (10' trot Z1 progressif + 5' gammes : talons-fesses, montées genoux, foulées bondissantes + 5' accélérations progressives)
- Retour au calme : 5' trot léger Z1
- Durée maximum : 90 minutes

Pour les séances vélo :
- Échauffement : 15' progressif Z1→Z2 (finir avec 3×30s à 100 rpm)
- Retour au calme : 5' Z1 (<55% FTP, cadence souple)

Pour la natation :
- Échauffement : 200m nage souple
- Retour au calme : 100m nage souple
- Durée maximum : 60 minutes

Réponds UNIQUEMENT en JSON valide, sans markdown, sans texte avant ou après le JSON."""


def _log_exchange(prompt: str, system: str, response: str, error: str | None = None) -> None:
    """Enregistre le prompt et la réponse dans logs/claude_YYYY-MM-DD.jsonl."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"claude_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "model": MODEL,
            "system_hash": str(hash(system))[-6:],  # empreinte courte (pas le texte entier)
            "prompt_chars": len(prompt),
            "prompt": prompt,
            "response": response,
        }
        if error:
            entry["error"] = error
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️  Log Claude non écrit : {e}")


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
    sys = system or SYSTEM_PROMPT

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=sys,
            messages=[{"role": "user", "content": prompt}],
        )
        response = message.content[0].text
        _log_exchange(prompt, sys, response)
        return response
    except Exception as e:
        _log_exchange(prompt, sys, "", error=str(e))
        raise


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
