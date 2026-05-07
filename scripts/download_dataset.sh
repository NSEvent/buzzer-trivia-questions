#!/bin/bash
# Download the 538K Jeopardy clue dataset.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/../data"
mkdir -p "$DATA_DIR"
echo "Downloading 538K clue dataset..."
curl -sL "https://raw.githubusercontent.com/jwolle1/jeopardy_clue_dataset/main/combined_season1-41.tsv" -o "$DATA_DIR/clues.tsv"
echo "✅ Downloaded $(wc -l < "$DATA_DIR/clues.tsv") clues to data/clues.tsv"
