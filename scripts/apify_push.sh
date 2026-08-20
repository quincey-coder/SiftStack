#!/usr/bin/env bash
# Deploy the Actor. ALWAYS use this instead of a bare `apify push` from repo root.
#
# Why: apify-cli 1.8.0 zips the ENTIRE git-tracked file set (it does not honor
# .apifyignore), and once the repo vendored Ty's skill library (2026-08-20) that
# upload grew to ~14 MB — which reliably truncates in the CLI's uploader and the
# platform build then dies with "Failed to download the archive: unzip exited
# with code 9". Staging only what the Dockerfile consumes keeps the upload ~2 MB
# and deterministic. (Verified: bare push failed twice, staged push built 1.1.86.)
set -euo pipefail
cd "$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"

STAGE="$(mktemp -d)/apify_stage"
mkdir -p "$STAGE"
git archive HEAD Dockerfile requirements.txt src .actor .apifyignore | tar -x -C "$STAGE"
echo "Staged $(find "$STAGE" -type f | wc -l) tracked files -> $STAGE"

APIFY_BIN="${APIFY_BIN:-apify}"
command -v "$APIFY_BIN" >/dev/null 2>&1 || \
  APIFY_BIN="/c/Users/Q/AppData/Local/Microsoft/WinGet/Packages/OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe/node-v24.19.0-win-x64/apify"

(cd "$STAGE" && "$APIFY_BIN" push --force "$@")
rm -rf "$(dirname "$STAGE")"
