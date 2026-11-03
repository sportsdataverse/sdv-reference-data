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
#: the fifth table of a {league}_groups tag (sdv_reference/team_aliases.py); see CONTRACT.md
TEAM_ALIASES: dict[str, pl.DataType] = {
    "league": pl.Utf8,
    "team_id": pl.Utf8,
    "team_id_source": pl.Utf8,
    "source": pl.Utf8,
    "source_id": pl.Utf8,
    "id_kind": pl.Utf8,
    "name_kind": pl.Utf8,
    "value": pl.Utf8,
    "valid_from": pl.Int32,
    "valid_to": pl.Int32,
    "match_method": pl.Utf8,
    "evidence": pl.Utf8,
}
TEAM_ALIASES_KEYS = [
    "league",
    "team_id",
    "team_id_source",
    "source",
    "id_kind",
    "match_method",
    "evidence",
]
TEAM_ALIAS_SOURCES = {
    "fox", "yahoo", "cbs", "torvik", "kenpom", "ncaa", "nba_stats", "wnba_stats", "mlb", "nhl", "cfbd",
    "sports247", "on3",
}  # fmt: skip
TEAM_ALIAS_METHODS = {
    "deterministic",
    "crosswalk",
    "curated",
    "alias",
    "exact_name",
    "dict",
}
LEVELS = {"league", "subdivision", "conference", "division"}
ENDING_YEAR_LEAGUES = {"nba", "nhl", "mbb", "wbb", "ncaa_mhockey", "ncaa_whockey"}


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
    # a team id means something only within its id space: ESPN college-baseball ids reuse NCAA org ids
    key = ["team_id_source", "team_id", "season"]
    if tgs.select(key).is_duplicated().any():
        dup = tgs.filter(tgs.select(key).is_duplicated()).head(3)
        problems.append(
            f"team_group_seasons: duplicate (team_id_source, team_id, season), e.g. {dup.select(key).rows()}"
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


def validate_team_aliases(
    league: str, ta: pl.DataFrame, tgs: pl.DataFrame
) -> list[str]:
    """Problems with a league's team_aliases table, checked against its team_group_seasons; empty = meets the
    contract. A null valid_from / valid_to is an open bound, checked over every season the team has."""
    if dict(ta.schema) != TEAM_ALIASES:
        return [f"team_aliases: schema {dict(ta.schema)} != {TEAM_ALIASES}"]
    problems = []
    for k in TEAM_ALIASES_KEYS:
        if n := ta[k].null_count():
            problems.append(f"team_aliases.{k}: {n} nulls in a key column")
    if (ta["league"] != league).any():
        problems.append("team_aliases: rows for another league")
    if bad := set(ta["source"].unique()) - TEAM_ALIAS_SOURCES:
        problems.append(f"team_aliases: unknown sources {bad}")
    if bad := set(ta["match_method"].unique()) - TEAM_ALIAS_METHODS:
        problems.append(f"team_aliases: unknown match_method {bad}")
    if ta.filter(pl.col("source_id").is_null() & pl.col("value").is_null()).height:
        problems.append("team_aliases: rows with neither a source_id nor a value")
    if ta.filter(pl.col("name_kind").is_null() != pl.col("value").is_null()).height:
        problems.append(
            "team_aliases: name_kind and value must be both set or both null"
        )
    if ta.filter(pl.col("valid_from") > pl.col("valid_to")).height:
        problems.append("team_aliases: valid_from after valid_to")
    if problems:
        return problems
    hub = (
        tgs.filter(pl.col("team_id_source") == "espn")
        .select("season", "team_id")
        .unique()
    )
    span = hub.group_by("team_id").agg(
        pl.col("season").min().alias("lo"), pl.col("season").max().alias("hi")
    )
    rows = (
        ta.with_row_index("_r")
        .join(span, on="team_id", how="left")
        .with_columns(
            pl.int_ranges(
                pl.coalesce("valid_from", "lo"), pl.coalesce("valid_to", "hi") + 1
            ).alias("season")
        )
        .explode("season")
        .with_columns(pl.col("season").cast(pl.Int32))
    )
    bounded = rows.filter(
        pl.col("valid_from").is_not_null() | pl.col("valid_to").is_not_null()
    )
    miss = bounded.join(hub, on=["season", "team_id"], how="anti")
    if miss.height:
        problems.append(
            f"team_aliases: {miss.height} team-seasons not in team_group_seasons, e.g. {miss.select('team_id', 'source', 'season').head(3).rows()}"
        )
    rows = rows.join(hub, on=["season", "team_id"], how="semi")
    ids = rows.filter(pl.col("source_id").is_not_null())
    for by, other, what in [
        (["source", "id_kind", "source_id"], "team_id", "an id on two teams"),
        (
            ["source", "id_kind", "team_id"],
            "source_id",
            "a team with two ids of one source",
        ),
    ]:
        dup = (
            ids.group_by("season", *by)
            .agg(pl.col(other).n_unique().alias("n"))
            .filter(pl.col("n") > 1)
        )
        if dup.height:
            problems.append(
                f"team_aliases: {what} in a season, e.g. {dup.head(3).rows()}"
            )
    dup = (
        rows.filter(pl.col("source_id").is_null())
        .group_by("season", "source", "name_kind", "value")
        .agg(pl.col("team_id").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if dup.height:
        problems.append(
            f"team_aliases: a name on two teams in a season, e.g. {dup.head(3).rows()}"
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
