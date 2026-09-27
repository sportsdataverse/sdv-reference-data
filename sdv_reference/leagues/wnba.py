"""WNBA groups by season (single-year keys). Conferences only: the WNBA has never had divisions.

Sources (ClaudeCowork notes/2026-09-26-conference-reference/05-pro-leagues.md, 01-espn-groups.md):
- membership 1997-2026: the captured stats.wnba.com `leaguestandingsv3` JSON (wehoop-wnba-stats-raw, one directory
  per season), `Conference` per `TeamID`, with the cited fixes in curated/wnba_membership_overrides.csv;
- cross-check: ESPN core groups per season (1997+);
- team ids: ESPN ids through curated/wnba_teams.csv (stats TeamID windows → ESPN id); names from the stats feed.

`fetch()` writes raw/wnba/; `build()` reads only raw/ and curated/.
"""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl

from sdv_reference.leagues.nba import (
    assemble,
    copy_stats_standings,
    cross_check,
    espn_aliases,
    espn_membership,
    fetch_espn,
    read_curated,
    stats_members,
)

LEAGUE = "wnba"
STATS_RAW = Path(
    os.environ.get(
        "SDV_WNBA_STATS_RAW",
        "/mnt/sdv_repos/wehoop-wnba-stats-raw/wnba_stats/json/leaguestandingsv3",
    )
)
SEASONS = range(1997, 2027)
# ESPN's group lists carry Sacramento twice in 2001-2006 (2793 "SACRA" and 13 "SAC"); its games use 13
ESPN_TEAM_ALIASES = {"2793": "13"}


def fetch() -> None:
    """raw/wnba/stats/{season}.json.gz (captured standings) and raw/wnba/espn/{season}.json.gz (with team objects)."""
    copy_stats_standings(LEAGUE, STATS_RAW, SEASONS)
    fetch_espn(LEAGUE, SEASONS, team_objects=True, refresh_from=max(SEASONS))


def build() -> dict[str, pl.DataFrame]:
    gdef = read_curated("wnba_groups.csv")
    stats = stats_members(LEAGUE, gdef, read_curated("wnba_teams.csv"), season_offset=0)
    ov = read_curated("wnba_membership_overrides.csv").with_columns(
        pl.col("season").cast(pl.Int32)
    )
    ov = ov.select(
        "season",
        native_id="wnba_stats_team_id",
        ov_conf="conference_id",
        ov_note="notes",
    )
    tgs = (
        stats.join(ov, on=["season", "native_id"], how="left")
        .with_columns(
            conference_id=pl.coalesce("ov_conf", "conference_id"),
            source=pl.when(pl.col("ov_conf").is_null())
            .then(pl.lit("wnba_stats"))
            .otherwise(pl.lit("curated")),
            notes=pl.concat_str(
                [
                    pl.lit("wnba_stats_team_id=") + pl.col("native_id"),
                    pl.when(pl.col("ov_conf").is_not_null()).then(
                        pl.lit("overrides wnba_stats ")
                        + pl.col("conference")
                        + ": "
                        + pl.col("ov_note")
                    ),
                ],
                separator="; ",
                ignore_nulls=True,
            ),
        )
        .select(
            "season",
            "team_id",
            "team_name",
            "conference_id",
            "division_id",
            "source",
            "notes",
        )
        .with_columns(team_id_source=pl.lit("espn"))
    )
    assert tgs.height == stats.height
    espn, _, espn_seasons = espn_membership(LEAGUE, gdef, ESPN_TEAM_ALIASES)
    tgs = cross_check(tgs, espn, espn_seasons)
    native = stats.select(
        "season", group_id="conference_id", value="conference"
    ).with_columns(
        source=pl.lit("wnba_stats"),
        source_id=pl.lit(None, pl.Utf8),
        name_kind=pl.lit("name"),
    )
    gs = assemble(LEAGUE, tgs, gdef, [])["group_seasons"]
    return assemble(LEAGUE, tgs, gdef, [native, espn_aliases(LEAGUE, gdef, gs)])
