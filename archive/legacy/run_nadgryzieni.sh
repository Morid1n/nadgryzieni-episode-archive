#!/usr/bin/env bash
# Wrapper for the Nadgryzieni archive scraper.
# Usage: ./run_nadgryzieni.sh [--dry]
#   --dry  : perform a dry run (writes to Episode Archive.dry.md, no commit)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${NADGRYZIENI_PYTHON:-python3}"
VENV="${NADGRYZIENI_VENV:-/Users/tarkin/.hermes/hermes-agent/venv/bin/python3}"

# Prefer the historical Hermes environment when it still exists; otherwise use python3.
if [ -x "$VENV" ]; then
    PYTHON="$VENV"
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" "$SCRIPT_DIR/scrape_nadgryzieni_v2.py" "$@"
