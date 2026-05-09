#!/usr/bin/env python3
"""
Generate manifest.json describing the question bank's date range.

The app's QuestionLoader uses this to wrap around when today's date is
beyond the bank's range. Without it, users past the last date would
just see the bundled fallback forever.

Usage: python3 scripts/generate_manifest.py

Run after adding boards. The output `manifest.json` lives at the repo
root and is fetched by the app on launch.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
QDIR = REPO / "questions"


def main():
    files = sorted(QDIR.glob("*.json"), key=lambda f: f.stem)
    if not files:
        print("No question files found")
        return

    dates = [f.stem for f in files]
    start = dates[0]
    end = dates[-1]

    # Verify no gaps (the wrap-around math assumes consecutive days)
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    expected = (e - s).days + 1
    if len(dates) != expected:
        missing = []
        cur = s
        actual = set(dates)
        while cur <= e:
            ds = cur.strftime("%Y-%m-%d")
            if ds not in actual:
                missing.append(ds)
            cur += timedelta(days=1)
        print(f"⚠ Bank has {len(missing)} gaps — wrap-around may skip dates")
        print(f"  Missing: {missing[:5]}{'...' if len(missing) > 5 else ''}")

    manifest = {
        "start": start,
        "end": end,
        "totalDays": len(dates),
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    out = REPO / "manifest.json"
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"✅ Wrote {out}")
    print(f"   start: {manifest['start']}")
    print(f"   end:   {manifest['end']}")
    print(f"   total: {manifest['totalDays']} days")


if __name__ == "__main__":
    main()
