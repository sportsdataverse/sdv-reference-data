"""Offline checks on the cfb tables: real realignments, era names, tiers, the ESPN crosswalk and the contract."""

import polars as pl
import pytest

from sdv_reference.leagues import cfb
from sdv_reference.schema import validate


@pytest.fixture(scope="module")
def t():
    return cfb.build()


def row(t, team_id, season):
    r = t["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    assert r.height == 1, (team_id, season)
    return r.row(0, named=True)


def gseason(t, group_id, season):
    r = t["group_seasons"].filter(
        (pl.col("group_id") == group_id) & (pl.col("season") == season)
    )
    assert r.height == 1, (group_id, season)
    return r.row(0, named=True)


def test_contract(t):
    assert validate("cfb", t) == []


@pytest.mark.parametrize(
    "team_id, season, before, after",
    [
        ("251", 2024, "cfb:big-12", "cfb:sec"),  # Texas
        ("201", 2024, "cfb:big-12", "cfb:sec"),  # Oklahoma
        ("30", 2024, "cfb:pac-12", "cfb:big-ten"),  # USC
        ("26", 2024, "cfb:pac-12", "cfb:big-ten"),  # UCLA
        ("158", 2011, "cfb:big-12", "cfb:big-ten"),  # Nebraska
        ("2567", 2024, "cfb:american", "cfb:acc"),  # SMU
        ("25", 2024, "cfb:pac-12", "cfb:acc"),  # Cal
        ("24", 2024, "cfb:pac-12", "cfb:acc"),  # Stanford
        (
            "68",
            2026,
            "cfb:mountain-west",
            "cfb:pac-12",
        ),  # Boise State, the 2026 Pac-12 rebuild
        ("68", 2011, "cfb:wac", "cfb:mountain-west"),
        ("2628", 2012, "cfb:mountain-west", "cfb:big-12"),  # TCU
        ("41", 2013, "cfb:big-east", "cfb:american"),  # UConn
    ],
)
def test_realignments(t, team_id, season, before, after):
    assert row(t, team_id, season - 1)["conference_id"] == before
    assert row(t, team_id, season)["conference_id"] == after


def test_pac12_collapse_and_rebuild(t):
    n = {s: gseason(t, "cfb:pac-12", s)["n_teams"] for s in (2023, 2024, 2025, 2026)}
    assert n == {2023: 12, 2024: 2, 2025: 2, 2026: 8}
    for team_id in ("204", "265"):  # Oregon State, Washington State stay throughout
        assert all(
            row(t, team_id, s)["conference_id"] == "cfb:pac-12"
            for s in (2024, 2025, 2026)
        )


def test_names_as_of_season(t):
    assert gseason(t, "cfb:pac-12", 1970)["short_name"] == "Pac-8"
    assert gseason(t, "cfb:pac-12", 2010)["short_name"] == "Pac-10"
    assert gseason(t, "cfb:pac-12", 2011)["short_name"] == "Pac-12"
    assert gseason(t, "cfb:american", 2013)["name"] == "American Athletic Conference"
    assert gseason(t, "cfb:american", 2024)["name"] == "American Athletic Conference"
    assert gseason(t, "cfb:american", 2025)["name"] == "American Conference"
    assert gseason(t, "cfb:mvfc", 2007)["name"] == "Gateway Football Conference"
    assert gseason(t, "cfb:caa", 2022)["name"] == "Colonial Athletic Association"
    assert gseason(t, "cfb:caa", 2023)["name"] == "Coastal Athletic Association"
    assert gseason(t, "cfb:fbs", 2005)["short_name"] == "Division I-A"
    assert gseason(t, "cfb:fbs", 2006)["short_name"] == "FBS"
    assert gseason(t, "cfb:fcs", 1990)["short_name"] == "Division I-AA"
    assert gseason(t, "cfb:big-ten-legends", 2012)["name"] == "Big Ten Legends"


def test_divisions(t):
    assert row(t, "158", 2011)["division_id"] == "cfb:big-ten-legends"
    assert row(t, "158", 2014)["division_id"] == "cfb:big-ten-west"
    assert row(t, "158", 2024)["division_id"] is None
    assert row(t, "251", 2010)["division_id"] == "cfb:big-12-south"
    gs = t["group_seasons"]
    divs = gs.filter(pl.col("level") == "division").select("parent_group_id", "season")
    confs = gs.filter(pl.col("level") == "conference").select(
        pl.col("group_id").alias("parent_group_id"), "season"
    )
    assert divs.join(confs, on=["parent_group_id", "season"], how="anti").height == 0


def test_levels_and_parents(t):
    gs = t["group_seasons"]
    level = dict(t["groups"].select("group_id", "level").iter_rows())
    assert set(gs.filter(pl.col("level") == "subdivision")["group_id"]) == {
        "cfb:fbs",
        "cfb:fcs",
        "cfb:d2",
        "cfb:d3",
        "cfb:college-division",
    }
    assert (
        gs.filter(pl.col("level") == "subdivision")["parent_group_id"].null_count()
        == gs.filter(pl.col("level") == "subdivision").height
    )
    parents = gs.filter(
        (pl.col("level") == "conference") & pl.col("parent_group_id").is_not_null()
    )
    assert {level[p] for p in parents["parent_group_id"]} == {"subdivision"}
    # FBS vs FCS, including the WAC that ESPN files under FCS in every season
    assert gseason(t, "cfb:sec", 2025)["parent_group_id"] == "cfb:fbs"
    assert gseason(t, "cfb:mvfc", 2025)["parent_group_id"] == "cfb:fcs"
    assert gseason(t, "cfb:wac", 2010)["parent_group_id"] == "cfb:fbs"
    assert gseason(t, "cfb:wac", 2022)["parent_group_id"] == "cfb:fcs"
    assert row(t, "68", 2010)["subdivision_id"] == "cfb:fbs"
    assert (
        row(t, "2534", 2022)["subdivision_id"] == "cfb:fcs"
    )  # Sam Houston, FCS then FBS
    assert row(t, "2534", 2023)["subdivision_id"] == "cfb:fbs"
    assert 125 <= gseason(t, "cfb:fbs", 2025)["n_teams"] <= 140
    assert 115 <= gseason(t, "cfb:fcs", 2025)["n_teams"] <= 135
    # no tier before the NCAA had one
    assert gs.filter(pl.col("group_id") == "cfb:fcs")["season"].min() == 1978
    assert (
        row(t, "194", 1950)["subdivision_id"] == "cfb:fbs"
    )  # Ohio State, major college


def test_espn_crosswalk(t):
    ga = t["group_aliases"].filter(
        (pl.col("source") == "espn") & (pl.col("name_kind") == "name")
    )

    def espn(source_id):
        return sorted(
            ga.filter(pl.col("source_id") == source_id)
            .select("group_id", "valid_from", "valid_to")
            .rows()
        )

    assert espn("9") == [("cfb:pac-12", 2001, None)]
    assert espn("10") == [("cfb:big-east", 2001, 2012)]
    assert espn("179") == [("cfb:ovc", 2023, None)]
    assert espn("52") == [
        ("cfb:big-ten-east", 2014, 2023),
        ("cfb:big-ten-legends", 2011, 2013),
    ]
    sdv = t["group_aliases"].filter(
        (pl.col("source") == "sdv") & (pl.col("value") == "Pac-10")
    )
    assert set(sdv.select("valid_from", "valid_to").rows()) == {(1978, 2010)}


def test_sources_agree(t):
    tgs = t["team_group_seasons"]
    assert (
        tgs.filter(pl.col("season") < cfb.CROSS_CHECK_FROM)[
            "sources_agree"
        ].null_count()
        == tgs.filter(pl.col("season") < cfb.CROSS_CHECK_FROM).height
    )
    assert row(t, "251", 2024)["sources_agree"] is True
    checked = tgs.filter(pl.col("sources_agree").is_not_null())
    assert (
        checked.height > 1500
        and checked.filter(pl.col("sources_agree") == False).height < 10
    )
    # 2020 opt-outs CFBD drops come back from ESPN's group lists
    yale = row(t, "43", 2020)
    assert (yale["conference_id"], yale["source"], yale["sources_agree"]) == (
        "cfb:ivy",
        "espn",
        None,
    )
    assert gseason(t, "cfb:ivy", 2020)["n_teams"] == 8


def test_ids_are_utf8_and_espn(t):
    tgs = t["team_group_seasons"]
    assert not tgs["team_id"].str.contains(r"\.").any()
    assert (
        tgs.filter(pl.col("season") >= 2001)["team_id_source"]
        .value_counts()
        .filter(pl.col("team_id_source") == "espn")["count"][0]
        > 0.95 * tgs.filter(pl.col("season") >= 2001).height
    )
