import polars as pl
import pytest

from sdv_reference.leagues import ncaa_baseball, ncaa_softball
from sdv_reference.schema import validate

L = "ncaa_softball"


@pytest.fixture(scope="module")
def t():
    return ncaa_softball.build()


def conf(t, team_id, season, source="ncaa_org"):
    r = t["team_group_seasons"].filter(
        (pl.col("team_id") == team_id)
        & (pl.col("season") == season)
        & (pl.col("team_id_source") == source)
    )
    assert r.height == 1, (team_id, season)
    return r["conference_id"][0]


def test_contract(t):
    assert validate(L, t) == []


def test_season_cleaning(t):
    seasons = set(t["team_group_seasons"]["season"])
    assert min(seasons) == 1982 and max(seasons) == 2026  # 2026 rolled forward (roll_forward)
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
        ("1356", 2013, "pacific-coast", "wac"),  # Seattle U
        # 2026, rolled forward from stats.ncaa.org baseball 2026 (July 2025 realignment)
        ("1356", 2026, "wac", "wcc"),  # Seattle U
        ("180", 2026, "caa", "cusa"),  # Delaware
        ("669", 2026, "mvc", "cusa"),  # Missouri State
        ("1104", 2026, "wac", "mountain-west"),  # Grand Canyon
        ("400", 2026, "atlantic-10", "mac"),  # UMass
        ("470", 2026, "ne10", "nec"),  # New Haven, D-II to D-I
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
    pick = lambda d, c: (
        d["team_group_seasons"]
        .filter(pl.col("team_id_source") == "ncaa_org")
        .select(
            "team_id",
            "season",
            pl.col("conference_id").str.split(":").list.get(1).alias(c),
        )
    )
    j = pick(bb, "bb").join(pick(sb, "sb"), on=["team_id", "season"]).drop_nulls()
    assert j.height > 10_000
    assert (j["bb"] == j["sb"]).mean() > 0.97


def test_espn_keyed_rows(t):
    tgs = t["team_group_seasons"]
    org, espn = (tgs.filter(pl.col("team_id_source") == s) for s in ("ncaa_org", "espn"))
    # each ESPN row is a copy of its school's ncaa_org row, same season and groups
    twin = espn.with_columns(pl.col("notes").str.extract(r"^ncaa_org_id=(\d+)").alias("team_id")).join(
        org, on=["team_id", "season"], suffix="_org"
    )
    assert twin.height == espn.height > 10_000
    assert twin["conference_id"].eq_missing(twin["conference_id_org"]).all()
    # Texas is 538 in ESPN softball (126 in baseball, 251 elsewhere): to the SEC for 2025
    assert conf(t, "538", 2024, "espn") == f"{L}:big-12"
    assert conf(t, "538", 2025, "espn") == f"{L}:sec"


def test_2026_rolled_forward(t):
    tgs = t["team_group_seasons"].filter(pl.col("season") == 2026)
    org = tgs.filter(pl.col("team_id_source") == "ncaa_org")
    assert org.height > 950 and org["notes"].str.contains("2026 not in source").all()
    # curated: SFA has been in the Southland since July 2024 (the softball source says WAC in 2025)
    assert conf(t, "676", 2026) == f"{L}:southland"
    # Saint Francis (PA) plays its last NEC season in 2025-26, then goes to D-III
    assert conf(t, "600", 2026) == f"{L}:nec"
    assert tgs.filter(pl.col("team_id_source") == "espn").height > 300


def test_2026_agrees_with_espn(t):
    """ESPN's core groups (ten D-I conferences) list every team the rolled-forward rows put in them, and no other."""
    org = t["team_group_seasons"].filter((pl.col("season") == 2026) & (pl.col("team_id_source") == "ncaa_org"))
    checked = org.filter(pl.col("sources_agree").is_not_null())
    assert checked.height > 110 and checked["sources_agree"].all()
    assert t["team_group_seasons"].filter(pl.col("season") < 2026)["sources_agree"].null_count() == (
        t["team_group_seasons"].filter(pl.col("season") < 2026).height
    )
