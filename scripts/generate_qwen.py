#!/usr/bin/env python3
"""
Generate daily trivia boards using qwenr (local Qwen model) for wrong answers.
Same dataset-based approach as generate_from_dataset.py but uses qwenr instead of claude.

Usage:
    python3 scripts/generate_qwen.py [start_date] [num_days]
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

from clue_registry import clue_hash, load_used_clues

SCRIPT_DIR = Path(__file__).parent
REPO_DIR = SCRIPT_DIR.parent
QUESTIONS_DIR = REPO_DIR / "questions_qwen"  # Separate dir for comparison
DATA_FILE = REPO_DIR / "data" / "clues.tsv"

VALUE_BUCKETS = {
    200: (100, 400),
    400: (400, 800),
    600: (600, 1200),
    800: (800, 1600),
    1000: (1000, 2000),
}


def load_clues():
    clues_by_category = {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
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
            clue = {
                "category": row["category"].strip(),
                "answer": row["answer"].strip(),
                "question": row["question"].strip(),
                "value": value,
            }
            if len(clue["answer"]) < 10 or len(clue["answer"]) > 200:
                continue
            if len(clue["question"]) < 2 or len(clue["question"]) > 80:
                continue
            cat = clue["category"]
            clues_by_category.setdefault(cat, []).append(clue)
    return clues_by_category


def pick_categories(clues_by_category, count=3, used=set()):
    candidates = []
    for cat, clues in clues_by_category.items():
        if cat in used:
            continue
        if len(clues) >= 8 and len({c["value"] for c in clues}) >= 3:
            candidates.append(cat)
    random.shuffle(candidates)
    return candidates[:count]


def pick_questions_for_category(clues, used_global_hashes, target_values=[200, 400, 600, 800, 1000]):
    picked = []
    used_clues_local = set()
    for target in target_values:
        low, high = VALUE_BUCKETS[target]
        candidates = [c for c in clues
                      if low <= c["value"] <= high
                      and c["answer"] not in used_clues_local
                      and clue_hash(c["answer"]) not in used_global_hashes]
        if not candidates:
            candidates = [c for c in clues
                          if c["answer"] not in used_clues_local
                          and clue_hash(c["answer"]) not in used_global_hashes]
        if not candidates:
            return None
        chosen = random.choice(candidates)
        used_clues_local.add(chosen["answer"])
        used_global_hashes.add(clue_hash(chosen["answer"]))
        picked.append({
            "value": target,
            "clue": chosen["answer"],
            "correct_response": chosen["question"],
        })
    return picked


def clean_answer(text):
    """Strip Jeopardy phrasing from a raw answer if present."""
    text = text.strip()
    text = re.sub(r'^(what|who|where|when)\s+(is|are|was|were)\s+', '', text, flags=re.IGNORECASE)
    text = text.rstrip('?').strip()
    return text


def strip_thinking(text):
    """Qwen wraps its thinking in ANSI dim codes (\\x1b[2m...\\x1b[0m).
    The actual output comes AFTER the closing reset code."""
    # Split on the ANSI reset — everything after the last reset is the real answer
    parts = text.split("\x1b[0m")
    if len(parts) > 1:
        # Take what's after the last reset code
        text = parts[-1]
    # Strip any remaining ANSI codes
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    return text.strip()


QWEN_HOST = "kmacstudio:18080"

def _qwen_batch(batch, base_index):
    """Call qwen API directly with high max_tokens to allow verbose reasoning + final JSON."""
    import urllib.request

    prompt_lines = []
    for i, q in enumerate(batch):
        prompt_lines.append(f'{base_index + i}. Category: {q["category"]} | Clue: {q["clue"]} | Answer: {q["correct_response"]}')

    prompt = f"""For each numbered trivia question below, generate exactly 3 plausible but WRONG answers.
Output the raw answer only — NO "What is" / "Who is" prefix, NO question marks. Just the noun phrase.
Examples: "Paris", "Marie Curie", "the Eiffel Tower", "1789", "blue whale".
Wrong answers should match the type/format of the correct answer.

Output ONLY a JSON array. Each element: {{"index": N, "wrong": [...3 strings...]}}. No markdown, no explanation.

Questions:
{chr(10).join(prompt_lines)}"""

    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8192,
        "stream": False,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"http://{QWEN_HOST}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Extract content from chat completion response
        content = data["choices"][0]["message"]["content"]

        # Some models return reasoning in <think>...</think> tags or just inline
        # Strip any <think> blocks
        content = re.sub(r'<think>[\s\S]*?</think>', '', content, flags=re.IGNORECASE).strip()

        # Strip markdown fences
        content = re.sub(r'^```(?:json)?\s*\n?', '', content)
        content = re.sub(r'\n?```\s*$', '', content).strip()

        # Try direct parse
        try:
            return json.loads(content)
        except:
            pass

        # Find JSON array
        match = re.search(r'\[[\s\S]*\]', content)
        if match:
            try:
                return json.loads(match.group())
            except Exception as e:
                print(f"  [warn] Array parse failed: {e}", file=sys.stderr)
                print(f"  [debug] First 200: {content[:200]}", file=sys.stderr)

    except Exception as e:
        print(f"  [warn] qwen call failed: {e}", file=sys.stderr)

    return None


def generate_wrong_answers(questions_batch, batch_size=4):
    """Generate wrong answers in small batches; fall back to single-question calls on failure."""
    all_results = []
    covered = set()

    for start in range(0, len(questions_batch), batch_size):
        chunk = questions_batch[start:start + batch_size]
        print(f"    [batch {start//batch_size + 1}: questions {start}-{start+len(chunk)-1}]", file=sys.stderr)
        result = _qwen_batch(chunk, base_index=start)

        if result:
            valid = [r for r in result if "index" in r and "wrong" in r and len(r.get("wrong", [])) >= 3]
            for r in valid:
                covered.add(r["index"])
            all_results.extend(valid)

        # Fall back: any question in this chunk not covered, retry individually
        for i, q in enumerate(chunk):
            global_idx = start + i
            if global_idx in covered:
                continue
            print(f"    [fallback] retrying Q{global_idx} individually", file=sys.stderr)
            single = _qwen_batch([q], base_index=global_idx)
            if single:
                valid = [r for r in single if "index" in r and "wrong" in r and len(r.get("wrong", [])) >= 3]
                for r in valid:
                    covered.add(r["index"])
                all_results.extend(valid)

    return all_results if all_results else None


def build_daily_game(date_str, clues_by_category, used_global_hashes):
    used_categories = set()
    categories = pick_categories(clues_by_category, count=3, used=used_categories)
    if len(categories) < 3:
        return None

    game = {"date": date_str, "categories": []}
    all_questions = []

    for cat_name in categories:
        used_categories.add(cat_name)
        clues = clues_by_category[cat_name]
        picked = pick_questions_for_category(clues, used_global_hashes)
        if not picked:
            return None
        for q in picked:
            q["category"] = cat_name
            all_questions.append(q)
        game["categories"].append({"name": cat_name, "questions": picked})

    # Daily double from a different category
    dd_cats = pick_categories(clues_by_category, count=1, used=used_categories)
    if dd_cats:
        dd_cat = dd_cats[0]
        used_categories.add(dd_cat)
        dd_candidates = [c for c in clues_by_category[dd_cat]
                         if 600 <= c["value"] <= 1600
                         and clue_hash(c["answer"]) not in used_global_hashes]
        if not dd_candidates:
            dd_candidates = [c for c in clues_by_category[dd_cat]
                             if clue_hash(c["answer"]) not in used_global_hashes]
        if not dd_candidates:
            return None
        dd_chosen = random.choice(dd_candidates)
        used_global_hashes.add(clue_hash(dd_chosen["answer"]))
        dd_q = {"category": dd_cat, "clue": dd_chosen["answer"],
                "correct_response": dd_chosen["question"]}
        all_questions.append(dd_q)

    print(f"  Generating wrong answers for {len(all_questions)} questions via qwenr...")
    wrong_answers = generate_wrong_answers(all_questions)
    if not wrong_answers:
        return None

    wrong_map = {item["index"]: item["wrong"] for item in wrong_answers if "index" in item and "wrong" in item}

    q_idx = 0
    for cat_data in game["categories"]:
        final_questions = []
        for q in cat_data["questions"]:
            wrongs = wrong_map.get(q_idx, [])
            while len(wrongs) < 3:
                wrongs.append("unknown")
            correct = clean_answer(q["correct_response"])
            wrongs = [clean_answer(w) for w in wrongs[:3]]
            correct_idx = random.randint(0, 3)
            choices = list(wrongs)
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
    while len(dd_wrongs) < 3:
        dd_wrongs.append("unknown")
    dd_correct = clean_answer(dd_q["correct_response"])
    dd_wrongs = [clean_answer(w) for w in dd_wrongs[:3]]
    dd_correct_idx = random.randint(0, 3)
    dd_choices = list(dd_wrongs)
    dd_choices.insert(dd_correct_idx, dd_correct)
    game["dailyDouble"] = {
        "category": dd_q["category"],
        "clue": dd_q["clue"],
        "choices": dd_choices[:4],
        "correctIndex": dd_correct_idx,
    }

    return game


def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    num_days = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    QUESTIONS_DIR.mkdir(exist_ok=True)
    print(f"Loading clue dataset...")
    clues_by_category = load_clues()
    print(f"Loaded {sum(len(v) for v in clues_by_category.values())} clues across {len(clues_by_category)} categories")

    used_global_hashes = load_used_clues()
    print(f"Loaded {len(used_global_hashes)} previously-used clue hashes\n")

    start = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(num_days)]

    success = 0
    for date_str in dates:
        outfile = QUESTIONS_DIR / f"{date_str}.json"
        if outfile.exists():
            print(f"⏭  {date_str}.json exists, skipping")
            continue
        print(f"🎯 Generating {date_str}...")
        game = build_daily_game(date_str, clues_by_category, used_global_hashes)
        if game:
            with open(outfile, "w") as f:
                json.dump(game, f, indent=2)
            print(f"  ✅ {date_str}.json")
            success += 1
        else:
            print(f"  ❌ {date_str}.json failed")

    print(f"\n{success}/{num_days} boards generated to {QUESTIONS_DIR}")


if __name__ == "__main__":
    main()
