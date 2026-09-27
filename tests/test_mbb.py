"""MBB groups: contract + real realignments, built offline from raw/mbb + curated/."""

import polars as pl
import pytest

from sdv_reference.leagues import mbb
from sdv_reference.schema import validate


@pytest.fixture(scope="module")
def t():
    return mbb.build()


def conf(t, team_id: str, season: int) -> str | None:
    r = t["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    return r["conference_id"].item() if r.height else None


def name(t, group_id: str, season: int) -> str:
    gs = t["group_seasons"]
    return gs.filter((pl.col("group_id") == group_id) & (pl.col("season") == season))[
        "name"
    ].item()


def test_contract(t):
    assert validate("mbb", t) == []
    tgs = t["team_group_seasons"]
    assert tgs["season"].min() == 2002
    assert (
        tgs.group_by("season").len()["len"] >= 320
    ).all()  # D-I had 321+ teams every season


@pytest.mark.parametrize(
    "team, before, after, season",
    [
        ("120", "mbb:acc", "mbb:big-ten", 2015),  # Maryland, 2014-15
        ("251", "mbb:big-12", "mbb:sec", 2025),  # Texas
        ("201", "mbb:big-12", "mbb:sec", 2025),  # Oklahoma
        ("30", "mbb:pac-12", "mbb:big-ten", 2025),  # USC
        ("26", "mbb:pac-12", "mbb:big-ten", 2025),  # UCLA
        ("156", "mbb:mvc", "mbb:big-east", 2014),  # Creighton
        ("2086", "mbb:atlantic-10", "mbb:big-east", 2014),  # Butler
        ("2752", "mbb:atlantic-10", "mbb:big-east", 2014),  # Xavier
        ("2086", "mbb:horizon", "mbb:atlantic-10", 2013),  # Butler's one A-10 season
        ("2547", "mbb:wac", "mbb:wcc", 2026),  # Seattle U
        ("2320", "mbb:southland", "mbb:wac", 2022),  # Lamar to the WAC, 2021-22
        ("2320", "mbb:wac", "mbb:southland", 2024),  # Lamar back to the Southland, 2023-24
        ("2253", "mbb:wac", "mbb:mountain-west", 2026),  # Grand Canyon
        ("2567", "mbb:american", "mbb:acc", 2025),  # SMU
        ("158", "mbb:big-12", "mbb:big-ten", 2012),  # Nebraska
    ],
)
def test_realignments(t, team, before, after, season):
    assert (conf(t, team, season - 1), conf(t, team, season)) == (before, after)


def test_big_east_split_and_american(t):
    assert (
        conf(t, "41", 2013) == "mbb:big-east" and conf(t, "41", 2014) == "mbb:american"
    )  # UConn
    assert conf(t, "41", 2021) == "mbb:big-east"  # back 2020-21
    assert (
        t["groups"].filter(pl.col("group_id") == "mbb:american")["first_season"].item()
        == 2014
    )


def test_pre_2014_holes_filled_from_standings(t):
    tgs = t["team_group_seasons"]
    assert (
        conf(t, "2335", 2005) == "mbb:big-south"
    )  # Liberty: ESPN core falls back to today's CUSA
    filled = tgs.filter(
        (pl.col("source") == "espn_standings") & pl.col("season").is_in([2005, 2012])
    )
    assert set(filled["conference_id"]) >= {"mbb:ovc", "mbb:southland", "mbb:big-south"}
    assert "mbb:great-west" in set(filled["conference_id"])


def test_historical_names(t):
    assert name(t, "mbb:pac-12", 2011) == "Pacific-10 Conference"
    assert name(t, "mbb:pac-12", 2012) == "Pac-12 Conference"
    assert name(t, "mbb:summit", 2007) == "Mid-Continent Conference"
    assert name(t, "mbb:summit", 2008) == "The Summit League"
    assert name(t, "mbb:caa", 2023) == "Colonial Athletic Association"
    assert name(t, "mbb:caa", 2024) == "Coastal Athletic Association"
    assert name(t, "mbb:american", 2014) == "American Athletic Conference"
    assert name(t, "mbb:wac", 2026) == "Western Athletic Conference"
    gs = t["group_seasons"]
    assert "Metro Conference" not in set(gs["name"])  # ESPN's label for the MAAC (13)
    assert "United Athletic Conference" not in set(
        gs.filter(pl.col("season") <= 2026)["name"]
    )


def test_pac12_gap(t):
    seasons = set(
        t["group_seasons"].filter(pl.col("group_id") == "mbb:pac-12")["season"]
    )
    assert {2024} <= seasons and not {2025, 2026} & seasons


def test_divisions(t):
    gs = t["group_seasons"]
    sec_e = gs.filter(pl.col("group_id") == "mbb:sec-east")
    assert sec_e["season"].max() == 2011 and set(sec_e["parent_group_id"]) == {
        "mbb:sec"
    }
    assert conf(t, "2", 2011) == "mbb:sec"  # Auburn
    div = (
        t["team_group_seasons"]
        .filter((pl.col("team_id") == "2") & (pl.col("season") == 2011))["division_id"]
        .item()
    )
    assert div == "mbb:sec-west"


def test_sources(t):
    tgs = t["team_group_seasons"]
    kp = tgs.filter(pl.col("season").is_between(2002, 2026))
    assert (
        kp["sources_agree"].null_count() < 0.02 * kp.height
    )  # KenPom covers nearly every team-season
    assert kp["sources_agree"].mean() > 0.97
    ga = t["group_aliases"]
    maac = ga.filter(pl.col("group_id") == "mbb:maac")
    assert {"espn", "sdv", "kenpom", "ncaa"} <= set(maac["source"])
    assert maac.filter(pl.col("source") == "espn")["source_id"].unique().to_list() == [
        "13"
    ]


def test_2003_copy_corrected(t):
    # ESPN's 2003 membership is a copy of 2004; the curated overrides restore 2002-03 from KenPom
    assert (conf(t, "2378", 2003), conf(t, "2378", 2004)) == (
        "mbb:nec",
        "mbb:america-east",
    )  # UMBC
    assert (conf(t, "2678", 2003), conf(t, "2678", 2004)) == (
        "mbb:socon",
        "mbb:big-south",
    )  # VMI
    r = t["team_group_seasons"].filter(
        (pl.col("team_id") == "2378") & (pl.col("season") == 2003)
    )
    assert r["source"].item() == "kenpom" and r["sources_agree"].item() is False
