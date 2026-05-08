"""
Shared utility for tracking which clues have been used across all boards.

On import, scans existing questions/*.json files to populate a hash set of
already-used clues. Provides functions to check, mark, and persist the registry.
"""
import hashlib
import json
import re
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
QUESTIONS_DIR = REPO_DIR / "questions"


def clue_hash(clue_text: str) -> str:
    """Stable hash of a clue's text, case-insensitive and whitespace-normalized."""
    normalized = re.sub(r'\s+', ' ', clue_text.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def load_used_clues() -> set:
    """Scan all existing question files and return a set of used clue hashes."""
    used = set()
    if not QUESTIONS_DIR.exists():
        return used
    for f in sorted(QUESTIONS_DIR.glob("*.json")):
        try:
            data = json.load(open(f))
        except:
            continue
        for cat in data.get("categories", []):
            for q in cat.get("questions", []):
                if "clue" in q:
                    used.add(clue_hash(q["clue"]))
        for key in ("dailyDouble", "bonusRound"):
            obj = data.get(key)
            if obj and "clue" in obj:
                used.add(clue_hash(obj["clue"]))
    return used
