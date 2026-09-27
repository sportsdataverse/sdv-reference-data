"""WNBA groups: offline (raw/ + curated/), asserting real expansions, relocations and conference placements."""

from functools import cache

import polars as pl

from sdv_reference.leagues import wnba
from sdv_reference.schema import validate


@cache
def tables():
    return wnba.build()


def conf(team_id: str, season: int) -> str | None:
    t = tables()["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    return t["conference_id"].item() if t.height else None


def n_teams(group_id: str, season: int) -> int:
    gs = tables()["group_seasons"]
    return gs.filter((pl.col("group_id") == group_id) & (pl.col("season") == season))[
        "n_teams"
    ].item()


def test_contract():
    assert validate("wnba", tables()) == []


def test_seasons_and_levels():
    tgs = tables()["team_group_seasons"]
    assert (tgs["season"].min(), tgs["season"].max()) == (1997, 2026)
    assert (
        tgs["division_id"].null_count() == tgs.height
    )  # the WNBA has never had divisions
    assert (tgs["team_id_source"] == "espn").all()
    assert set(tables()["groups"]["group_id"]) == {
        "wnba:wnba",
        "wnba:east",
        "wnba:west",
    }


def test_team_counts_by_season():
    counts = {
        s: n_teams("wnba:wnba", s)
        for s in (
            1997,
            1998,
            1999,
            2000,
            2002,
            2003,
            2006,
            2008,
            2010,
            2024,
            2025,
            2026,
        )
    }
    assert counts == {
        1997: 8,
        1998: 10,
        1999: 12,
        2000: 16,
        2002: 16,
        2003: 14,
        2006: 14,
        2008: 14,
        2010: 12,
        2024: 12,
        2025: 13,
        2026: 15,
    }


def test_expansion_teams_2025_2026():
    assert (
        conf("129689", 2024) is None and conf("129689", 2025) == "wnba:west"
    )  # Golden State Valkyries
    assert (
        conf("131935", 2025) is None and conf("131935", 2026) == "wnba:east"
    )  # Toronto Tempo
    assert (
        conf("132052", 2025) is None and conf("132052", 2026) == "wnba:west"
    )  # Portland Fire (2026)
    assert (n_teams("wnba:east", 2026), n_teams("wnba:west", 2026)) == (7, 8)


def test_houston_1997_east_override():
    # stats.wnba.com and ESPN both back-apply West; the Comets won the 1997 East
    assert conf("4", 1997) == "wnba:east" and conf("4", 1998) == "wnba:west"
    row = tables()["team_group_seasons"].filter(
        (pl.col("team_id") == "4") & (pl.col("season") == 1997)
    )
    assert row["source"].item() == "curated" and row["sources_agree"].item() is False


def test_relocations_keep_espn_ids():
    tgs = tables()["team_group_seasons"]

    def name(t: str, s: int) -> str:
        return tgs.filter((pl.col("team_id") == t) & (pl.col("season") == s))[
            "team_name"
        ].item()

    # Detroit Shock (East) -> Tulsa Shock (West) 2010 -> Dallas Wings 2016
    assert (name("3", 2009), conf("3", 2009)) == ("Detroit Shock", "wnba:east")
    assert (name("3", 2010), conf("3", 2010)) == ("Tulsa Shock", "wnba:west")
    assert name("3", 2016) == "Dallas Wings"
    # ESPN splits Utah/San Antonio and Orlando/Connecticut into separate ids
    assert (
        name("15", 2002) == "Utah Starzz"
        and name("17", 2003) == "San Antonio Silver Stars"
    )
    assert name("17", 2018) == "Las Vegas Aces"
    assert (
        name("10", 2002) == "Orlando Miracle" and name("18", 2003) == "Connecticut Sun"
    )
    # the 2000-02 Portland Fire and the 2026 expansion Fire share ids in both sources
    assert [s for s in range(1997, 2027) if conf("132052", s)] == [
        2000,
        2001,
        2002,
        2026,
    ]


def test_espn_cross_check():
    tgs = tables()["team_group_seasons"]
    assert (
        tgs["sources_agree"].null_count() == 0
    )  # ESPN covers every season and team (2793 = 13 folded in)
    assert tgs.filter(pl.col("sources_agree") == False).select(
        "season", "team_id"
    ).rows() == [(1997, "4")]
