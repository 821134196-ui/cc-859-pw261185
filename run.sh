#!/usr/bin/env bash
# One-command demo: initialise deterministic demo DBs and launch the UI.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"
exec python3 -m relocheck serve --port "$PORT"
