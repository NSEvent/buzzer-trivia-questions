#!/usr/bin/env python3
"""
Strip leftover escape artifacts from clue/choice strings.

The Jeopardy dataset's raw text often contains escaped quotes like \" inside
the clue or answer fields. When the dataset was loaded into our generators,
these escape sequences survived literally — so the JSON contains strings
like \\"Like A Rolling Stone\\" that render with visible backslashes.

This script unescapes those sequences in-place across all question files.
"""
import json
import re
from pathlib import Path

QDIR = Path(__file__).parent.parent / "questions"


def clean(text):
    if not isinstance(text, str):
        return text
    # Replace \" with " (literal backslash-quote → quote)
    text = text.replace('\\"', '"')
    # Replace \\ with \ (double backslash → single)
    text = text.replace('\\\\', '\\')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def clean_question(q):
    if "clue" in q:
        q["clue"] = clean(q["clue"])
    if "category" in q and isinstance(q["category"], str):
        q["category"] = clean(q["category"])
    if "choices" in q and isinstance(q["choices"], list):
        q["choices"] = [clean(c) for c in q["choices"]]
    return q


def main():
    files_changed = 0
    fields_changed = 0
    for f in sorted(QDIR.glob("*.json")):
        original = f.read_text()
        data = json.loads(original)

        # Category names
        for cat in data.get("categories", []):
            if "name" in cat:
                old = cat["name"]
                cat["name"] = clean(old)
                if cat["name"] != old:
                    fields_changed += 1
            for q in cat.get("questions", []):
                before = json.dumps(q)
                clean_question(q)
                if json.dumps(q) != before:
                    fields_changed += 1

        for key in ("dailyDouble", "bonusRound"):
            if data.get(key):
                before = json.dumps(data[key])
                clean_question(data[key])
                if json.dumps(data[key]) != before:
                    fields_changed += 1

        new_text = json.dumps(data, indent=2)
        if new_text != original:
            files_changed += 1
            f.write_text(new_text)

    print(f"Cleaned {fields_changed} fields across {files_changed} files")


if __name__ == "__main__":
    main()
