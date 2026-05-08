#!/usr/bin/env python3
"""
Find boards where the same clue appears twice (in different categories within
the same daily file) and replace one of them with a fresh clue from the dataset.
"""
import csv
import json
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from clue_registry import clue_hash, load_used_clues

REPO = Path(__file__).parent.parent
QDIR = REPO / "questions"
DATA = REPO / "data" / "clues.tsv"

VALUE_BUCKETS = {
    200: (100, 400),
    400: (400, 800),
    600: (600, 1200),
    800: (800, 1600),
    1000: (1000, 2000),
}


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


def regen_wrongs(category, clue, correct):
    prompt = f"""Generate exactly 3 plausible but WRONG answers for this trivia question.
Output the raw answer only — NO "What is" prefix, NO question marks.

Category: {category}
Clue: {clue}
Correct answer: {correct}

Output ONLY a JSON array of 3 strings. No markdown, no explanation."""
    try:
        r = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet", prompt],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )
        text = r.stdout.strip()
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
        print(f"    [warn] {e}")
    return None


def find_within_file_dups():
    """Find files where the same clue appears in multiple questions (including DD/bonus)."""
    affected = []
    for f in sorted(QDIR.glob("*.json")):
        data = json.load(open(f))
        # location is ("category", ci, qi) or ("dailyDouble", None, None) or ("bonusRound", None, None)
        clue_locs = {}
        for ci, cat in enumerate(data.get("categories", [])):
            for qi, q in enumerate(cat.get("questions", [])):
                if "clue" in q:
                    norm = re.sub(r'\s+', ' ', q["clue"].strip().lower())
                    clue_locs.setdefault(norm, []).append(("category", ci, qi))
        for key in ("dailyDouble", "bonusRound"):
            obj = data.get(key)
            if obj and "clue" in obj:
                norm = re.sub(r'\s+', ' ', obj["clue"].strip().lower())
                clue_locs.setdefault(norm, []).append((key, None, None))
        dups = {k: v for k, v in clue_locs.items() if len(v) > 1}
        if dups:
            affected.append((f, dups))
    return affected


def main():
    clues_by_cat = load_clues()
    used = load_used_clues()

    affected = find_within_file_dups()
    print(f"Found {len(affected)} files with within-file duplicate clues")

    for f, dups in affected:
        data = json.load(open(f))
        for clue_text, locs in dups.items():
            print(f"\n📄 {f.name}: clue \"{clue_text[:60]}...\" duplicated at {locs}")
            # Replace the second occurrence — prefer to replace category Q rather than DD/bonus
            target = None
            for loc in locs[1:]:
                if loc[0] == "category":
                    target = loc
                    break
            if not target:
                target = locs[1]  # fall back to whichever

            kind, ci, qi = target
            if kind == "category":
                cat = data["categories"][ci]
                q = cat["questions"][qi]
                target_value = q["value"]
                cat_name = cat["name"]
            else:
                obj = data[kind]
                target_value = 1000  # DD/bonus tend to be hardest tier
                cat_name = obj.get("category", "")

            # Pick a fresh clue from the same category if possible
            pool = clues_by_cat.get(cat_name, [])
            low, high = VALUE_BUCKETS.get(target_value, (600, 1600))
            fresh = [c for c in pool
                     if low <= c["value"] <= high
                     and clue_hash(c["clue"]) not in used]
            if not fresh:
                fresh = [c for c in pool if clue_hash(c["clue"]) not in used]
            if not fresh:
                for clues in clues_by_cat.values():
                    for c in clues:
                        if low <= c["value"] <= high and clue_hash(c["clue"]) not in used:
                            fresh.append(c)

            if not fresh:
                print(f"  ❌ no fresh clue available")
                continue

            new_clue = random.choice(fresh)
            correct = clean(new_clue["answer"])
            print(f"  🔧 [{kind}] Replacing with: \"{new_clue['clue'][:60]}...\" → {correct}")

            wrongs = regen_wrongs(cat_name, new_clue["clue"], correct)
            if not wrongs or len(wrongs) < 3:
                print(f"    ❌ wrongs failed")
                continue
            wrongs = [clean(w) for w in wrongs[:3]]

            correct_idx = random.randint(0, 3)
            choices = list(wrongs)
            choices.insert(correct_idx, correct)

            new_q = {"clue": new_clue["clue"], "choices": choices[:4], "correctIndex": correct_idx}
            if kind == "category":
                new_q["value"] = target_value
                data["categories"][ci]["questions"][qi] = new_q
            else:
                new_q["category"] = cat_name
                data[kind] = new_q
            used.add(clue_hash(new_clue["clue"]))
            print(f"    ✅")

        with open(f, "w") as out:
            json.dump(data, out, indent=2)


if __name__ == "__main__":
    main()
