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


SYSTEM_PROMPT = """Coach triathlon expert, triathlètes amateurs confirmés. Méthodologie (refs Q1 peer-reviewed) :
- Polarisé 80/20 : 80% Z1-Z2, 20% Z4-Z5, minimal Z3 (Seiler & Kjerland 2006 ; Stöggl & Sperlich 2014)
- Intervalles haute intensité pour VO2max (Helgerud et al. 2007)
- Charge : modèle CTL/ATL/TSB (Morton, Fitz-Clarke & Banister 1990)
- Renforcement améliore économie de course (Beattie et al. 2017)
- Briques vélo+CAP pour adaptation neuromusculaire triathlon (Vleck, Bürgi & Bentley 2006)
Progression bloc 4+1 : S1 base → S2+8% → S3+8% → S4 pic → S5 récup 60-65%.
Warmup/cooldown gérés automatiquement par l'application — ne pas les inclure dans les blocs Run/VirtualRide.
Contraintes durée STRICTES :
- Mardi, Mercredi, Jeudi : duration_min ≤ 60 min (séances de semaine)
- Samedi (hors sortie longue) : duration_min ≤ 60 min
- Dimanche uniquement : sortie longue sans plafond
Réponds UNIQUEMENT en JSON valide, sans markdown, sans texte avant ou après."""


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
