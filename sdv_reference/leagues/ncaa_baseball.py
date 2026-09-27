"""NCAA baseball groups reference: stats.ncaa.org membership and conf_ids, via baseballr-data's team lookup.

    uv run python -m sdv_reference.leagues.ncaa_baseball fetch   # refresh raw/ncaa_baseball/ from baseballr-data
    uv run python -m sdv_reference.build ncaa_baseball            # offline: raw/ + curated/ -> build/ncaa_baseball/

NCAA conf_ids are stable across seasons and sports, but stats.ncaa.org labels are retroactive (the MAAC reads "Metro",
the WAC "UAC", the old Big East "AAC"). So lineage keys on conf_id, and each season's names come from
curated/ncaa_conferences.csv. team_id is the NCAA org_id. Independents get no conference.

The table assembly lives here and ncaa_softball imports it: a `_ncaa.py` helper would be listed as a league by
build.py's pkgutil discovery.
"""

from __future__ import annotations

import gzip
import os
import sys
from pathlib import Path

import polars as pl

from sdv_reference.schema import conform

LEAGUE = "ncaa_baseball"
ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "curated"
RAW = ROOT / "raw" / LEAGUE / "ncaa_team_lookup.csv.gz"
SOURCE = os.environ.get(
    "SDV_NCAA_BASEBALL_LOOKUP",
    "/mnt/sdv_repos/baseballr-data/ncaa/teams_info/ncaa_team_lookup.parquet",
)
# stats.ncaa.org's independent sentinels (02-ncaa.md section 2); 0 spans all three divisions
INDEPENDENT = {0, 26388, 99000, 99001, 99005, 99010, 99020}
DIVISIONS = {
    1: ("Division I", "D-I", "DI"),
    2: ("Division II", "D-II", "DII"),
    3: ("Division III", "D-III", "DIII"),
}


def write_csv_gz(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(
        path, "wb", mtime=0
    ) as f:  # mtime=0: unchanged data, unchanged bytes
        df.write_csv(f)


def fetch() -> None:
    """Snapshot baseballr-data's ncaa_team_lookup ($SDV_NCAA_BASEBALL_LOOKUP: a path or URL) into raw/."""
    df = pl.read_parquet(SOURCE)
    # every Float64 column is an integer id or year
    df = df.with_columns(pl.col(pl.Float64).cast(pl.Int64)).sort(
        "year", "division", "team_name", "season_team_id"
    )
    write_csv_gz(df, RAW)
    print(f"{LEAGUE}: {df.height} rows -> {RAW.relative_to(ROOT)}", flush=True)


def read_lookup() -> pl.DataFrame:
    ints = [
        "team_id",
        "conference_id",
        "division",
        "year",
        "season_id",
        "season_team_id",
    ]
    return pl.read_csv(RAW, schema_overrides={c: pl.Int64 for c in ints})


def conferences() -> pl.DataFrame:
    """curated/ncaa_conferences.csv: one row per lineage slug x conf_id x name era (valid_from/to inclusive)."""
    return pl.read_csv(
        CURATED / "ncaa_conferences.csv",
        schema_overrides={
            "conf_id": pl.Int64,
            "valid_from": pl.Int32,
            "valid_to": pl.Int32,
        },
    )


def conf_slugs() -> dict[int, str]:
    return dict(
        conferences()
        .filter(pl.col("conf_id").is_not_null())
        .select("conf_id", "slug")
        .unique()
        .iter_rows()
    )


def label_slugs() -> dict[str, str | None]:
    """Every stats.ncaa.org label the baseball lookup uses -> lineage slug (None = independent)."""
    slugs, out = conf_slugs(), {}
    for label, cid in (
        read_lookup().select("conference", "conference_id").unique().iter_rows()
    ):
        slug = None if cid in INDEPENDENT else slugs[cid]
        if out.get(label, slug) != slug:
            raise ValueError(f"label {label!r} maps to both {out[label]} and {slug}")
        out[label] = slug
    return out


def add_note(note: pl.Expr) -> pl.Expr:
    """Append `note` (null = nothing) to the running `note` column."""
    return pl.concat_str(
        [pl.col("note"), note], separator="; ", ignore_nulls=True
    ).replace("", None)


def resolve_duplicates(df: pl.DataFrame, *tiebreaks: pl.Expr) -> pl.DataFrame:
    """One row per (team_id, season). stats.ncaa.org lists independents and reclassifying schools under several
    divisions (Merrimack 2019 in both the NE10 and the NEC). Keep a real conference first, then `tiebreaks` (False
    sorts first), then the division the team has in its nearest unambiguous season, then the lowest division."""
    n = pl.len().over("team_id", "season")
    solo = {
        (t, s): d
        for t, s, d in df.filter(n == 1)
        .select("team_id", "season", "division")
        .iter_rows()
    }

    def nearest(t, s):
        for k in range(1, 100):
            for y in (s - k, s + k):
                if (t, y) in solo:
                    return solo[(t, y)]
        return None

    keys = df.filter(n > 1).select("team_id", "season").unique().rows()
    near = pl.DataFrame(
        [(t, s, nearest(t, s)) for t, s in keys],
        schema={
            "team_id": df.schema["team_id"],
            "season": df.schema["season"],
            "near": pl.Int64,
        },
        orient="row",
    )
    return (
        df.join(near, on=["team_id", "season"], how="left")
        .sort(
            [
                pl.col("slug").is_null(),
                *tiebreaks,
                (pl.col("division") == pl.col("near")).not_().fill_null(True),
                pl.col("division"),
            ]
        )
        .with_columns(
            add_note(
                pl.when(n > 1).then(
                    pl.format(
                        "listed in {} divisions; kept D-{}", n, pl.col("division")
                    )
                )
            ).alias("note")
        )
        .unique(["team_id", "season"], keep="first", maintain_order=True)
        .drop("near")
    )


def members() -> pl.DataFrame:
    """One row per (org_id, season): season, team_id, team_name, division, slug, label, note."""
    df = read_lookup().rename(
        {"year": "season", "conference": "label", "conference_id": "conf_id"}
    )
    by_name = (
        df.filter(pl.col("team_id").is_not_null())
        .group_by("team_name")
        .agg(pl.col("team_id").unique())
        .filter(pl.col("team_id").list.len() == 1)
        .select("team_name", pl.col("team_id").list.first().alias("name_id"))
    )
    fills = pl.read_csv(
        CURATED / "ncaa_org_id_fills.csv", schema_overrides={"org_id": pl.Int64}
    ).select("team_name", pl.col("org_id").alias("fill_id"))
    slugs = conf_slugs()
    unknown = set(df["conf_id"].drop_nulls()) - set(slugs) - INDEPENDENT
    if unknown:
        raise ValueError(
            f"{LEAGUE}: conf_ids missing from curated/ncaa_conferences.csv: {sorted(unknown)}"
        )
    df = (
        df.join(by_name, on="team_name", how="left")
        .join(fills, on="team_name", how="left")
        .with_columns(
            pl.concat_str(
                [
                    pl.when(pl.col("team_id").is_null()).then(
                        pl.lit("org_id missing in source, filled from the team name")
                    ),
                    pl.when(pl.col("conf_id").is_in(list(INDEPENDENT))).then(
                        pl.format("independent (NCAA conf_id {})", pl.col("conf_id"))
                    ),
                ],
                separator="; ",
                ignore_nulls=True,
            )
            .replace("", None)
            .alias("note"),
            pl.coalesce("team_id", "name_id", "fill_id").alias("team_id"),
            pl.col("conf_id")
            .replace_strict(slugs, default=None, return_dtype=pl.Utf8)
            .alias("slug"),
        )
    )
    lost = df.filter(pl.col("team_id").is_null())
    if lost.height:
        print(
            f"{LEAGUE}: dropped {lost.height} rows with no org_id: "
            f"{sorted(set(lost['team_name']))}",
            flush=True,
        )
    df = resolve_duplicates(
        df.filter(pl.col("team_id").is_not_null()), pl.col("season_team_id").is_null()
    )
    return df.select(
        "season",
        pl.col("team_id").cast(pl.Utf8),
        "team_name",
        "division",
        "slug",
        "label",
        "note",
    )


def assemble(
    league: str, m: pl.DataFrame, league_name: str, league_note: str
) -> dict[str, pl.DataFrame]:
    """The four contract tables from membership rows (season, team_id, team_name, division, slug, label, note)."""
    p = f"{league}:"
    conf = conferences()
    m = m.with_columns(pl.col("season").cast(pl.Int32))

    tgs = conform(
        "team_group_seasons",
        m.with_columns(
            pl.lit(league).alias("league"),
            pl.lit("ncaa_org").alias("team_id_source"),
            pl.format(p + "d{}", pl.col("division")).alias("subdivision_id"),
            pl.concat_str([pl.lit(p), pl.col("slug")]).alias("conference_id"),
            pl.lit("ncaa").alias("source"),
            pl.col("note").alias("notes"),
        ),
    )

    # conferences: names as of each season from the curated windows; parent = the members' modal division
    cs = (
        m.filter(pl.col("slug").is_not_null())
        .group_by("slug", "season")
        .agg(
            pl.len().alias("n_teams"), pl.col("division").mode().min().alias("division")
        )
    )
    named = cs.join(conf, on="slug", how="left").filter(
        (pl.col("valid_from").fill_null(0) <= pl.col("season"))
        & (pl.col("season") <= pl.col("valid_to").fill_null(9999))
    )
    counts = cs.join(
        named.group_by("slug", "season").len(), on=["slug", "season"], how="left"
    )
    bad = counts.filter(pl.col("len").fill_null(0) != 1)
    if bad.height:
        raise ValueError(
            f"{league}: conference seasons without exactly one curated name: {bad.select('slug', 'season').rows()[:10]}"
        )
    conf_gs = named.select(
        pl.concat_str([pl.lit(p), pl.col("slug")]).alias("group_id"),
        "season",
        pl.lit("conference").alias("level"),
        "name",
        "short_name",
        "abbreviation",
        pl.format(p + "d{}", pl.col("division")).alias("parent_group_id"),
        "n_teams",
    )
    sub_gs = (
        m.group_by("division", "season")
        .agg(pl.len().alias("n_teams"))
        .with_columns(
            pl.format(p + "d{}", pl.col("division")).alias("group_id"),
            pl.lit("subdivision").alias("level"),
            *(
                pl.col("division")
                .replace_strict(
                    {k: v[i] for k, v in DIVISIONS.items()}, return_dtype=pl.Utf8
                )
                .alias(c)
                for i, c in enumerate(["name", "short_name", "abbreviation"])
            ),
            pl.lit(p + "ncaa").alias("parent_group_id"),
        )
    )
    league_gs = (
        m.group_by("season")
        .agg(pl.len().alias("n_teams"))
        .with_columns(
            pl.lit(p + "ncaa").alias("group_id"),
            pl.lit("league").alias("level"),
            pl.lit(league_name).alias("name"),
            pl.lit("NCAA").alias("short_name"),
            pl.lit("NCAA").alias("abbreviation"),
        )
    )
    gs = pl.concat(
        [
            conform("group_seasons", d.with_columns(pl.lit(league).alias("league")))
            for d in (league_gs, sub_gs, conf_gs)
        ]
    )

    conf_notes = {
        p + slug: "; ".join(
            ["NCAA conf_id " + "/".join(map(str, ids)) if ids else "no NCAA conf_id"] + cites
        )
        for slug, ids, cites in conf.group_by("slug", maintain_order=True)
        .agg(
            pl.col("conf_id").drop_nulls().unique(maintain_order=True),
            pl.col("citation").drop_nulls(),
        )
        .iter_rows()
    }
    notes = {
        p + "ncaa": league_note,
        **{
            p + f"d{k}": f"NCAA {v[0]} as stats.ncaa.org lists each school that season"
            for k, v in DIVISIONS.items()
        },
        **conf_notes,
    }
    groups = conform(
        "groups",
        gs.group_by("group_id", "level")
        .agg(
            pl.col("season").min().alias("first_season"),
            pl.col("season").max().alias("last_season"),
        )
        .with_columns(
            pl.lit(league).alias("league"),
            pl.col("group_id")
            .replace_strict(notes, default=None, return_dtype=pl.Utf8)
            .alias("notes"),
        ),
    )

    # aliases: SDV slugs and curated names, NCAA labels (with conf_id) as each league's source uses them, NCAA.com slugs
    present = set(groups["group_id"])
    conf_rows = conf.with_columns(
        pl.concat_str([pl.lit(p), pl.col("slug")]).alias("group_id")
    ).filter(pl.col("group_id").is_in(present))
    labels = (
        m.filter(pl.col("slug").is_not_null())
        .join(named.select("slug", "season", "conf_id"), on=["slug", "season"])
        .group_by("slug", "label", "conf_id")
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
        )
        .select(
            pl.concat_str([pl.lit(p), pl.col("slug")]).alias("group_id"),
            pl.lit("ncaa").alias("source"),
            pl.col("conf_id").cast(pl.Utf8).alias("source_id"),
            pl.lit("short_name").alias("name_kind"),
            pl.col("label").alias("value"),
            "valid_from",
            "valid_to",
        )
    )
    curated_names = pl.concat(
        [
            conf_rows.select(
                "group_id",
                pl.lit("sdv").alias("source"),
                pl.lit(None, pl.Utf8).alias("source_id"),
                pl.lit(kind).alias("name_kind"),
                pl.col(kind).alias("value"),
                "valid_from",
                "valid_to",
            )
            for kind in ("name", "short_name", "abbreviation")
        ]
    )
    ncaa_com = conf_rows.filter(pl.col("ncaa_com_slug").is_not_null()).select(
        "group_id",
        pl.lit("ncaa").alias("source"),
        pl.col("conf_id").cast(pl.Utf8).alias("source_id"),
        pl.lit("slug").alias("name_kind"),
        pl.col("ncaa_com_slug").alias("value"),
    )
    sdv_slugs = pl.DataFrame({"group_id": sorted(present)}).select(
        "group_id",
        pl.lit("sdv").alias("source"),
        pl.lit("slug").alias("name_kind"),
        pl.col("group_id").str.strip_prefix(p).alias("value"),
    )
    div_labels = pl.DataFrame(
        [
            (p + f"d{k}", kind, v[i])
            for k, v in DIVISIONS.items()
            for i, kind in enumerate(["name", "short_name"])
        ],
        schema=["group_id", "name_kind", "value"],
        orient="row",
    ).with_columns(pl.lit("ncaa").alias("source"))
    ga = pl.concat(
        [
            conform("group_aliases", d.with_columns(pl.lit(league).alias("league")))
            for d in (sdv_slugs, curated_names, labels, ncaa_com, div_labels)
        ]
    )
    ga = ga.filter(
        pl.col("value").is_not_null() & pl.col("group_id").is_in(present)
    ).unique(maintain_order=True)
    return {
        "groups": groups,
        "group_seasons": gs,
        "group_aliases": ga,
        "team_group_seasons": tgs,
    }


def build() -> dict[str, pl.DataFrame]:
    return assemble(
        LEAGUE,
        members(),
        "NCAA Baseball",
        "stats.ncaa.org team lookup (baseballr-data ncaa_team_lookup, 2010+), D-I/II/III. Labels there are "
        "scrape-vintage, so names come from curated/ncaa_conferences.csv; independents have no conference.",
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
