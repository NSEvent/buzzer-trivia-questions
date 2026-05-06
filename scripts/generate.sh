#!/bin/bash
# Generate daily trivia boards using Claude Code CLI.
# Usage: ./scripts/generate.sh [start_date] [num_days]
# Example: ./scripts/generate.sh 2026-05-06 14

set -uo pipefail

START_DATE="${1:-$(date +%Y-%m-%d)}"
NUM_DAYS="${2:-7}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
QUESTIONS_DIR="$SCRIPT_DIR/../questions"

mkdir -p "$QUESTIONS_DIR"

dates=()
for ((i=0; i<NUM_DAYS; i++)); do
  if [[ "$OSTYPE" == "darwin"* ]]; then
    d=$(date -j -v+"${i}d" -f "%Y-%m-%d" "$START_DATE" +%Y-%m-%d)
  else
    d=$(date -d "$START_DATE + $i days" +%Y-%m-%d)
  fi
  dates+=("$d")
done

echo "Generating $NUM_DAYS boards starting from $START_DATE"
echo "Dates: ${dates[*]}"
echo ""

for date in "${dates[@]}"; do
  outfile="$QUESTIONS_DIR/$date.json"

  if [[ -f "$outfile" ]]; then
    echo "⏭  $date.json already exists, skipping"
    continue
  fi

  if [[ "$OSTYPE" == "darwin"* ]]; then
    dow=$(date -j -f "%Y-%m-%d" "$date" +%u)
  else
    dow=$(date -d "$date" +%u)
  fi

  bonus=""
  if [[ "$dow" == "6" ]]; then
    bonus=" This is Saturday, so ALSO include a bonusRound field with category, clue, 4 choices, correctIndex."
  fi

  echo "🎯 Generating $date (day $dow)..."

  tmpfile=$(mktemp)

  claude -p --output-format text --model sonnet "Generate a daily trivia game JSON for $date. Output ONLY compact JSON (no pretty-printing, no markdown). Rules: 3 categories with 5 questions each. Values: 200,400,600,800,1000. Each question: clue (short, 1 sentence), 4 Jeopardy-style choices, correctIndex 0-3. Include dailyDouble with category, clue, choices, correctIndex. Keep clues concise. Diverse topics. Factually accurate.${bonus}" < /dev/null > "$tmpfile" 2>/dev/null || true

  # Extract and pretty-print JSON
  if python3 "$SCRIPT_DIR/extract_json.py" "$tmpfile" "$outfile" 2>/dev/null; then
    echo "  ✅ $date.json"
  else
    echo "  ❌ $date.json — failed to extract valid JSON"
    rm -f "$outfile"
  fi

  rm -f "$tmpfile"
done

echo ""
echo "Running validation..."
node "$SCRIPT_DIR/validate.js"
