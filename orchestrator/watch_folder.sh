#!/usr/bin/env bash
# Watches data/incoming/registrants.csv for changes and fires run_pipeline.py.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCH_FILE="$ROOT/data/incoming/registrants.csv"
RUN() { echo "[watch_folder] change detected, running pipeline..."; python3 "$ROOT/orchestrator/run_pipeline.py" "$@"; }
if command -v fswatch >/dev/null 2>&1; then
  echo "[watch_folder] using fswatch on $WATCH_FILE"
  fswatch -0 "$WATCH_FILE" | while read -r -d "" _; do RUN "$@"; done
else
  echo "[watch_folder] fswatch not found, polling mtime every 10s"
  last=0
  while true; do
    cur=$(stat -f %m "$WATCH_FILE" 2>/dev/null || stat -c %Y "$WATCH_FILE" 2>/dev/null || echo 0)
    [ "$cur" != "$last" ] && { last="$cur"; RUN "$@"; }
    sleep 10
  done
fi
