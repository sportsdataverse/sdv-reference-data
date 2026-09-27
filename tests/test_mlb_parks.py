"""mlb_park_dimensions: offline, from raw/mlb_parks/ + curated/, checked against published fence moves."""

from functools import cache

import polars as pl
import pytest

from sdv_reference import build as builder
from sdv_reference.parks import mlb as parks

FENWAY, CAMDEN, COMERICA, DAIKIN, ORACLE, CITI, PETCO, TMOBILE = (
    "3",
    "2",
    "2394",
    "2392",
    "2395",
    "3289",
    "2680",
    "680",
)


@cache
def table():
    return parks.build()


def park(venue_id, season):
    t = table().filter((pl.col("venue_id") == venue_id) & (pl.col("season") == season))
    assert t.height == 1, (venue_id, season)
    return t.row(0, named=True)


def fences(venue_id, season):
    r = park(venue_id, season)
    return tuple(r[c] for c in parks.DISTANCES.values())


def test_contract():
    t = table()
    assert parks.validate(t) == []
    assert t.schema["venue_id"] == pl.Utf8 and t.schema["season"] == pl.Int32
    # per-season records start in 2001; every season since has the 30 regular-season parks and more
    assert t["season"].min() == parks.FIRST_SEASON == 2001
    per = dict(t.group_by("season").agg(pl.len()).iter_rows())
    assert set(per) == set(range(2001, 2027)) and min(per.values()) >= 30
    assert not t.filter(
        pl.col("venue_name").is_in(["TBD", "AL Stadium", "NL Stadium"])
    ).height


def test_fenway_green_monster_and_pesky_pole():
    for y in (2001, 2012, 2026):
        r = park(FENWAY, y)
        assert (r["left_line_ft"], r["center_ft"], r["right_line_ft"]) == (
            310,
            420,
            302,
        )
        assert (r["retro_park_id"], r["turf_type"], r["roof_type"]) == (
            "BOS07",
            "Grass",
            "Open",
        )


def test_oracle_park_2020_fences_and_names():
    # right field line 309 ft throughout; the 2020 bullpen move brought centre 399 -> 391 and Triples Alley 421 -> 415
    assert {park(ORACLE, y)["right_line_ft"] for y in range(2001, 2027)} == {309}
    r19, r20 = park(ORACLE, 2019), park(ORACLE, 2020)
    assert (r19["center_ft"], r19["right_center_ft"]) == (399, 421)
    assert (r20["center_ft"], r20["right_center_ft"]) == (391, 415)
    names = [park(ORACLE, y)["venue_name"] for y in (2001, 2004, 2006, 2019)]
    assert names == [
        "PacBell Park",
        "SBC Park",
        "AT&T Park",
        "Oracle Park",
    ]  # as of the season, not today's


def test_citi_field_2012_and_2015_fence_moves():
    assert (
        park(CITI, 2011)["left_ft"] is None and park(CITI, 2012)["left_ft"] == 358
    )  # the new 358 ft marker
    assert park(CITI, 2011)["left_center_ft"] > park(CITI, 2012)["left_center_ft"]
    r14, r15 = park(CITI, 2014), park(CITI, 2015)
    assert (r14["right_center_ft"], r14["right_ft"]) == (390, 375)
    assert (r15["right_center_ft"], r15["right_ft"]) == (380, 370)


def test_camden_yards_left_field_2022_and_2025():
    assert park(CAMDEN, 2021)["left_ft"] == 364
    r22 = park(CAMDEN, 2022)  # the API has 2021's wall until 2023: curated
    assert (r22["left_ft"], r22["left_center_ft"]) == (384, 398) and "curated" in r22[
        "notes"
    ]
    assert (
        fences(CAMDEN, 2022) == fences(CAMDEN, 2023)
        and park(CAMDEN, 2023)["notes"] is None
    )
    assert park(CAMDEN, 2025)["left_ft"] == 363  # moved back in for 2025
    assert {park(CAMDEN, y)["left_line_ft"] for y in range(2001, 2027)} == {333}


def test_daikin_park_tals_hill_removed_2017():
    assert (park(DAIKIN, 2016)["center_ft"], park(DAIKIN, 2017)["center_ft"]) == (
        435,
        409,
    )
    assert (
        park(DAIKIN, 2016)["venue_name"] == "Minute Maid Park"
        and park(DAIKIN, 2025)["venue_name"] == "Daikin Park"
    )


def test_curated_lags_and_misses():
    # Petco: moved in for 2013, which the API records from 2015
    assert (
        park(PETCO, 2012)["right_center_ft"],
        park(PETCO, 2013)["right_center_ft"],
    ) == (411, 391)
    assert fences(PETCO, 2013) == fences(PETCO, 2015)
    # T-Mobile: moved in for 2013 and never moved back; the API reverts to the old fences from 2019
    assert fences(TMOBILE, 2013) == fences(TMOBILE, 2018) == fences(TMOBILE, 2026)
    assert (
        park(TMOBILE, 2026)["left_center_ft"],
        park(TMOBILE, 2026)["center_ft"],
    ) == (378, 401)
    assert (
        park(TMOBILE, 2012)["left_center_ft"],
        park(TMOBILE, 2012)["center_ft"],
    ) == (390, 405)
    # Comerica: centre field 420 -> 412 for 2023, which the API still lacks
    assert (park(COMERICA, 2022)["center_ft"], park(COMERICA, 2023)["center_ft"]) == (
        420,
        412,
    )


def test_turf_roof_and_elevation():
    assert park("19", 2026)["elevation_ft"] > 5000  # Coors Field, a mile high
    assert (park("15", 2018)["turf_type"], park("15", 2019)["turf_type"]) == (
        "Grass",
        "Artificial Turf",
    )  # Chase
    assert (park("4169", 2019)["turf_type"], park("4169", 2020)["turf_type"]) == (
        "Grass",
        "Artificial Turf",
    )  # Marlins
    assert park("12", 2026)["roof_type"] == "Dome"  # Tropicana Field


def test_override_that_changes_nothing_fails(tmp_path, monkeypatch):
    p = tmp_path / "o.csv"
    p.write_text(
        "venue_id,valid_from,valid_to,column,value,source,notes\n3,2001,2001,left_line_ft,310,x,no-op\n"
    )
    monkeypatch.setattr(parks, "OVERRIDES", p)
    with pytest.raises(ValueError, match="changes nothing"):
        parks.build()


def test_validate_catches_problems():
    t = table()
    assert any("duplicate" in p for p in parks.validate(pl.concat([t, t.head(1)])))
    bad = t.with_columns(
        pl.when(pl.col("venue_id") == FENWAY)
        .then(3100)
        .otherwise(pl.col("center_ft"))
        .alias("center_ft")
    )
    assert any(
        "outside" in p
        for p in parks.validate(bad.with_columns(pl.col("center_ft").cast(pl.Int32)))
    )
    assert any(
        "schema" in p
        for p in parks.validate(t.with_columns(pl.col("season").cast(pl.Int64)))
    )


def test_pipeline_names():
    assert builder.available()[-1] == "mlb_parks" and "mlb" in builder.available()
    assert builder.module("mlb_parks") is parks
