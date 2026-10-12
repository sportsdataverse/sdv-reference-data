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

# Contract: `mlb_park_dimensions` (tag `mlb_parks`)

One row per MLB venue per season: every venue the MLB Stats API lists under `sportId=1` for the season (regular-season
parks, spring-training parks, neutral and international sites), with its fences, capacity, turf and roof as of that
season. Built by `sdv_reference/parks/mlb.py` from `venues?sportId=1&season=YYYY&hydrate=fieldInfo,location,xrefId`;
`sdv_reference.parks.mlb.validate()` enforces this contract.

| column | type | |
|---|---|---|
| league | Utf8 | `mlb` |
| season | Int32 | the single year |
| venue_id | Utf8 | MLB Stats API venue id (`venue.id` in MLB game feeds and schedules) |
| venue_name | Utf8 | **as of that season** (PacBell Park 2001-03, SBC Park 2004-05, AT&T Park 2006-18, Oracle Park 2019-) |
| retro_park_id | Utf8 | Retrosheet park id (`BOS07`), from the API's xref; null for most spring-training and minor-league parks |
| left_line_ft, left_ft, left_center_ft, center_ft, right_center_ft, right_ft, right_line_ft | Int32 | feet from home plate to the fence at MLB's seven markers, left-field pole to right-field pole. Each park publishes five to seven; the API gives no angles, and a park may re-label a marker (Oracle Park's `left_center_ft` is 364 through 2019, then the 399 ft deep left-centre) |
| capacity | Int32 | seats |
| turf_type | Utf8 | `Grass`, `Artificial Turf` |
| roof_type | Utf8 | `Open`, `Retractable`, `Dome` |
| azimuth_deg | Float64 | MLB's `azimuthAngle`: degrees clockwise from north of the line from home plate to centre field (Fenway 45, Progressive Field 0) |
| elevation_ft | Int32 | feet above sea level |
| latitude, longitude | Float64 | decimal degrees |
| notes | Utf8 | null unless a curated correction applies: which columns changed, from what, why, and the citation |

Invariants: no nulls in `league`, `season`, `venue_id`, `venue_name`; no duplicate `(venue_id, season)`; every fence
distance within 250-500 ft. Venues with no fence distance and no capacity (the API's `TBD`, `AL Stadium` and
`NL Stadium` placeholders) are left out.

Coverage is **2001 on**. The API answers every season from 1901, but for 1901-2000 it returns one undated record per
venue: no venue's fieldInfo differs between any two of those seasons (probed 2026-09-27: every season 1985-2026 and
every fifth season 1901-1980). Yankee Stadium I in 1925 carries its 1988-2008 fences and the Oakland Coliseum in 1970 its
post-1996 capacity, so those seasons are not fetched. Per-season records begin in 2001 (13 capacities change from
2000); fence changes appear from 2006. Early fences can still be a configuration MLB recorded later: Comerica Park's 2003
left-centre move never appears.

The API lags or misses some fence moves. `curated/mlb_park_overrides.csv` corrects them, one cited row per venue,
season range and column (an empty `valid_to` is open-ended), and the build refuses a correction that no longer changes
anything. Corrected so far: Camden Yards 2022 (recorded from 2023) and a 2017 key shift; Petco Park 2013-14 (recorded
from 2015); T-Mobile Park 2019- (reverts to the pre-2013 fences); Comerica Park 2023- (412 ft centre field, missing);
one-season 2022 errors at Rate Field and Progressive Field. Known and not corrected: Rogers Centre's 2023 walls
(the API keeps 328-375-400/404-375-328).

Seamheads' ballpark database is not used, even as a cross-check in tests: its licence forbids redistribution.

# Contract: the NCAA <-> ESPN basketball crosswalk (tags `mbb_crosswalk`, `wbb_crosswalk`)

Keyed on the **NCAA school** (`ncaa_org_id`, stats.ncaa.org's org id). ESPN college team ids are school-level too (one
id for a school's men's and women's programs, and its football team), so one school key serves every sport; college
baseball is excluded, because ESPN's baseball team ids are a separate id system. Built by
`sdv_reference/crosswalks/_ncaa_espn.py` (moved from sdv-py's `tools/crosswalk/`); `validate()` enforces this contract.
Each tag holds three tables, in parquet and csv; the last two are identical in both tags.

`ncaa_espn_team_crosswalk`: one row per stats.ncaa.org team-season of that league. Its first 13 columns are exactly
sdv-py's `ncaa_espn_team_crosswalk(league=)`, which reads this asset.

| column | type | |
|---|---|---|
| season | Utf8 | `YYYY-YY` (`2025-26`), stats.ncaa.org's label |
| ncaa_team_id | Int64 | stats.ncaa.org's team id, new every season |
| ncaa_org_id | Int64 | the school's stats.ncaa.org org id, stable across seasons and sports |
| ncaa_team, ncaa_conference | Utf8 | stats.ncaa.org's name; the conference's NCAA label that season (from `{league}_groups`), else stats.ncaa.org's |
| conference_id | Utf8 | SDV group id that season (`mbb:big-ten`) |
| espn_team_id | Utf8 | null only when unresolved |
| espn_display_name, espn_location, espn_mascot, espn_abbreviation | Utf8 | ESPN's names (today's) |
| espn_conference_name, espn_conference_id | Utf8 | that season's, from `{league}_groups` |
| match_method | Utf8 | `alias`, `exact`, `dict`, `org` (the school's id from its other seasons), `ambiguous`, `unmatched` |

`ncaa_espn_schools`: one row per `ncaa_org_id`: its latest `ncaa_team`, `espn_team_id` and `espn_display_name`.
`ncaa_espn_school_seasons`: one row per `(ncaa_org_id, sport, season)` the school played D-I basketball (`sport` =
`mbb`/`wbb`), with that season's `ncaa_team_id` and `espn_team_id`.

Invariants: every row of the NCAA team list is kept (never dropped); no duplicate `(season, ncaa_team_id)`; an ESPN id
is on at most one team per season; each school resolves to one ESPN id and each ESPN id to one school (the build fails
otherwise). There is no fuzzy matching: a new name is resolved by `curated/ncaa_espn_aliases.csv` (each row verified by
hand) or by its school's id from other seasons. Coverage ends where the NCAA team list does
(`raw/{league}_crosswalk/ncaa_teamids.csv.gz`, from sdv-py's bundled `ncaa_teamids_{league}.csv`): 2009-10 to 2025-26.

# Contract: the NCAA <-> ESPN college-baseball crosswalk (tag `ncaa_baseball_crosswalk`)

Keyed on the **NCAA school** (`ncaa_team_id` = stats.ncaa.org's org id, the `team_id` of `ncaa_baseball_groups`). ESPN's
college-baseball team ids are a separate id system from its football/basketball school ids (Alabama is 148 in
baseball, 333 elsewhere), so this is its own map. Built by `sdv_reference/crosswalks/ncaa_baseball.py`; `validate()`
enforces this contract. D-I only, 2010 on (the `ncaa_baseball` lookup snapshot).

`ncaa_baseball_espn_team_crosswalk`: one row per stats.ncaa.org D-I team-season.

| column | type | |
|---|---|---|
| season | Int64 | the spring year (`2026`) |
| ncaa_team_id | Int64 | the school's stats.ncaa.org org id, stable across seasons (filled by `ncaa_baseball` when the source leaves it blank) |
| ncaa_season_team_id | Int64 | stats.ncaa.org's per-season team id |
| ncaa_team, ncaa_conference | Utf8 | stats.ncaa.org's name and conference label that season |
| conference_id | Utf8 | SDV group id that season (`ncaa_baseball:sec`); null for an independent |
| espn_team_id | Utf8 | ESPN college-baseball team id; null only when unresolved |
| espn_display_name, espn_location, espn_mascot, espn_abbreviation | Utf8 | ESPN's names (today's) |
| match_method | Utf8 | `alias`, `exact`, `school_hint` (the school's ESPN basketball name, from `{mbb,wbb}_crosswalk`, looked up in the baseball list), `org` (the school's id from its other seasons), `ambiguous`, `unmatched` |

`ncaa_baseball_espn_schools`: one row per `ncaa_team_id`: latest `ncaa_team`, `first_season`, `last_season`,
`espn_team_id`, `espn_display_name`. `ncaa_baseball_espn_unmatched.csv`: the schools with no ESPN id, for triage.

Invariants: as the basketball crosswalk (every D-I team-season kept, unique `(season, ncaa_team_id)`, an ESPN id on at
most one team per season, one ESPN id per school and one school per ESPN id), plus every ESPN id is in the snapshot of
ESPN's team list (`raw/ncaa_baseball_crosswalk/espn_teams.csv.gz`, 437 teams on 2026-10-12). No fuzzy matching: a new
name is resolved by `curated/ncaa_baseball_espn_aliases.csv` (each row verified by hand).
