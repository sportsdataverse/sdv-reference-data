"""The four {league}_groups tables: dtypes, validation and writing. See CONTRACT.md."""

from __future__ import annotations

from pathlib import Path

import polars as pl

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "groups": {
        "league": pl.Utf8,
        "group_id": pl.Utf8,
        "level": pl.Utf8,
        "first_season": pl.Int32,
        "last_season": pl.Int32,
        "notes": pl.Utf8,
    },
    "group_seasons": {
        "league": pl.Utf8,
        "group_id": pl.Utf8,
        "season": pl.Int32,
        "level": pl.Utf8,
        "name": pl.Utf8,
        "short_name": pl.Utf8,
        "abbreviation": pl.Utf8,
        "parent_group_id": pl.Utf8,
        "n_teams": pl.Int32,
    },
    "group_aliases": {
        "league": pl.Utf8,
        "group_id": pl.Utf8,
        "source": pl.Utf8,
        "source_id": pl.Utf8,
        "name_kind": pl.Utf8,
        "value": pl.Utf8,
        "valid_from": pl.Int32,
        "valid_to": pl.Int32,
    },
    "team_group_seasons": {
        "league": pl.Utf8,
        "season": pl.Int32,
        "team_id": pl.Utf8,
        "team_id_source": pl.Utf8,
        "team_name": pl.Utf8,
        "subdivision_id": pl.Utf8,
        "conference_id": pl.Utf8,
        "division_id": pl.Utf8,
        "source": pl.Utf8,
        "sources_agree": pl.Boolean,
        "notes": pl.Utf8,
    },
}
KEYS = {
    "groups": ["league", "group_id", "level"],
    "group_seasons": ["league", "group_id", "season", "level"],
    "group_aliases": ["league", "group_id", "source", "name_kind", "value"],
    "team_group_seasons": ["league", "season", "team_id", "team_id_source", "source"],
}
LEVELS = {"league", "subdivision", "conference", "division"}
ENDING_YEAR_LEAGUES = {"nba", "nhl", "mbb", "wbb"}


def conform(name: str, df: pl.DataFrame) -> pl.DataFrame:
    """Select and cast to the contract's columns, in order; a missing optional column becomes null."""
    schema = SCHEMAS[name]
    return df.select(
        [
            (pl.col(c) if c in df.columns else pl.lit(None)).cast(t).alias(c)
            for c, t in schema.items()
        ]
    )


def validate(league: str, tables: dict[str, pl.DataFrame]) -> list[str]:
    """Problems with a league's tables; an empty list means they meet the contract."""
    problems = []
    for name, schema in SCHEMAS.items():
        df = tables.get(name)
        if df is None:
            problems.append(f"{name}: missing")
            continue
        if dict(df.schema) != schema:
            problems.append(f"{name}: schema {dict(df.schema)} != {schema}")
            continue
        for k in KEYS[name]:
            n = df[k].null_count()
            if n:
                problems.append(f"{name}.{k}: {n} nulls in a key column")
        if (df["league"] != league).any():
            problems.append(f"{name}: rows for another league")
    if problems:
        return problems

    g, gs, ga, tgs = (tables[n] for n in SCHEMAS)
    for name, df, col in [
        ("groups", g, "group_id"),
        ("group_seasons", gs, "group_id"),
        ("group_aliases", ga, "group_id"),
    ]:
        bad = df.filter(~pl.col(col).str.starts_with(f"{league}:"))
        if bad.height:
            problems.append(
                f"{name}: {bad.height} group ids not prefixed '{league}:', e.g. {bad[col][0]}"
            )
    bad_levels = set(g["level"].unique()) - LEVELS
    if bad_levels:
        problems.append(f"groups: unknown levels {bad_levels}")
    if g["group_id"].is_duplicated().any():
        problems.append("groups: duplicate group_id")
    if gs.select(["group_id", "season"]).is_duplicated().any():
        problems.append("group_seasons: duplicate (group_id, season)")
    if tgs.select(["team_id", "season"]).is_duplicated().any():
        dup = tgs.filter(tgs.select(["team_id", "season"]).is_duplicated()).head(3)
        problems.append(
            f"team_group_seasons: duplicate (team_id, season), e.g. {dup.select(['team_id', 'season']).rows()}"
        )

    known = set(g["group_id"])
    for name, df, cols in [
        ("group_seasons", gs, ["group_id", "parent_group_id"]),
        ("group_aliases", ga, ["group_id"]),
        ("team_group_seasons", tgs, ["subdivision_id", "conference_id", "division_id"]),
    ]:
        for c in cols:
            missing = set(df[c].drop_nulls()) - known
            if missing:
                problems.append(f"{name}.{c}: ids not in groups: {sorted(missing)[:5]}")
    # every (group, season) a team or a child points at exists that season
    have = set(gs.select(["group_id", "season"]).iter_rows())
    for c in ["subdivision_id", "conference_id", "division_id"]:
        refs = set(
            tgs.filter(pl.col(c).is_not_null()).select([c, "season"]).iter_rows()
        )
        miss = refs - have
        if miss:
            problems.append(
                f"team_group_seasons.{c}: {len(miss)} (group, season) pairs not in group_seasons, e.g. {sorted(miss)[:3]}"
            )
    parents = set(
        gs.filter(pl.col("parent_group_id").is_not_null())
        .select(["parent_group_id", "season"])
        .iter_rows()
    )
    if parents - have:
        problems.append(
            f"group_seasons.parent_group_id: {len(parents - have)} parent seasons missing, e.g. {sorted(parents - have)[:3]}"
        )
    span = gs.group_by("group_id").agg(
        pl.col("season").min().alias("lo"), pl.col("season").max().alias("hi")
    )
    off = g.join(span, on="group_id", how="left").filter(
        (pl.col("first_season") != pl.col("lo"))
        | (pl.col("last_season") != pl.col("hi"))
    )
    if off.height:
        problems.append(
            f"groups: first/last_season disagree with group_seasons for {off['group_id'].to_list()[:5]}"
        )
    return problems


def write_league(
    league: str, tables: dict[str, pl.DataFrame], out_dir: Path
) -> list[Path]:
    """Write the release files: each table as parquet + csv, plus team_group_seasons one file per season."""
    problems = validate(league, tables)
    if problems:
        raise ValueError(
            f"{league}: tables fail the contract:\n  " + "\n  ".join(problems)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in SCHEMAS:
        df = tables[name].sort(KEYS[name])
        for ext in ("parquet", "csv"):
            p = out_dir / f"{league}_{name}.{ext}"
            df.write_parquet(p) if ext == "parquet" else df.write_csv(p)
            written.append(p)
    for season, part in (
        tables["team_group_seasons"]
        .sort(KEYS["team_group_seasons"])
        .group_by("season", maintain_order=True)
    ):
        s = season[0] if isinstance(season, tuple) else season
        for ext in ("parquet", "csv"):
            p = out_dir / f"{league}_team_group_seasons_{s}.{ext}"
            part.write_parquet(p) if ext == "parquet" else part.write_csv(p)
            written.append(p)
    return written
