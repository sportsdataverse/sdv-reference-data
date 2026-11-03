# Contract: the `{league}_groups` tables

Every league builder produces the same four tables (eight leagues add a fifth, `team_aliases`, below). `sdv_reference.schema.validate()` enforces this contract,
and the publish step refuses anything that fails it.

## Leagues and season keys

| `league` | Season key | Example |
|---|---|---|
| `nfl`, `cfb`, `mlb`, `wnba`, `ncaa_baseball`, `ncaa_softball` | the single / starting year | CFB 2025 = fall 2025 |
| `nba`, `nhl`, `mbb`, `wbb` | the **ending** year | MBB 2025 = 2024-25 |
| `ncaa_mhockey`, `ncaa_whockey` | the **ending** year | 2026 = 2025-26 |

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
| source | Utf8 | `espn`, `ncaa`, `cfbd`, `kenpom`, `torvik`, `sports_reference`, `fox`, `mlb`, `nhl`, `nflverse`, `nba_stats`, `wnba_stats`, `uscho`, `sdv` |
| source_id | Utf8 | the source's own id (ESPN group id, NCAA conf_id, CFBD id, MLB division id), when it has one |
| name_kind | Utf8 | `name`, `short_name`, `abbreviation`, `slug`, `code` |
| value | Utf8 | |
| valid_from, valid_to | Int32 | seasons (inclusive); null = unbounded |

`team_group_seasons`: one row per team per season, per id space. A league keyed on its own ids may repeat a team under
its ESPN id: `ncaa_baseball` and `ncaa_softball` keep every stats.ncaa.org row (`ncaa_org`) and add an `espn` copy
for each school their crosswalk (`ncaa_baseball_crosswalk`, `ncaa_softball_crosswalk`) maps, for every season of that
school (its non-D-I seasons too), with `ncaa_org_id=N` first in `notes`. Read one `team_id_source` at a time: ESPN
college-baseball and -softball ids reuse other schools' NCAA org ids.

`ncaa_softball`'s source (a Feb 2025 stats.ncaa.org scrape) ends at 2025, so its 2026 rows are derived: each 2025 school
takes its stats.ncaa.org baseball 2026 conference and division where its softball and baseball shared both in 2025
(NCAA conf_ids are cross-sport), else keeps 2025's; `curated/ncaa_softball_rollforward.csv` (cited) corrects the rest.
Each 2026 row's `notes` says which. Programs new in 2026 are missing. 2026 `sources_agree` compares the rows with
ESPN's core college-softball groups (ten D-I conferences, `raw/ncaa_softball/espn/`); null outside them.

| column | type | |
|---|---|---|
| league, season | Utf8, Int32 | |
| team_id | Utf8 | the ESPN team id where ESPN covers the team; otherwise the league's own id (or both, as above) |
| team_id_source | Utf8 | `espn`, `mlb`, `nhl`, `ncaa_org`, `nba_stats`, … |
| team_name | Utf8 | as of that season |
| subdivision_id, conference_id, division_id | Utf8 | SDV group ids; null where the level doesn't apply |
| source | Utf8 | where the membership came from |
| sources_agree | Boolean | null when only one source covers the season |
| notes | Utf8 | |

## Invariants (checked by `validate`)

1. Columns and dtypes as above; no nulls in keys.
2. `group_seasons` has no duplicate `(group_id, season)`, and `team_group_seasons` has no duplicate
   `(team_id_source, team_id, season)`.
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

## `team_aliases` (the fifth table: nfl, cfb, nba, wnba, mbb, wbb, mlb, nhl)

`{league}_team_aliases`: every id and name another source uses for a team, keyed on the ESPN team id, with the seasons
it held. Built by `sdv_reference/team_aliases.py`; `validate_team_aliases()` enforces this contract. It consolidates
legs SDV already has, and asks no provider for anything: the Fox / Yahoo / Torvik columns of the published team
crosswalks (`{nba,wnba,mbb,wbb,cfb}_crosswalk`, snapshotted into `raw/team_aliases/{league}/` with provenance), nfl-raw's
Fox / Yahoo / CBS player crosswalks (the team pair on each player row), and curated maps. It also matches provider team
directories captured by hand (`sdv_reference/team_directories.py`, below).

| column | type | |
|---|---|---|
| league, team_id, team_id_source | Utf8 | the ESPN team id, as in `team_group_seasons` (`team_id_source` = `espn`) |
| source | Utf8 | `fox`, `yahoo`, `cbs`, `torvik`, `kenpom`, `ncaa`, `nba_stats`, `wnba_stats`, `mlb`, `nhl`, `cfbd`, `sports247`, `on3` |
| source_id | Utf8 | the source's own id (`25`, `nba.t.1`, `193`); null for a name-only source (KenPom, Torvik) |
| id_kind | Utf8 | `team` (the source's id for this sport's team: 247 `teamId`), or `school` (one id for every sport of a school: the stats.ncaa.org org id, 247 `institutionKey`, On3 `orgKey`) |
| name_kind, value | Utf8 | `name` / `abbreviation` and the source's own spelling, when it gives one; both null otherwise. An id with a name and an abbreviation is two rows |
| valid_from, valid_to | Int32 | the seasons (inclusive, the league's season key) the source shows this id or name on the team; a gap in the source splits the window. Both null for an undated curated map (CBS college football), checked over every season the team has |
| match_method | Utf8 | `deterministic` (a formula or shared key: Yahoo NBA / NFL `{lg}.t.{espn id}`, asserted on every row; CFBD ids are ESPN ids), `crosswalk` (carried from a published crosswalk leg), `curated` (a cited map in `curated/`); for a directory row, `alias` (`curated/team_directory_aliases.csv`), `exact_name` (normalized name), `dict` (`curated/team_directory_names.csv`) |
| evidence | Utf8 | the asset or file the row came from |

Sources by league:

| League | Sources |
|---|---|
| nfl | fox, yahoo, cbs (nfl-raw `players_{fox,yahoo,cbs}.json`) |
| cfb | fox, yahoo (`cfb_teams_crosswalk`, a name join: Yahoo's number is not the ESPN id), cbs (`curated/cfb_cbs_teams.csv`, sdv-py's `_CBS_TEAM_BY_ESPN`, undated), cfbd |
| nba | fox, yahoo (`nba_team_crosswalk`), nba_stats (`curated/nba_teams.csv`) |
| wnba | fox, yahoo (`wnba_team_crosswalk`; `wnba.t.N` is **not** the ESPN id), wnba_stats (`curated/wnba_teams.csv`) |
| mbb | fox, yahoo, torvik (`mbb_team_crosswalk`), kenpom (`curated/mbb_kenpom_teams.csv` over `raw/mbb/kenpom_team_info.csv.gz`), ncaa (this repo's `mbb_crosswalk`) |
| wbb | fox, yahoo, torvik (`wbb_team_crosswalk`), ncaa (`wbb_crosswalk`) |
| mlb, nhl | mlb / nhl (`curated/{mlb,nhl}_team_espn.csv`, per season from `team_group_seasons` notes); fox, yahoo, cbs (directories) |
| cfb, mbb, wbb (directories) | sports247 (`teamId`: cfb from football, mbb from basketball; `institutionKey`: cfb, mbb, wbb), on3 (`orgKey`: cfb from football, mbb and wbb from basketball) |
| nba (directory) | cbs |

A source id means something only within its source: Fox 25 is Ohio State, ESPN 25 is California.

Directory sources (247Sports, On3; Fox / Yahoo / CBS for MLB and NHL; CBS for the NBA) come from team directories
captured by hand into `raw/team_aliases/{source}/{file}.csv.gz` (`provenance.json`: URL, capture date, rows), one
request at a time from a residential machine. They are not part of stage 10: the Fox, CBS and 247 API hosts disallow
crawling, and the owner approved one-off low-rate snapshots (2026-11-03). A directory lists today's teams, so a
capture holds for one season of each league, its current season on the capture date (cfb 2026, mbb / wbb / nba / nhl
2027, mlb 2026 for the 2026-11-03 capture). A row is matched by curated alias, then exact normalized name (accents and
apostrophes folded) against ESPN's names (the college pool `curated/ncaa_espn_teams.csv` plus the league's
`team_group_seasons` names that season), then the curated name dictionary; a name that reaches two ESPN teams is not
matched. Rows left over (all-star teams, schools outside ESPN's D-I) go to the unmatched file with the reason.
Yahoo's MLB numbers equal the ESPN id on all 30 teams (NHL: 29 of 32), but they are matched by name, not assumed.

Invariants (checked by `validate_team_aliases`):
1. Columns and dtypes as above; no nulls in `league`, `team_id`, `team_id_source`, `source`, `id_kind`, `match_method`,
   `evidence`; each row has a `source_id` or a `value`; `name_kind` and `value` are both set or both null.
2. Every team-season a window covers is an ESPN team-season of `team_group_seasons`.
3. In any season, a `(source, id_kind, source_id)` is on one team, a team has at most one id per `(source, id_kind)`,
   and a name-only `(source, name_kind, value)` is on one team.

No fuzzy matching and no guessing. A row is dropped, and written to `{league}_team_aliases_unmatched.csv` (published
with the table) with its seasons and reason, when its source gives no ESPN team, its team-season is not in
`team_group_seasons` (an ESPN team outside the league that season, the NFL Pro Bowl rosters), or its id breaks
invariant 3 in any season (the whole id goes, every season). MBB / WBB Yahoo ids exist only for the seasons a
crosswalk was built after its Yahoo leg landed (2002, and 2003 for WBB), so they are not carried to other seasons.

## `player_aliases` (beside `team_aliases`: nfl, cfb, nba, wnba, mbb, wbb)

`{league}_player_aliases`: every player id another source uses for an ESPN athlete, with the ESPN team (the block the
match was made in) and the seasons it held. Built by `sdv_reference/player_aliases.py`; `validate_player_aliases()` and
the match-rate gate enforce this contract. Like `team_aliases`, it consolidates legs SDV has already matched and asks no
provider for anything; it re-matches no names. The legs are snapshotted into `raw/player_aliases/{league}/`, one file per
season, with provenance.

| column | type | |
|---|---|---|
| league, athlete_id, athlete_id_source | Utf8 | the ESPN athlete id (`athlete_id_source` = `espn`) |
| source | Utf8 | `fox`, `yahoo`, `cbs`, `kenpom`, `ncaa`, `nba_stats`, `wnba_stats` |
| source_player_id | Utf8 | the source's own player id (`nba.p.5637`, `76284`); stats.ncaa.org roster ids are new every season |
| team_id | Utf8 | the ESPN team the athlete was on (an ESPN team-season of `team_group_seasons`) |
| source_team_id | Utf8 | the source's own team id when the leg gives one (the season's stats.ncaa.org team id; Fox / Yahoo / CBS NFL), else null |
| valid_from, valid_to | Int32 | the seasons (inclusive, the league's season key) the id is on that team; a gap or a method change splits the window |
| match_method | Utf8 | the leg's own method (`exact_name`, `fuzzy_jw`, NCAA `name_jersey` / `name` / `last_jersey` / `jaro_winkler`, CFB rosters `name`), `crosswalk` (a leg that names none), `propagated` (step 3 below), `override` |
| match_confidence | Float64 | the leg's own confidence where it gives one (the lowest in the window); the NCAA tiers' ordinal scores 1.0 / 0.95 / 0.9 / 0.85; else null |
| evidence | Utf8 | the asset or file the row came from |

Sources by league:

| League | Sources (leg) |
|---|---|
| nfl | fox, yahoo, cbs (nfl-raw `players_{fox,yahoo,cbs}.json`, matched rows only) |
| cfb | cbs, yahoo (`cfb_players_crosswalk`, 2016-26; Fox and most Yahoo are null there: sportsdataverse-py#1019), fox, yahoo (`cfb_rosters_crosswalk`, 2026), ncaa (ncaa-mfb-football-raw; its academic-year key is shifted to the fall season) |
| nba, wnba | nba_stats / wnba_stats, fox, yahoo (`{lg}_player_crosswalk`; `match_method` describes the Stats leg) |
| mbb | fox, yahoo, kenpom (`mbb_player_crosswalk`, 2025-26; `match_method` describes the Fox leg), ncaa (ncaa-mbb-hoops-raw, 2010-26) |
| wbb | fox, yahoo (`wbb_player_crosswalk`, 2014-22, 2025-26), ncaa (ncaa-wbb-hoops-raw, 2010-27) |

The NCAA leg is the raw repos' stage-07 map (sdv-py `scrape/ncaa/espn_player_xwalk.py`): each stats.ncaa.org roster
row is compared only with the ESPN athletes of its (season, ESPN team) block, the NCAA team resolved through the NCAA
team crosswalk (football: the raw repo's stage-06 team map); then normalized full name (ASCII-folded, lower-cased,
apostrophes and punctuation dropped, Jr./Sr./II-IV dropped) + jersey, the name alone, last name + jersey, and last a
guarded Jaro-Winkler step: the one ESPN athlete left in the block scoring >= 0.92, which no other NCAA player also
reaches. Each tier joins only on a key unique on both sides, so a tie stays unmatched (`ambiguous`). The 0.92 floor was
checked on a labelled sample (2026-11-03: 35,682 NCAA players matched at a deterministic tier in MBB 2025-26 and WBB
2020-22 / 2025-26; ClaudeCowork `notes/2026-11-03-player-aliases-jw-threshold.md`): with each player's true athlete
removed from its block, the guarded step wrongly accepts a remaining unmatched athlete for 0.076% of players at 0.92
(0.098% at 0.88, 0.196% at 0.80), and any other athlete of the block for 0.27% (0.81% at 0.88, 4.9% at 0.80).

On top of the legs, the build, in order:
1. **Overrides.** `curated/player_overrides.csv` (`league, source, source_player_id, athlete_id, verified_by, note`)
   sets a provider id's athlete (including a leg's miss), or (blank `athlete_id`) leaves it unmatched. Each row is
   verified by hand, and the build fails on a row that no longer changes anything. An override still passes steps 2
   and 3's checks; if it breaks step 3's rules the build fails rather than dropping it.
2. **Team block.** A row is kept only when its (season, ESPN team) is an ESPN team-season of `team_group_seasons`; an
   NCAA basketball row only when its ESPN team is the one `ncaa_espn_team_crosswalk` gives its stats.ncaa.org team that
   season; an NFL row only when its provider team is that ESPN team in `nfl_team_aliases` that season.
3. **Cross-season stability.** A provider id is on one athlete in every season, and an athlete has at most one id of a
   source on a team in a season. An id that breaks either is dropped, every season (ESPN duplicate athletes and
   provider ids reassigned to another player both occur). A pair kept in one season is carried to the same provider
   id's unmatched rows in other seasons (`propagated`), on whatever ESPN team they show, which links a player across
   transfers; two carried ids landing on one athlete-team-season stay misses. The athlete links the NCAA leg's per-season
   ids the same way.
4. **Match-rate gate.** `{league}_player_aliases_rates.csv` (published) gives, per source and season, the leg's rows
   and how many reached the table (an ESPN-anchored leg counts every ESPN athlete row, so a missing provider id is a
   miss). The build fails when a season falls below its floor in `curated/player_match_floors.csv`, set era by era one
   point (rounded down) under the rate measured when the floor was added. Floors are never lowered. A season with no
   row is not gated: the ending-year 2027 seasons (NBA, MBB, WBB) just began, so their rates are still filling.

Invariants (checked by `validate_player_aliases`): columns and dtypes as above; no nulls except `source_team_id` and
`match_confidence`; every season a window covers is an ESPN team-season of `team_group_seasons`; step 3's two rules.
No guessing: everything not published, including the legs' own misses (`no_espn_team`, `no_espn_players`,
`ambiguous`, `no_candidate` for NCAA), goes to `{league}_player_aliases_unmatched.csv` with its seasons and reason.

## College hockey (`ncaa_mhockey`, `ncaa_whockey`)

D-I only, 1998-99 (season 1999) on, from USCHO's per-season D-I standings. `team_id` is the ESPN team id
(`team_id_source = "espn"`), mapped from USCHO's team slug by `curated/ncaa_hockey_espn_teams.csv`; it is usually the
school id, but ESPN gives some newer programs a separate id per gender (LIU: men 110133, women 2341), so the map is
per league. The root is the subdivision `{league}:d1`; independents have a null `conference_id`. The men's table is
cross-checked against ESPN's core groups from 2024-25 (ESPN's older trees are frozen); ESPN lists no women's groups,
so `sources_agree` is null for every women's row.

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

# Contract: the NCAA <-> ESPN college-softball crosswalk (tag `ncaa_softball_crosswalk`)

The baseball crosswalk's build, contract and file layout (`ncaa_softball_espn_team_crosswalk`,
`ncaa_softball_espn_schools`, `ncaa_softball_espn_unmatched.csv`), over the `ncaa_softball` league's D-I team-seasons
(1982-2025) and ESPN's college-softball team list (`raw/ncaa_softball_crosswalk/espn_teams.csv.gz`, 446 teams on
2026-11-02). ESPN's softball ids are a third id system (Alabama: 560 in softball, 148 in baseball, 333 in
football/basketball). Built by `sdv_reference/crosswalks/ncaa_softball.py`. `ncaa_season_team_id` is null: the
softball source has no per-season team id. New names are resolved by `curated/ncaa_softball_espn_aliases.csv`, which
also picks the id with games where ESPN lists a school twice (Clemson, Le Moyne, Wofford).
