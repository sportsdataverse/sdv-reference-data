"""NFL groups by season (single-year keys), 1970 (the merger) onward.

Sources (ClaudeCowork notes/2026-09-26-conference-reference/05-pro-leagues.md, 01-espn-groups.md):
- membership 1999+: nflverse schedules (`games.csv`, the table nflreadr::load_schedules() reads). `div_game` links
  every pair of division rivals, so each season's connected components are its divisions; each component is named
  by the curated anchor team in curated/nfl_groups.csv (e.g. the component holding PIT is the AFC Central through
  2001 and the AFC North from 2002). Conferences follow from the division;
- membership 1970-1998: curated/nfl_membership_static.csv (cited per row; checked against Wikipedia's per-season
  division standings);
- cross-check: ESPN core groups per season (1986+);
- team ids and names: curated/nfl_teams.csv (ESPN id per franchise, name/abbreviation windows for relocations and
  renames). nflverse abbreviations (OAK, SD, STL, LA, ...) key the join in the nflverse era.

`fetch()` writes raw/nfl/; `build()` reads only raw/ and curated/.
"""

from __future__ import annotations

import gzip
import io

import polars as pl
import requests

from sdv_reference.leagues.nba import (
    RAW,
    assemble,
    cross_check,
    espn_aliases,
    espn_frames,
    espn_membership,
    expand_seasons,
    fetch_espn,
    in_window,
    join_window,
    read_curated,
    write_gz,
)
from sdv_reference.refresh import current_season

LEAGUE = "nfl"
# nflreadr::load_schedules() reads nfldata/data/games.rds; games.csv beside it is the same table
GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
ESPN_SEASONS = range(1986, current_season(ending_year=False) + 1)  # ESPN NFL groups start with 1986
# the schedule columns build() reads, plus game_id: scores and kickoff times change every game day, these don't
GAMES_KEEP = ["game_id", "season", "away_team", "home_team", "div_game"]
FIRST_NFLVERSE = 1999


def fetch() -> None:
    """raw/nfl/nflverse_games.csv.gz (schedules, 1999+: one call for every season, GAMES_KEEP columns) and
    raw/nfl/espn/{season}.json.gz (with team objects, refresh window only)."""
    r = requests.get(GAMES_URL, timeout=120)
    r.raise_for_status()
    games = pl.read_csv(io.BytesIO(r.content), columns=GAMES_KEEP, infer_schema=False)
    write_gz(RAW / LEAGUE / "nflverse_games.csv.gz", games.write_csv().encode())
    fetch_espn(LEAGUE, ESPN_SEASONS, team_objects=True)


def _find(parent: dict[str, str], x: str) -> str:
    """Union-find root with path halving."""
    while parent.setdefault(x, x) != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def division_components(games: pl.DataFrame) -> pl.DataFrame:
    """(season, team_abbr, component) from div_game edges: union-find per season."""
    rows = []
    for (season,), g in games.group_by("season", maintain_order=True):
        parent: dict[str, str] = {}
        for a, b in (
            g.filter(pl.col("div_game") == 1)
            .select("away_team", "home_team")
            .iter_rows()
        ):
            parent[_find(parent, a)] = _find(parent, b)
        teams = set(g["away_team"]) | set(g["home_team"])
        rows += [(season, t, _find(parent, t)) for t in teams]
    return pl.DataFrame(
        rows,
        schema={"season": pl.Int32, "team_abbr": pl.Utf8, "component": pl.Utf8},
        orient="row",
    )


def nflverse_members(gdef: pl.DataFrame) -> pl.DataFrame:
    """(season, team_abbr, division_id) for 1999+, naming each div_game component by its anchor team."""
    with gzip.open(RAW / LEAGUE / "nflverse_games.csv.gz") as fh:
        games = pl.read_csv(
            io.BytesIO(fh.read()),
            columns=["season", "away_team", "home_team", "div_game"],
        )
    comp = division_components(games.with_columns(pl.col("season").cast(pl.Int32)))
    anchors = gdef.filter(pl.col("anchor_team").is_not_null()).select(
        division_id="group_id",
        team_abbr="anchor_team",
        first_season="first_season",
        last_season="last_season",
    )
    named = in_window(comp.join(anchors, on="team_abbr")).select(
        "season", "component", "division_id"
    )
    # every component holds exactly one anchor, and every season has as many components as divisions
    per = named.group_by("season", "component").len()
    assert (per["len"] == 1).all(), (
        f"component with two anchors: {per.filter(pl.col('len') > 1).rows()[:3]}"
    )
    out = comp.join(named, on=["season", "component"], how="left")
    unnamed = out.filter(pl.col("division_id").is_null())
    assert unnamed.height == 0, (
        f"division components without an anchor: {unnamed.head(5).rows()}"
    )
    return out.select("season", "team_abbr", "division_id")


def build() -> dict[str, pl.DataFrame]:
    gdef = read_curated("nfl_groups.csv")
    teams = read_curated("nfl_teams.csv")
    nv = nflverse_members(gdef).with_columns(source=pl.lit("nflverse"))
    static = read_curated("nfl_membership_static.csv")
    static = expand_seasons(static).select(
        "season", "team_abbr", "division_id", source=pl.lit("curated")
    )
    assert int(static["season"].max()) < FIRST_NFLVERSE <= int(nv["season"].min())
    m = pl.concat([static, nv])
    m = join_window(
        m,
        teams.select(
            "team_abbr", "espn_team_id", "team_name", "first_season", "last_season"
        ),
        "team_abbr",
        "nfl team windows",
    )
    tgs = m.join(
        gdef.select(division_id="group_id", conference_id="parent_group_id"),
        on="division_id",
    ).select(
        "season",
        team_id="espn_team_id",
        team_id_source=pl.lit("espn"),
        team_name="team_name",
        conference_id="conference_id",
        division_id="division_id",
        source="source",
        notes=pl.lit("abbr=") + pl.col("team_abbr"),
    )
    espn, _, espn_seasons = espn_membership(LEAGUE, gdef)
    tgs = cross_check(tgs, espn, espn_seasons)
    gs = assemble(LEAGUE, tgs, gdef, [])["group_seasons"]
    nflverse = (
        gs.filter(pl.col("season") >= FIRST_NFLVERSE)
        .join(gdef.select("group_id", "nflverse_name"), on="group_id")
        .filter(pl.col("nflverse_name").is_not_null())
        .select(
            "group_id",
            "season",
            value="nflverse_name",
            source=pl.lit("nflverse"),
            source_id=pl.lit(None, pl.Utf8),
            name_kind=pl.lit("name"),
        )
    )
    return assemble(LEAGUE, tgs, gdef, [nflverse, espn_aliases(LEAGUE, gdef, gs)])


def espn_name_mismatches(tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Team-seasons whose curated name differs from ESPN's team-season displayName (a report, not a gate)."""
    _, _, t = espn_frames(LEAGUE)
    return (
        tables["team_group_seasons"]
        .join(
            t.select("season", team_id="espn_team_id", espn_name="display_name"),
            on=["season", "team_id"],
        )
        .filter(pl.col("team_name") != pl.col("espn_name"))
        .select("season", "team_id", "team_name", "espn_name")
    )
