# sdv-reference-data

Season-by-season reference data for SportsDataverse leagues. For each league it records:
- which conference, division and subdivision each team was in, season by season;
- what each group was called, abbreviated and parented at the time;
- every name and id other sources use for it.

| League | Seasons | Published as |
|---|---|---|
| College football | 1869–2026 | `cfb_groups` |
| Men's / women's college basketball | 2002–2027 | `mbb_groups`, `wbb_groups` |
| NFL | 1970–2026 | `nfl_groups` |
| NBA | 1971–2027 | `nba_groups` |
| WNBA | 1997–2026 | `wnba_groups` |
| MLB | 1901–2026 | `mlb_groups` |
| NHL | 1918–2026 | `nhl_groups` |
| College baseball | 2010–2026 | `ncaa_baseball_groups` |
| College softball | 1982–2025 | `ncaa_softball_groups` |

Tags are releases of [sportsdataverse-data](https://github.com/sportsdataverse/sportsdataverse-data). Each tag holds four
tables, in parquet and csv:
- `{league}_groups`
- `{league}_group_seasons`
- `{league}_group_aliases`
- `{league}_team_group_seasons`, plus one file per season

[CONTRACT.md](CONTRACT.md) defines the columns, the SDV group ids (`{league}:{slug}`, one per lineage) and each league's
season key. The ending year is used for NBA, NHL and college basketball.

## How it's built

- `sdv_reference/leagues/{league}.py` has a `fetch()` that snapshots the league's sources into `raw/{league}/`.
  The snapshots are committed for provenance. A re-fetch keeps the seasons already there and refreshes only the newest
  two plus any new one (RUNBOOK.md, Refresh window), so it is cheap enough to run weekly.
- Its `build()` reads only `raw/` and hand-curated, cited rows in `curated/`, so builds are offline and reproducible.
- Current names are never applied to past seasons. ESPN, stats.ncaa.org and CFBD all show today's labels for every
  season, so historical names come from dated curated rows.
- Each league is cross-checked against a second source where one exists (`sources_agree`), and tested against real
  realignments.

```sh
scripts/run_pipeline.sh cfb        # fetch -> build -> publish one league (RUNBOOK.md)
uv run python -m sdv_reference.build
uv run pytest
```

The research behind the source choices is in ClaudeCowork `notes/2026-09-26-conference-reference/`, and the design
in `specs/2026-09-26-sdv-identity-reference-design.md`.
