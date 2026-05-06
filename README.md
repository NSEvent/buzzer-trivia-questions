# Buzzer Trivia Questions

Daily Jeopardy-style trivia question bank for [Buzzer Trivia](https://github.com/NSEvent/jeopardy-buzzer-trivia-tv-os-app).

## Structure

```
questions/
├── 2026-05-06.json          # Weekday: 3 categories + daily double
├── 2026-05-10-saturday.json # Saturday: includes bonusRound
└── ...
schema/
└── daily-game.schema.json   # JSON Schema for validation
scripts/
├── generate.sh              # Generate boards using Claude Code CLI
└── validate.js              # Validate all question files
```

## Generating Questions

```bash
# Generate 7 days starting from today
./scripts/generate.sh

# Generate 14 days starting from a specific date
./scripts/generate.sh 2026-05-06 14
```

Uses `claude -p` (Claude Code CLI) to generate factually accurate, Jeopardy-style multiple choice questions with scaled difficulty ($200=easy → $1000=hard).

## Validating

```bash
node scripts/validate.js
```

Checks: 3 categories, 5 questions each with correct values [200-1000], 4 choices per question, correctIndex 0-3, Saturday files include bonusRound.

## JSON Format

Each daily file contains 3 categories × 5 questions + 1 daily double. Saturday files also include a bonus round.

Choices are phrased Jeopardy-style: "What is Stockholm?", "Who is Marie Curie?", etc.
