"""NBA groups: offline (raw/ + curated/), asserting real realignments. Seasons are ENDING years (2005 = 2004-05)."""

import json
from functools import cache

import polars as pl

from sdv_reference.leagues import nba
from sdv_reference.schema import validate


@cache
def tables():
    return nba.build()


def row(team_id: str, season: int) -> dict | None:
    t = tables()["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    return t.row(0, named=True) if t.height else None


def div(team_id: str, season: int) -> str | None:
    r = row(team_id, season)
    return r and r["division_id"]


def members(group_id: str, season: int) -> set[str]:
    tgs = tables()["team_group_seasons"].filter(pl.col("season") == season)
    return set(
        tgs.filter(
            (pl.col("division_id") == group_id) | (pl.col("conference_id") == group_id)
        )["team_id"]
    )


def test_contract():
    assert validate("nba", tables()) == []


def test_season_range_and_counts():
    gs = tables()["group_seasons"].filter(pl.col("group_id") == "nba:nba")
    n = dict(gs.select("season", "n_teams").iter_rows())
    assert (min(n), max(n)) == (1971, 2027)
    assert (
        n[1971],
        n[1977],
        n[1981],
        n[1989],
        n[1990],
        n[1996],
        n[2004],
        n[2005],
        n[2027],
    ) == (17, 22, 23, 25, 27, 29, 29, 30, 30)


def test_2004_05_six_divisions():
    g = tables()["groups"].filter(pl.col("level") == "division")
    span = {
        r["group_id"]: (r["first_season"], r["last_season"])
        for r in g.iter_rows(named=True)
    }
    assert span["nba:midwest"] == (1971, 2004)
    assert (
        span["nba:southeast"]
        == span["nba:southwest"]
        == span["nba:northwest"]
        == (2005, 2027)
    )
    divs_2005 = tables()["group_seasons"].filter(
        (pl.col("season") == 2005) & (pl.col("level") == "division")
    )
    assert sorted(divs_2005["n_teams"]) == [5] * 6
    # New Orleans: East/Central through 2003-04, then West/Southwest
    assert (row("3", 2004)["conference_id"], div("3", 2004)) == (
        "nba:east",
        "nba:central",
    )
    assert (row("3", 2005)["conference_id"], div("3", 2005)) == (
        "nba:west",
        "nba:southwest",
    )
    # Charlotte Bobcats: expansion into the Southeast in 2004-05, absent the two seasons before
    assert row("30", 2003) is None and row("30", 2004) is None
    assert (
        div("30", 2005) == "nba:southeast"
        and row("30", 2005)["team_name"] == "Charlotte Bobcats"
    )
    assert members("nba:southeast", 2005) == {
        "1",
        "14",
        "19",
        "27",
        "30",
    }  # ATL, MIA, ORL, WAS, CHA
    assert (div("28", 2004), div("28", 2005)) == (
        "nba:central",
        "nba:atlantic",
    )  # Toronto
    assert (div("29", 2004), div("29", 2005)) == (
        "nba:midwest",
        "nba:southwest",
    )  # Memphis
    assert (div("22", 2004), div("22", 2005)) == (
        "nba:pacific",
        "nba:northwest",
    )  # Portland


def test_charlotte_and_new_orleans_ids():
    # the 1988-2002 Hornets are ESPN id 3 (ESPN's New Orleans lineage); stats.nba.com files them under 1610612766
    assert (
        row("3", 2002)["team_name"] == "Charlotte Hornets"
        and div("3", 2002) == "nba:central"
    )
    assert row("3", 2002)["notes"].startswith("nba_stats_team_id=1610612766")
    assert row("3", 2003)["team_name"] == "New Orleans Hornets"
    assert row("3", 2014)["team_name"] == "New Orleans Pelicans"
    assert row("30", 2015)["team_name"] == "Charlotte Hornets"


def test_ending_year_season_key():
    # stats SeasonID 22007 = 2007-08 = season 2008, Seattle's last; Oklahoma City from 2008-09
    assert row("25", 2008)["team_name"] == "Seattle SuperSonics"
    assert row("25", 2009)["team_name"] == "Oklahoma City Thunder"
    assert (
        row("17", 2012)["team_name"] == "New Jersey Nets"
        and row("17", 2013)["team_name"] == "Brooklyn Nets"
    )


def test_static_era_realignments():
    assert (div("24", 1977), div("24", 1980), div("24", 1981)) == (
        "nba:central",
        "nba:central",
        "nba:midwest",
    )  # Spurs
    assert (div("10", 1972), div("10", 1973), div("10", 1981)) == (
        "nba:pacific",
        "nba:central",
        "nba:midwest",
    )  # Rockets
    assert (div("8", 1978), div("8", 1979)) == ("nba:midwest", "nba:central")  # Pistons
    assert (
        row("12", 1978)["team_name"] == "Buffalo Braves"
        and div("12", 1979) == "nba:pacific"
    )  # to San Diego
    assert (div("19", 1990), div("19", 1991), div("19", 1992)) == (
        "nba:central",
        "nba:midwest",
        "nba:atlantic",
    )  # Magic


def test_espn_cross_check():
    tgs = tables()["team_group_seasons"]
    assert (
        tgs.filter(pl.col("season") < 1985)["sources_agree"].null_count()
        == tgs.filter(pl.col("season") < 1985).height
    )
    assert tgs.filter(pl.col("season") >= 1985)["sources_agree"].null_count() == 0
    # ESPN swaps Miami and Orlando in 1989-90; Wikipedia's division standings agree with the curated table
    assert sorted(
        tgs.filter(pl.col("sources_agree") == False).select("season", "team_id").rows()
    ) == [(1990, "14"), (1990, "19")]


def test_stats_snapshots_keyed_by_season_id(tmp_path, monkeypatch):
    """hoopR-nba-stats-raw re-keyed its directories START -> END year on 2026-09-30; the snapshot key comes from
    SeasonID, so either layout lands 2001-02 in raw/nba/stats/2002, and a mis-keyed leftover is dropped."""

    def standings(season_id: str) -> dict:
        return {
            "resultSets": [
                {
                    "headers": ["SeasonID", "TeamID", "TeamCity", "TeamName", "Conference", "Division"],
                    "rowSet": [[season_id, 1610612766, "Charlotte", "Hornets", "East", "Central"]],
                }
            ]
        }

    src = tmp_path / "src"
    (src / "2002").mkdir(parents=True)
    (src / "2002" / "regular-season.json").write_text(json.dumps(standings("22001")))  # END-year directory
    monkeypatch.setattr(nba, "RAW", tmp_path / "raw")
    nba.write_gz(tmp_path / "raw" / "nba" / "stats" / "2001.json.gz", standings("22001"))  # START-keyed leftover
    nba.copy_stats_standings("nba", src, [2002], season_offset=1)
    assert sorted(p.name for p in (tmp_path / "raw" / "nba" / "stats").iterdir()) == ["2002.json.gz"]
    assert nba.stats_rows("nba", season_offset=1).rows() == [
        (2002, "1610612766", "Charlotte Hornets", "East", "Central")
    ]
