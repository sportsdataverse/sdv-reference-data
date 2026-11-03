"""NCAA D-I men's and women's hockey groups: offline (raw/ + curated/), asserting real realignments, the ESPN
cross-check and the curated ESPN ids."""

from functools import cache

import polars as pl
import pytest

from sdv_reference.leagues import ncaa_mhockey, ncaa_whockey
from sdv_reference.leagues.nba import RAW, read_curated
from sdv_reference.schema import validate

BUILDERS = {"ncaa_mhockey": ncaa_mhockey, "ncaa_whockey": ncaa_whockey}


@cache
def tables(league: str):
    return BUILDERS[league].build()


def conf(league: str, team_id: str, season: int) -> str | None:
    t = tables(league)["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    assert t.height == 1, (league, team_id, season)
    return t["conference_id"].item()


def name(league: str, group_id: str, season: int) -> str:
    gs = tables(league)["group_seasons"]
    return gs.filter((pl.col("group_id") == group_id) & (pl.col("season") == season))[
        "name"
    ].item()


@pytest.mark.parametrize("league", BUILDERS)
def test_contract(league):
    assert validate(league, tables(league)) == []
    tgs = tables(league)["team_group_seasons"]
    assert (tgs["season"].min(), tgs["season"].max()) == (1999, 2027)
    assert (tgs["team_id_source"] == "espn").all()
    assert (tgs["subdivision_id"] == f"{league}:d1").all()


@pytest.mark.parametrize("league", BUILDERS)
def test_espn_ids_are_espn_listed_or_explained(league):
    ids = read_curated("ncaa_hockey_espn_teams.csv").filter(pl.col("league") == league)
    listed = set(
        pl.read_csv(RAW / league / "espn_teams.csv.gz", infer_schema=False)[
            "espn_team_id"
        ]
    )
    off = ids.filter(
        ~pl.col("espn_team_id").is_in(listed) & (pl.col("notes").fill_null("") == "")
    )
    assert off.height == 0, off.rows()


def test_men_realignments():
    m = "ncaa_mhockey"
    assert (conf(m, "130", 2013), conf(m, "130", 2014)) == (
        "ncaa_mhockey:ccha-1971",
        "ncaa_mhockey:big-ten",
    )
    # Notre Dame: CCHA -> Hockey East 2013-14 -> Big Ten 2017-18
    assert [conf(m, "87", s) for s in (2013, 2014, 2017, 2018)] == [
        "ncaa_mhockey:ccha-1971",
        "ncaa_mhockey:hockey-east",
        "ncaa_mhockey:hockey-east",
        "ncaa_mhockey:big-ten",
    ]
    assert (conf(m, "213", 2013), conf(m, "213", 2014)) == (
        None,
        "ncaa_mhockey:big-ten",
    )  # Penn State
    assert (conf(m, "9", 2024), conf(m, "9", 2025)) == (
        None,
        "ncaa_mhockey:nchc",
    )  # Arizona State
    assert (conf(m, "2900", 2026), conf(m, "2900", 2027)) == (
        "ncaa_mhockey:ccha",
        "ncaa_mhockey:nchc",
    )  # St. Thomas
    assert (conf(m, "261", 2005), conf(m, "261", 2006)) == (
        "ncaa_mhockey:ecac",
        "ncaa_mhockey:hockey-east",
    )  # Vermont
    assert (conf(m, "2514", 2005), conf(m, "2514", 2006)) == (
        "ncaa_mhockey:atlantic-hockey",
        "ncaa_mhockey:ecac",
    )
    assert (
        conf(m, "2008", 2021) == "ncaa_mhockey:wcha"
    )  # Alabama Huntsville's last season
    # USCHO also lists Merrimack under the independents through 2020-21: Hockey East wins
    assert {conf(m, "2771", s) for s in range(1999, 2028)} == {
        "ncaa_mhockey:hockey-east"
    }
    seasons = set(
        tables(m)["team_group_seasons"].filter(pl.col("team_id") == "2523")["season"]
    )
    assert {2021, 2024} <= seasons and not {
        2022,
        2023,
    } & seasons  # Robert Morris: dropped 2021, back 2023-24


def test_men_conference_eras():
    m = "ncaa_mhockey"
    assert [
        name(m, "ncaa_mhockey:atlantic-hockey", s) for s in (2003, 2004, 2024, 2025)
    ] == [
        "Metro Atlantic Athletic Conference",
        "Atlantic Hockey Association",
        "Atlantic Hockey Association",
        "Atlantic Hockey America",
    ]
    assert (name(m, "ncaa_mhockey:ecac", 2007), name(m, "ncaa_mhockey:ecac", 2008)) == (
        "ECAC Hockey League",
        "ECAC Hockey",
    )
    g = tables(m)["groups"].select("group_id", "first_season", "last_season").rows()
    assert ("ncaa_mhockey:wcha", 1999, 2021) in g
    assert ("ncaa_mhockey:ccha-1971", 1999, 2013) in g and (
        "ncaa_mhockey:ccha",
        2022,
        2027,
    ) in g
    gs = tables(m)["group_seasons"].filter(pl.col("season") == 2027)
    assert dict(gs.select("group_id", "n_teams").iter_rows())["ncaa_mhockey:d1"] == 63


def test_men_espn_cross_check():
    tgs = tables("ncaa_mhockey")["team_group_seasons"]
    assert (
        tgs.filter(pl.col("season") < 2025)["sources_agree"].null_count()
        == tgs.filter(pl.col("season") < 2025).height
    )
    recent = tgs.filter(pl.col("season") >= 2025)
    # ESPN still has St. Thomas in the CCHA for 2026-27; Maryville is new and not in ESPN's groups
    assert recent.filter(pl.col("sources_agree") == False)["team_id"].to_list() == [
        "2900"
    ]  # noqa: E712
    assert recent.filter(pl.col("sources_agree").is_null())["team_name"].to_list() == [
        "Maryville"
    ]


def test_women_realignments():
    w = "ncaa_whockey"
    assert (
        conf(w, "48", 2026) == "ncaa_whockey:atlantic-hockey"
    )  # Delaware's first D-I season
    assert [conf(w, "2385", s) for s in (2002, 2003, 2025)] == [
        "ncaa_whockey:glwha",
        "ncaa_whockey:atlantic-hockey",
        "ncaa_whockey:atlantic-hockey",
    ]
    assert (
        name(w, "ncaa_whockey:atlantic-hockey", 2024),
        name(w, "ncaa_whockey:atlantic-hockey", 2025),
    ) == (
        "College Hockey America",
        "Atlantic Hockey America",
    )
    assert (conf(w, "107", 2018), conf(w, "107", 2019)) == (
        "ncaa_whockey:newha",
        "ncaa_whockey:hockey-east",
    )  # Holy Cross
    assert conf(w, "155", 2017) == "ncaa_whockey:wcha"  # North Dakota's last season
    assert conf(w, "2900", 2022) == "ncaa_whockey:wcha"  # St. Thomas's first D-I season
    # LIU: ESPN uses a different id per gender
    assert (
        conf(w, "2341", 2027) == "ncaa_whockey:newha"
        and conf("ncaa_mhockey", "110133", 2027) is None
    )
    assert (
        tables(w)["team_group_seasons"]["sources_agree"].null_count()
        == tables(w)["team_group_seasons"].height
    )


@pytest.mark.parametrize(
    "league,listed,floor", [("ncaa_mhockey", 64, 0.55), ("ncaa_whockey", 46, 0.97)]
)
def test_espn_list_coverage(league, listed, floor):
    """Share of ESPN's listed teams whose latest row has a conference (the rest: non-D-I teams ESPN also lists, and
    D-I independents)."""
    latest = (
        tables(league)["team_group_seasons"].sort("season").group_by("team_id").last()
    )
    espn = pl.read_csv(RAW / league / "espn_teams.csv.gz", infer_schema=False)
    j = espn.join(latest, left_on="espn_team_id", right_on="team_id", how="left")
    n = j["conference_id"].is_not_null().sum()
    assert n == listed and n / espn.height >= floor
