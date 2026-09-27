"""NHL groups: offline, from raw/nhl/ + curated/. Team ids are ESPN's (franchise-level) where ESPN has the team."""

from functools import cache

import polars as pl

from sdv_reference.leagues import nhl
from sdv_reference.schema import validate


@cache
def tables():
    return nhl.build()


def members(season, group_col, group_id):
    t = tables()["team_group_seasons"]
    return set(
        t.filter((pl.col("season") == season) & (pl.col(group_col) == group_id))[
            "team_id"
        ]
    )


def row(team_id, season):
    t = tables()["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    assert t.height == 1, (team_id, season)
    return t.row(0, named=True)


def test_contract():
    assert validate("nhl", tables()) == []


def test_seasons_are_ending_years():
    t = tables()["team_group_seasons"]
    assert (t["season"].min(), t["season"].max()) == (1918, 2026)  # 1917-18 .. 2025-26
    assert t.filter(pl.col("season") == 2005).height == 0  # the 2004-05 lockout
    n = dict(t.group_by("season").len().iter_rows())
    assert (n[1918], n[1968], n[2001], n[2018], n[2022]) == (4, 12, 30, 31, 32)


def test_2013_14_realignment():
    # ESPN ids: CAR 7, CBJ 29, NJ 11, NYI 12, NYR 13, PHI 15, PIT 16, WSH 23
    assert members(2014, "division_id", "nhl:metropolitan") == {
        "7",
        "29",
        "11",
        "12",
        "13",
        "15",
        "16",
        "23",
    }
    # BOS 1, BUF 2, DET 5, FLA 26, MTL 10, OTT 14, TB 20, TOR 21
    assert members(2014, "division_id", "nhl:atlantic") == {
        "1",
        "2",
        "5",
        "26",
        "10",
        "14",
        "20",
        "21",
    }
    assert members(2014, "division_id", "nhl:central") == {
        "4",
        "17",
        "9",
        "30",
        "27",
        "19",
        "28",
    }
    assert members(2014, "division_id", "nhl:pacific") == {
        "25",
        "3",
        "6",
        "8",
        "24",
        "18",
        "22",
    }
    # Detroit and Columbus move West -> East
    for team in ("5", "29"):
        assert row(team, 2013)["conference_id"] == "nhl:western"
        assert row(team, 2014)["conference_id"] == "nhl:eastern"
    assert (
        row("28", 2013)["division_id"] == "nhl:southeast"
    )  # Winnipeg still in the Southeast in 2012-13
    assert row("28", 2014)["division_id"] == "nhl:central"
    g = tables()["groups"].filter(pl.col("level") == "division")
    span = {
        r["group_id"]: (r["first_season"], r["last_season"])
        for r in g.iter_rows(named=True)
    }
    assert span["nhl:metropolitan"] == (2014, 2026)
    assert span["nhl:patrick-atlantic"] == (
        1975,
        2013,
    )  # the 1993-2013 Atlantic is not today's Atlantic


def test_2020_21_temporary_divisions():
    t = tables()["team_group_seasons"].filter(pl.col("season") == 2021)
    assert t.height == 31 and t["conference_id"].null_count() == 31
    assert set(t["division_id"]) == {
        "nhl:east-2021",
        "nhl:central-2021",
        "nhl:west-2021",
        "nhl:north-2021",
    }
    # the seven Canadian teams: CGY 3, EDM 6, MTL 10, OTT 14, TOR 21, VAN 22, WPG 28
    assert members(2021, "division_id", "nhl:north-2021") == {
        "3",
        "6",
        "10",
        "14",
        "21",
        "22",
        "28",
    }
    gs = tables()["group_seasons"].filter(pl.col("season") == 2021)
    assert set(gs["level"]) == {"league", "division"}
    assert set(gs.filter(pl.col("level") == "division")["parent_group_id"]) == {
        "nhl:nhl"
    }
    assert (
        gs.filter(pl.col("group_id") == "nhl:north-2021")["name"][0]
        == "Scotia North Division"
    )
    assert members(
        2022, "division_id", "nhl:metropolitan"
    )  # the 2013 divisions resume in 2021-22


def test_seattle_kraken_2021_22_pacific():
    t = tables()["team_group_seasons"].filter(pl.col("team_id") == "124292")
    assert t["season"].min() == 2022
    assert set(t["division_id"]) == {"nhl:pacific"}


def test_utah_2024_25_central_and_two_nhl_ids():
    r25, r26 = row("129764", 2025), row("129764", 2026)
    assert (r25["division_id"], r25["team_name"], r25["notes"].split(";")[0]) == (
        "nhl:central",
        "Utah Hockey Club",
        "nhl_team_id=59",
    )
    assert (r26["division_id"], r26["team_name"], r26["notes"].split(";")[0]) == (
        "nhl:central",
        "Utah Mammoth",
        "nhl_team_id=68",
    )


def test_arizona_coyotes_history():
    t = tables()["team_group_seasons"].filter(pl.col("team_id") == "24")
    names = dict(t.select("season", "team_name").iter_rows())
    assert (names[1980], names[1997], names[2015]) == (
        "Winnipeg Jets",
        "Phoenix Coyotes",
        "Arizona Coyotes",
    )
    assert t["season"].max() == 2024  # Utah is a new franchise, not the Coyotes
    div = dict(t.select("season", "division_id").iter_rows())
    assert div[1982] == "nhl:norris-central" and div[1983] == "nhl:smythe-pacific"
    assert div[1997] == "nhl:norris-central" and div[1999] == "nhl:smythe-pacific"
    assert (div[2014], div[2021], div[2022], div[2024]) == (
        "nhl:pacific",
        "nhl:west-2021",
        "nhl:central",
        "nhl:central",
    )


def test_1993_renames_keep_lineage():
    gs = tables()["group_seasons"]
    name = {(r["group_id"], r["season"]): r["name"] for r in gs.iter_rows(named=True)}
    assert name[("nhl:eastern", 1993)] == "Prince of Wales Conference"
    assert name[("nhl:eastern", 1994)] == "Eastern Conference"
    assert name[("nhl:patrick-atlantic", 1993)] == "Patrick Division"
    assert name[("nhl:patrick-atlantic", 1994)] == "Atlantic Division"
    parent = {
        (r["group_id"], r["season"]): r["parent_group_id"]
        for r in gs.iter_rows(named=True)
    }
    # Patrick and Norris swap conferences in 1981-82
    assert (
        parent[("nhl:patrick-atlantic", 1981)],
        parent[("nhl:patrick-atlantic", 1982)],
    ) == ("nhl:western", "nhl:eastern")
    assert (
        parent[("nhl:norris-central", 1981)],
        parent[("nhl:norris-central", 1982)],
    ) == ("nhl:eastern", "nhl:western")


def test_defunct_teams_keep_native_ids():
    t = tables()["team_group_seasons"]
    sen = t.filter(pl.col("team_name") == "Ottawa Senators")
    assert set(sen.filter(pl.col("season") <= 1934)["team_id"]) == {
        "36"
    }  # the 1917 club, NHL id
    assert set(sen.filter(pl.col("season") <= 1934)["team_id_source"]) == {"nhl"}
    assert set(sen.filter(pl.col("season") >= 1993)["team_id"]) == {
        "14"
    }  # the 1992 club, ESPN id
    assert row("36", 1927)["division_id"] == "nhl:canadian"


def test_espn_cross_check():
    assert row("1", 2015)["sources_agree"] is True  # Boston, Atlantic in both
    assert row("129764", 2025)["sources_agree"] is True
    assert row("6", 2021)["sources_agree"] is None  # ESPN has no groups for 2020-21
    assert row("6", 1990)["sources_agree"] is None  # nor for 1975-1992
    # ESPN drops the defunct Coyotes franchise (24) from every group list
    ari = row("24", 2024)
    assert ari["sources_agree"] is False and "absent" in ari["notes"]
    # ESPN applies Quebec's 1995-96 move to Colorado (West) a season early
    assert row("17", 1995)["sources_agree"] is False
    notes = tables()["groups"].filter(pl.col("group_id") == "nhl:nhl")["notes"][0]
    assert "ESPN cross-check 1993-2026" in notes
