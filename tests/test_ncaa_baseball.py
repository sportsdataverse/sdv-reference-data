import polars as pl
import pytest

from sdv_reference.leagues import ncaa_baseball
from sdv_reference.schema import validate

L = "ncaa_baseball"


@pytest.fixture(scope="module")
def t():
    return ncaa_baseball.build()


def row(t, team_id, season):
    r = t["team_group_seasons"].filter(
        (pl.col("team_id") == team_id) & (pl.col("season") == season)
    )
    assert r.height == 1, (team_id, season)
    return r.row(0, named=True)


def name(t, slug, season):
    return (
        t["group_seasons"]
        .filter((pl.col("group_id") == f"{L}:{slug}") & (pl.col("season") == season))[
            "name"
        ]
        .item()
    )


def test_contract(t):
    assert validate(L, t) == []
    tgs = t["team_group_seasons"]
    assert (tgs["season"].min(), tgs["season"].max()) == (2010, 2026)


@pytest.mark.parametrize(
    "team_id, season, before, after",
    [
        ("392", 2015, "acc", "big-ten"),  # Maryland
        ("703", 2025, "big-12", "sec"),  # Texas
        ("522", 2025, "big-12", "sec"),  # Oklahoma
        ("77", 2024, "wcc", "big-12"),  # BYU
        ("77", 2012, "mountain-west", "wcc"),  # BYU
        ("1356", 2026, "wac", "wcc"),  # Seattle U
        ("1104", 2026, "wac", "mountain-west"),  # Grand Canyon
        ("1104", 2014, "pacwest", "wac"),  # Grand Canyon, D-II to D-I
        (
            "367",
            2015,
            "american",
            "acc",
        ),  # Louisville: old Big East (823), a year in the American, then the ACC
        ("164", 2021, "american", "big-east"),  # UConn to the new Big East (30184)
        ("732", 2012, "mountain-west", "pac-12"),  # Utah
        ("410", 2020, "ne10", "nec"),  # Merrimack, D-II to D-I
    ],
)
def test_realignments(t, team_id, season, before, after):
    assert row(t, team_id, season - 1)["conference_id"] == f"{L}:{before}"
    assert row(t, team_id, season)["conference_id"] == f"{L}:{after}"


def test_subdivision_moves(t):
    assert row(t, "1104", 2013)["subdivision_id"] == f"{L}:d2"
    assert row(t, "1104", 2014)["subdivision_id"] == f"{L}:d1"


def test_names_as_of_season(t):
    assert name(t, "american", 2013) == "Big East Conference"
    assert name(t, "american", 2014) == "American Athletic Conference"
    assert name(t, "american", 2026) == "American Conference"
    assert name(t, "pac-12", 2011) == "Pacific-10 Conference"
    assert name(t, "pac-12", 2012) == "Pac-12 Conference"
    # stats.ncaa.org shows these retroactively as "Metro" and "UAC"
    assert name(t, "maac", 2026) == "Metro Atlantic Athletic Conference"
    assert name(t, "wac", 2026) == "Western Athletic Conference"
    assert name(t, "caa", 2023) == "Colonial Athletic Association"
    assert name(t, "caa", 2024) == "Coastal Athletic Association"


def test_every_ncaa_conf_id_has_a_lineage(t):
    raw = (
        set(ncaa_baseball.read_lookup()["conference_id"].drop_nulls())
        - ncaa_baseball.INDEPENDENT
    )
    ga = t["group_aliases"].filter(pl.col("source") == "ncaa")
    assert raw <= {int(i) for i in ga["source_id"].drop_nulls()}
    # the Great West's two conf_ids are one lineage
    gwc = ga.filter(pl.col("group_id") == f"{L}:great-west")
    assert set(gwc["source_id"].drop_nulls()) == {"30062", "30156"}


def test_cross_division_duplicates(t):
    # Merrimack 2019 is listed in the NE10 (D-II) and the NEC (D-I); it moved for 2019-20
    r = row(t, "410", 2019)
    assert (r["subdivision_id"], r["conference_id"]) == (f"{L}:d2", f"{L}:ne10")
    # New York Tech 2016: D-I independent (the D-I row carries the season team id)
    r = row(t, "477", 2016)
    assert (r["subdivision_id"], r["conference_id"]) == (f"{L}:d1", None)
    assert "independent" in r["notes"]


def test_org_id_fills(t):
    # the lookup has null team_id for 2025+ renames; UAH is Alabama Huntsville (org 10)
    assert row(t, "10", 2025)["team_name"] == "UAH"
    assert row(t, "10", 2024)["team_name"] == "Alabama Huntsville"
