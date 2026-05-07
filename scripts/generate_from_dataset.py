#!/usr/bin/env python3
"""
Generate daily trivia boards from the 538K Jeopardy clue dataset.

Picks real clues + correct answers, then uses Claude Code CLI to generate
3 plausible wrong answers for each, formatted Jeopardy-style.

Usage:
    python3 scripts/generate_from_dataset.py [start_date] [num_days]
    python3 scripts/generate_from_dataset.py 2026-05-13 14
"""

import csv
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_DIR = SCRIPT_DIR.parent
QUESTIONS_DIR = REPO_DIR / "questions"
DATA_FILE = REPO_DIR / "data" / "clues.tsv"

# Value mapping: dataset uses 100-2000 (doubled after 2001), we use 200-1000
VALUE_BUCKETS = {
    200: (100, 400),    # easy: $100-$400
    400: (400, 800),    # medium-easy: $400-$800
    600: (600, 1200),   # medium: $600-$1200
    800: (800, 1600),   # hard: $800-$1600
    1000: (1000, 2000), # very hard: $1000-$2000
}


def load_clues():
    """Load and index clues by category."""
    clues_by_category = {}
    all_clues = []

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            # Skip daily doubles, final jeopardy, and clues without answers
            if not row.get("answer") or not row.get("question"):
                continue
            if row.get("daily_double_value", "0") != "0":
                continue
            if row.get("round", "") == "3":  # Final Jeopardy
                continue

            try:
                value = int(row["clue_value"])
            except (ValueError, KeyError):
                continue

            clue = {
                "category": row["category"].strip(),
                "answer": row["answer"].strip(),  # This is the clue text
                "question": row["question"].strip(),  # This is the correct response
                "value": value,
            }

            # Skip very short or very long clues
            if len(clue["answer"]) < 10 or len(clue["answer"]) > 200:
                continue
            if len(clue["question"]) < 2 or len(clue["question"]) > 80:
                continue

            cat = clue["category"]
            if cat not in clues_by_category:
                clues_by_category[cat] = []
            clues_by_category[cat].append(clue)
            all_clues.append(clue)

    return clues_by_category, all_clues


def pick_categories(clues_by_category, count=3, used_categories=set()):
    """Pick N categories that have enough clues across difficulty levels."""
    candidates = []
    for cat, clues in clues_by_category.items():
        if cat in used_categories:
            continue
        # Need at least 5 clues with reasonable value spread
        values = [c["value"] for c in clues]
        if len(clues) >= 8 and len(set(values)) >= 3:
            candidates.append(cat)

    random.shuffle(candidates)
    return candidates[:count]


def pick_questions_for_category(clues, target_values=[200, 400, 600, 800, 1000]):
    """Pick 5 questions at escalating difficulty from a category's clues."""
    picked = []
    used_clues = set()

    for target in target_values:
        low, high = VALUE_BUCKETS[target]
        # Find clues in the value range
        candidates = [
            c for c in clues
            if low <= c["value"] <= high and c["answer"] not in used_clues
        ]
        if not candidates:
            # Fallback: any unused clue
            candidates = [c for c in clues if c["answer"] not in used_clues]

        if not candidates:
            return None  # Not enough clues

        chosen = random.choice(candidates)
        used_clues.add(chosen["answer"])
        picked.append({
            "value": target,
            "clue": chosen["answer"],
            "correct_response": chosen["question"],
        })

    return picked


def generate_wrong_answers(questions_batch):
    """Use Claude to generate 3 wrong answers for each question in batch."""
    # Build a compact prompt with all questions
    prompt_lines = []
    for i, q in enumerate(questions_batch):
        prompt_lines.append(f'{i}. Category: {q["category"]} | Clue: {q["clue"]} | Answer: {q["correct_response"]}')

    prompt = f"""For each numbered trivia question below, generate exactly 3 plausible but WRONG answers.
Format each answer Jeopardy-style (e.g., "What is Paris?" or "Who is Einstein?").
The wrong answers should be the same type/format as the correct answer — believable but clearly wrong to someone who knows the topic.

Output ONLY a JSON array where each element has "index" and "wrong" (array of 3 strings). No markdown, no explanation.

Questions:
{chr(10).join(prompt_lines)}"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet", prompt],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL
        )
        text = result.stdout.strip()

        # Strip markdown fences
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)

        # Try direct parse
        try:
            return json.loads(text)
        except:
            pass

        # Find JSON array
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            return json.loads(match.group())

    except Exception as e:
        print(f"  [warn] Claude call failed: {e}", file=sys.stderr)

    return None


def build_daily_game(date_str, clues_by_category, all_clues, used_categories):
    """Build a complete daily game JSON for the given date."""
    categories = pick_categories(clues_by_category, count=3, used_categories=used_categories)
    if len(categories) < 3:
        print(f"  [error] Not enough categories available")
        return None

    game = {
        "date": date_str,
        "categories": [],
    }

    all_questions = []  # Flat list for batch wrong-answer generation

    for cat_name in categories:
        used_categories.add(cat_name)
        clues = clues_by_category[cat_name]
        picked = pick_questions_for_category(clues)
        if not picked:
            print(f"  [error] Not enough clues in category '{cat_name}'")
            return None

        for q in picked:
            q["category"] = cat_name
            all_questions.append(q)

        game["categories"].append({
            "name": cat_name,
            "questions": picked,  # Will be filled in after wrong answer generation
        })

    # Pick a daily double from a different category
    dd_cats = pick_categories(clues_by_category, count=1, used_categories=used_categories)
    if dd_cats:
        dd_cat = dd_cats[0]
        used_categories.add(dd_cat)
        dd_clues = clues_by_category[dd_cat]
        dd_candidates = [c for c in dd_clues if 600 <= c["value"] <= 1600]
        if not dd_candidates:
            dd_candidates = dd_clues
        dd_chosen = random.choice(dd_candidates)
        dd_q = {
            "category": dd_cat,
            "clue": dd_chosen["answer"],
            "correct_response": dd_chosen["question"],
        }
        all_questions.append(dd_q)

    # Check if Saturday — add bonus round
    day_of_week = datetime.strptime(date_str, "%Y-%m-%d").isoweekday()
    bonus_q = None
    if day_of_week == 6:  # Saturday
        bonus_cats = pick_categories(clues_by_category, count=1, used_categories=used_categories)
        if bonus_cats:
            bonus_cat = bonus_cats[0]
            used_categories.add(bonus_cat)
            bonus_clues = clues_by_category[bonus_cat]
            bonus_candidates = [c for c in bonus_clues if 800 <= c["value"] <= 2000]
            if not bonus_candidates:
                bonus_candidates = bonus_clues
            bonus_chosen = random.choice(bonus_candidates)
            bonus_q = {
                "category": bonus_cat,
                "clue": bonus_chosen["answer"],
                "correct_response": bonus_chosen["question"],
            }
            all_questions.append(bonus_q)

    # Generate wrong answers for all questions in one batch
    print(f"  Generating wrong answers for {len(all_questions)} questions...")
    wrong_answers = generate_wrong_answers(all_questions)
    if not wrong_answers:
        print(f"  [error] Failed to generate wrong answers")
        return None

    # Build wrong answer lookup
    wrong_map = {}
    for item in wrong_answers:
        wrong_map[item["index"]] = item["wrong"]

    # Assemble final questions with choices
    q_idx = 0
    for cat_data in game["categories"]:
        final_questions = []
        for q in cat_data["questions"]:
            wrongs = wrong_map.get(q_idx, [])
            if len(wrongs) < 3:
                print(f"  [warn] Not enough wrong answers for Q{q_idx}, padding")
                while len(wrongs) < 3:
                    wrongs.append(f"What is unknown?")

            correct = q["correct_response"]
            # Format correct answer Jeopardy-style if not already
            if not correct.lower().startswith(("what ", "who ", "where ", "when ")):
                correct = f"What is {correct}?"
            elif not correct.endswith("?"):
                correct += "?"

            # Randomize correct answer position
            correct_idx = random.randint(0, 3)
            choices = list(wrongs[:3])
            choices.insert(correct_idx, correct)

            final_questions.append({
                "value": q["value"],
                "clue": q["clue"],
                "choices": choices[:4],
                "correctIndex": correct_idx,
            })
            q_idx += 1

        cat_data["questions"] = final_questions

    # Daily double
    dd_wrongs = wrong_map.get(q_idx, [])
    if len(dd_wrongs) < 3:
        while len(dd_wrongs) < 3:
            dd_wrongs.append("What is unknown?")

    dd_correct = dd_q["correct_response"]
    if not dd_correct.lower().startswith(("what ", "who ", "where ", "when ")):
        dd_correct = f"What is {dd_correct}?"
    elif not dd_correct.endswith("?"):
        dd_correct += "?"

    dd_correct_idx = random.randint(0, 3)
    dd_choices = list(dd_wrongs[:3])
    dd_choices.insert(dd_correct_idx, dd_correct)
    q_idx += 1

    game["dailyDouble"] = {
        "category": dd_q["category"],
        "clue": dd_q["clue"],
        "choices": dd_choices[:4],
        "correctIndex": dd_correct_idx,
    }

    # Bonus round
    if bonus_q:
        bonus_wrongs = wrong_map.get(q_idx, [])
        if len(bonus_wrongs) < 3:
            while len(bonus_wrongs) < 3:
                bonus_wrongs.append("What is unknown?")

        bonus_correct = bonus_q["correct_response"]
        if not bonus_correct.lower().startswith(("what ", "who ", "where ", "when ")):
            bonus_correct = f"What is {bonus_correct}?"
        elif not bonus_correct.endswith("?"):
            bonus_correct += "?"

        bonus_correct_idx = random.randint(0, 3)
        bonus_choices = list(bonus_wrongs[:3])
        bonus_choices.insert(bonus_correct_idx, bonus_correct)

        game["bonusRound"] = {
            "category": bonus_q["category"],
            "clue": bonus_q["clue"],
            "choices": bonus_choices[:4],
            "correctIndex": bonus_correct_idx,
        }

    return game


def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    num_days = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    QUESTIONS_DIR.mkdir(exist_ok=True)

    print(f"Loading clue dataset from {DATA_FILE}...")
    clues_by_category, all_clues = load_clues()
    print(f"Loaded {len(all_clues)} clues across {len(clues_by_category)} categories")

    start = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(num_days)]

    print(f"\nGenerating {num_days} boards starting from {start_date}")
    print(f"Dates: {' '.join(dates)}\n")

    used_categories = set()
    success = 0

    for date_str in dates:
        outfile = QUESTIONS_DIR / f"{date_str}.json"
        if outfile.exists():
            print(f"⏭  {date_str}.json already exists, skipping")
            continue

        print(f"🎯 Generating {date_str}...")
        game = build_daily_game(date_str, clues_by_category, all_clues, used_categories)

        if game:
            with open(outfile, "w") as f:
                json.dump(game, f, indent=2)
            print(f"  ✅ {date_str}.json")
            success += 1
        else:
            print(f"  ❌ {date_str}.json — generation failed")

    print(f"\n{success}/{num_days} boards generated successfully")
    print("\nRunning validation...")
    subprocess.run(["node", str(SCRIPT_DIR / "validate.js")])


if __name__ == "__main__":
    main()
