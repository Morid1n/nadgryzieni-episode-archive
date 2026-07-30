#!/usr/bin/env bash
# Wrapper for the Nadgryzieni archive scraper.
# Usage: ./run_nadgryzieni.sh [--dry]
#   --dry  : perform a dry run (writes to Episode Archive.dry.md, no commit)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="/Users/tarkin/.hermes/hermes-agent/venv"

# Activate the Hermes Python virtual environment
source "$VENV/bin/activate"

cd "$SCRIPT_DIR"
python3 "$SCRIPT_DIR/scrape_nadgryzieni_v2.py" "$@"