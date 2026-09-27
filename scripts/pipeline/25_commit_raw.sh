#!/usr/bin/env bash
# Stage 25: gate on the tests, then land refreshed raw/ snapshots on main (data-repo convention: raw refreshes go
# straight to main; code changes go through PRs). Exits 3 when raw/ is unchanged, which run_pipeline.sh treats as
# "nothing to publish". Writes build/.changed_leagues so stage 30 re-uploads only the leagues whose sources moved.
set -euo pipefail
cd "$(dirname "$0")/../.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-/mnt/sdv_repos/.uv-cache}"
rm -f build/.changed_leagues
changed=$(git status --porcelain --untracked-files=all -- raw/ | awk '{print $2}' | cut -d/ -f2 | sort -u)
if [ -z "$changed" ]; then
  echo "raw/ unchanged"
  exit 3
fi
echo "raw/ changed for: $(echo $changed)"
uv run pytest -q
git add -- raw/
git commit -q -m "chore(raw): refresh source snapshots $(date -u +%F) ($(echo $changed | tr ' ' ','))"
git fetch -q origin main
git rebase -q --merge origin/main
git push -q origin HEAD:main
echo "$changed" > build/.changed_leagues
echo "committed $(git rev-parse --short HEAD) for: $(echo $changed)"
