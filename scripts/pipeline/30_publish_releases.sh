#!/usr/bin/env bash
# Stage 30: upload build/{league}/ to the sportsdataverse-data release {league}_groups (build/{league}_parks/ to
# {league}_parks, build/{league}_crosswalk/ to {league}_crosswalk), creating the tag if needed.
# Idempotent: only files whose sha256 differs from the release's asset digest upload (--clobber replaces them);
# a new tag gets a description over 250 characters, which the sportsdataverse-data notes generator keeps (its per-family prose lives in that repo's data-raw/families.py).
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=sportsdataverse/sportsdataverse-data
errf=$(mktemp); trap 'rm -f "$errf"' EXIT
# gh with a bounded retry: 3 tries, backoff, and only on a 5xx or a dropped connection -- a 4xx fails the same
# way every time. `--clobber` deletes before it re-uploads, so a repeat of a half-landed upload is safe.
gh_retry() {
  local attempt
  for attempt in 1 2 3; do
    gh "$@" 2>"$errf" && return 0
    cat "$errf" >&2
    grep -qE 'HTTP 4[0-9][0-9]' "$errf" && return 1
    grep -qiE 'HTTP 5[0-9][0-9]|connection (reset|refused)|timed? ?out|unexpected EOF|TLS handshake|no such host|temporary failure' "$errf" || return 1
    [ "$attempt" -lt 3 ] && { echo "gh $1 $2: transient failure (attempt $attempt/3), retrying" >&2; sleep $(( ${GH_RETRY_BACKOFF_S:-5} * attempt )); }
  done
  return 1
}
list_names() { gh api "repos/$REPO/releases/tags/$1" --jq '.assets[].name' 2>/dev/null; }
leagues=("$@")
# with no arguments: the leagues stage 25 found changed, else every built league
if [ ${#leagues[@]} -eq 0 ] && [ -f build/.changed_leagues ]; then
  mapfile -t leagues < build/.changed_leagues
fi
[ ${#leagues[@]} -gt 0 ] || mapfile -t leagues < <(ls build)
# a crosswalk's conference columns come from its league's groups: republish it when the league changed
for l in "${leagues[@]}"; do
  [ -d "build/${l}_crosswalk" ] && [[ " ${leagues[*]} " != *" ${l}_crosswalk "* ]] && leagues+=("${l}_crosswalk")
done
for l in "${leagues[@]}"; do
  [ -d "build/$l" ] || { echo "no build/$l; run stage 20 first"; exit 1; }
  if [[ "$l" == *_crosswalk ]]; then
    tag="$l"
    notes="NCAA school <-> ESPN team crosswalk for ${l%_crosswalk}, keyed on the NCAA school. ncaa_espn_team_crosswalk: one row per stats.ncaa.org team-season (season YYYY-YY, the season's NCAA team id, the school's NCAA org id) with its ESPN team id and that season's conference, and how it was matched. ncaa_espn_schools: one row per NCAA org id with its ESPN id. ncaa_espn_school_seasons: which seasons each school played men's and women's D-I basketball. Built by sportsdataverse/sdv-reference-data (sdv_reference/crosswalks/) from committed snapshots; see that repo's CONTRACT.md."
  elif [[ "$l" == *_parks ]]; then
    tag="$l"
    notes="Playing-surface reference for ${l%_parks}, by venue and season. mlb_park_dimensions: each MLB-used venue's outfield fence distances in feet (left line to right line, five to seven markers), capacity, turf, roof, azimuth, elevation and location, with the Retrosheet park id. From the MLB Stats API's per-season venue fieldInfo, 2001 on, with cited curated corrections where the API lags. Built by sportsdataverse/sdv-reference-data (sdv_reference/parks/${l%_parks}.py) from committed source snapshots; see that repo's CONTRACT.md for the schema."
  else
    tag="${l}_groups"
    notes="Season-by-season conference, division and subdivision reference for ${l}: every group's name, abbreviation and parent as of each season, every source's ids and names for it (group_aliases), and each team's affiliation by season (team_group_seasons, one file per season). Built by sportsdataverse/sdv-reference-data (sdv_reference/leagues/${l}.py) from committed source snapshots; see that repo's CONTRACT.md for the schema and season key."
  fi
  # ONE REST listing per tag: every asset carries a GitHub-computed sha256 digest, so only files whose bytes
  # changed upload (the droplet shares one 5,000/h API quota). Only a 404 means "no release yet".
  if ! listing=$(gh api "repos/$REPO/releases/tags/$tag" --jq '.assets[] | "\(.digest)  \(.name)"' 2>"$errf"); then
    grep -q "HTTP 404" "$errf" || { echo "cannot list release $tag: $(head -c 300 "$errf")"; exit 1; }
    if ! gh_retry release create "$tag" -R "$REPO" --title "$tag" --notes "$notes"; then
      # a 5xx can land the create and still fail; a repeat then gets 422 already_exists
      list_names "$tag" >/dev/null || { echo "::error ::cannot create release $tag"; exit 1; }
      echo "release $tag create failed but the tag exists; continuing"
    fi
    listing=""
  fi
  todo=()
  for pass in other parquet; do # parquet last: a matching parquet means the upload before it finished
    for f in build/"$l"/*; do
      [[ "$f" == *.parquet ]] && [ "$pass" = other ] && continue
      [[ "$f" != *.parquet ]] && [ "$pass" = parquet ] && continue
      grep -qxF "sha256:$(sha256sum "$f" | cut -d' ' -f1)  ${f##*/}" <<<"$listing" || todo+=("$f")
    done
  done
  if [ ${#todo[@]} -gt 0 ] && ! gh_retry release upload "$tag" -R "$REPO" "${todo[@]}" --clobber; then
    after=$(list_names "$tag") || after=""
    for f in "${todo[@]}"; do
      grep -qxF "${f##*/}" <<<"$after" ||
        echo "::error ::ASSET MISSING: $tag:${f##*/} was deleted by --clobber and not re-uploaded"
    done
    echo "::error ::upload to $tag failed"; exit 1
  fi
  echo "published $tag: ${#todo[@]} of $(ls build/"$l" | wc -l) files changed"
done
