"""NCAA <-> ESPN college-baseball team crosswalk: offline, from raw/ncaa_baseball{,_crosswalk} + curated/."""

from functools import cache
from pathlib import Path

import polars as pl

from sdv_reference.crosswalks import ncaa_baseball as xw

ALIASES = (
    Path(__file__).resolve().parents[1] / "curated" / "ncaa_baseball_espn_aliases.csv"
)


@cache
def built() -> pl.DataFrame:
    return xw.build()


def espn_id(team: str, season: int = 2026) -> str:
    return (
        built()
        .filter((pl.col("ncaa_team") == team) & (pl.col("season") == season))[
            "espn_team_id"
        ]
        .item()
    )


def test_contract():
    assert xw.validate(built()) == []


def test_baseball_ids_are_not_the_school_ids():
    """ESPN's baseball ids are their own system: Alabama is 333 in football/basketball, 148 in baseball."""
    assert espn_id("Alabama") == "148"
    assert (
        espn_id("Southern California") == "68"
    )  # school_hint: the basketball map's "USC" name


def test_alias_picks_the_live_duplicate():
    """ESPN lists Boise State twice; the alias pins the id that carries the 2020 games."""
    assert espn_id("Boise St.", 2020) == "1139"


def test_org_id_filled_for_a_transition_school():
    """stats.ncaa.org leaves Mercyhurst's 2026 org id blank; the ncaa_baseball members() fill keys it."""
    row = (
        built()
        .filter((pl.col("ncaa_team") == "Mercyhurst") & (pl.col("season") == 2026))
        .row(0, named=True)
    )
    assert row["ncaa_team_id"] == 408 and row["espn_team_id"] is not None


def test_one_espn_id_per_school():
    pairs = (
        built()
        .drop_nulls("espn_team_id")
        .select("ncaa_team_id", "espn_team_id")
        .unique()
    )
    assert (
        pairs["ncaa_team_id"].is_unique().all()
        and pairs["espn_team_id"].is_unique().all()
    )


def test_unmatched_school_stays_with_a_null_id(monkeypatch):
    """A school no tier resolves is kept, null, and marked -- never dropped or guessed."""
    monkeypatch.setattr(xw, "_csv", lambda path, real=xw._csv: real(path).filter(pl.col("team_id") != "148")
                        if path.name == "espn_teams.csv.gz" else real(path))  # fmt: skip
    df = xw.build(hints={})
    bama = df.filter(pl.col("ncaa_team") == "Alabama")
    assert bama.height > 0 and bama["espn_team_id"].null_count() == bama.height
    assert set(bama["match_method"]) == {"unmatched"}
    assert xw.validate(df) == []


def test_every_alias_is_used_and_justified():
    alias = pl.read_csv(ALIASES, infer_schema=False)
    assert alias["ncaa_team"].is_unique().all()
    assert alias["espn_team_id"].null_count() == 0 and alias["note"].null_count() == 0
    used = set(built().filter(pl.col("match_method") == "alias")["ncaa_team"].to_list())
    assert not sorted(set(alias["ncaa_team"]) - used), "alias rows matched nothing"
