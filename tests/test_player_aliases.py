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
        _row(2025, "1", "a4", "f4"), _row(2025, "1", None, "f5"),  # f5: a miss with no pair anywhere
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

    # a blank athlete forces the id unmatched; on an id that is already unmatched it changes nothing
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f4,,x,y\n")
    a, u, _ = player_aliases.build("mbb", hub, pl.DataFrame())
    assert a.filter(pl.col("source_player_id") == "f4").height == 0
    assert u.filter(pl.col("source_player_id") == "f4")["reason"].to_list() == ["curated override: no ESPN athlete"]
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f5,,x,y\n")
    with pytest.raises(AssertionError, match="change nothing"):
        player_aliases.build("mbb", hub, pl.DataFrame())
    # an override can set the athlete of a leg's miss (team-block checks still apply)
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f5,a5,x,y\n")
    a = player_aliases.build("mbb", hub, pl.DataFrame())[0].filter(pl.col("source_player_id") == "f5")
    assert a.select("athlete_id", "team_id", "match_method").rows() == [("a5", "1", "override")]


@pytest.mark.parametrize("league", ["nba", "cfb"])
def test_windows_span_seasons(league):
    a = built(league)[0].filter(pl.col("source") != "ncaa")
    assert a.filter(pl.col("valid_to") > pl.col("valid_from")).height > 1000


def _cbs_league(monkeypatch, tmp_path, rows, statline):
    """A one-team cfb whose cbs leg is `rows` and whose ESPN stat-line athletes are `statline`."""
    monkeypatch.setattr(player_aliases, "legs", lambda league: pl.DataFrame(rows, schema=player_aliases.LONG))
    monkeypatch.setattr(player_aliases, "statlines", lambda league: pl.DataFrame(
        {"season": [2026] * len(statline), "athlete_id": statline}, schema={"season": pl.Int32, "athlete_id": pl.Utf8}))  # fmt: skip
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\n")
    (tmp_path / "player_match_floors.csv").write_text(
        "league,source,season_from,season_to,floor_pct,note\ncfb,cbs,2026,2026,98.0,test\n"
    )
    monkeypatch.setattr(player_aliases, "CURATED", tmp_path)
    hub = pl.DataFrame({"season": [2026], "team_id": ["1"], "team_id_source": ["espn"]},
                       schema_overrides={"season": pl.Int32})  # fmt: skip
    return player_aliases.build("cfb", hub, pl.DataFrame())[2]


def _cbs(athlete, pid):
    return {**_row(2026, "1", athlete, pid), "source": "cbs", "unmatched_reason": None}


def test_cfb_cbs_gate_counts_only_athletes_with_an_espn_stat_line(monkeypatch, tmp_path):
    # 10 ESPN athletes with a stat line, all with a CBS id; 10 linemen with none and no CBS id (CBS never lists them)
    rows = [_cbs(f"s{i}", f"c{i}") for i in range(10)] + [_cbs(f"ol{i}", None) for i in range(10)]
    rates = _cbs_league(monkeypatch, tmp_path, rows, [f"s{i}" for i in range(10)])
    r = rates.row(0, named=True)
    assert (r["match_rate"], r["match_rate_statline"], r["n_rows_statline"]) == (50.0, 100.0, 10)
    assert player_aliases.gate("cfb", rates) == []


def test_cfb_cbs_gate_fails_when_stat_line_athletes_lack_cbs_ids(monkeypatch, tmp_path):
    # 98% of all rows match, but 2 of the 50 stat-line athletes have no CBS id (96%): only the stat-line gate catches it
    rows = [_cbs(f"s{i}", f"c{i}" if i < 8 else None) for i in range(10)] + [_cbs(f"x{i}", f"d{i}") for i in range(90)]
    rates = _cbs_league(monkeypatch, tmp_path, rows, [f"s{i}" for i in range(10)] + [f"x{i}" for i in range(40)])
    r = rates.row(0, named=True)
    assert (r["match_rate"], r["match_rate_statline"]) == (98.0, 96.0)
    assert player_aliases.gate("cfb", rates) == ["cfb cbs 2026: match rate 96.0 < floor 98.0"]


@pytest.mark.parametrize("statline", [[], ["zzz"], ["s1"]])
def test_cfb_cbs_gate_never_falls_back_to_all_rows(monkeypatch, tmp_path, statline):
    """No snapshot, one sharing no athlete with the leg (id drift), or one covering under a quarter of it: the build fails."""
    rows = [_cbs(f"s{i}", f"c{i}") for i in range(5)]
    with pytest.raises(ValueError, match="no espn_statline snapshot"):
        _cbs_league(monkeypatch, tmp_path, rows, statline)


def test_other_legs_keep_the_all_rows_gate():
    rates = built("cfb")[2]
    assert rates.filter(pl.col("source") != "cbs")["match_rate_statline"].null_count() == rates.filter(
        pl.col("source") != "cbs"
    ).height
    assert rates.filter(pl.col("source") == "cbs")["match_rate_statline"].null_count() == 0


def test_torvik_leg_is_published():
    a = built("mbb")[0].filter(pl.col("source") == "torvik")
    assert a.height > 50_000
    assert set(a["match_method"]) <= {"name_jersey", "name", "last_jersey", "propagated"}


def test_torvik_tiers_on_a_synthetic_season(monkeypatch, tmp_path):
    """Name + jersey, then name, then last name + jersey, each only on a key unique on both sides of the block."""
    lg = tmp_path / "mbb"
    lg.mkdir()
    torvik = pl.DataFrame({
        "player_id": ["1", "2", "3", "4", "5", "6"],
        "player_name": ["José Núñez", "Al Smith", "Al Smith", "Bo Jones Jr.", "Cy Young", "Di Lee"],
        "team": ["Duke", "Duke", "Duke", "Duke", "Duke", "Nowhere St."],
        "number": ["1", "", "", "9", "4", "2"],
    })  # fmt: skip
    espn = pl.DataFrame({
        "season": ["2026"] * 5, "espn_team_id": ["150"] * 5, "espn_athlete_id": ["a1", "a2", "a3", "a4", "a5"],
        "espn_full_name": ["Jose Nunez", "Al Smith", "Al Smith", "Bo Jones", "Cyrus Young"],
        "espn_jersey": ["1", "3", "5", "9", "4"],
    })  # fmt: skip
    (lg / "torvik_2026.csv.gz").write_bytes(team_aliases._gz_csv(torvik))
    (lg / "mbb_player_crosswalk_2026.csv.gz").write_bytes(team_aliases._gz_csv(espn))
    monkeypatch.setattr(player_aliases, "RAW", tmp_path)
    ta = pl.DataFrame({"source": ["torvik"], "name_kind": ["name"], "value": ["Duke"], "team_id": ["150"],
                       "valid_from": [2026], "valid_to": [2026]}, schema_overrides={"valid_from": pl.Int32, "valid_to": pl.Int32})  # fmt: skip
    got = {
        r["source_player_id"]: (r["athlete_id"], r["match_method"], r["unmatched_reason"])
        for r in pl.concat(player_aliases._torvik_legs("mbb", ta)).to_dicts()
    }
    assert got["1"] == ("a1", "name_jersey", None)  # accents folded
    assert got["4"] == ("a4", "name_jersey", None)  # Jr. dropped
    assert got["5"] == ("a5", "last_jersey", None)  # Cy / Cyrus: last name + jersey
    # two Al Smiths with no Torvik jersey: a tie on every tier stays unmatched, never guessed
    assert got["2"][0] is None and got["3"][0] is None and got["2"][2].startswith("no unique name match")
    assert got["6"][0] is None and got["6"][2].startswith("no ESPN team")
