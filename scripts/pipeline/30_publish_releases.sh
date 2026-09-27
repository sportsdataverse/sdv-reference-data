#!/usr/bin/env bash
# Stage 30: upload build/{league}/ to the sportsdataverse-data release {league}_groups, creating the tag if needed.
# Idempotent: --clobber replaces same-named assets; a new tag gets a description over 250 characters, which the
# sportsdataverse-data notes generator keeps (its per-family prose lives in that repo's data-raw/families.py).
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=sportsdataverse/sportsdataverse-data
leagues=("$@")
# with no arguments: the leagues stage 25 found changed, else every built league
if [ ${#leagues[@]} -eq 0 ] && [ -f build/.changed_leagues ]; then
  mapfile -t leagues < build/.changed_leagues
fi
[ ${#leagues[@]} -gt 0 ] || mapfile -t leagues < <(ls build)
for l in "${leagues[@]}"; do
  tag="${l}_groups"
  [ -d "build/$l" ] || { echo "no build/$l; run stage 20 first"; exit 1; }
  if ! gh release view "$tag" -R "$REPO" >/dev/null 2>&1; then
    gh release create "$tag" -R "$REPO" --title "$tag" --notes "Season-by-season conference, division and subdivision reference for ${l}: every group's name, abbreviation and parent as of each season, every source's ids and names for it (group_aliases), and each team's affiliation by season (team_group_seasons, one file per season). Built by sportsdataverse/sdv-reference-data (sdv_reference/leagues/${l}.py) from committed source snapshots; see that repo's CONTRACT.md for the schema and season key."
  fi
  gh release upload "$tag" -R "$REPO" build/"$l"/* --clobber
  echo "published $tag: $(ls build/$l | wc -l) files"
done
