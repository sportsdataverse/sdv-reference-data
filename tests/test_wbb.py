"""WBB groups: contract + real realignments, built offline from raw/wbb + curated/."""

import polars as pl
import pytest

from sdv_reference.leagues import wbb
from sdv_reference.schema import validate


@pytest.fixture(scope="module")
def t():
    return wbb.build()


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
    assert validate("wbb", t) == []
    assert (
        t["team_group_seasons"]["season"].min() == 2002
    )  # ESPN 2001 is a copy of 2002


@pytest.mark.parametrize(
    "team, before, after, season",
    [
        ("120", "wbb:acc", "wbb:big-ten", 2015),  # Maryland, 2014-15
        ("251", "wbb:big-12", "wbb:sec", 2025),  # Texas
        ("201", "wbb:big-12", "wbb:sec", 2025),  # Oklahoma
        ("30", "wbb:pac-12", "wbb:big-ten", 2025),  # USC
        ("26", "wbb:pac-12", "wbb:big-ten", 2025),  # UCLA
        ("156", "wbb:mvc", "wbb:big-east", 2014),  # Creighton
        ("2086", "wbb:atlantic-10", "wbb:big-east", 2014),  # Butler
        ("2752", "wbb:atlantic-10", "wbb:big-east", 2014),  # Xavier
        ("2547", "wbb:wac", "wbb:wcc", 2026),  # Seattle U
        ("2320", "wbb:southland", "wbb:wac", 2022),  # Lamar to the WAC, 2021-22
        ("2320", "wbb:wac", "wbb:southland", 2024),  # Lamar back to the Southland, 2023-24
        ("2253", "wbb:wac", "wbb:mountain-west", 2026),  # Grand Canyon
        ("41", "wbb:big-east", "wbb:american", 2014),  # UConn
        ("41", "wbb:american", "wbb:big-east", 2021),
    ],
)
def test_realignments(t, team, before, after, season):
    assert (conf(t, team, season - 1), conf(t, team, season)) == (before, after)


def test_ids_differ_from_mbb(t):
    ga = t["group_aliases"].filter(pl.col("source") == "espn")
    assert set(ga.filter(pl.col("group_id") == "wbb:summit")["source_id"]) == {
        "47"
    }  # 49 in MBB
    sb_e = t["group_seasons"].filter(pl.col("group_id") == "wbb:sun-belt-east")
    assert sb_e.height and set(sb_e["parent_group_id"]) == {
        "wbb:sun-belt"
    }  # ESPN 37: SEC East in MBB


def test_pre_2014_holes_filled_from_standings(t):
    tgs = t["team_group_seasons"]
    filled = tgs.filter(
        (pl.col("source") == "espn_standings") & (pl.col("season") == 2012)
    )
    assert {"wbb:ovc", "wbb:southland", "wbb:great-west"} <= set(
        filled["conference_id"]
    )


def test_historical_names(t):
    assert name(t, "wbb:pac-12", 2011) == "Pacific-10 Conference"
    assert name(t, "wbb:pac-12", 2012) == "Pac-12 Conference"
    assert name(t, "wbb:caa", 2024) == "Coastal Athletic Association"
    assert name(t, "wbb:american", 2025) == "American Athletic Conference"
    assert name(t, "wbb:wac", 2026) == "Western Athletic Conference"
    assert "Metro Conference" not in set(t["group_seasons"]["name"])
    seasons = set(
        t["group_seasons"].filter(pl.col("group_id") == "wbb:pac-12")["season"]
    )
    assert 2024 in seasons and not {2025, 2026} & seasons


def test_ncaa_cross_check(t):
    tgs = t["team_group_seasons"].filter(pl.col("season").is_between(2010, 2025))
    assert tgs["sources_agree"].null_count() < 0.05 * tgs.height
    assert tgs["sources_agree"].mean() > 0.97


def test_curated_overrides(t):
    tgs = t["team_group_seasons"]
    assert (
        conf(t, "2377", 2002) == "wbb:southland"
    )  # McNeese: ESPN lumps the 2002 Southland into the Horizon
    assert (
        tgs.filter(
            (pl.col("season") == 2002) & (pl.col("conference_id") == "wbb:horizon")
        ).height
        < 12
    )
    assert (conf(t, "2378", 2003), conf(t, "2378", 2004)) == (
        "wbb:nec",
        "wbb:america-east",
    )  # UMBC
