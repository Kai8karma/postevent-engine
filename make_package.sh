#!/usr/bin/env bash
# Builds a judge-ready submission zip: dist/postevent-engine-<date-arg>.zip
#
# Usage: ./make_package.sh [date-arg]     (default: "submission")
#
# Includes the whole repo except: _internal/, out/ (except a freshly
# generated out/sample-run/, added back explicitly so judges see real
# output without running anything, plus out/clay-live-proof/ and
# out/live-proof/ when present -- the live-lane receipts), dist/,
# .DS_Store, __pycache__/, *.pyc, and modules/*/out/ (stray local test output).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DATE_ARG="${1:-submission}"
ZIP_NAME="postevent-engine-${DATE_ARG}.zip"
ZIP_PATH="dist/${ZIP_NAME}"

mkdir -p dist
rm -f "$ZIP_PATH"

echo "Generating fresh out/sample-run/ (offline lane, zero network)..."
rm -rf out/sample-run
python3 orchestrator/run_pipeline.py --out out/sample-run

echo "Zipping repo (excluding _internal/, out/*, dist/, caches)..."
zip -r -q "$ZIP_PATH" . \
  -x "_internal/*" "_internal" \
  -x "out/*" "out" \
  -x "dist/*" "dist" \
  -x "*.DS_Store" \
  -x "*__pycache__*" \
  -x "*.pyc" \
  -x "modules/*/out/*" "modules/*/out"

echo "Adding out/sample-run/ back in..."
zip -r -q "$ZIP_PATH" out/sample-run

# Live-lane receipts (present only if the live runs were made on this machine):
for proof in out/clay-live-proof out/live-proof; do
  if [ -d "$proof" ]; then
    echo "Adding $proof/ (live receipt) ..."
    zip -r -q "$ZIP_PATH" "$proof" -x "*__pycache__*" -x "*.DS_Store"
  fi
done

FILE_COUNT="$(unzip -l "$ZIP_PATH" | tail -1 | awk '{print $2}')"
ZIP_SIZE="$(du -h "$ZIP_PATH" | cut -f1)"

echo ""
echo "Package built: ${ZIP_PATH}"
echo "Size: ${ZIP_SIZE}"
echo "Files: ${FILE_COUNT}"
