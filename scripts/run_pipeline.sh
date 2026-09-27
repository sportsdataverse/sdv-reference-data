#!/usr/bin/env bash
# fetch -> build -> publish, in order; pass league names to limit the run. See RUNBOOK.md.
set -uo pipefail
cd "$(dirname "$0")/.."
echo "=== run_pipeline $(date -u +%FT%TZ) $*"
for stage in scripts/pipeline/[0-9][0-9]_*.sh; do
  SECONDS=0
  bash "$stage" "$@"; rc=$?
  echo "STAGE=$(basename "$stage" .sh) DURATION=${SECONDS}s EXIT=$rc"
  [ "$rc" -eq 0 ] || { echo "EXIT=$rc"; exit "$rc"; }
done
echo "EXIT=0"
