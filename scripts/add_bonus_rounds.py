#!/usr/bin/env python3
"""
Adds a bonusRound to any Saturday board file that's missing one.
Uses the same dataset + claude pipeline, just one question per board.
"""
import csv
import json
import random
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from clue_registry import clue_hash, load_used_clues

REPO = Path(__file__).parent.parent
QDIR = REPO / "questions"
DATA = REPO / "data" / "clues.tsv"


def load_clues():
    by_cat = {}
    with open(DATA, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if not row.get("answer") or not row.get("question"):
                continue
            if row.get("daily_double_value", "0") != "0":
                continue
            if row.get("round", "") == "3":
                continue
            try:
                value = int(row["clue_value"])
            except (ValueError, KeyError):
                continue
            if not (800 <= value <= 2000):
                continue
            ans = row["answer"].strip()
            q = row["question"].strip()
            if len(ans) < 10 or len(ans) > 200 or len(q) < 2 or len(q) > 60:
                continue
            by_cat.setdefault(row["category"].strip(), []).append({
                "category": row["category"].strip(),
                "clue": ans,
                "answer": q,
                "value": value,
            })
    return by_cat


def clean(text):
    text = text.strip()
    text = re.sub(r'^(what|who|where|when)\s+(is|are|was|were)\s+', '', text, flags=re.IGNORECASE)
    return text.rstrip('?').strip()


def generate_wrongs(category, clue, correct):
    prompt = f"""Generate exactly 3 plausible but WRONG answers for this trivia question.
Output the raw answer only — NO "What is" prefix, NO question marks.

Category: {category}
Clue: {clue}
Correct answer: {correct}

Output ONLY a JSON array of 3 strings. No markdown, no explanation."""
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet", prompt],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )
        text = result.stdout.strip()
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text).strip()
        try:
            return json.loads(text)
        except:
            pass
        m = re.search(r'\[[\s\S]*\]', text)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"    [warn] claude failed: {e}")
    return None


def main():
    clues_by_cat = load_clues()
    used = load_used_clues()

    # Find Saturday files without bonus rounds
    targets = []
    for f in sorted(QDIR.glob("*.json")):
        date = f.stem
        try:
            dow = datetime.strptime(date, "%Y-%m-%d").isoweekday()
        except:
            continue
        if dow != 6:
            continue
        data = json.load(open(f))
        if "bonusRound" not in data or not data["bonusRound"]:
            targets.append(f)

    print(f"Found {len(targets)} Saturday files missing bonus rounds")

    success = 0
    for f in targets:
        date = f.stem
        data = json.load(open(f))
        existing_cats = {c["name"] for c in data["categories"]}
        existing_cats.add(data["dailyDouble"]["category"])

        # Pick a category not already used in this board
        candidates = [
            cat for cat, clues in clues_by_cat.items()
            if cat not in existing_cats
            and any(clue_hash(c["clue"]) not in used for c in clues)
        ]
        if not candidates:
            print(f"  ❌ {date}: no fresh category available")
            continue

        random.shuffle(candidates)
        chosen_clue = None
        for cat in candidates:
            fresh = [c for c in clues_by_cat[cat] if clue_hash(c["clue"]) not in used]
            if fresh:
                chosen_clue = random.choice(fresh)
                break

        if not chosen_clue:
            print(f"  ❌ {date}: couldn't pick clue")
            continue

        correct = clean(chosen_clue["answer"])
        print(f"  🎯 {date}: {chosen_clue['category']} → {correct}")
        wrongs = generate_wrongs(chosen_clue["category"], chosen_clue["clue"], correct)
        if not wrongs or len(wrongs) < 3:
            print(f"    ❌ wrong-answer generation failed")
            continue

        wrongs = [clean(w) for w in wrongs[:3]]
        correct_idx = random.randint(0, 3)
        choices = list(wrongs)
        choices.insert(correct_idx, correct)

        data["bonusRound"] = {
            "category": chosen_clue["category"],
            "clue": chosen_clue["clue"],
            "choices": choices[:4],
            "correctIndex": correct_idx,
        }

        with open(f, "w") as out:
            json.dump(data, out, indent=2)
        used.add(clue_hash(chosen_clue["clue"]))
        success += 1
        print(f"    ✅")

    print(f"\nFilled in {success}/{len(targets)} bonus rounds")
    print("\nRunning validation...")
    subprocess.run(["node", str(Path(__file__).parent / "validate.js")])


if __name__ == "__main__":
    main()
