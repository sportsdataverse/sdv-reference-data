"""cfb_espn_team_crosswalk: one row per ESPN team-season, each provider id read off cfb_team_aliases' windows."""

from functools import cache

import polars as pl

from sdv_reference import team_aliases
from sdv_reference.crosswalks import cfb as cw
from sdv_reference.leagues import cfb


@cache
def built():
    tgs = cfb.build()["team_group_seasons"]
    return tgs, team_aliases.build("cfb", tgs)[0]


def test_contract_on_committed_raw():
    tgs, aliases = built()
    df = cw.build(tgs, aliases)
    assert cw.validate(df, tgs) == []
    osu = df.filter((pl.col("espn_team_id") == "194") & (pl.col("season") == 2025)).row(0, named=True)
    assert (osu["conference_id"], osu["fox_team_id"], osu["yahoo_team_id"]) == ("cfb:big-ten", "25", "ncaaf.t.33")


def test_every_aliases_window_lands_on_its_seasons():
    """Each dated team_aliases id appears in exactly the crosswalk seasons its window covers."""
    tgs, aliases = built()
    df = cw.build(tgs, aliases)
    for (source, kind), col in cw.ID_COLUMNS.items():
        want = (
            aliases.filter((pl.col("source") == source) & (pl.col("id_kind") == kind) & pl.col("valid_from").is_not_null())
            .select("team_id", "source_id", pl.int_ranges("valid_from", pl.col("valid_to") + 1, dtype=pl.Int32).alias("season"))
            .explode("season").unique().select("season", pl.col("team_id").alias("espn_team_id"), pl.col("source_id").alias(col))
        )  # fmt: skip
        got = df.drop_nulls(col).select("season", "espn_team_id", col)
        if source != "cbs":  # cbs is undated: it fills every hub season (test below)
            assert want.join(got, on=list(want.columns), how="anti").height == 0, col
            assert got.join(want, on=list(want.columns), how="anti").height == 0, col


def _tgs(rows):
    return pl.DataFrame(
        [dict(season=s, team_id=t, team_id_source="espn", subdivision_id="cfb:fbs", conference_id="cfb:x") for s, t in rows],
        schema_overrides={"season": pl.Int32},
    )


def _alias(team, source, kind, sid, lo, hi):
    return dict(team_id=team, source=source, id_kind=kind, source_id=sid, valid_from=lo, valid_to=hi)


def test_dated_and_undated_ids_on_a_synthetic_league():
    tgs = _tgs([(2024, "1"), (2025, "1"), (2026, "1"), (2025, "2")])
    aliases = pl.DataFrame(
        [_alias("1", "fox", "team", "f1", 2025, 2026), _alias("1", "cbs", "team", "c1", None, None),
         _alias("2", "yahoo", "team", "y2", 2025, 2025)],  # fmt: skip
        schema_overrides={"valid_from": pl.Int32, "valid_to": pl.Int32},
    )
    df = cw.build(tgs, aliases)
    assert cw.validate(df, tgs) == []
    assert df.select("season", "espn_team_id", "fox_team_id", "yahoo_team_id", "cbs_team_id").rows() == [
        (2024, "1", None, None, "c1"),
        (2025, "1", "f1", None, "c1"),
        (2025, "2", None, "y2", None),
        (2026, "1", "f1", None, "c1"),
    ]


def test_an_id_on_two_teams_in_a_season_fails():
    tgs = _tgs([(2025, "1"), (2025, "2")])
    aliases = pl.DataFrame(
        [_alias("1", "fox", "team", "f1", 2025, 2025), _alias("2", "fox", "team", "f1", 2025, 2025)],
        schema_overrides={"valid_from": pl.Int32, "valid_to": pl.Int32},
    )
    assert cw.validate(cw.build(tgs, aliases), tgs) == ["fox_team_id on two ESPN teams in one season: [(2025, 'f1', 2)]"]
