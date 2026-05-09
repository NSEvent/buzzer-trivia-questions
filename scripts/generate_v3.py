#!/usr/bin/env python3
"""
Generate daily trivia boards using a robust pipeline that prevents most
of the issues we had to clean up in v1/v2:

- JSON schema enforcement (no markdown fences, no truncation, no 'unknown' filler)
- Per-question validation with retry (up to 3 attempts before dropping the clue)
- Dataset preprocessing (strip escape artifacts at load time)
- Filter unsuitable clue patterns ("Of A, B, C..." comparison clues, etc.)
- Within-board dedup on clue text AND correct answer
- Type-aware distractor prompts (forces matching answer type)

Backend: defaults to qwen (local llama-server). --backend claude swaps to
Claude Code CLI. Both use the same JSON schema.

Usage:
    python3 scripts/generate_v3.py [start_date] [num_days] [--backend qwen|claude]
"""
import argparse
import csv
import json
import random
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from clue_registry import clue_hash, load_used_clues

REPO = Path(__file__).parent.parent
QDIR = REPO / "questions"
DATA = REPO / "data" / "clues.tsv"
QWEN_HOST = "kmacstudio:18080"

VALUE_BUCKETS = {
    200: (100, 400),
    400: (400, 800),
    600: (600, 1200),
    800: (800, 1600),
    1000: (1000, 2000),
}

# ANSI colors
DIM = "\033[2m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"; RESET = "\033[0m"

# Patterns that indicate a clue is unsuitable for multiple-choice format
BAD_CLUE_PATTERNS = [
    re.compile(r'\bof\s+(\w+,?\s+){2,}\bor\s+\w+', re.IGNORECASE),  # "Of A, B, or C..."
    re.compile(r'\bbetween\s+\w+\s+and\s+\w+', re.IGNORECASE),       # "Between X and Y..."
    re.compile(r'\b(this|that)\s+(clue|category|round)\b', re.IGNORECASE),  # self-referencing
    re.compile(r'\bthe\s+(first|second|third|fourth|fifth)\s+(of\s+)?(these|the\s+\w+)\b', re.IGNORECASE),  # "first of these"
]

# Patterns that suggest the answer might be multi-valued
MULTI_VALUE_ANSWER_PATTERNS = [
    re.compile(r'\bor\b', re.IGNORECASE),  # "X or Y"
    re.compile(r'\b(?:and|/)\b'),           # "X and Y" or "X/Y"
]

TPS_STATS = {"total_calls": 0, "total_completion_tokens": 0, "total_seconds": 0.0, "failures": 0}


# ============================================================================
# Dataset loading with cleanup
# ============================================================================

def clean_text(text):
    """Strip escape artifacts and normalize whitespace from a raw dataset string."""
    if not isinstance(text, str):
        return ""
    text = text.replace('\\"', '"').replace('\\\\', '\\')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def is_bad_clue(clue_text, answer):
    """Return True if this clue should be filtered out (unsuitable for MC)."""
    for pat in BAD_CLUE_PATTERNS:
        if pat.search(clue_text):
            return True
    for pat in MULTI_VALUE_ANSWER_PATTERNS:
        if pat.search(answer):
            return True
    # Answer text contained verbatim in clue → too easy / unfair
    answer_clean = re.sub(r'[^a-z0-9]', '', answer.lower())
    clue_clean = re.sub(r'[^a-z0-9]', '', clue_text.lower())
    if answer_clean and len(answer_clean) >= 4 and answer_clean in clue_clean:
        return True
    return False


def load_clues():
    """Load clues, indexed by category. All strings cleaned. Bad clues filtered."""
    by_cat = {}
    n_total = 0
    n_filtered = 0
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

            n_total += 1
            clue = clean_text(row["answer"])    # dataset's "answer" = the clue shown
            answer = clean_text(row["question"]) # dataset's "question" = the response
            category = clean_text(row["category"])

            if not clue or not answer or not category:
                continue
            if len(clue) < 10 or len(clue) > 200 or len(answer) < 2 or len(answer) > 60:
                continue
            if is_bad_clue(clue, answer):
                n_filtered += 1
                continue

            by_cat.setdefault(category, []).append({
                "clue": clue, "answer": answer, "value": value, "category": category,
            })

    print(f"  Loaded {sum(len(v) for v in by_cat.values()):,} clues ({n_filtered:,} filtered as unsuitable for MC)")
    return by_cat


# ============================================================================
# Type detection for distractor matching
# ============================================================================

def detect_answer_type(answer):
    """Heuristic classification of an answer's type, used to guide distractors."""
    a = answer.strip()
    # Year (4 digits)
    if re.fullmatch(r'(19|20)\d{2}', a):
        return "year"
    # Number with units
    if re.fullmatch(r'\d+\s*(percent|%|years?|miles?|km|kg|pounds?)', a, re.IGNORECASE):
        return "number"
    # All caps short → acronym
    if a.isupper() and len(a) <= 6:
        return "acronym"
    # Quoted title
    if a.startswith('"') and a.endswith('"'):
        return "title"
    # Multi-word with both parts capitalized → likely a name
    words = a.split()
    if len(words) >= 2:
        cap_words = [w for w in words[:3] if w and w[0].isupper()]
        if len(cap_words) >= 2 and not a.lower().startswith(("the ", "a ", "an ")):
            return "person"
    # Single capitalized word (could be place or thing)
    if len(words) == 1 and a[0].isupper():
        return "noun"
    return "phrase"


# ============================================================================
# Schema-enforced LLM call with reasoning_content support
# ============================================================================

WRONGS_SCHEMA = {
    "name": "wrong_answers",
    "schema": {
        "type": "object",
        "properties": {
            "wrong": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 80},
                "minItems": 3,
                "maxItems": 3,
            }
        },
        "required": ["wrong"],
        "additionalProperties": False,
    },
}

BATCH_SCHEMA = {
    "name": "batch_wrong_answers",
    "schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "wrong": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1, "maxLength": 80},
                            "minItems": 3,
                            "maxItems": 3,
                        },
                    },
                    "required": ["index", "wrong"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    },
}


def call_qwen_schema(prompt, schema=WRONGS_SCHEMA, max_tokens=8192, timeout=300):
    """Call qwen with response_format json_schema. Returns parsed dict or None."""
    import time
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": schema},
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"http://{QWEN_HOST}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.monotonic() - t0
        usage = data.get("usage", {})
        TPS_STATS["total_calls"] += 1
        TPS_STATS["total_completion_tokens"] += usage.get("completion_tokens", 0)
        TPS_STATS["total_seconds"] += elapsed

        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        TPS_STATS["failures"] += 1
        print(f"    {DIM}[qwen] {e}{RESET}", file=sys.stderr)
        return None


def call_claude_schema(prompt, schema=WRONGS_SCHEMA, timeout=60):
    """Call claude -p with --json-schema. Returns parsed dict or None."""
    schema_json = json.dumps(schema["schema"])
    try:
        r = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet",
             "--json-schema", schema_json, prompt],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        text = r.stdout.strip()
        # Strip markdown fences if any sneak in
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text).strip()
        return json.loads(text)
    except Exception as e:
        TPS_STATS["failures"] += 1
        print(f"    {DIM}[claude] {e}{RESET}", file=sys.stderr)
        return None


# ============================================================================
# Distractor generation with retry + validation
# ============================================================================

def build_distractor_prompt(category, clue, correct, answer_type):
    type_hints = {
        "year": "Wrong answers must be other years from a similar era (within 50 years).",
        "number": "Wrong answers must be other numbers in the same units and similar magnitude.",
        "acronym": "Wrong answers must be other plausible acronyms of similar length.",
        "title": "Wrong answers must be other quoted titles in the same medium (book/song/film).",
        "person": "Wrong answers must be other people of the same era and field.",
        "noun": "Wrong answers must be other nouns of the same category (e.g., if the answer is a city, all distractors are cities).",
        "phrase": "Wrong answers must be other short phrases of similar form.",
    }
    return f"""Generate exactly 3 plausible but WRONG answers for this trivia question.

Category: {category}
Clue: {clue}
Correct answer: {correct}
Answer type: {answer_type}

Rules:
- Output the raw answer only — no "What is" prefix, no question marks, no quotes around the answer
- All 3 distractors must be DISTINCT from each other and DISTINCT from the correct answer (case-insensitive)
- All 3 distractors must be CLEARLY WRONG — none of them should be a valid alternative answer
- {type_hints.get(answer_type, "Wrong answers must match the type of the correct answer.")}
- Match the format and length of the correct answer (e.g., if correct is "Marie Curie" both first+last, distractors should also be first+last names)

Return JSON: {{"wrong": ["...", "...", "..."]}}"""


def validate_choices(correct, wrongs):
    """Return (ok, reason) — ok=True if the 4 choices form a valid question."""
    if len(wrongs) != 3:
        return False, f"got {len(wrongs)} wrongs"
    all_choices = [correct] + wrongs
    # No empty / placeholder
    for c in all_choices:
        if not c or not c.strip():
            return False, "empty choice"
        if c.strip().lower() == "unknown":
            return False, "'unknown' placeholder"
    # All distinct (case-insensitive, whitespace-normalized)
    norm = [re.sub(r'\s+', ' ', c.strip().lower()) for c in all_choices]
    if len(set(norm)) != 4:
        return False, "duplicate choice"
    # Correct shouldn't be a substring of any wrong (or vice versa) — would be too obvious or ambiguous
    correct_clean = re.sub(r'[^a-z0-9]', '', correct.lower())
    for w in wrongs:
        w_clean = re.sub(r'[^a-z0-9]', '', w.lower())
        if not correct_clean or not w_clean:
            continue
        if correct_clean in w_clean or w_clean in correct_clean:
            if abs(len(correct_clean) - len(w_clean)) < 4:
                return False, f"distractor too similar to correct ('{w}' vs '{correct}')"
    return True, None


def clean_choice(text):
    """Strip Jeopardy phrasing and trailing punctuation from a single answer."""
    t = text.strip()
    t = re.sub(r'^(what|who|where|when)\s+(is|are|was|were)\s+', '', t, flags=re.IGNORECASE)
    t = t.rstrip('?').strip()
    return t


def build_batch_prompt(clue_records):
    """Build a single prompt asking for distractors for multiple questions at once."""
    lines = []
    for i, c in enumerate(clue_records):
        correct = clean_choice(c["answer"])
        answer_type = detect_answer_type(correct)
        lines.append(f'{i}. [{c["category"]}] [{answer_type}] Clue: "{c["clue"]}" — Correct: "{correct}"')

    return f"""For each numbered trivia question, generate exactly 3 plausible but WRONG answers.

Rules for EVERY question:
- Output the raw answer only — no "What is" prefix, no question marks, no quotes
- All 3 distractors must be DISTINCT from each other and DISTINCT from the correct answer
- All 3 distractors must be CLEARLY WRONG (no valid alternative answers)
- Match the answer type shown in brackets: person→other people of same era/field; noun→same category (city/object/etc); year→other years from same era; title→same medium
- Match the format/length of the correct answer

Return JSON: {{"results": [{{"index": 0, "wrong": ["...", "...", "..."]}}, ...]}} — one entry per numbered question.

Questions:
{chr(10).join(lines)}"""


def call_qwen_batch_schema(prompt, max_tokens=4096, timeout=300):
    return call_qwen_schema(prompt, schema=BATCH_SCHEMA, max_tokens=max_tokens, timeout=timeout)


def call_claude_batch_schema(prompt, timeout=120):
    return call_claude_schema(prompt, schema=BATCH_SCHEMA, timeout=timeout)


def generate_questions_batch(clue_records, backend, max_retries=2):
    """
    Generate distractors for a batch of clues in a single call.
    Returns list-of-question-dicts (None entries for failures).
    """
    call_fn = call_qwen_batch_schema if backend == "qwen" else call_claude_batch_schema
    results = [None] * len(clue_records)
    failed_indices = list(range(len(clue_records)))

    for attempt in range(max_retries + 1):
        if not failed_indices:
            break
        # Build prompt with only the failed (or all on first pass) clues
        active_records = [clue_records[i] for i in failed_indices]
        active_to_global = {local: global_ for local, global_ in enumerate(failed_indices)}
        prompt = build_batch_prompt(active_records)
        response = call_fn(prompt)
        if not response or "results" not in response:
            continue

        # Map results back, validate each
        new_failed = []
        responded_indices = {r["index"] for r in response["results"] if "index" in r}
        for r in response["results"]:
            local_idx = r.get("index")
            if local_idx is None or local_idx not in active_to_global:
                continue
            global_idx = active_to_global[local_idx]
            if results[global_idx] is not None:
                continue  # already have a good one

            clue_record = clue_records[global_idx]
            correct = clean_choice(clue_record["answer"])
            wrongs = [clean_choice(w) for w in r.get("wrong", [])[:3]]
            ok, reason = validate_choices(correct, wrongs)
            if not ok:
                # leave for retry
                continue
            correct_idx = random.randint(0, 3)
            choices = list(wrongs)
            choices.insert(correct_idx, correct)
            results[global_idx] = {
                "clue": clue_record["clue"],
                "choices": choices[:4],
                "correctIndex": correct_idx,
                "value": clue_record.get("value"),
            }

        # Identify which clues still need work
        failed_indices = [i for i, q in enumerate(results) if q is None]

    return results


# ============================================================================
# Board assembly
# ============================================================================

def pick_categories(by_cat, count, used_categories):
    candidates = [
        cat for cat, clues in by_cat.items()
        if cat not in used_categories
        and len(clues) >= 8
        and len({c["value"] for c in clues}) >= 3
    ]
    random.shuffle(candidates)
    return candidates[:count]


def pick_clue(clues, target_value, used_global, used_in_board, used_answers_in_board):
    """Pick a clue at the target value tier, avoiding global + within-board duplicates."""
    low, high = VALUE_BUCKETS[target_value]
    candidates = [
        c for c in clues
        if low <= c["value"] <= high
        and clue_hash(c["clue"]) not in used_global
        and clue_hash(c["clue"]) not in used_in_board
        and c["answer"].strip().lower() not in used_answers_in_board
    ]
    if not candidates:
        # Fallback: any value, still respecting all dedup constraints
        candidates = [
            c for c in clues
            if clue_hash(c["clue"]) not in used_global
            and clue_hash(c["clue"]) not in used_in_board
            and c["answer"].strip().lower() not in used_answers_in_board
        ]
    return random.choice(candidates) if candidates else None


def build_board(date_str, by_cat, used_global, backend, batch_size=4):
    """Build a complete daily game using batched LLM calls (more efficient)."""
    used_categories = set()
    used_in_board = set()
    used_answers = set()

    cats = pick_categories(by_cat, 3, used_categories)
    if len(cats) < 3:
        return None
    for c in cats:
        used_categories.add(c)

    # Pick all 15 regular clues + DD + (Saturday) bonus before any LLM calls
    selected = []  # list of (slot_kind, slot_meta, clue_record)

    for cat_name in cats:
        clues = by_cat[cat_name]
        for target_value in [200, 400, 600, 800, 1000]:
            clue = pick_clue(clues, target_value, used_global, used_in_board, used_answers)
            if not clue:
                print(f"  {RED}[error] no clue for ${target_value} in {cat_name}{RESET}")
                return None
            used_in_board.add(clue_hash(clue["clue"]))
            used_answers.add(clue["answer"].strip().lower())
            used_global.add(clue_hash(clue["clue"]))
            selected.append(("category", {"category": cat_name, "value": target_value}, clue))

    dd_cats = pick_categories(by_cat, 1, used_categories)
    if not dd_cats:
        return None
    dd_cat = dd_cats[0]
    used_categories.add(dd_cat)
    dd_clue = pick_clue(by_cat[dd_cat], 800, used_global, used_in_board, used_answers)
    if not dd_clue:
        print(f"  {RED}[error] no DD clue in {dd_cat}{RESET}")
        return None
    used_in_board.add(clue_hash(dd_clue["clue"]))
    used_answers.add(dd_clue["answer"].strip().lower())
    used_global.add(clue_hash(dd_clue["clue"]))
    selected.append(("dailyDouble", {"category": dd_cat}, dd_clue))

    dow = datetime.strptime(date_str, "%Y-%m-%d").isoweekday()
    if dow == 6:
        bonus_cats = pick_categories(by_cat, 1, used_categories)
        if bonus_cats:
            bonus_cat = bonus_cats[0]
            bonus_clue = pick_clue(by_cat[bonus_cat], 1000, used_global, used_in_board, used_answers)
            if bonus_clue:
                used_in_board.add(clue_hash(bonus_clue["clue"]))
                used_answers.add(bonus_clue["answer"].strip().lower())
                used_global.add(clue_hash(bonus_clue["clue"]))
                selected.append(("bonusRound", {"category": bonus_cat}, bonus_clue))

    # Generate all distractors in batches
    questions = [None] * len(selected)
    for batch_start in range(0, len(selected), batch_size):
        batch = selected[batch_start:batch_start + batch_size]
        clue_records = [b[2] for b in batch]
        results = generate_questions_batch(clue_records, backend)
        for i, q in enumerate(results):
            if q is None:
                slot_kind, _, clue_rec = batch[i]
                print(f"  {YELLOW}[warn] couldn't generate distractors for {slot_kind} clue '{clue_rec['clue'][:40]}...'{RESET}")
                return None
            questions[batch_start + i] = q

    # Assemble the final game structure (apply slot value, not original dataset value)
    game = {"date": date_str, "categories": []}
    by_category = {}
    for (slot_kind, slot_meta, _), q in zip(selected, questions):
        if slot_kind == "category":
            cat_name = slot_meta["category"]
            q["value"] = slot_meta["value"]
            by_category.setdefault(cat_name, []).append(q)
        elif slot_kind == "dailyDouble":
            q.pop("value", None)
            q["category"] = slot_meta["category"]
            game["dailyDouble"] = q
        elif slot_kind == "bonusRound":
            q.pop("value", None)
            q["category"] = slot_meta["category"]
            game["bonusRound"] = q

    for cat_name in cats:
        # Sort questions by value to guarantee 200/400/600/800/1000 order
        qs = sorted(by_category[cat_name], key=lambda q: q.get("value", 0))
        game["categories"].append({"name": cat_name, "questions": qs})

    return game


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("start_date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("num_days", nargs="?", type=int, default=7)
    parser.add_argument("--backend", choices=["qwen", "claude"], default="qwen")
    parser.add_argument("--output", type=Path, default=QDIR,
                        help="Output directory (default: questions/). Use a different "
                             "directory to generate boards without polluting the live bank.")
    parser.add_argument("--prefix", default="",
                        help="Filename prefix. e.g. --prefix fallback- gives 'fallback-2030-01-01.json'")
    args = parser.parse_args()

    out_dir = args.output
    out_dir.mkdir(exist_ok=True, parents=True)
    print(f"Loading clue dataset...")
    by_cat = load_clues()
    used_global = load_used_clues()
    print(f"  {len(used_global):,} hashes already used")
    print(f"  Output: {out_dir}\n")

    start = datetime.strptime(args.start_date, "%Y-%m-%d")
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.num_days)]

    print(f"Generating {args.num_days} boards via {args.backend}, starting {args.start_date}\n")

    success = 0
    for date_str in dates:
        out = out_dir / f"{args.prefix}{date_str}.json"
        if out.exists():
            print(f"⏭  {out.name} exists")
            continue
        print(f"🎯 {date_str}")
        game = build_board(date_str, by_cat, used_global, args.backend)
        if game:
            with open(out, "w") as f:
                json.dump(game, f, indent=2)
            print(f"  {GREEN}✅ saved{RESET}")
            success += 1
        else:
            print(f"  {RED}❌ failed{RESET}")

    print(f"\n{success}/{args.num_days} boards generated")
    if TPS_STATS["total_calls"] > 0 and args.backend == "qwen":
        avg_tps = TPS_STATS["total_completion_tokens"] / TPS_STATS["total_seconds"]
        print(f"\n[Qwen Stats] {TPS_STATS['total_calls']} calls, "
              f"{TPS_STATS['total_completion_tokens']:,} toks, "
              f"{TPS_STATS['total_seconds']:.0f}s, {avg_tps:.1f} tok/s, "
              f"{TPS_STATS['failures']} failures")

    print("\nValidating...")
    subprocess.run(["python3", str(Path(__file__).parent / "check_questions.py")])


if __name__ == "__main__":
    main()
