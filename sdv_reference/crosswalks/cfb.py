"""ESPN team-season crosswalk for college football (tag cfb_crosswalk, asset cfb_espn_team_crosswalk).

One row per ESPN team-season of cfb_team_group_seasons, with its conference that season and each provider's team id
valid that season, read off cfb_team_aliases' windows. It replaces cfbfastR-cfb-data's cfb_teams_crosswalk_{season}
(a name outer-join copied into every season, keyed on a normalized name) as the season-aware source of truth, the way
{mbb,wbb}_crosswalk carry the NCAA <-> ESPN map per season. Nothing is matched here: every id is a team_aliases row
that already passed its contract (one id per team per source and season, one team per id). See CONTRACT.md.

    uv run python -m sdv_reference.build cfb cfb_crosswalk    # offline: raw/ + curated/ -> build/cfb_crosswalk/
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from sdv_reference import team_aliases
from sdv_reference.leagues import cfb

#: (source, id_kind) -> column; every provider id cfb_team_aliases carries
ID_COLUMNS = {
    ("cfbd", "team"): "cfbd_team_id",
    ("fox", "team"): "fox_team_id",
    ("yahoo", "team"): "yahoo_team_id",
    ("cbs", "team"): "cbs_team_id",
    ("sports247", "team"): "sports247_team_id",
    ("sports247", "school"): "sports247_school_id",
    ("on3", "school"): "on3_school_id",
}
COLUMNS = {
    "season": pl.Int32,
    "espn_team_id": pl.Utf8,
    "subdivision_id": pl.Utf8,
    "conference_id": pl.Utf8,
    **{c: pl.Utf8 for c in ID_COLUMNS.values()},
}
NAME = "cfb_espn_team_crosswalk"


def fetch() -> None:
    """Nothing of its own: its inputs are cfb's raw/ snapshots, which fetch('cfb') refreshes."""


def build(
    tgs: pl.DataFrame | None = None, aliases: pl.DataFrame | None = None
) -> pl.DataFrame:
    if tgs is None:
        tgs = cfb.build()["team_group_seasons"]
    if aliases is None:
        aliases = team_aliases.build("cfb", tgs)[0]
    hub = tgs.filter(pl.col("team_id_source") == "espn").select(
        pl.col("season").cast(pl.Int32),
        pl.col("team_id").alias("espn_team_id"),
        "subdivision_id",
        "conference_id",
    )
    unknown = set(
        aliases.filter(pl.col("source_id").is_not_null())
        .select("source", "id_kind")
        .unique()
        .rows()
    )
    assert unknown <= set(ID_COLUMNS), (
        f"cfb_team_aliases ids with no crosswalk column: {unknown - set(ID_COLUMNS)}"
    )
    ids = (
        aliases.filter(pl.col("source_id").is_not_null())
        .select(
            pl.col("team_id").alias("espn_team_id"),
            "source",
            "id_kind",
            "source_id",
            "valid_from",
            "valid_to",
        )
        .unique()
    )
    # a dated row covers its window; an undated one (the curated CBS map) every season its team is in the hub
    dated = (
        ids.filter(pl.col("valid_from").is_not_null())
        .with_columns(
            pl.int_ranges("valid_from", pl.col("valid_to") + 1, dtype=pl.Int32).alias(
                "season"
            )
        )
        .explode("season")
    )
    undated = ids.filter(pl.col("valid_from").is_null()).join(
        hub.select("season", "espn_team_id"), on="espn_team_id"
    )
    per = pl.concat(
        [dated.drop("valid_from", "valid_to"), undated.drop("valid_from", "valid_to")],
        how="diagonal",
    )
    for k in ("season", "espn_team_id"):
        assert per.schema[k] == hub.schema[k], (
            f"{k}: {per.schema[k]} != {hub.schema[k]}"
        )
    per = per.unique()
    out = hub
    for (source, kind), col in ID_COLUMNS.items():
        one = per.filter(
            (pl.col("source") == source) & (pl.col("id_kind") == kind)
        ).select("season", "espn_team_id", pl.col("source_id").alias(col))
        out = out.join(one, on=["season", "espn_team_id"], how="left")
    return out.select(list(COLUMNS)).cast(COLUMNS).sort("season", "espn_team_id")


def validate(df: pl.DataFrame, tgs: pl.DataFrame) -> list[str]:
    """Contract problems (CONTRACT.md); [] when it passes."""
    problems = []
    if dict(df.schema) != COLUMNS:
        return [f"schema {dict(df.schema)} != {COLUMNS}"]
    hub = tgs.filter(pl.col("team_id_source") == "espn")
    if df.height != hub.height:
        problems.append(f"{df.height} rows for {hub.height} ESPN team-seasons")
    if df.select("season", "espn_team_id").is_duplicated().any():
        problems.append("duplicate (season, espn_team_id)")
    for col in ID_COLUMNS.values():
        clash = (
            df.drop_nulls(col).group_by("season", col).len().filter(pl.col("len") > 1)
        )
        if clash.height:
            problems.append(
                f"{col} on two ESPN teams in one season: {clash.head(3).rows()}"
            )
    return problems


def write(out_dir: Path) -> list[Path]:
    tgs = cfb.build()["team_group_seasons"]
    df = build(tgs)
    problems = validate(df, tgs)
    if problems:
        raise ValueError(
            "cfb_crosswalk fails the contract:\n  " + "\n  ".join(problems)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [out_dir / f"{NAME}.parquet", out_dir / f"{NAME}.csv"]
    df.write_parquet(files[0])
    df.write_csv(files[1])
    fill = ", ".join(
        f"{c.removesuffix('_id')} {df[c].is_not_null().sum()}"
        for c in ID_COLUMNS.values()
    )
    print(f"cfb_crosswalk: {df.height} team-seasons ({df['season'].min()}..{df['season'].max()}); with an id: {fill}",
          flush=True)  # fmt: skip
    return files
