"""NCAA <-> ESPN team crosswalk for D-I college baseball, keyed on the NCAA school (tag ncaa_baseball_crosswalk).

    uv run python -c "from sdv_reference.build import module; module('ncaa_baseball_crosswalk').fetch()"  # 1 ESPN call
    uv run python -m sdv_reference.build ncaa_baseball_crosswalk   # offline: raw/ + curated/ -> build/

ESPN's college-baseball team ids are their own id system, not the football/basketball school ids (Colgate is 333 in
baseball, Alabama 148), so this is a separate map from {mbb,wbb}_crosswalk (ClaudeCowork plans/
2026-10-11-provider-crosswalk-jobs.md, J8).

Inputs:
- raw/ncaa_baseball/ncaa_team_lookup.csv.gz: the stats.ncaa.org team-seasons (the ncaa_baseball league's snapshot of
  baseballr-data's ncaa_team_lookup); `team_id` there is the NCAA org id. Division I only;
- raw/ncaa_baseball_crosswalk/espn_teams.csv.gz: ESPN's college-baseball team list (site API, one request);
- curated/ncaa_baseball_espn_aliases.csv: hand-verified NCAA name -> ESPN baseball id;
- the basketball school map (_ncaa_espn.build(), offline), used only as a NAME hint: the school's ESPN basketball
  location/display name looked up in the baseball list, never its id.

Match order per NCAA name (a candidate counts only when it resolves to exactly one ESPN id): `alias`, `exact`
(normalized name), `school_hint` (the org's ESPN basketball names). No fuzzy matching. Then the school key: every org
resolves to one ESPN id across seasons, and a row its name left unresolved takes its org's id (`org`).
"""

from __future__ import annotations

import gzip
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from sdv_reference.crosswalks._ncaa_espn import (
    CURATED,
    RAW,
    _csv,
    _espn_index,
    _school_key,
    normalize,
)

OUT = RAW / "ncaa_baseball_crosswalk"
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/teams?limit=1000"
ESPN_COLUMNS = [
    "team_id",
    "location",
    "display_name",
    "short_name",
    "mascot",
    "nickname",
    "abbreviation",
]
COLUMNS = [
    "season", "ncaa_team_id", "ncaa_season_team_id", "ncaa_team", "ncaa_conference", "conference_id",
    "espn_team_id", "espn_display_name", "espn_location", "espn_mascot", "espn_abbreviation", "match_method",
]  # fmt: skip


def fetch() -> None:
    """Snapshot ESPN's college-baseball team list into raw/ncaa_baseball_crosswalk/espn_teams.csv.gz (one live call,
    through $SDV_API_PROXY). Unchanged data rewrites nothing, provenance included."""
    from sdv_reference.espn import get_json

    payload = get_json(ESPN_TEAMS)
    teams = [t["team"] for t in payload["sports"][0]["leagues"][0]["teams"]]
    df = pl.DataFrame(
        [{"team_id": str(t["id"]), "location": t.get("location"), "display_name": t.get("displayName"),
          "short_name": t.get("shortDisplayName"), "mascot": t.get("name"), "nickname": t.get("nickname"),
          "abbreviation": t.get("abbreviation")} for t in teams],
        schema={c: pl.Utf8 for c in ESPN_COLUMNS},
    ).sort(pl.col("team_id").cast(pl.Int64))  # fmt: skip
    assert df["team_id"].is_unique().all() and df.height > 300, (
        f"unexpected ESPN team list: {df.height} rows"
    )
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        df.write_csv(fh)
    path = OUT / "espn_teams.csv.gz"
    if path.exists() and path.read_bytes() == buf.getvalue():
        print(
            f"ncaa_baseball_crosswalk: ESPN team list unchanged ({df.height} teams)",
            flush=True,
        )
        return
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (OUT / "provenance.json").write_text(
        json.dumps({"espn_teams.csv.gz": f"{ESPN_TEAMS} {stamp}"}, indent=1) + "\n"
    )
    print(f"ncaa_baseball_crosswalk: {df.height} ESPN teams", flush=True)


def _ncaa_teams() -> pl.DataFrame:
    """D-I team-seasons, ids Int64 (as in {mbb,wbb}_crosswalk). The ncaa_baseball league's members() fills the org
    ids stats.ncaa.org leaves blank for a school in transition (Mercyhurst, West Ga., New Haven 2025-26)."""
    from sdv_reference.leagues.ncaa_baseball import members, read_lookup

    season_ids = read_lookup().filter(pl.col("division") == 1).select(
        pl.col("year").alias("season"), pl.col("team_name").alias("ncaa_team"),
        pl.col("season_team_id").alias("ncaa_season_team_id"),
    )  # fmt: skip
    return (
        members().filter(pl.col("division") == 1)
        .select(
            "season", pl.col("team_id").cast(pl.Int64).alias("ncaa_team_id"), pl.col("team_name").alias("ncaa_team"),
            pl.col("label").alias("ncaa_conference"),
            pl.when(pl.col("slug").is_not_null()).then(pl.format("ncaa_baseball:{}", "slug")).alias("conference_id"),
        )
        .join(season_ids, on=["season", "ncaa_team"], how="left")
    )  # fmt: skip


def _school_hints() -> dict[int, list[str]]:
    """NCAA org id -> the school's ESPN basketball names (location, display name), from the basketball map."""
    from sdv_reference.crosswalks import _ncaa_espn

    schools = _ncaa_espn.build()[1].drop_nulls("espn_team_id")
    bb = _csv(CURATED / "ncaa_espn_teams.csv").select(
        pl.col("team_id").alias("espn_team_id"), "location", "display_name"
    )
    rows = schools.join(bb, on="espn_team_id", how="left").select(
        "ncaa_org_id", "location", "display_name"
    )
    return {org: [n for n in names if n] for org, *names in rows.iter_rows()}


def build(hints: dict[int, list[str]] | None = None) -> pl.DataFrame:
    espn = _csv(OUT / "espn_teams.csv.gz")
    index = _espn_index(espn)
    alias = dict(
        _csv(CURATED / "ncaa_baseball_espn_aliases.csv")
        .select("ncaa_team", "espn_team_id")
        .iter_rows()
    )
    hints = _school_hints() if hints is None else hints

    def one(keys) -> set[str]:
        return set().union(*(index.get(normalize(k), set()) for k in keys))

    def resolve(team: str, org: int) -> tuple[str | None, str]:
        if team in alias:
            return alias[team], "alias"
        hit = one([team])
        if len(hit) == 1:
            return next(iter(hit)), "exact"
        hint = one(hints.get(org, []))
        if len(hint) == 1:
            return next(iter(hint)), "school_hint"
        return None, "ambiguous" if (hit or hint) else "unmatched"

    teams = _ncaa_teams()
    pairs = teams.select("ncaa_team", "ncaa_team_id").unique()
    resolved = pl.DataFrame(
        [(t, o, *resolve(t, o)) for t, o in pairs.iter_rows()],
        schema={
            "ncaa_team": pl.Utf8,
            "ncaa_team_id": pl.Int64,
            "espn_team_id": pl.Utf8,
            "match_method": pl.Utf8,
        },
        orient="row",
    )
    matches = teams.join(resolved, on=["ncaa_team", "ncaa_team_id"], how="left")
    key = _school_key(matches.rename({"ncaa_team_id": "ncaa_org_id"})).rename(
        {"ncaa_org_id": "ncaa_team_id"}
    )
    assert matches.schema["ncaa_team_id"] == key.schema["ncaa_team_id"]
    return (
        matches.join(key, on="ncaa_team_id", how="left")
        .with_columns(
            pl.when(pl.col("espn_team_id").is_null() & pl.col("org_espn_team_id").is_not_null())
            .then(pl.lit("org")).otherwise(pl.col("match_method")).alias("match_method"),
            pl.coalesce("espn_team_id", "org_espn_team_id").alias("espn_team_id"),
        )
        .join(espn.select(
            pl.col("team_id").alias("espn_team_id"), pl.col("display_name").alias("espn_display_name"),
            pl.col("location").alias("espn_location"), pl.col("mascot").alias("espn_mascot"),
            pl.col("abbreviation").alias("espn_abbreviation"),
        ), on="espn_team_id", how="left")
        .select(COLUMNS)
        .sort("season", "ncaa_team")
    )  # fmt: skip


def validate(crosswalk: pl.DataFrame) -> list[str]:
    """Contract problems (CONTRACT.md); [] when it passes."""
    problems = []
    if crosswalk.height != _ncaa_teams().height:
        problems.append(
            f"{crosswalk.height} rows for {_ncaa_teams().height} NCAA D-I team-seasons"
        )
    if crosswalk.select("season", "ncaa_team_id").is_duplicated().any():
        problems.append("duplicate (season, ncaa_team_id)")
    clash = (
        crosswalk.drop_nulls("espn_team_id")
        .group_by("season", "espn_team_id")
        .len()
        .filter(pl.col("len") > 1)
    )
    if clash.height:
        problems.append(
            f"an ESPN id on two NCAA teams in one season: {clash.head(5).to_dicts()}"
        )
    bad = crosswalk.filter(
        pl.col("espn_team_id").is_null()
        != pl.col("match_method").is_in(["unmatched", "ambiguous"])
    )
    if bad.height:
        problems.append(
            f"match_method disagrees with espn_team_id: {bad.head(5).to_dicts()}"
        )
    ghost = crosswalk.filter(
        pl.col("espn_team_id").is_not_null() & pl.col("espn_display_name").is_null()
    )
    if ghost.height:
        problems.append(
            f"an ESPN id not in ESPN's team list: {ghost.head(5).to_dicts()}"
        )
    return problems


def write(out_dir: Path) -> list[Path]:
    """Build, validate and write build/ncaa_baseball_crosswalk/: the team-season crosswalk, the school table (one row
    per NCAA org: latest name and its ESPN id), and the unmatched schools for triage."""
    df = build()
    problems = validate(df)
    if problems:
        raise ValueError(
            "ncaa_baseball_crosswalk fails the contract:\n  " + "\n  ".join(problems)
        )
    schools = (
        df.sort("season").group_by("ncaa_team_id")
        .agg(pl.col("ncaa_team").last(), pl.col("season").min().alias("first_season"),
             pl.col("season").max().alias("last_season"), pl.col("espn_team_id").drop_nulls().last(),
             pl.col("espn_display_name").drop_nulls().last())
        .sort("ncaa_team_id")
    )  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name, table in (
        ("ncaa_baseball_espn_team_crosswalk", df),
        ("ncaa_baseball_espn_schools", schools),
    ):
        for ext in ("parquet", "csv"):
            p = out_dir / f"{name}.{ext}"
            table.write_parquet(p) if ext == "parquet" else table.write_csv(p)
            files.append(p)
    unmatched = schools.filter(pl.col("espn_team_id").is_null())
    p = out_dir / "ncaa_baseball_espn_unmatched.csv"
    unmatched.select(
        "ncaa_team_id", "ncaa_team", "first_season", "last_season"
    ).write_csv(p)
    files.append(p)
    methods = dict(df["match_method"].value_counts().iter_rows())
    print(
        f"ncaa_baseball_crosswalk: {df.height} D-I team-seasons ({df['season'].min()}..{df['season'].max()}), "
        f"{df['espn_team_id'].is_not_null().sum()} matched {methods}; {schools.height} schools, "
        f"{unmatched.height} unmatched",
        flush=True,
    )
    return files
