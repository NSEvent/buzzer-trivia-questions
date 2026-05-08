#!/usr/bin/env python3
"""
Find questions with 'unknown' placeholders in their choices and re-generate
the wrong answers using claude.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

QDIR = Path(__file__).parent.parent / "questions"


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


def fix_question(q, category):
    """If question has any 'unknown' choices OR duplicate choices, regenerate the wrongs."""
    choices = q.get("choices", [])
    has_unknown = any(c.strip().lower() == "unknown" for c in choices)
    has_dups = len(choices) != len({c.strip().lower() for c in choices})
    if not (has_unknown or has_dups):
        return False  # nothing to fix

    correct_idx = q["correctIndex"]
    correct = clean(choices[correct_idx])
    print(f"    🔧 {category} | {q.get('clue', '')[:60]}... → correct: {correct}")

    wrongs = regen_wrongs(category, q["clue"], correct)
    if not wrongs or len(wrongs) < 3:
        print(f"      ❌ regen failed")
        return False

    wrongs = [clean(w) for w in wrongs[:3]]
    new_choices = list(wrongs)
    new_choices.insert(correct_idx, correct)
    q["choices"] = new_choices[:4]
    print(f"      ✅")
    return True


def main():
    fixed = 0
    files_changed = 0
    for f in sorted(QDIR.glob("*.json")):
        data = json.load(open(f))
        changed = False

        def needs_fix(choices):
            return (any(c.strip().lower() == "unknown" for c in choices)
                    or len(choices) != len({c.strip().lower() for c in choices}))

        for cat in data.get("categories", []):
            for q in cat.get("questions", []):
                if needs_fix(q.get("choices", [])):
                    if fix_question(q, cat["name"]):
                        fixed += 1
                        changed = True

        for key in ("dailyDouble", "bonusRound"):
            obj = data.get(key)
            if obj and needs_fix(obj.get("choices", [])):
                if fix_question(obj, obj.get("category", key)):
                    fixed += 1
                    changed = True

        if changed:
            files_changed += 1
            with open(f, "w") as out:
                json.dump(data, out, indent=2)
            print(f"  💾 {f.name}")

    print(f"\nFixed {fixed} questions across {files_changed} files")


if __name__ == "__main__":
    main()
