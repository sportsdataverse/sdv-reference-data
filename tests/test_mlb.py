"""MLB groups: offline, from raw/mlb/ + curated/. Team ids are ESPN's (franchise-level) where ESPN has the team."""

from functools import cache

import polars as pl

from sdv_reference.leagues import mlb
from sdv_reference.schema import validate


@cache
def tables():
    return mlb.build()


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
    assert validate("mlb", tables()) == []


def test_astros_nl_central_to_al_west_2013():
    assert (row("18", 1993)["division_id"], row("18", 1994)["division_id"]) == (
        "mlb:nl-west",
        "mlb:nl-central",
    )
    r12, r13 = row("18", 2012), row("18", 2013)
    assert (r12["conference_id"], r12["division_id"]) == ("mlb:nl", "mlb:nl-central")
    assert (r13["conference_id"], r13["division_id"]) == ("mlb:al", "mlb:al-west")


def test_brewers_al_to_nl_1998():
    r69, r70 = row("8", 1969), row("8", 1970)
    assert (r69["team_name"], r69["division_id"]) == ("Seattle Pilots", "mlb:al-west")
    assert (r70["team_name"], r70["division_id"]) == (
        "Milwaukee Brewers",
        "mlb:al-west",
    )
    assert row("8", 1972)["division_id"] == "mlb:al-east"
    assert row("8", 1994)["division_id"] == "mlb:al-central"
    r97, r98 = row("8", 1997), row("8", 1998)
    assert (r97["conference_id"], r97["division_id"]) == ("mlb:al", "mlb:al-central")
    assert (r98["conference_id"], r98["division_id"]) == ("mlb:nl", "mlb:nl-central")


def test_1994_three_division_era():
    gs = tables()["group_seasons"].filter(pl.col("level") == "division")
    divs = lambda y: dict(
        gs.filter(pl.col("season") == y).select("group_id", "n_teams").iter_rows()
    )
    assert divs(1993) == {
        "mlb:al-east": 7,
        "mlb:al-west": 7,
        "mlb:nl-east": 7,
        "mlb:nl-west": 7,
    }
    assert divs(1994) == {
        "mlb:al-east": 5,
        "mlb:al-central": 5,
        "mlb:al-west": 4,
        "mlb:nl-east": 5,
        "mlb:nl-central": 5,
        "mlb:nl-west": 4,
    }
    # CWS 4, CLE 5, KC 7, MIL 8, MIN 9 / CHC 16, CIN 17, HOU 18, PIT 23, STL 24
    assert members(1994, "division_id", "mlb:al-central") == {"4", "5", "7", "8", "9"}
    assert members(1994, "division_id", "mlb:nl-central") == {
        "16",
        "17",
        "18",
        "23",
        "24",
    }
    assert (row("15", 1993)["division_id"], row("15", 1994)["division_id"]) == (
        "mlb:nl-west",
        "mlb:nl-east",
    )  # ATL
    # the 1969-93 East/West share MLB's ids with the 1994+ East/West: one lineage each
    g = {r["group_id"]: r for r in tables()["groups"].iter_rows(named=True)}
    assert (g["mlb:al-east"]["first_season"], g["mlb:al-central"]["first_season"]) == (
        1969,
        1994,
    )
    ga = tables()["group_aliases"].filter(
        (pl.col("source") == "mlb") & (pl.col("name_kind") == "abbreviation")
    )
    assert dict(ga.select("group_id", "source_id").iter_rows())["mlb:al-east"] == "201"


def test_1998_expansion_and_detroit():
    assert (
        row("29", 1998)["division_id"] == "mlb:nl-west"
        and row("30", 1998)["division_id"] == "mlb:al-east"
    )
    assert (row("6", 1997)["division_id"], row("6", 1998)["division_id"]) == (
        "mlb:al-east",
        "mlb:al-central",
    )


def test_pre_divisional_leagues():
    t = tables()["team_group_seasons"]
    t68 = t.filter(
        (pl.col("season") == 1968) & pl.col("conference_id").is_in(["mlb:al", "mlb:nl"])
    )
    assert t68.height == 20 and t68["division_id"].null_count() == 20
    assert "7" not in set(
        t68["team_id"]
    )  # the Royals are listed for 1968 with no league: dropped
    assert (
        row("1", 1901)["team_name"] == "Milwaukee Brewers"
    )  # ESPN 1 = the Orioles franchise
    assert (row("10", 1901)["team_name"], row("10", 1901)["notes"].split(";")[0]) == (
        "Baltimore Orioles",
        "mlb_team_id=298",
    )
    fl = t.filter(pl.col("conference_id") == "mlb:federal-league")
    assert sorted(fl["season"].unique()) == [1914, 1915] and set(
        fl["team_id_source"]
    ) == {"mlb"}


def test_names_are_as_of_the_season():
    assert (row("5", 2021)["team_name"], row("5", 2022)["team_name"]) == (
        "Cleveland Indians",
        "Cleveland Guardians",
    )
    assert (row("11", 1967)["team_name"], row("11", 1968)["team_name"]) == (
        "Kansas City Athletics",
        "Oakland Athletics",
    )
    assert (
        row("20", 2004)["team_name"] == "Montreal Expos"
        and row("20", 2005)["team_name"] == "Washington Nationals"
    )


def test_negro_leagues_and_federal_league():
    # the Stats API files the 1920-48 Negro leagues under sportId 1; ESPN carries most of their clubs too
    kc = tables()["team_group_seasons"].filter(pl.col("team_name") == "Kansas City Monarchs")
    assert set(kc["team_id"]) == {"308"} and set(kc["team_id_source"]) == {"espn"}
    conf = dict(kc.select("season", "conference_id").iter_rows())
    assert (conf[1920], conf[1937]) == ("mlb:negro-national-league-i", "mlb:negro-american-league")
    gs = tables()["group_seasons"]
    n = dict(gs.filter(pl.col("season") == 1920).select("group_id", "n_teams").iter_rows())
    assert (n["mlb:negro-national-league-i"], n["mlb:al"], n["mlb:nl"], n["mlb:mlb"]) == (8, 8, 8, 24)
    assert row("282", 1931)["team_name"] == "Cleveland Cubs"  # the Stats API's " - Deprecate" flag stripped


def test_espn_cross_check():
    t = tables()["team_group_seasons"]
    alnl = t.filter(pl.col("conference_id").is_in(["mlb:al", "mlb:nl"]))
    # every AL/NL team-season 1901-2026 has an ESPN id and ESPN agrees on league and division
    assert alnl["sources_agree"].null_count() == 0 and alnl["sources_agree"].all()
    assert row("18", 2013)["sources_agree"] is True  # ESPN also moves the Astros in 2013
    fl = t.filter(pl.col("conference_id") == "mlb:federal-league")
    assert fl["sources_agree"].null_count() == fl.height  # no ESPN coverage
