# Contract: the `{league}_groups` tables

Every league builder produces the same four tables. `sdv_reference.schema.validate()` enforces this contract,
and the publish step refuses anything that fails it.

## Leagues and season keys

| `league` | Season key | Example |
|---|---|---|
| `nfl`, `cfb`, `mlb`, `wnba`, `ncaa_baseball`, `ncaa_softball` | the single / starting year | CFB 2025 = fall 2025 |
| `nba`, `nhl`, `mbb`, `wbb` | the **ending** year | MBB 2025 = 2024-25 |

## Ids

- Every id column is **Utf8**, including ESPN's numeric ids (`"150"`, not `150`).
- `group_id` is SDV's own: `{league}:{slug}`, e.g. `cfb:big-ten`, `nfl:afc-east`, `mbb:maac`, `nhl:metropolitan`.
  - It names a **lineage**: one id across renames that keep continuity (Pac-10 → Pac-12 stays `cfb:pac-12`;
    American Athletic → American stays `cfb:american`).
  - A new body gets a new id: a new conference, or a merger that the sources themselves treat as new.
  - Each builder records its lineage calls in `groups.notes`.
- Leagues and subdivisions are groups too: `nfl:nfl`, `cfb:fbs`, `cfb:fcs`, `mbb:d1`.

## Tables

`groups`: one row per group lineage.

| column | type | |
|---|---|---|
| league | Utf8 | |
| group_id | Utf8 | `{league}:{slug}` |
| level | Utf8 | `league`, `subdivision`, `conference`, `division` |
| first_season, last_season | Int32 | seasons with at least one member |
| notes | Utf8 | lineage decisions, source caveats |

`group_seasons`: one row per group per season it existed.

| column | type | |
|---|---|---|
| league, group_id | Utf8 | |
| season | Int32 | |
| level | Utf8 | |
| name, short_name, abbreviation | Utf8 | **as of that season**, not today's |
| parent_group_id | Utf8 | as of that season (division → conference → subdivision → league) |
| n_teams | Int32 | members that season |

`group_aliases`: every name and id any source uses for a group, with its validity window.

| column | type | |
|---|---|---|
| league, group_id | Utf8 | |
| source | Utf8 | `espn`, `ncaa`, `cfbd`, `kenpom`, `torvik`, `sports_reference`, `fox`, `mlb`, `nhl`, `nflverse`, `nba_stats`, `wnba_stats`, `sdv` |
| source_id | Utf8 | the source's own id (ESPN group id, NCAA conf_id, CFBD id, MLB division id), when it has one |
| name_kind | Utf8 | `name`, `short_name`, `abbreviation`, `slug`, `code` |
| value | Utf8 | |
| valid_from, valid_to | Int32 | seasons (inclusive); null = unbounded |

`team_group_seasons`: one row per team per season.

| column | type | |
|---|---|---|
| league, season | Utf8, Int32 | |
| team_id | Utf8 | the ESPN team id where ESPN covers the team; otherwise the league's own id |
| team_id_source | Utf8 | `espn`, `mlb`, `nhl`, `ncaa_org`, `nba_stats`, … |
| team_name | Utf8 | as of that season |
| subdivision_id, conference_id, division_id | Utf8 | SDV group ids; null where the level doesn't apply |
| source | Utf8 | where the membership came from |
| sources_agree | Boolean | null when only one source covers the season |
| notes | Utf8 | |

## Invariants (checked by `validate`)

1. Columns and dtypes as above; no nulls in keys.
2. `group_seasons` has no duplicate `(group_id, season)`, and `team_group_seasons` has no duplicate `(team_id, season)`.
3. Every referenced group id exists in `groups`, and in `group_seasons` for that season.
4. Every `group_id` starts with `{league}:`.
5. Each `groups.first_season` / `last_season` matches `group_seasons`.

## Rules for builders

- **Membership comes from the most reliable per-season source**, and today's labels are never applied to the past.
  The research notes list which source is reliable where; ESPN, stats.ncaa.org, CFBD and Fox all back-apply today's names.
- **Cross-check** with a second source wherever one exists, and set `sources_agree`.
- **Keep season keys straight at every boundary.** Fox's `season` is the starting year, and NHL ids read `19171918`.
- **Assert real realignments** in tests, e.g.:
  - Texas/Oklahoma → SEC (CFB 2024, MBB 2025)
  - USC/UCLA → Big Ten
  - Nebraska → Big Ten 2011
  - Houston Astros → AL West 2013
  - the NFL's 2002 realignment
  - the NHL's 2014 realignment
  - Maryland → Big Ten 2014-15
