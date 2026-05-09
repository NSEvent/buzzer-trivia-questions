#!/usr/bin/env python3
"""
Quality-verify questions in the bank by sending them through Claude with a
structured-output check. For each question, Claude assesses:
  - Is the correct answer factually correct?
  - Are any of the wrong answers actually also valid (multi-answer ambiguity)?
  - Are the distractors all clearly wrong but plausible?

Questions flagged as bad get auto-fixed: a new set of wrong answers is generated.
Questions where the *correct* answer is bad get the whole question replaced.

Usage:
    python3 scripts/verify_questions.py [start_date] [num_days] [--apply]

Without --apply, runs a dry-run that just reports issues.
"""
import argparse
import csv
import json
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from clue_registry import clue_hash, load_used_clues

REPO = Path(__file__).parent.parent
QDIR = REPO / "questions"
DATA = REPO / "data" / "clues.tsv"

GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"; DIM = "\033[2m"; RESET = "\033[0m"


VERIFY_SCHEMA = {
    "name": "verify_results",
    "schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["ok", "bad_correct", "bad_distractor", "ambiguous"]},
                        "bad_choices": {"type": "array", "items": {"type": "integer"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "verdict"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    },
}

QWEN_HOST = "kmacstudio:18080"


def call_qwen_verify(prompt, max_tokens=8192, timeout=300):
    import urllib.request
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": VERIFY_SCHEMA},
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://{QWEN_HOST}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"  {DIM}[qwen] {e}{RESET}", file=sys.stderr)
        return None


def call_claude_verify(prompt, timeout=120):
    """Claude verify path. Plain text output, parse JSON manually."""
    try:
        r = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet", prompt],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        text = r.stdout.strip()
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text).strip()
        try:
            return json.loads(text)
        except:
            pass
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  {DIM}[claude] {e}{RESET}", file=sys.stderr)
    return None


def build_verify_prompt(items):
    """Each item: {index, category, clue, correct, choices}"""
    lines = []
    for it in items:
        choices_str = " | ".join(f"[{i}] {c}" for i, c in enumerate(it["choices"]))
        lines.append(
            f'{it["index"]}. [{it["category"]}] Clue: {it["clue"]}\n'
            f'   Correct: "{it["correct"]}"  Choices: {choices_str}'
        )
    return f"""You are auditing a trivia game. **Default to verdict "ok"**. Only flag a question if you are HIGHLY CONFIDENT (90%+) about a real problem. Don't second-guess yourself — if you'd need to think hard about whether to flag it, the answer is "ok".

Verdict options:
- "bad_correct": the correct answer is FACTUALLY WRONG for the clue (e.g., clue says Library of Congress magazine, but the answer is actually a Smithsonian magazine)
- "ambiguous": one of the OTHER choices is ALSO a valid correct answer for the clue (creates a true ambiguity, not just a "close" answer)
- "bad_distractor": a wrong answer is so far off-topic or wrong-type that it's obviously eliminated and reduces the question to 3 effective choices (e.g., correct is "Paris", a distractor is "broccoli")
- "ok": everything else, including weak distractors, stylistic preferences, or borderline cases

Keep your "reason" field SHORT (one sentence max). Don't reason out loud — give the verdict directly.

Return JSON: {{"results": [{{"index": N, "verdict": "ok|bad_correct|bad_distractor|ambiguous", "bad_choices": [indices_of_bad_choices], "reason": "brief"}}]}}.

Questions:
{chr(10).join(lines)}"""


def collect_questions(date_strs):
    """Yield (file_path, question_path, item_dict) for every question."""
    for date_str in date_strs:
        f = QDIR / f"{date_str}.json"
        if not f.exists():
            continue
        data = json.load(open(f))
        for ci, cat in enumerate(data.get("categories", [])):
            for qi, q in enumerate(cat["questions"]):
                yield f, ("category", ci, qi), {
                    "category": cat["name"],
                    "clue": q["clue"],
                    "correct": q["choices"][q["correctIndex"]],
                    "correctIndex": q["correctIndex"],
                    "choices": q["choices"],
                }
        for key in ("dailyDouble", "bonusRound"):
            obj = data.get(key)
            if obj:
                yield f, (key, None, None), {
                    "category": obj.get("category", key),
                    "clue": obj["clue"],
                    "correct": obj["choices"][obj["correctIndex"]],
                    "correctIndex": obj["correctIndex"],
                    "choices": obj["choices"],
                }


def regen_wrongs(category, clue, correct):
    prompt = (f"Generate exactly 3 plausible but WRONG answers for this trivia question. "
              f"Output the raw answer only (no \"What is\" prefix, no question marks). "
              f"Distractors must:\n"
              f"- Be CLEARLY WRONG (not valid alternatives)\n"
              f"- Be DISTINCT from each other and from the correct answer\n"
              f"- Match the type/format AND any pattern/theme implied by the category name "
              f"(e.g., if the category is 'STARTS WITH L', all 3 distractors must also start with L)\n\n"
              f"Category: {category}\nClue: {clue}\nCorrect: {correct}\n\n"
              f'Output ONLY a JSON array of 3 strings, like: ["X", "Y", "Z"]. No markdown, no commentary.')
    try:
        r = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet", prompt],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        text = r.stdout.strip()
        text = re.sub(r'^```(?:json)?\s*\n?', '', text).strip()
        text = re.sub(r'\n?```\s*$', '', text).strip()
        try:
            result = json.loads(text)
        except:
            m = re.search(r'\[[\s\S]*\]', text)
            if not m:
                return None
            result = json.loads(m.group())
        if isinstance(result, list):
            return [w.strip().rstrip('?') for w in result[:3]]
        if isinstance(result, dict):
            for key in ("wrong", "wrongs", "results", "answers"):
                if key in result and isinstance(result[key], list):
                    return [w.strip().rstrip('?') for w in result[key][:3]]
    except Exception as e:
        print(f"    {DIM}regen failed: {e}{RESET}", file=sys.stderr)
    return None


def apply_fix(file_path, q_path, item, verdict, bad_choices):
    """Mutate the JSON file to fix this question."""
    data = json.load(open(file_path))

    # Locate the question object
    if q_path[0] == "category":
        _, ci, qi = q_path
        q_obj = data["categories"][ci]["questions"][qi]
    else:
        q_obj = data[q_path[0]]

    if verdict == "bad_correct":
        # Whole question is broken — drop it. Caller would re-pick a clue, but
        # for simplicity here we just leave a marker. (Realistically, you'd
        # want to swap in a fresh clue from the same category at the same value.)
        # For now: skip — too invasive for verification pass.
        return False

    # bad_distractor or ambiguous → regenerate all 3 wrong answers
    correct = item["correct"]
    correct_idx = item["correctIndex"]
    new_wrongs = regen_wrongs(item["category"], item["clue"], correct)
    if not new_wrongs or len(new_wrongs) < 3:
        return False
    new_choices = list(new_wrongs)
    new_choices.insert(correct_idx, correct)
    q_obj["choices"] = new_choices[:4]

    with open(file_path, "w") as out:
        json.dump(data, out, indent=2)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("start_date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("num_days", nargs="?", type=int, default=7)
    parser.add_argument("--apply", action="store_true", help="Actually fix issues (default is dry-run)")
    parser.add_argument("--batch", type=int, default=8, help="Questions per LLM call")
    parser.add_argument("--backend", choices=["qwen", "claude"], default="claude",
                        help="Which LLM to use as verifier (default: claude)")
    args = parser.parse_args()
    verify_fn = call_claude_verify if args.backend == "claude" else call_qwen_verify

    start = datetime.strptime(args.start_date, "%Y-%m-%d")
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.num_days)]

    # Collect all questions
    items = list(collect_questions(dates))
    print(f"Verifying {len(items)} questions across {args.num_days} days "
          f"({args.start_date} → {dates[-1]})")
    print(f"Mode: {'APPLY (will fix issues)' if args.apply else 'DRY RUN (report only)'}\n")

    # Process in batches
    issues = []
    for batch_start in range(0, len(items), args.batch):
        batch = items[batch_start:batch_start + args.batch]
        verify_items = [
            {"index": batch_start + i, **(it[2])} for i, it in enumerate(batch)
        ]
        prompt = build_verify_prompt(verify_items)
        result = verify_fn(prompt)
        if not result or "results" not in result:
            print(f"  {YELLOW}[warn] verification batch failed at offset {batch_start}{RESET}")
            continue

        for r in result["results"]:
            idx = r.get("index")
            if idx is None or idx < batch_start or idx >= batch_start + len(batch):
                continue
            item = batch[idx - batch_start]
            verdict = r.get("verdict", "ok")
            if verdict == "ok":
                continue
            issues.append((item, r))

        progress = (batch_start + len(batch)) / len(items) * 100
        sys.stderr.write(f"  Progress: {batch_start + len(batch)}/{len(items)} ({progress:.0f}%)\r")
        sys.stderr.flush()

    sys.stderr.write("\n\n")

    # Report + (optionally) fix
    fixed = 0
    skipped = 0
    for (file_path, q_path, item), r in issues:
        verdict = r["verdict"]
        reason = r.get("reason", "")
        bad = r.get("bad_choices", [])
        location = f"{file_path.stem} [{q_path[0]}]"
        marker = {"bad_correct": "🔴", "bad_distractor": "🟡", "ambiguous": "🟠"}.get(verdict, "❓")
        print(f"{marker} {location}: {verdict}")
        print(f"   clue:    {item['clue'][:100]}")
        print(f"   correct: \"{item['correct']}\"  choices: {item['choices']}")
        print(f"   bad:     {bad}  reason: {reason}")

        if args.apply:
            ok = apply_fix(file_path, q_path, item, verdict, bad)
            if ok:
                fixed += 1
                print(f"   {GREEN}✓ fixed{RESET}\n")
            else:
                skipped += 1
                print(f"   {YELLOW}✗ couldn't fix (verdict {verdict}){RESET}\n")
        else:
            print()

    print(f"\n{'='*60}")
    print(f"Found {len(issues)} issues out of {len(items)} questions ({100*len(issues)/max(1,len(items)):.1f}%)")
    if args.apply:
        print(f"Fixed: {fixed}  |  Skipped: {skipped}")
    else:
        print(f"Run with --apply to auto-fix.")


if __name__ == "__main__":
    main()
