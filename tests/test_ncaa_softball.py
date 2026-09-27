import polars as pl
import pytest

from sdv_reference.leagues import ncaa_baseball, ncaa_softball
from sdv_reference.schema import validate

L = "ncaa_softball"


@pytest.fixture(scope="module")
def t():
    return ncaa_softball.build()


def conf(t, team_id, season):
    r = t["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    assert r.height == 1, (team_id, season)
    return r["conference_id"][0]


def test_contract(t):
    assert validate(L, t) == []


def test_season_cleaning(t):
    seasons = set(t["team_group_seasons"]["season"])
    assert min(seasons) == 1982 and max(seasons) == 2025
    assert 2000 in seasons  # repaired from the "1900" misparse
    # Alabama's 2000 season (66-14) was stored as 1900
    assert conf(t, "8", 2000) == f"{L}:sec"


@pytest.mark.parametrize(
    "team_id, season, before, after",
    [
        ("392", 2015, "acc", "big-ten"),  # Maryland
        ("703", 2025, "big-12", "sec"),  # Texas
        ("522", 2025, "big-12", "sec"),  # Oklahoma
        ("77", 2024, "wcc", "big-12"),  # BYU
        (
            "77",
            2013,
            "wac",
            "pacific-coast",
        ),  # BYU softball: WAC 2012, Pacific Coast 2013, WCC 2014
        ("77", 2014, "pacific-coast", "wcc"),
        ("732", 2025, "pac-12", "big-12"),  # Utah
        (
            "1356",
            2013,
            "pacific-coast",
            "wac",
        ),  # Seattle U (WCC move is 2026, after this data ends)
        ("1104", 2014, "pacwest", "wac"),  # Grand Canyon, D-II to D-I
        ("367", 2015, "american", "acc"),  # Louisville
        ("164", 2021, "american", "big-east"),  # UConn
    ],
)
def test_realignments(t, team_id, season, before, after):
    assert conf(t, team_id, season - 1) == f"{L}:{before}"
    assert conf(t, team_id, season) == f"{L}:{after}"


def test_labels_all_mapped(t):
    notes = t["team_group_seasons"]["notes"].drop_nulls()
    assert not notes.str.contains("unmatched").any()
    # "The American" is applied to the old Big East years; the name comes from the curated window
    gs = t["group_seasons"].filter(pl.col("group_id") == f"{L}:american")
    assert gs.filter(pl.col("season") == 2010)["name"].item() == "Big East Conference"
    assert (
        gs.filter(pl.col("season") == 2025)["name"].item()
        == "American Athletic Conference"
    )


def test_agrees_with_baseball():
    """Same org, same season, both sports in a conference: softball's name-mapped lineage matches baseball's
    conf_id lineage almost everywhere; the rest are sport-specific affiliations (Big Sky has no baseball)."""
    sb, bb = ncaa_softball.build(), ncaa_baseball.build()
    pick = lambda d, c: d["team_group_seasons"].select(
        "team_id", "season", pl.col("conference_id").str.split(":").list.get(1).alias(c)
    )
    j = pick(bb, "bb").join(pick(sb, "sb"), on=["team_id", "season"]).drop_nulls()
    assert j.height > 10_000
    assert (j["bb"] == j["sb"]).mean() > 0.97
