#!/usr/bin/env bash
# Stage 10: refresh each league's raw source snapshots (network). Usage: 10_fetch_sources.sh [league ...]
# ESPN's APIs block this droplet after heavy traffic, so ESPN JSON calls go through the decodo proxy,
# whose credentials are read from ~/.Renviron at run time (never echoed). CFBD reads CFBD_API_KEY the same way.
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -z "${SDV_API_PROXY:-}" ] && [ -f "$HOME/.Renviron" ]; then
  user=$(sed -n 's/^DECODO_USER_NAME=//p' "$HOME/.Renviron" | tr -d "\"'")
  pass=$(sed -n 's/^DECODO_PASSWORD=//p' "$HOME/.Renviron" | tr -d "\"'")
  [ -n "$user" ] && [ -n "$pass" ] && export SDV_API_PROXY="http://${user}:${pass}@gate.decodo.com:7000"
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-/mnt/sdv_repos/.uv-cache}"
leagues=("$@")
# every league module, then each {league}_parks module (sdv_reference.build.available)
[ ${#leagues[@]} -gt 0 ] || mapfile -t leagues < <(uv run python -c "from sdv_reference.build import available; print(*available(), sep='\n')")
# every league runs even when one fails, so a cron run doesn't skip the rest; the stage fails at the end
failed=()
for l in "${leagues[@]}"; do
  echo "fetch $l"
  uv run python -c "from sdv_reference.build import module; module('$l').fetch()" || failed+=("$l")
done
[ ${#failed[@]} -eq 0 ] || { echo "fetch failed: ${failed[*]}"; exit 1; }
