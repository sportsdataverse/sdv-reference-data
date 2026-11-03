"""{league}_team_aliases: offline (raw/ + curated/), asserting the deterministic legs and the no-guessing rules."""

import importlib
from functools import cache

import polars as pl
import pytest

from sdv_reference import team_aliases
from sdv_reference.schema import validate_team_aliases


@cache
def tgs(league: str) -> pl.DataFrame:
    return importlib.import_module(f"sdv_reference.leagues.{league}").build()[
        "team_group_seasons"
    ]


@cache
def aliases(league: str) -> pl.DataFrame:
    return team_aliases.build(league, tgs(league))[0]


def ids(league: str, source: str) -> pl.DataFrame:
    return (
        aliases(league)
        .filter(pl.col("source") == source)
        .select("team_id", "source_id", "valid_from", "valid_to")
        .unique()
    )


@pytest.mark.parametrize("league", team_aliases.LEAGUES)
def test_contract(league):
    assert validate_team_aliases(league, aliases(league), tgs(league)) == []


def test_yahoo_nba_is_the_espn_id_every_season():
    y = ids("nba", "yahoo")
    assert y.height == 30
    assert (y["source_id"] == "nba.t." + y["team_id"]).all()
    assert set(y["valid_from"]) == {2014} and set(y["valid_to"]) == {
        2027
    }  # Yahoo history starts 2013-14


def test_yahoo_nfl_is_the_espn_id():
    y = ids("nfl", "yahoo")
    assert y["team_id"].n_unique() == 32
    assert (y["source_id"] == "nfl.t." + y["team_id"]).all()


@pytest.mark.parametrize("source", ["fox", "cbs"])
def test_nfl_fox_cbs_one_to_one(source):
    x = ids("nfl", source)
    assert x["team_id"].n_unique() == x["source_id"].n_unique() == x.height == 32


def test_wnba_yahoo_not_assumed_espn():
    y = ids("wnba", "yahoo")
    assert y.height == 15
    assert (y["source_id"] != "wnba.t." + y["team_id"]).any()
    assert set(aliases("wnba").filter(pl.col("source") == "yahoo")["match_method"]) == {
        "crosswalk"
    }


def test_fox_ids_collide_with_espn_ids_across_systems():
    # Fox 25 is Ohio State; ESPN 25 is California: a source id only means something within its source
    fox = ids("cfb", "fox")
    assert fox.filter(pl.col("source_id") == "25")["team_id"].unique().to_list() == [
        "194"
    ]
    assert fox.filter(pl.col("team_id") == "25")["source_id"].unique().to_list() == [
        "87"
    ]


def test_cfbd_is_espn_and_cbs_is_undated():
    a = aliases("cfb")
    cfbd = a.filter(pl.col("source") == "cfbd")
    assert (cfbd["source_id"] == cfbd["team_id"]).all()
    cbs = a.filter(pl.col("source") == "cbs")
    assert cbs["valid_from"].null_count() == cbs["valid_to"].null_count() == cbs.height
    assert cbs.filter(pl.col("team_id") == "333")["source_id"].to_list() == [
        "457"
    ]  # Alabama


def test_basketball_legs():
    duke = aliases("mbb").filter(pl.col("team_id") == "150")
    got = {
        (s, i, v) for s, i, v in duke.select("source", "source_id", "value").iter_rows()
    }
    assert {
        ("fox", "23", "Duke Blue Devils"),
        ("kenpom", None, "Duke"),
        ("torvik", None, "Duke"),
        ("ncaa", "193", None),
    } <= got
    assert set(aliases("mbb").filter(pl.col("source") == "ncaa")["id_kind"]) == {
        "school"
    }


@pytest.mark.parametrize(
    "league,source",
    [("mlb", "mlb"), ("nhl", "nhl"), ("nba", "nba_stats"), ("wnba", "wnba_stats")],
)
def test_native_ids_cover_every_espn_team_season(league, source):
    a = aliases(league).filter(pl.col("source") == source)
    got = a.with_columns(
        pl.int_ranges("valid_from", pl.col("valid_to") + 1).alias("season")
    ).explode("season")
    hub = tgs(league).filter(pl.col("team_id_source") == "espn")
    if (
        league == "nba"
    ):  # nba_teams.csv starts in 1997 (stats.nba.com's first standings season)
        hub = hub.filter(pl.col("season") >= 1997)
    missing = hub.join(
        got.select("team_id", pl.col("season").cast(pl.Int32)),
        on=["team_id", "season"],
        how="anti",
    )
    assert missing.height == 0, missing.head().rows()


def test_conflicts_are_dropped_and_reported(monkeypatch):
    hub = pl.DataFrame({"season": [2020, 2020, 2021], "team_id": ["1", "2", "1"], "team_id_source": "espn"},
                       schema_overrides={"season": pl.Int32})  # fmt: skip
    rows = pl.DataFrame(
        {
            "season": [2020, 2020, 2021, 2020, 2020, 2021],
            "team_id": ["1", "2", "1", "1", "1", "9"],
            "source": ["fox", "fox", "fox", "cbs", "cbs", "cbs"],
            "source_id": ["X", "X", "X", "A", "B", "C"],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32), id_kind=pl.lit("team"), name_kind=pl.lit(None, pl.Utf8),
        value=pl.lit(None, pl.Utf8), match_method=pl.lit("crosswalk"), evidence=pl.lit("test"),
    ).select(list(team_aliases.LONG))  # fmt: skip
    monkeypatch.setattr(team_aliases, "legs", lambda league, t: rows)
    a, u = team_aliases.build("nfl", hub)
    assert (
        a.height == 0
    )  # X: two teams in 2020 (its clean 2021 goes too); A/B: one team, two ids; C: no hub team
    reasons = dict(u.group_by("source_id").agg(pl.col("reason").first()).iter_rows())
    assert set(reasons) == {"X", "A", "B", "C"}
    assert reasons["C"].startswith("team-season not in")


def test_windows_split_on_gaps():
    df = pl.DataFrame(
        {"season": [2014, 2015, 2017], "k": ["a", "a", "a"], "_u": False},
        schema_overrides={"season": pl.Int32},
    )
    w = team_aliases._windows(df, ["k"]).sort("valid_from")
    assert w.select("valid_from", "valid_to").rows() == [(2014, 2015), (2017, 2017)]


def test_undated_rows_without_a_hub_team_are_reported():
    _, unmatched = team_aliases.build("cfb", tgs("cfb"))
    stray = unmatched.filter((pl.col("source") == "cbs") & pl.col("reason").str.contains("undated map"))
    cbs = pl.read_csv(team_aliases.CURATED / "cfb_cbs_teams.csv", infer_schema=False)
    assert stray.height + aliases("cfb").filter(pl.col("source") == "cbs").height == cbs.height
    assert stray.height > 0 and stray["season_from"].null_count() == stray.height
