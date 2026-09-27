"""NCAA softball groups reference: stats.ncaa.org team history pages, via softballR-data's ncaa_team_info.RDS.

    uv run python -m sdv_reference.leagues.ncaa_softball fetch   # refresh raw/ncaa_softball/ (needs Rscript)
    uv run python -m sdv_reference.build ncaa_softball            # offline: raw/ + curated/ -> build/ncaa_softball/

The source has conference labels (the Feb 2025 scrape's current names, applied to every season) but no conf_id.
Labels map to lineages through the baseball lookup's label -> conf_id pairs (NCAA conf_ids are cross-sport), then
curated/ncaa_softball_labels.csv for softball-only labels. Cleaning rules:
- a blank season is the history page's career-totals row: dropped;
- rows identical but for team_name collapse to one (org 30083 is on the team_codes page twice, as
  Cobleskill St. and SUNY Cobleskill);
- season "1900" is 1999-00 misparsed by an older get_ncaa_team_info.R (Alabama 66-14 is its 2000 season): -> 2000;
- seasons before 1982 are dropped: the NCAA's first softball championships were 1982, and earlier rows are
  AIAW-era with retroactive NCAA divisions;
- a blank division is kept only when the conference label maps (reclassifying members), taking the conference's
  division; otherwise the row is not an NCAA member season and is dropped.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import sys

import polars as pl

from sdv_reference.leagues.ncaa_baseball import (
    CURATED,
    ROOT,
    add_note,
    assemble,
    label_slugs,
    resolve_duplicates,
)

LEAGUE = "ncaa_softball"
RAW = ROOT / "raw" / LEAGUE / "ncaa_team_info.csv.gz"
SOURCE = os.environ.get(
    "SDV_NCAA_SOFTBALL_TEAM_INFO",
    "/mnt/sdv_repos/softballR-data/data/ncaa_team_info.RDS",
)
FIRST_SEASON = 1982


def fetch() -> None:
    """Snapshot softballR-data's ncaa_team_info.RDS ($SDV_NCAA_SOFTBALL_TEAM_INFO) into raw/ as csv, via base R."""
    csv = subprocess.run(
        [
            "Rscript",
            "-e",
            "write.csv(readRDS(commandArgs(TRUE)[1]), stdout(), row.names = FALSE)",
            SOURCE,
        ],
        env={**os.environ, "R_ENVIRON_USER": "/dev/null"},
        capture_output=True,
        check=True,
    ).stdout
    RAW.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(RAW, "wb", mtime=0) as f:
        f.write(csv)
    print(
        f"{LEAGUE}: {len(csv.splitlines()) - 1} rows -> {RAW.relative_to(ROOT)}", flush=True
    )


def members() -> pl.DataFrame:
    """One row per (org_id, season): season, team_id, team_name, division, slug, label, note."""
    labels = label_slugs() | dict(
        pl.read_csv(CURATED / "ncaa_softball_labels.csv")
        .select("label", "slug")
        .iter_rows()
    )
    label = pl.col("label")
    raw = pl.read_csv(RAW, infer_schema=False)
    df = (
        raw.sort("team_name")
        .unique([c for c in raw.columns if c != "team_name"], keep="first", maintain_order=True)
        .rename({"conference": "label"})
        .with_columns(
            pl.col("season").replace("1900", "2000").cast(pl.Int32, strict=False),
            pl.col("division").replace_strict(
                {"D-I": 1, "D-II": 2, "D-III": 3}, default=None, return_dtype=pl.Int64
            ),
            label.replace_strict(labels, default=None, return_dtype=pl.Utf8).alias(
                "slug"
            ),
            pl.when(label.is_null() | label.is_in(["-", "NA"]))
            .then(pl.lit("no conference in source"))
            .when(label.is_in(list(labels)))
            .then(
                pl.when(label.replace_strict(labels, default=None).is_null()).then(
                    pl.lit("independent")
                )
            )
            .when(label == pl.col("team_name"))
            .then(
                pl.lit("independent: the conference cell holds the school's own name")
            )
            .otherwise(pl.format("unmatched conference label {}", label))
            .alias("note"),
        )
        .filter(pl.col("season") >= FIRST_SEASON)
    )
    conf_div = (
        df.filter(pl.col("division").is_not_null() & pl.col("slug").is_not_null())
        .group_by("slug", "season")
        .agg(pl.col("division").mode().min().alias("conf_div"))
    )
    df = (
        df.join(conf_div, on=["slug", "season"], how="left")
        .with_columns(
            add_note(
                pl.when(
                    pl.col("division").is_null() & pl.col("conf_div").is_not_null()
                ).then(
                    pl.lit("division blank in source; subdivision from its conference")
                )
            ).alias("note"),
            pl.coalesce("division", "conf_div").alias("division"),
        )
        .filter(pl.col("division").is_not_null())
    )
    unmatched = df.filter(pl.col("note").str.contains("unmatched"))
    if unmatched.height:
        print(
            f"{LEAGUE}: {unmatched.height} rows with unmatched labels: {sorted(set(unmatched['label']))}",
            flush=True,
        )
    return resolve_duplicates(df).select(
        "season", "team_id", "team_name", "division", "slug", "label", "note"
    )


def build() -> dict[str, pl.DataFrame]:
    return assemble(
        LEAGUE,
        members(),
        "NCAA Softball",
        "stats.ncaa.org team history pages (softballR-data ncaa_team_info.RDS, Feb 2025 scrape), 1982+, D-I/II/III. "
        "Conference labels there are retroactive and carry no conf_id: mapped through the baseball lookup's "
        "label/conf_id pairs plus curated/ncaa_softball_labels.csv; names come from curated/ncaa_conferences.csv. "
        "Conference labels are sparse before about 1997 (D-I) and 2002 (D-II/III).",
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
