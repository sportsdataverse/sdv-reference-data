"""NFL groups: offline (raw/ + curated/), asserting the real realignments, expansions and relocations."""

from functools import cache

import polars as pl

from sdv_reference.leagues import nfl
from sdv_reference.schema import validate


@cache
def tables():
    return nfl.build()


def row(team_id: str, season: int) -> dict | None:
    t = tables()["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    return t.row(0, named=True) if t.height else None


def div(team_id: str, season: int) -> str | None:
    r = row(team_id, season)
    return r and r["division_id"]


def members(division_id: str, season: int) -> set[str]:
    tgs = tables()["team_group_seasons"]
    return set(
        tgs.filter(
            (pl.col("division_id") == division_id) & (pl.col("season") == season)
        )["team_id"]
    )


def test_contract():
    assert validate("nfl", tables()) == []


def test_team_counts():
    gs = tables()["group_seasons"].filter(pl.col("group_id") == "nfl:nfl")
    n = dict(gs.select("season", "n_teams").iter_rows())
    assert (min(n), max(n)) == (1970, 2026)
    assert (n[1970], n[1976], n[1995], n[1996], n[1999], n[2001], n[2002], n[2026]) == (
        26,
        28,
        30,
        30,
        31,
        31,
        32,
        32,
    )


def test_2002_realignment():
    span = {
        r["group_id"]: (r["first_season"], r["last_season"])
        for r in tables()["groups"].iter_rows(named=True)
    }
    assert span["nfl:afc-central"] == span["nfl:nfc-central"] == (1970, 2001)
    for g in ("nfl:afc-north", "nfl:afc-south", "nfl:nfc-north", "nfl:nfc-south"):
        assert span[g] == (2002, 2026)
    assert (div("26", 2001), div("26", 2002)) == (
        "nfl:afc-west",
        "nfl:nfc-west",
    )  # Seattle
    assert row("26", 2002)["conference_id"] == "nfl:nfc"
    assert (
        row("34", 2001) is None and div("34", 2002) == "nfl:afc-south"
    )  # Houston Texans expansion
    assert members("nfl:afc-north", 2002) == {
        "4",
        "5",
        "23",
        "33",
    }  # CIN, CLE, PIT, BAL
    assert members("nfl:afc-south", 2002) == {
        "10",
        "11",
        "30",
        "34",
    }  # TEN, IND, JAX, HOU
    assert members("nfl:nfc-south", 2002) == {"1", "18", "27", "29"}  # ATL, NO, TB, CAR
    assert members("nfl:nfc-west", 2002) == {
        "14",
        "22",
        "25",
        "26",
    }  # STL, ARI, SF, SEA
    for s in range(2002, 2027):
        sizes = tables()["group_seasons"].filter(
            (pl.col("season") == s) & (pl.col("level") == "division")
        )["n_teams"]
        assert sorted(sizes) == [4] * 8, s


def test_1999_2001_from_nflverse():
    assert members("nfl:afc-central", 1999) == {
        "33",
        "4",
        "5",
        "30",
        "23",
        "10",
    }  # BAL CIN CLE JAX PIT TEN
    assert members("nfl:nfc-west", 2001) == {
        "1",
        "29",
        "18",
        "25",
        "14",
    }  # ATL CAR NO SF STL
    assert row("5", 1999)["source"] == "nflverse"


def test_static_era():
    assert (
        row("5", 1995) and row("5", 1996) is None and row("5", 1998) is None
    )  # Browns inactive 1996-98
    assert div("33", 1996) == "nfl:afc-central"  # Ravens
    assert (div("27", 1976), div("27", 1977)) == (
        "nfl:afc-west",
        "nfl:nfc-central",
    )  # Tampa Bay
    assert (div("26", 1976), div("26", 1977)) == (
        "nfl:nfc-west",
        "nfl:afc-west",
    )  # Seattle
    assert (
        row("11", 1983)["team_name"] == "Baltimore Colts"
        and row("11", 1984)["team_name"] == "Indianapolis Colts"
    )
    assert row("17", 1970)["team_name"] == "Boston Patriots"
    assert (
        row("22", 1988)["team_name"] == "Phoenix Cardinals"
        and div("22", 1988) == "nfl:nfc-east"
    )
    assert (div("29", 1995), div("30", 1995)) == (
        "nfl:nfc-west",
        "nfl:afc-central",
    )  # 1995 expansion


def test_names_by_season():
    def name(t: str, s: int) -> str:
        return row(t, s)["team_name"]

    assert (name("24", 2016), name("24", 2017)) == (
        "San Diego Chargers",
        "Los Angeles Chargers",
    )
    assert (name("13", 2019), name("13", 2020)) == (
        "Oakland Raiders",
        "Las Vegas Raiders",
    )
    assert (name("13", 1982), name("13", 1995)) == (
        "Los Angeles Raiders",
        "Oakland Raiders",
    )
    assert (name("14", 1994), name("14", 2015), name("14", 2016)) == (
        "Los Angeles Rams",
        "St. Louis Rams",
        "Los Angeles Rams",
    )
    assert (name("28", 2019), name("28", 2021), name("28", 2022)) == (
        "Washington Redskins",
        "Washington Football Team",
        "Washington Commanders",
    )
    assert (name("10", 1996), name("10", 1998), name("10", 1999)) == (
        "Houston Oilers",
        "Tennessee Oilers",
        "Tennessee Titans",
    )


def test_espn_cross_check():
    tgs = tables()["team_group_seasons"]
    assert (
        tgs.filter(pl.col("season") < 1986)["sources_agree"].null_count()
        == tgs.filter(pl.col("season") < 1986).height
    )
    covered = tgs.filter(pl.col("season") >= 1986)
    assert covered["sources_agree"].null_count() == 0
    assert (covered["sources_agree"] == True).all()
