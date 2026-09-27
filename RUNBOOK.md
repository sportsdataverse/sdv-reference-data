# Runbook

`scripts/run_pipeline.sh [league ...]` runs every stage in order (all leagues when none are named).

| Stage | Script | Frequency | Idempotency | Typical duration |
|---|---|---|---|---|
| 10 | `scripts/pipeline/10_fetch_sources.sh [league ...]` | weekly (see [Cadence](#cadence)); any time a season starts | refreshes only the newest seasons in `raw/{league}/` ([Refresh window](#refresh-window)); an unchanged source rewrites the same bytes; commit what changes | ~2 min for NHL + NBA; ~15 min for all leagues (~650 ESPN requests at ~1 req/s) |
| 20 | `scripts/pipeline/20_build_tables.sh [league ...]` | after every stage 10, and after any `curated/` change | offline, from `raw/` + `curated/` (same tables every run); refuses tables that fail `CONTRACT.md` | ~10 s for all leagues |
| 30 | `scripts/pipeline/30_publish_releases.sh [league ...]` | after a reviewed build whose `raw/` or `curated/` changed | `--clobber` per asset; creates `{league}_groups` once | ~1 min per league |

Stage 10 changes committed data: review the `raw/` diff and rebuild before publishing. Run stages individually when
you only need to rebuild or republish.

## Refresh window

Every `fetch()` sends its per-season sources through `sdv_reference/refresh.py`:
- seasons already in `raw/` are kept;
- only the newest `SDV_REF_REFRESH_SEASONS` (default 2) seasons in `raw/` are fetched again, plus any season newer
  than `raw/`, up to the league's current season (`current_season()`: the calendar year, or next year from July for
  the ending-year leagues);
- a season the source doesn't have yet comes back empty and stays absent, with no error:
  - ESPN answers an unknown season with an empty group list;
  - the NHL lists a season in `standings-season` before it has standings;
  - the MLB Stats API returns no teams.
- a season missing from `raw/` that is older than the window is a gap in the source (ESPN has no NHL groups for
  1974-92), so it is never re-probed.

Overrides:
- `SDV_REF_REFRESH_SEASONS=1` halves the ESPN load, e.g. for an extra run on a season's opening week.
- `SDV_REF_REFRESH_SEASONS=200` re-walks everything (about 11,300 ESPN requests; only through `$SDV_API_PROXY`).

Snapshots keep no fetch timestamps and no in-season counters, so a weekly run shows a diff only when membership,
names or ids change. Specifically:
- NHL `gamesPlayed` is dropped.
- nflverse `games.csv` is cut to `game_id, season, away_team, home_team, div_game`.
- NBA/WNBA stats standings keep only the identity columns.
- Standings lists are sorted by team.

## Requests per stage-10 run with `raw/` already populated

"After" is measured: every HTTP request was counted (a `requests.Session.request` counter on `PYTHONPATH`) during real
stage-10 runs on 2026-09-27. "Before" is origin/main at `8a7872b`, computed from its code against the committed `raw/`.
The same computation predicted every measured "after" figure exactly.

| League | Sources | Before | After (window 2) |
|---|---|---|---|
| cfb | CFBD conferences + affiliations (all seasons, 1 call each); ESPN FBS/FCS tree; cfbfastR-cfb-data teams (local) | 2 (ESPN copied from the 2026-09-26 research cache: never refreshed, fails if that cache is gone) | 128: CFBD 2, ESPN 126 (2025-26) |
| mbb | ESPN D-I tree + site.web standings; sdv-py crosswalks (local git) | 0 (skip-if-exists: 2026-27 never refreshed; ceiling hardcoded at 2027) | 134 (2026-27) |
| wbb | same as mbb | 0 (same) | 134 (2026-27) |
| nfl | nflverse `games.csv` (all seasons, 1 call); ESPN tree + team objects | 55: GitHub 1, ESPN 54 (2026 only; ceiling hardcoded at 2026) | 109: GitHub 1, ESPN 108 (2025-26) |
| nba | stats.nba.com standings (local hoopR-nba-stats-raw copy); ESPN tree | 36 (2026-27; ceiling hardcoded at 2027) | 36 (2026-27) |
| wnba | stats.wnba.com standings (local wehoop copy); ESPN tree + team objects | 23 (2026 only; ceiling hardcoded at 2026) | 43 (2025-26) |
| mlb | MLB Stats API teams/leagues/divisions; ESPN tree | 344: Stats API 310 (every season 1901-2026), ESPN 34 | 40: Stats API 6, ESPN 34 |
| nhl | NHL web standings; NHL stats REST (all seasons, 1 call each x2); ESPN tree | 206: web 109 (every season), REST 2, ESPN 95 (26 + 69 empty re-probes of seasons ESPN lacks) | 31: web 3, REST 2, ESPN 26 |
| ncaa_baseball | baseballr-data `ncaa_team_lookup.parquet` (local) | 0 | 0 |
| ncaa_softball | softballR-data `ncaa_team_info.RDS` (local, Rscript) | 0 | 0 |
| **all** | | **666**, 242 of them ESPN; new seasons need code edits in 5 leagues | **655**, 641 of them ESPN; new seasons arrive by themselves |

Verified 2026-09-27:
- NHL + NBA were fetched twice in a row: 67 requests each run, and `git status raw/` was clean after the second.
- mbb's second run was byte-identical.
- Every league refreshed once, and all 10 builds are table-equal to origin/main's.
- The only first-run diffs are the one-time format changes above.

After the change there are more ESPN requests than before, because cfb, mbb and wbb now refresh their current seasons
and nfl/wnba refresh two seasons, not one. The number is bounded by the window, not the history. A full ESPN re-walk
of every season in `raw/` is about 11,300 requests.

## Cadence

- **Stage 10, weekly, all leagues.** With the window, an off-season run costs ~15 minutes, and its diff is empty
  unless a source changed. In-season it catches mid-season realignment fixes and each new season on its own. New
  seasons appear when their source creates them:
  - CFB: once CFBD has a spell in the new season
  - NFL/ESPN: late summer
  - NHL: opening night, early October
  - NBA: October (ESPN has 2026-27 already)
  - MBB/WBB: ESPN's new tree, around October
  - MLB: January (Stats API)
  - WNBA: spring
  - NCAA baseball/softball: when baseballr-data / softballR-data refresh their files
- **Stage 20** after every stage 10, gated by `uv run pytest -q` (the realignment assertions).
- **Stage 30** only for leagues whose `raw/` or `curated/` changed, after the diff has been reviewed.
  - Don't use build hashes to detect a change. Byte order is not stable across builds: rows that tie on the output sort
    keys (`cfb`/`ncaa_*` group_aliases) and the example list in the `nhl:nhl` notes vary run to run on the same `raw/`.
  - Use `git diff --quiet raw/ curated/` instead.

Proposed cron line (not installed). It runs weekly on Mondays at 09:17 UTC; the droplet clock drifts, so keep it off
the hour:

```
17 9 * * 1 cd /mnt/sdv_repos/sdv-reference-data && mkdir -p logs && { git pull -q --ff-only && scripts/run_pipeline.sh; echo "EXIT=$?"; } >> logs/pipeline.log 2>&1
```

Watch it with `tail -f /mnt/sdv_repos/sdv-reference-data/logs/pipeline.log`; each stage logs `STAGE=… DURATION=… EXIT=…`.

### Landing `raw/` changes (proposal, not implemented)

- The README commits snapshots "for provenance", and this runbook asks for review before publishing. So scheduled
  runs should not publish unreviewed data.
- Proposed: `run_pipeline.sh` gains a `25_land_raw.sh` stage between build and publish:
  1. `git diff --quiet raw/`: nothing changed, so print `RAW=unchanged` and stop before stage 30 (nothing to
     republish).
  2. Otherwise run `uv run pytest -q`, then commit only `raw/` on a dated branch `raw-refresh/YYYY-MM-DD` as
     `chore(raw): refresh <leagues> (YYYY-MM-DD)`, one commit per run and no AI trailers. Push that branch, open a PR
     whose body lists the changed leagues and seasons, and stop before stage 30.
  3. After the PR merges, run `scripts/pipeline/30_publish_releases.sh <leagues>` from `main`.
- A deliberate stop before 30 exits with code 3, which `run_pipeline.sh` treats as a clean stop rather than a failure.
- Alternative, if raw snapshots count as data under the org rule "data repos commit to main":
  - commit `raw/` straight to `main` and publish in the same run;
  - `validate()` and the tests are then the only review.
