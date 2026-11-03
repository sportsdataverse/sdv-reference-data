"""NCAA <-> ESPN college-softball team crosswalk: offline, from raw/ncaa_softball{,_crosswalk} + curated/."""

from functools import cache

import polars as pl

from sdv_reference.crosswalks import ncaa_softball as xw


@cache
def built() -> pl.DataFrame:
    return xw.build()


def espn_id(team: str, season: int = 2025) -> str:
    return (
        built()
        .filter((pl.col("ncaa_team") == team) & (pl.col("season") == season))["espn_team_id"]
        .item()
    )


def test_contract():
    assert xw.validate(built()) == []


def test_softball_ids_are_their_own_system():
    """Alabama is 333 in football/basketball, 148 in baseball, 560 in softball."""
    assert espn_id("Alabama") == "560"
    assert espn_id("Texas") == "538"


def test_duplicate_espn_teams_resolve_to_the_one_with_games():
    # ESPN lists Clemson, Le Moyne and Wofford twice; the aliases pick the id that has games
    assert (espn_id("Clemson"), espn_id("Le Moyne"), espn_id("Wofford")) == ("1140", "1283", "1285")


def test_only_long_gone_d1_schools_are_unmatched():
    # schools ESPN's list lacks: D-I softball seasons from the 1980s-90s, and UTRGV's one 2014 season
    df = built()
    assert df.filter(pl.col("espn_team_id").is_null() & (pl.col("season") > 2014)).height == 0
    assert df.filter(pl.col("season") >= 2010)["espn_team_id"].null_count() == 1
