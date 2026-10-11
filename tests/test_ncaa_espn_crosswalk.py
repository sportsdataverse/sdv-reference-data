"""NCAA <-> ESPN basketball crosswalk: offline, from raw/{mbb,wbb}_crosswalk + curated/ + the groups build."""

from functools import cache
from pathlib import Path

import polars as pl
import pytest

from sdv_reference.crosswalks import _ncaa_espn as xw

RAW = Path(__file__).resolve().parents[1] / "raw"


@cache
def built():
    return xw.build()


@pytest.mark.parametrize("league", xw.LEAGUES)
def test_contract(league):
    assert xw.validate(built()[0][league], league) == []


@pytest.mark.parametrize("league", xw.LEAGUES)
def test_matches_the_sdv_py_build_it_replaced(league):
    """Row for row, the sdv-py build's output (raw/{league}/ncaa_espn_team_crosswalk.csv.gz) is reproduced."""
    old = pl.read_csv(
        RAW / league / "ncaa_espn_team_crosswalk.csv.gz", infer_schema=False
    )
    new = built()[0][league].select(old.columns).with_columns(pl.all().cast(pl.Utf8))
    new = new.filter(pl.col("season").is_in(old["season"].unique().to_list()))
    assert new.sort("season", "ncaa_team_id").equals(old.sort("season", "ncaa_team_id"))


def test_one_espn_id_per_school():
    schools = built()[1]
    assert schools["ncaa_org_id"].is_unique().all()
    assert schools.drop_nulls("espn_team_id")["espn_team_id"].is_unique().all()
    # ESPN ids are school-level, so the men's and women's programs of a school share one
    presence = built()[2].drop_nulls("espn_team_id")
    assert (
        presence.group_by("ncaa_org_id")
        .agg(pl.col("espn_team_id").n_unique())
        .filter(pl.col("espn_team_id") > 1)
        .height
        == 0
    )


def test_org_fix_applied():
    """Mount St. Mary's 2019-20 MBB team id sits under a stray org on stats.ncaa.org (curated fix)."""
    row = built()[0]["mbb"].filter(pl.col("ncaa_team_id") == 487652).row(0, named=True)
    assert (row["ncaa_org_id"], row["espn_team_id"]) == (450, "116")


def test_realignment_follows_the_season():
    mbb = built()[0]["mbb"].filter(pl.col("espn_team_id") == "120")  # Maryland
    conf = dict(mbb.select("season", "conference_id").iter_rows())
    assert (conf["2013-14"], conf["2014-15"]) == ("mbb:acc", "mbb:big-ten")


@pytest.mark.parametrize(
    "name,key",
    [
        ("St. John's (NY)", "saint john s ny"),
        ("Ohio St.", "ohio state"),
        ("Texas A&M", "texas a and m"),
    ],
)
def test_normalize(name, key):
    assert xw.normalize(name) == key


def test_unresolved_name_takes_its_schools_espn_id(monkeypatch):
    """A rename the name matchers miss (here FDU, forced unmatched) inherits the id its org resolves to elsewhere."""
    real = xw._resolver

    def resolver(espn):
        resolve = real(espn)
        return lambda team: (None, "unmatched") if team == "FDU" else resolve(team)

    monkeypatch.setattr(xw, "_resolver", resolver)
    fdu = xw.build()[0]["mbb"].filter(pl.col("ncaa_org_id") == 222)
    assert set(fdu.filter(pl.col("ncaa_team") == "FDU")["match_method"]) == {"org"}
    assert fdu["espn_team_id"].n_unique() == 1 and fdu["espn_team_id"].null_count() == 0
