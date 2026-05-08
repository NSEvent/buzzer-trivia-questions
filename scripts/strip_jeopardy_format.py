#!/usr/bin/env python3
"""
One-time script to strip "What is X?" / "Who is Y?" Jeopardy phrasing from all
existing question files, leaving raw answers like "X" / "Y".

Usage: python3 scripts/strip_jeopardy_format.py
"""
import json
import re
from pathlib import Path

QUESTIONS_DIR = Path(__file__).parent.parent / "questions"


def clean(text):
    text = text.strip()
    # Strip "what is / who is / where is / when is/are/was/were" prefix
    text = re.sub(r'^(what|who|where|when)\s+(is|are|was|were)\s+', '', text, flags=re.IGNORECASE)
    # Strip trailing question mark
    text = text.rstrip('?').strip()
    return text


def clean_question(q):
    q["choices"] = [clean(c) for c in q["choices"]]
    return q


def clean_game(game):
    for cat in game.get("categories", []):
        cat["questions"] = [clean_question(q) for q in cat["questions"]]
    if "dailyDouble" in game:
        game["dailyDouble"] = clean_question(game["dailyDouble"])
    if "bonusRound" in game:
        game["bonusRound"] = clean_question(game["bonusRound"])
    return game


def main():
    files = sorted(QUESTIONS_DIR.glob("*.json"))
    print(f"Processing {len(files)} files...")
    for f in files:
        data = json.load(open(f))
        cleaned = clean_game(data)
        with open(f, "w") as out:
            json.dump(cleaned, out, indent=2)
    print(f"✅ Stripped Jeopardy phrasing from {len(files)} files")


if __name__ == "__main__":
    main()
