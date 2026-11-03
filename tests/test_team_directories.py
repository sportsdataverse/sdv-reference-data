"""Directory legs of {league}_team_aliases (247Sports, On3, Fox, Yahoo, CBS): offline, over the committed snapshots."""

import importlib
from functools import cache

import polars as pl
import pytest

from sdv_reference import team_aliases, team_directories


@cache
def tgs(league: str) -> pl.DataFrame:
    return importlib.import_module(f"sdv_reference.leagues.{league}").build()[
        "team_group_seasons"
    ]


@cache
def built(league: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    return team_aliases.build(league, tgs(league))


def ids(league: str, source: str, kind: str = "team") -> pl.DataFrame:
    a = built(league)[0]
    return (
        a.filter((pl.col("source") == source) & (pl.col("id_kind") == kind))
        .select("team_id", "source_id", "valid_from", "valid_to", "match_method")
        .unique()
    )


def team_of(league: str, source: str, source_id: str, kind: str = "team") -> list[str]:
    x = ids(league, source, kind).filter(pl.col("source_id") == source_id)
    return x["team_id"].unique().to_list()


# (league, source, id_kind) -> distinct ids matched from the 2026-11-03 capture; a drop means a match was lost
COUNTS = [
    ("cfb", "sports247", "team", 266), ("cfb", "sports247", "school", 266), ("cfb", "on3", "school", 151),
    ("mbb", "sports247", "team", 365), ("mbb", "sports247", "school", 365), ("mbb", "on3", "school", 344),
    ("wbb", "sports247", "school", 363), ("wbb", "on3", "school", 342),
    ("mlb", "fox", "team", 30), ("mlb", "yahoo", "team", 30), ("mlb", "cbs", "team", 30),
    ("nhl", "fox", "team", 32), ("nhl", "yahoo", "team", 32), ("nhl", "cbs", "team", 32),
    ("nba", "cbs", "team", 30),
]  # fmt: skip


@pytest.mark.parametrize("league,source,kind,n", COUNTS)
def test_counts_and_one_to_one(league, source, kind, n):
    x = ids(league, source, kind)
    assert x["source_id"].n_unique() == n
    # no duplicates on the key: one team per id and one id per team (single-season capture windows)
    assert (
        x.group_by("source_id")
        .agg(pl.col("team_id").n_unique())
        .filter(pl.col("team_id") > 1)
        .height
        == 0
    )
    assert (
        x.group_by("team_id")
        .agg(pl.col("source_id").n_unique())
        .filter(pl.col("source_id") > 1)
        .height
        == 0
    )
    assert set(x["match_method"]) <= {"alias", "exact_name", "dict"}


@pytest.mark.parametrize("league,season", [("mlb", 2026), ("nhl", 2027), ("nba", 2027)])
def test_pro_directories_cover_every_espn_team(league, season):
    hub = set(
        tgs(league).filter(
            (pl.col("season") == season) & (pl.col("team_id_source") == "espn")
        )["team_id"]
    )
    for source in [
        s
        for (s, f), (_, _, legs) in team_directories.DIRECTORIES.items()
        if legs[0][0] == league
    ]:
        x = ids(league, source)
        assert set(x["team_id"]) == hub, source
        assert (
            set(x["valid_from"]) == set(x["valid_to"]) == {season}
        )  # a capture holds for its season only


def test_247_one_school_key_across_sports():
    # Alabama: football teamId 164, basketball 165, one institutionKey 24096; ESPN 333 in cfb, mbb and wbb
    assert (
        team_of("cfb", "sports247", "164")
        == team_of("mbb", "sports247", "165")
        == ["333"]
    )
    for league in ("cfb", "mbb", "wbb"):
        assert team_of(league, "sports247", "24096", "school") == ["333"]
    fb = team_directories._read("sports247", "football")
    bb = team_directories._read("sports247", "basketball")
    j = fb.join(bb, on="institution_key", suffix="_bb")
    assert (
        j.filter(pl.col("team_id") == pl.col("team_id_bb")).height == 0
    )  # teamId is per sport
    assert j.filter(pl.col("institution_key") == "24096").select(
        "team_id", "team_id_bb"
    ).rows() == [("164", "165")]


def test_known_ids():
    assert team_of("cfb", "on3", "1867", "school") == ["333"]  # On3 Alabama
    assert team_of("mlb", "fox", "5") == ["8"]  # Fox 5 = Milwaukee Brewers (ESPN 8)
    assert (
        team_of("nhl", "fox", "18") == team_of("nhl", "cbs", "395") == ["21"]
    )  # Toronto Maple Leafs
    assert team_of("nba", "cbs", "344") == [
        "12"
    ]  # CBS "Los Angeles Clippers" = ESPN "LA Clippers" (dict)
    assert team_of("mbb", "on3", "15308", "school") == ["2599"]  # St. John's Red Storm


def test_yahoo_mlb_numbers_are_espn_ids_but_not_assumed():
    # observed on the 2026-11-03 capture, matched by name (not by formula): MLB 30/30, NHL 29/32
    for league, n in (("mlb", 30), ("nhl", 29)):
        y = ids(league, "yahoo")
        assert (y["source_id"] == f"{league}.t." + y["team_id"]).sum() == n
        assert set(y["match_method"]) == {"exact_name"}


def test_unmatched_rows_carry_a_reason():
    u = built("mbb")[1].filter(pl.col("source") == "on3")
    # Saint John's (MN) shares St. John's (NY)'s location name: blocked by a blank curated alias, not guessed
    johnnies = u.filter(pl.col("source_id") == "15527")
    assert johnnies.height and set(johnnies["reason"]) == {
        "curated (team_directory_aliases.csv): not an ESPN team"
    }
    assert u["reason"].null_count() == 0
    # an all-star team is no ESPN team
    cbs = built("nba")[1].filter(pl.col("source") == "cbs")
    assert "Team LeBron All Stars" in cbs["value"].to_list()
