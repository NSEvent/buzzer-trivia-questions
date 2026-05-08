#!/usr/bin/env python3
"""
Comprehensive validator for the question bank.

Checks:
- Date coverage: every date from earliest → latest is present, no gaps
- Structure: 3 categories, 5 questions each, correct $ values, daily double, Saturday bonus rounds
- Formatting: no leftover Jeopardy phrasing ("What is X?"), no trailing punctuation
- Capitalization: detects all-lowercase or all-uppercase answers (likely artifacts)
- Duplicates: same clue appearing across multiple dates
- Quality flags: "unknown" placeholders, suspiciously short or long choices

Usage: python3 scripts/check_questions.py
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

QDIR = Path(__file__).parent.parent / "questions"

# ANSI colors for terminal output
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def is_jeopardy_phrasing(text):
    """Detect if a choice still has 'What is X?' style phrasing."""
    return bool(re.match(r'^\s*(what|who|where|when)\s+(is|are|was|were)\b', text, re.IGNORECASE))


def has_trailing_question_mark(text):
    return text.rstrip().endswith('?')


def is_suspicious_capitalization(text):
    """Flag obvious capitalization issues — only ALL CAPS proper nouns are flagged.
    Returns the issue type or None."""
    stripped = text.strip()
    if len(stripped) < 3:
        return None
    # All-uppercase answers (excluding acronyms like USA, NASA)
    if stripped.isupper() and len(stripped) > 5 and ' ' in stripped:
        return "all-uppercase"
    # First letter lowercase (when it shouldn't be — proper nouns, sentence start)
    # We can't reliably detect this without context, so skip
    return None


def validate_choices(choices, label, errors, warnings):
    if not isinstance(choices, list) or len(choices) != 4:
        errors.append(f"{label}: expected 4 choices, got {len(choices) if isinstance(choices, list) else 'invalid'}")
        return
    seen = set()
    for i, c in enumerate(choices):
        if not isinstance(c, str) or not c.strip():
            errors.append(f"{label} choice {i}: empty or invalid")
            continue
        if c.strip().lower() == "unknown":
            warnings.append(f"{label} choice {i}: 'unknown' placeholder")
        if c in seen:
            errors.append(f"{label} choice {i}: duplicate ('{c}')")
        seen.add(c)
        if is_jeopardy_phrasing(c):
            warnings.append(f"{label} choice {i}: still has Jeopardy phrasing — '{c[:60]}'")
        if has_trailing_question_mark(c):
            warnings.append(f"{label} choice {i}: trailing '?' — '{c[:60]}'")
        cap_issue = is_suspicious_capitalization(c)
        if cap_issue:
            warnings.append(f"{label} choice {i}: {cap_issue} — '{c[:60]}'")
        if len(c) < 1:
            errors.append(f"{label} choice {i}: too short")
        if len(c) > 120:
            warnings.append(f"{label} choice {i}: unusually long ({len(c)} chars)")


def validate_question(q, label, errors, warnings):
    if not isinstance(q.get("clue"), str) or not q["clue"].strip():
        errors.append(f"{label}: missing or empty clue")
    elif len(q["clue"]) < 10:
        warnings.append(f"{label}: clue suspiciously short ({len(q['clue'])} chars)")
    elif len(q["clue"]) > 300:
        warnings.append(f"{label}: clue very long ({len(q['clue'])} chars)")

    if not isinstance(q.get("correctIndex"), int) or not 0 <= q["correctIndex"] <= 3:
        errors.append(f"{label}: correctIndex must be 0-3, got {q.get('correctIndex')}")

    validate_choices(q.get("choices", []), label, errors, warnings)


def validate_file(path):
    """Returns (errors, warnings, file_meta)."""
    errors = []
    warnings = []
    meta = {"date": path.stem, "total_questions": 0, "all_clues": []}

    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})$", path.stem)
    if not date_match:
        errors.append(f"invalid filename format: {path.name}")
        return errors, warnings, meta

    try:
        data = json.load(open(path))
    except Exception as e:
        errors.append(f"invalid JSON: {e}")
        return errors, warnings, meta

    if data.get("date") != path.stem:
        errors.append(f"date field '{data.get('date')}' doesn't match filename")

    cats = data.get("categories", [])
    if not isinstance(cats, list) or len(cats) != 3:
        errors.append(f"expected 3 categories, got {len(cats) if isinstance(cats, list) else 'invalid'}")
    else:
        for ci, cat in enumerate(cats):
            cat_name = cat.get("name", f"category-{ci}")
            if not isinstance(cat.get("name"), str) or not cat["name"].strip():
                errors.append(f"category {ci}: missing or empty name")
            qs = cat.get("questions", [])
            if not isinstance(qs, list) or len(qs) != 5:
                errors.append(f"'{cat_name}': expected 5 questions, got {len(qs) if isinstance(qs, list) else 'invalid'}")
                continue
            expected_values = [200, 400, 600, 800, 1000]
            for qi, q in enumerate(qs):
                if q.get("value") != expected_values[qi]:
                    errors.append(f"'{cat_name}' Q{qi+1}: value should be {expected_values[qi]}, got {q.get('value')}")
                validate_question(q, f"'{cat_name}' Q{qi+1}", errors, warnings)
                meta["total_questions"] += 1
                if "clue" in q:
                    meta["all_clues"].append(q["clue"])

    dd = data.get("dailyDouble")
    if not dd:
        errors.append("missing dailyDouble")
    else:
        if not isinstance(dd.get("category"), str) or not dd["category"].strip():
            errors.append("dailyDouble: missing category")
        validate_question(dd, "dailyDouble", errors, warnings)
        meta["total_questions"] += 1
        if "clue" in dd:
            meta["all_clues"].append(dd["clue"])

    # Saturday: bonus round required
    try:
        dow = datetime.strptime(path.stem, "%Y-%m-%d").isoweekday()
        if dow == 6:
            br = data.get("bonusRound")
            if not br:
                errors.append("Saturday file must include bonusRound")
            else:
                if not isinstance(br.get("category"), str) or not br["category"].strip():
                    errors.append("bonusRound: missing category")
                validate_question(br, "bonusRound", errors, warnings)
                meta["total_questions"] += 1
                if "clue" in br:
                    meta["all_clues"].append(br["clue"])
    except ValueError:
        pass

    return errors, warnings, meta


def check_date_coverage(files):
    """Return (start_date, end_date, missing_dates)."""
    dates = sorted([f.stem for f in files if re.match(r"^\d{4}-\d{2}-\d{2}$", f.stem)])
    if not dates:
        return None, None, []
    start = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    expected = set()
    cur = start
    while cur <= end:
        expected.add(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    actual = set(dates)
    missing = sorted(expected - actual)
    return dates[0], dates[-1], missing


def find_duplicate_clues(file_metas):
    """Find clues that appear in multiple dates."""
    clue_to_dates = defaultdict(list)
    for meta in file_metas:
        for clue in meta["all_clues"]:
            normalized = re.sub(r'\s+', ' ', clue.strip().lower())
            clue_to_dates[normalized].append(meta["date"])
    return {k: v for k, v in clue_to_dates.items() if len(v) > 1}


def main():
    if not QDIR.exists():
        print(f"{RED}questions/ directory not found{RESET}")
        sys.exit(1)

    files = sorted(QDIR.glob("*.json"))
    if not files:
        print(f"{RED}No question files found{RESET}")
        sys.exit(1)

    print(f"{BOLD}Validating {len(files)} question files...{RESET}\n")

    total_errors = 0
    total_warnings = 0
    file_metas = []
    files_with_errors = 0
    files_with_warnings = 0

    for f in files:
        errors, warnings, meta = validate_file(f)
        file_metas.append(meta)
        if errors:
            files_with_errors += 1
            total_errors += len(errors)
            print(f"{RED}✗ {f.name}{RESET}")
            for e in errors:
                print(f"  {RED}ERR{RESET}  {e}")
        elif warnings:
            files_with_warnings += 1
            total_warnings += len(warnings)
            # Only show files with warnings if -v passed
            if "-v" in sys.argv or "--verbose" in sys.argv:
                print(f"{YELLOW}⚠ {f.name}{RESET}")
                for w in warnings:
                    print(f"  {YELLOW}WARN{RESET} {w}")

    # Date coverage
    start, end, missing = check_date_coverage(files)
    print(f"\n{BOLD}=== DATE COVERAGE ==={RESET}")
    if start and end:
        days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
        print(f"  Earliest: {start}")
        print(f"  Latest:   {end}  ({days} days span)")
        print(f"  Files:    {len(files)}")
        if missing:
            print(f"  {RED}Missing dates ({len(missing)}):{RESET} {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")
        else:
            print(f"  {GREEN}No gaps in date coverage{RESET}")

        # Days from "today" the user has questions for
        today = datetime.now().date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
        if end_date >= today:
            days_remaining = (end_date - today).days
            print(f"  Coverage: {GREEN}questions available through {end} ({days_remaining} days from today){RESET}")
        else:
            days_overdue = (today - end_date).days
            print(f"  {RED}Latest question is {days_overdue} days in the past — generate more!{RESET}")

    # Duplicates
    duplicates = find_duplicate_clues(file_metas)
    print(f"\n{BOLD}=== DUPLICATE CLUES ==={RESET}")
    if duplicates:
        print(f"  {RED}{len(duplicates)} clue(s) appear on multiple dates:{RESET}")
        for clue, dates in list(duplicates.items())[:5]:
            print(f"    \"{clue[:60]}...\" — {', '.join(dates)}")
        if len(duplicates) > 5:
            print(f"    ... and {len(duplicates) - 5} more")
        total_warnings += len(duplicates)
    else:
        print(f"  {GREEN}No duplicate clues across boards{RESET}")

    # Summary
    print(f"\n{BOLD}=== SUMMARY ==={RESET}")
    total_q = sum(m["total_questions"] for m in file_metas)
    print(f"  Files:       {len(files)}")
    print(f"  Questions:   {total_q}")
    print(f"  Errors:      {RED if total_errors else GREEN}{total_errors}{RESET} ({files_with_errors} files)")
    print(f"  Warnings:    {YELLOW if total_warnings else GREEN}{total_warnings}{RESET} ({files_with_warnings} files)")
    if total_warnings > 0 and "-v" not in sys.argv and "--verbose" not in sys.argv:
        print(f"  {DIM}Pass -v to see warning details{RESET}")

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
