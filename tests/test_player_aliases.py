"""{league}_player_aliases: offline (raw/ + curated/), the contract, the match-rate floors and the no-guessing rules."""

import importlib
from functools import cache

import polars as pl
import pytest

from sdv_reference import player_aliases, team_aliases
from sdv_reference.schema import validate_player_aliases


@cache
def tgs(league: str) -> pl.DataFrame:
    return importlib.import_module(f"sdv_reference.leagues.{league}").build()[
        "team_group_seasons"
    ]


@cache
def built(league: str):
    return player_aliases.build(
        league, tgs(league), team_aliases.build(league, tgs(league))[0]
    )


@pytest.mark.parametrize("league", player_aliases.LEAGUES)
def test_contract_and_floors(league):
    aliases, _, rates = built(league)
    assert validate_player_aliases(league, aliases, tgs(league)) == []
    assert player_aliases.gate(league, rates) == []


@pytest.mark.parametrize("league", ["mbb", "wbb", "cfb"])
def test_ncaa_leg_is_published(league):
    a = built(league)[0].filter(pl.col("source") == "ncaa")
    assert a.height > 50_000
    # stats.ncaa.org roster ids are per season: every NCAA row is one season on one NCAA team
    assert (a["valid_from"] == a["valid_to"]).all() and a[
        "source_team_id"
    ].null_count() == 0
    # transfers link through the athlete: some athletes carry NCAA ids on two ESPN teams
    assert (
        a.group_by("athlete_id")
        .agg(pl.col("team_id").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
        .height
        > 1000
    )


def test_nba_yahoo_player_ids_are_dotted():
    y = built("nba")[0].filter(pl.col("source") == "yahoo")
    assert y["source_player_id"].str.starts_with("nba.p.").all()


def test_every_dropped_row_has_a_reason():
    for league in ("nfl", "mbb"):
        assert built(league)[1]["reason"].null_count() == 0


def _row(season, team, athlete, pid, method="exact_name"):
    return dict(season=season, team_id=team, athlete_id=athlete, source="fox", source_player_id=pid, source_team_id=None,
                match_method=method, match_confidence=1.0, unmatched_reason=None if athlete else "no ESPN athlete in the leg",
                evidence="test")  # fmt: skip


def test_rules_on_a_synthetic_league(monkeypatch, tmp_path):
    rows = [
        _row(2024, "1", "a1", "f1"), _row(2025, "2", None, "f1"),  # f1 carried to 2025 (a transfer to team 2)
        _row(2024, "1", "a2", "f2"), _row(2025, "1", "a3", "f2"),  # f2 on two athletes: dropped every season
        _row(2025, "1", "a4", "f4"),
    ]  # fmt: skip
    monkeypatch.setattr(
        player_aliases,
        "legs",
        lambda league: pl.DataFrame(rows, schema=player_aliases.LONG),
    )
    hub = pl.DataFrame({"season": [2024, 2025, 2024, 2025], "team_id": ["1", "1", "2", "2"],
                        "team_id_source": ["espn"] * 4}, schema_overrides={"season": pl.Int32})  # fmt: skip
    (tmp_path / "player_overrides.csv").write_text(
        "league,source,source_player_id,athlete_id,verified_by,note\n"
    )
    (tmp_path / "player_match_floors.csv").write_text(
        "league,source,season_from,season_to,floor_pct,note\nmbb,fox,2024,2025,90.0,test\n"
    )
    monkeypatch.setattr(player_aliases, "CURATED", tmp_path)
    aliases, unmatched, rates = player_aliases.build("mbb", hub, pl.DataFrame())
    got = {
        (
            r["source_player_id"],
            r["athlete_id"],
            r["team_id"],
            r["valid_from"],
            r["match_method"],
        )
        for r in aliases.to_dicts()
    }
    assert got == {
        ("f1", "a1", "1", 2024, "exact_name"),
        ("f1", "a1", "2", 2025, "propagated"),
        ("f4", "a4", "1", 2025, "exact_name"),
    }
    assert set(
        unmatched.filter(pl.col("source_player_id") == "f2")["reason"].str.slice(0, 8)
    ) == {"conflict"}
    assert player_aliases.gate("mbb", rates)  # 2024: 1 of 2 rows kept -> 50% < 90%

    # an override that changes nothing fails the build; one that does is applied
    (tmp_path / "player_overrides.csv").write_text(
        "league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f4,a4,x,y\n"
    )
    with pytest.raises(AssertionError, match="change nothing"):
        player_aliases.build("mbb", hub, pl.DataFrame())
    (tmp_path / "player_overrides.csv").write_text(
        "league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f4,a9,x,y\n"
    )
    a = player_aliases.build("mbb", hub, pl.DataFrame())[0].filter(
        pl.col("source_player_id") == "f4"
    )
    assert a.select("athlete_id", "match_method").rows() == [("a9", "override")]
