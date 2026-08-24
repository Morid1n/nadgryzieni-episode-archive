#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/tarkin/.hermes/profiles/r2-d2/scripts/nadgryzieni_repo"
cd "$REPO_DIR"
exec /usr/bin/env python3 "$REPO_DIR/nadgryzieni_upcoming.py"
