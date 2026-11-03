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

The source ends at 2025. `roll_forward` adds the next season while the stats.ncaa.org baseball lookup has it (2026):
see its docstring. From 2026, ESPN's core college-softball groups (raw/ncaa_softball/espn/{season}.json.gz, ten D-I
conferences) cross-check the ncaa_org rows (`sources_agree`), mapped to org ids through ncaa_softball_crosswalk.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import sys

import polars as pl

from sdv_reference.leagues import ncaa_baseball
from sdv_reference.leagues.nba import espn_frames, fetch_espn
from sdv_reference.leagues.ncaa_baseball import (
    CURATED,
    ROOT,
    add_note,
    assemble,
    label_slugs,
    resolve_duplicates,
    with_espn_rows,
)
from sdv_reference.refresh import current_season

LEAGUE = "ncaa_softball"
RAW = ROOT / "raw" / LEAGUE / "ncaa_team_info.csv.gz"
SOURCE = os.environ.get(
    "SDV_NCAA_SOFTBALL_TEAM_INFO",
    "/mnt/sdv_repos/softballR-data/data/ncaa_team_info.RDS",
)
FIRST_SEASON = 1982
ESPN_FIRST_SEASON = 2026  # the cross-check's first season (ESPN core college-softball groups, probed 2026-11-02)
# ESPN core college-softball conference ids -> SDV slugs: the 2026 tree lists only these ten D-I conferences
ESPN_GROUPS = {
    "59": "american",
    "40": "acc",
    "65": "asun",
    "49": "big-ten",
    "45": "big-12",
    "60": "cusa",
    "64": "mountain-west",
    "32": "sec",
    "62": "sun-belt",
    "67": "wac",
}


def fetch() -> None:
    """Snapshot softballR-data's ncaa_team_info.RDS ($SDV_NCAA_SOFTBALL_TEAM_INFO) into raw/ as csv, via base R,
    then ESPN's core groups (network) into raw/ncaa_softball/espn/."""
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
    fetch_espn(LEAGUE, range(ESPN_FIRST_SEASON, current_season(False) + 1))


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


def roll_forward(m: pl.DataFrame) -> pl.DataFrame:
    """`m` plus the season after its last while the stats.ncaa.org baseball lookup already has that season (the
    softball source, a Feb 2025 scrape, ends at 2025; baseball has 2026). Each school of the last season comes along:
    a school whose softball and baseball shared a conference and division that last season takes baseball's new ones
    (NCAA conf_ids are cross-sport, so all-sports moves show up there); any other school (no baseball, or a
    sport-specific affiliation) keeps its last-season conference. Then curated/ncaa_softball_rollforward.csv, cited
    rows: a set division replaces or adds the school's row, an empty one drops it. Schools that start softball are not
    seen. Every derived row says which way it was made in `note`."""
    last = m["season"].max()
    nxt = last + 1
    bb = ncaa_baseball.members()
    if nxt not in set(bb["season"]):
        return m
    assert m.schema["team_id"] == bb.schema["team_id"], (m.schema["team_id"], bb.schema["team_id"])
    b_last = bb.filter(pl.col("season") == last).select("team_id", b_div="division", b_slug="slug")
    b_next = bb.filter(pl.col("season") == nxt).select(
        "team_id", n_div="division", n_slug="slug", n_label="label"
    )
    follow = (
        pl.col("slug").eq_missing(pl.col("b_slug"))
        & (pl.col("division") == pl.col("b_div"))
        & pl.col("n_div").is_not_null()
    )
    moved = follow & (~pl.col("n_slug").eq_missing(pl.col("slug")) | (pl.col("n_div") != pl.col("division")))
    rolled = (
        m.filter(pl.col("season") == last)
        .join(b_last, on="team_id", how="left")
        .join(b_next, on="team_id", how="left")
        .select(
            pl.lit(nxt, pl.Int32).alias("season"),
            "team_id",
            "team_name",
            pl.when(follow).then("n_div").otherwise("division").alias("division"),
            pl.when(follow).then("n_slug").otherwise("slug").alias("slug"),
            pl.when(moved).then("n_label").otherwise("label").alias("label"),
            pl.concat_str(
                [
                    "note",
                    pl.when(follow)
                    .then(pl.lit(f"{nxt} not in source: conference and division from stats.ncaa.org baseball {nxt} "
                                 f"(same in both sports in {last})"))
                    .otherwise(pl.lit(f"{nxt} not in source: carried from {last}")),
                ],
                separator="; ",
                ignore_nulls=True,
            ).alias("note"),
        )
    )  # fmt: skip
    cur = pl.read_csv(
        CURATED / "ncaa_softball_rollforward.csv",
        schema_overrides={"season": pl.Int32, "team_id": pl.Utf8, "division": pl.Int64},
    ).filter(pl.col("season") == nxt)
    rolled = pl.concat(
        [
            rolled.join(cur.select("team_id"), on="team_id", how="anti"),
            cur.filter(pl.col("division").is_not_null()).select(
                "season", "team_id", "team_name", "division", "slug",
                pl.lit(None, pl.Utf8).alias("label"),
                pl.format(f"{nxt} not in source: curated/ncaa_softball_rollforward.csv ({{}})", "citation").alias("note"),
            ),
        ],
        how="vertical_relaxed",
    )  # fmt: skip
    return pl.concat([m, rolled.select(m.columns)], how="vertical_relaxed")


def espn_check(tgs: pl.DataFrame, crosswalk: pl.DataFrame) -> pl.DataFrame:
    """Set `sources_agree` on the ncaa_org rows against ESPN's core groups, mapped to org ids through the crosswalk:
    null outside ESPN's seasons, and for a team ESPN doesn't list in a conference ESPN doesn't carry. ESPN's lists also
    hold two teams with no softball program behind them (Vanderbilt 532 in the SEC, a "Louisville KY" 1080 in the ACC
    beside Louisville's own id): no school maps to those ids, so they drop out."""
    _, members, _ = espn_frames(LEAGUE)
    schools = (
        crosswalk.drop_nulls("espn_team_id")
        .select(pl.col("ncaa_team_id").cast(pl.Utf8).alias("team_id"), "espn_team_id")
        .unique()
    )
    assert members.schema["espn_team_id"] == schools.schema["espn_team_id"]
    espn = (
        members.join(schools, on="espn_team_id")
        .group_by("season", "team_id")
        .agg(pl.col("espn_group_id").replace_strict(ESPN_GROUPS).unique().sort().str.join("|").alias("espn_slug"))
        .with_columns(pl.format(f"{LEAGUE}:{{}}", "espn_slug").alias("espn_conf"))
        .drop("espn_slug")
    )  # fmt: skip
    seasons = sorted(members["season"].unique())
    confs = [f"{LEAGUE}:{s}" for s in ESPN_GROUPS.values()]
    assert tgs.schema["team_id"] == espn.schema["team_id"]
    covered = (
        (pl.col("team_id_source") == "ncaa_org")
        & pl.col("season").is_in(seasons)
        & (pl.col("espn_conf").is_not_null() | pl.col("conference_id").is_in(confs))
    )
    return (
        tgs.join(espn, on=["season", "team_id"], how="left")
        .with_columns(
            pl.when(covered).then(pl.col("conference_id").eq_missing(pl.col("espn_conf"))).alias("sources_agree"),
            pl.concat_str(
                [
                    "notes",
                    pl.when(covered & pl.col("espn_conf").is_null())
                    .then(pl.lit("not in ESPN's group lists"))
                    .when(covered & ~pl.col("conference_id").eq_missing(pl.col("espn_conf")))
                    .then(pl.format("ESPN: {}", "espn_conf")),
                ],
                separator="; ",
                ignore_nulls=True,
            ).replace("", None).alias("notes"),
        )
        .select(tgs.columns)
    )  # fmt: skip


def build() -> dict[str, pl.DataFrame]:
    from sdv_reference.crosswalks import ncaa_softball as crosswalk

    cw = crosswalk.build()
    tables = assemble(
        LEAGUE,
        roll_forward(members()),
        "NCAA Softball",
        "stats.ncaa.org team history pages (softballR-data ncaa_team_info.RDS, Feb 2025 scrape), 1982-2025, "
        "D-I/II/III. Conference labels there are retroactive and carry no conf_id: mapped through the baseball "
        "lookup's label/conf_id pairs plus curated/ncaa_softball_labels.csv; names come from "
        "curated/ncaa_conferences.csv. Conference labels are sparse before about 1997 (D-I) and 2002 (D-II/III). "
        "2026 is derived, not scraped: each 2025 school, moved with its stats.ncaa.org baseball 2026 conference where "
        "the two sports shared one in 2025, else carried, plus curated/ncaa_softball_rollforward.csv (each row's "
        "notes say which); new 2026 programs are missing. From 2026, sources_agree compares the D-I rows with ESPN's "
        "core college-softball groups (ten conferences). team_group_seasons repeats each school "
        "ncaa_softball_crosswalk maps under its ESPN college-softball id (team_id_source espn).",
    )
    tgs = espn_check(tables["team_group_seasons"], cw)
    return with_espn_rows({**tables, "team_group_seasons": tgs}, cw)

if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
