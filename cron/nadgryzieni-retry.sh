#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export NADGRYZIENI_RUN_KIND=retry
exec python3 "$SCRIPT_DIR/../nadgryzieni_pipeline.py" "$@"
