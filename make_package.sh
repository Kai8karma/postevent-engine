#!/usr/bin/env bash
# Builds the reviewer-facing submission zip: dist/postevent-engine-<tag>.zip
#
# Usage: ./make_package.sh [name]        (default: the short commit sha)
#        ./make_package.sh --with-sample-run [name]
#
# The zip is produced with `git archive` from HEAD, so it contains exactly what is
# committed -- no more, no less. That matters for two reasons:
#   1. The 460 MB of webinar MP4/MP3 source media under data/incoming/media/ is
#      ignored by git. A plain `zip -r .` does NOT read .gitignore and would ship
#      all of it; git archive cannot.
#   2. The zip, the git tag and any deploy then describe the same tree, so a link
#      in the submission email resolves to the same bytes a reviewer unzips.
#
# _internal/ (self-assessment), _archive/ and dist/ are gitignored and therefore
# absent automatically. out/receipts/ IS tracked and ships -- it is the evidence.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WITH_SAMPLE_RUN=0
if [ "${1:-}" = "--with-sample-run" ]; then
  WITH_SAMPLE_RUN=1
  shift
fi

SHA="$(git rev-parse --short HEAD)"
NAME="${1:-$SHA}"
ZIP_PATH="dist/postevent-engine-${NAME}.zip"
mkdir -p dist
rm -f "$ZIP_PATH"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "WARNING: working tree has uncommitted changes. The zip is built from HEAD (${SHA})"
  echo "         and will NOT contain them. Commit first if they belong in the submission."
  echo ""
fi

echo "Archiving HEAD (${SHA})..."
git archive --format=zip --prefix="postevent-engine/" -o "$ZIP_PATH" HEAD

if [ "$WITH_SAMPLE_RUN" = "1" ]; then
  # Offline lane only. Live is the default in v2, so omitting --offline here would
  # spend real API calls during packaging.
  echo "Generating out/sample-run/ on the offline lane (zero network)..."
  rm -rf out/sample-run
  python3 orchestrator/run_pipeline.py --offline --out out/sample-run
  echo "Adding out/sample-run/ to the archive..."
  zip -r -q "$ZIP_PATH" out/sample-run -x "*__pycache__*" -x "*.DS_Store"
fi

RECEIPTS="$(unzip -l "$ZIP_PATH" | grep -c 'postevent-engine/out/receipts/' || true)"
MEDIA="$(unzip -l "$ZIP_PATH" | grep -cE '\.(mp4|mp3)$' || true)"
FILE_COUNT="$(unzip -l "$ZIP_PATH" | tail -1 | awk '{print $2}')"
ZIP_SIZE="$(du -h "$ZIP_PATH" | cut -f1)"

echo ""
echo "Package:  ${ZIP_PATH}"
echo "Commit:   ${SHA}"
echo "Size:     ${ZIP_SIZE}"
echo "Files:    ${FILE_COUNT}  (receipt files: ${RECEIPTS})"

if [ "$MEDIA" -gt 0 ]; then
  echo "ERROR: ${MEDIA} media file(s) leaked into the archive." >&2
  exit 1
fi
if [ "$RECEIPTS" -lt 1 ]; then
  echo "ERROR: no out/receipts/ files in the archive -- the evidence is missing." >&2
  exit 1
fi
echo "Checks:   no source media, receipts present."
