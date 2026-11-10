#!/usr/bin/env bash
# fetch -> build -> publish, in order; pass league names to limit the run. See RUNBOOK.md.
set -uo pipefail
cd "$(dirname "$0")/.."
echo "=== run_pipeline $(date -u +%FT%TZ) $*"
fetch_rc=0
for stage in scripts/pipeline/[0-9][0-9]_*.sh; do
  SECONDS=0
  bash "$stage" "$@"; rc=$?
  echo "STAGE=$(basename "$stage" .sh) DURATION=${SECONDS}s EXIT=$rc"
  # stage 10 fetches every league before it fails, so one league's error (a CFBD 429) must not hold back the
  # others' build and publish; the run still fails at the end, so the cron alert fires
  [[ "$rc" -ne 0 && "$stage" == */10_* ]] && { fetch_rc=$rc; continue; }
  # exit 3 = nothing changed upstream: stop before publishing, successfully
  [ "$rc" -eq 3 ] && { echo "nothing to publish"; echo "EXIT=$fetch_rc"; exit "$fetch_rc"; }
  [ "$rc" -eq 0 ] || { echo "EXIT=$rc"; exit "$rc"; }
done
echo "EXIT=$fetch_rc"
exit "$fetch_rc"
