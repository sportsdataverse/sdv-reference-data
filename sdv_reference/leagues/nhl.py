"""NHL groups reference: api-web.nhle.com season-end standings (membership, names), NHL stats REST (team ids),
cross-checked with ESPN.

uv run python -m sdv_reference.leagues.nhl fetch   # network: refresh raw/nhl/ snapshots
uv run python -m sdv_reference.build nhl            # offline: raw/nhl/ + curated/ -> build/nhl/

Season key: the ENDING year (NHL id 20242025 -> 2025). The NHL publishes no numeric conference or division ids,
so every group id is minted here; curated/nhl_groups.csv maps the API's (level, name, abbreviation) to them.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import polars as pl
import requests

from sdv_reference.leagues.mlb import (
    UA,
    assemble,
    espn_aliases,
    espn_check,
    espn_walk,
    read_json_gz,
    write_json_gz,
)
from sdv_reference.refresh import seasons_to_fetch

LEAGUE = "nhl"
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw" / LEAGUE
CURATED = ROOT / "curated"
WEB = "https://api-web.nhle.com/v1"
REST = "https://api.nhle.com/stats/rest/en"
STANDINGS_KEEP = (
    "seasonId",
    "teamAbbrev",
    "teamName",
    "teamCommonName",
    "placeName",
    "conferenceAbbrev",
    "conferenceName",
    "divisionAbbrev",
    "divisionName",
)  # no gamesPlayed or standing order: a weekly in-season refresh should diff only when membership changes


def _get(url: str, session: requests.Session) -> dict:
    for attempt in range(4):
        r = session.get(url, headers=UA, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(1.0)
        return r.json()
    r.raise_for_status()
    return r.json()


REST_PAGE = 100  # the stats REST API caps a page at 100 rows and ignores limit=-1 (seen 2026-11-25)


def _get_rest_all(path: str, query: str, sort: list[str], session: requests.Session) -> list[dict]:
    """Every row of a stats REST listing, paged REST_PAGE at a time in a stable sort order.

    Raises if the rows gathered don't add up to the API's own `total`, so a truncated listing never lands in raw/."""
    order = urllib.parse.quote(json.dumps([{"property": p, "direction": "ASC"} for p in sort]))
    rows: list[dict] = []
    while True:
        page = _get(f"{REST}/{path}?{query}&sort={order}&limit={REST_PAGE}&start={len(rows)}", session)
        rows += page["data"]
        if not page["data"] or len(rows) >= page["total"]:
            break
    if len(rows) != page["total"]:
        raise ValueError(f"nhl: stats {path} returned {len(rows)} rows, the API reports {page['total']}")
    return rows


def fetch() -> None:
    """Snapshot raw/nhl/: season list, standings per season, stats team-season ids, then ESPN's walk.

    Standings are requested only for refresh.seasons_to_fetch() seasons, as of the season's end or today if it is
    still running; a season with no standings yet (before opening night) stays absent. The season list and the two
    stats listings cover every season, so they stay full; the stats listings are paged and checked against the
    API's row total, and all three are fetched before any is written."""
    s = requests.Session()
    today = dt.datetime.now(dt.UTC).date().isoformat()
    seasons = [
        x
        for x in _get(f"{WEB}/standings-season", s)["seasons"]
        if x["standingsStart"] <= today
    ]
    path = RAW / "standings.json.gz"
    old = read_json_gz(path)["standings"] if path.exists() else {}

    def keep(rows: list[dict]) -> list[dict]:
        return sorted(({k: r.get(k) for k in STANDINGS_KEEP} for r in rows), key=lambda r: r["teamAbbrev"]["default"])

    standings = {k: keep(v) for k, v in old.items()}
    todo = seasons_to_fetch([x["id"] % 10000 for x in seasons], [int(k) % 10000 for k in old])
    for x in seasons:
        if x["id"] % 10000 not in todo:
            continue
        rows = _get(f"{WEB}/standings/{min(x['standingsEnd'], today)}", s)["standings"]
        if rows:
            standings[str(x["id"])] = keep(rows)
        print(f"standings {x['id']}: {len(rows)} teams", flush=True)
    summary = _get_rest_all("team/summary", "isAggregate=false&isGame=false&cayenneExp=gameTypeId=2", ["seasonId", "teamId"], s)
    teams = _get_rest_all("team", "", ["id"], s)
    write_json_gz(path, {"seasons": seasons, "standings": standings})
    write_json_gz(
        RAW / "stats_team_seasons.json.gz",
        sorted(
            ({k: r[k] for k in ("seasonId", "teamId", "teamFullName")} for r in summary),
            key=lambda r: (r["seasonId"], r["teamId"]),
        ),
    )
    write_json_gz(RAW / "stats_teams.json.gz", sorted(teams, key=lambda t: t["id"]))
    last = max(int(k) for k in standings) % 10000
    espn_walk(LEAGUE, list(range(1918, last + 1)), RAW / "espn_groups.json.gz")


def _curated() -> pl.DataFrame:
    return pl.read_csv(CURATED / "nhl_groups.csv", infer_schema_length=0).with_columns(
        pl.col("valid_from", "valid_to").cast(pl.Int32)
    )


def _team_seasons(eras: pl.DataFrame) -> pl.DataFrame:
    """One row per team per season from the season-end standings, with NHL team ids and SDV group ids."""
    standings = read_json_gz(RAW / "standings.json.gz")["standings"]
    ids = {
        (r["seasonId"], r["teamFullName"]): r["teamId"]
        for r in read_json_gz(RAW / "stats_team_seasons.json.gz")
    }
    missing = sorted(
        (int(sid), t["teamName"]["default"])
        for sid, teams in standings.items()
        for t in teams
        if (int(sid), t["teamName"]["default"]) not in ids
    )
    if missing:
        raise ValueError(
            f"nhl: {len(missing)} standings team-seasons have no row among the {len(ids)} in "
            f"raw/nhl/stats_team_seasons.json.gz (a truncated stats fetch?), e.g. {missing[:3]}"
        )
    rows = []
    for sid, teams in standings.items():
        for t in teams:
            name = t["teamName"]["default"]
            rows.append(
                {
                    "season": int(sid) % 10000,
                    "nhl_team_id": str(ids[(int(sid), name)]),
                    "team_name": re.sub(
                        r" \(\d{4}\)$", "", name
                    ),  # "Ottawa Senators (1917)" is today's disambiguator
                    "conf_name": t["conferenceName"],
                    "conf_abbrev": t["conferenceAbbrev"],
                    "div_name": t["divisionName"],
                    "div_abbrev": t["divisionAbbrev"],
                }
            )
    df = pl.DataFrame(rows, infer_schema_length=None).with_columns(pl.col("season").cast(pl.Int32))
    for level, col in (("conference", "conf"), ("division", "div")):
        key = eras.filter(pl.col("level") == level).select(
            pl.col("api_name").alias(f"{col}_name"),
            pl.col("api_abbrev").alias(f"{col}_abbrev"),
            "valid_from",
            "valid_to",
            pl.col("group_id").alias(f"{level}_id"),
        )
        if key.select(f"{col}_name", f"{col}_abbrev").is_duplicated().any():
            raise ValueError(f"nhl: a {level} (name, abbreviation) has two eras in curated/nhl_groups.csv")
        # a label seen outside its curated season range stays unmapped and fails below
        in_range = pl.col("season").is_between(pl.col("valid_from"), pl.col("valid_to").fill_null(9999))
        df = (
            df.join(key, on=[f"{col}_name", f"{col}_abbrev"], how="left")
            .with_columns(pl.when(in_range).then(pl.col(f"{level}_id")).alias(f"{level}_id"))
            .drop("valid_from", "valid_to")
        )
        bad = df.filter(
            pl.col(f"{col}_name").is_not_null() & pl.col(f"{level}_id").is_null()
        )
        if bad.height:
            raise ValueError(
                f"nhl: unmapped {level}s, add them to curated/nhl_groups.csv: "
                f"{bad.select('season', f'{col}_name', f'{col}_abbrev').unique().rows()[:5]}"
            )
    xwalk = dict(
        pl.read_csv(CURATED / "nhl_team_espn.csv", infer_schema_length=0)
        .select("nhl_team_id", "espn_team_id")
        .iter_rows()
    )
    # a new NHL id for a franchise ESPN carries (a relocation or rename, like Utah 59 -> 68) must be curated,
    # or it would silently fall back to the native id
    franchise = {str(t["id"]): t["franchiseId"] for t in read_json_gz(RAW / "stats_teams.json.gz")}
    covered = {franchise.get(i) for i in xwalk} - {None}
    stray = {i for i in df["nhl_team_id"].unique() if i not in xwalk and franchise.get(i) in covered}
    if stray:
        raise ValueError(f"nhl: team ids of ESPN-covered franchises missing from curated/nhl_team_espn.csv: {sorted(stray)}")
    return df.with_columns(
        team_id=pl.col("nhl_team_id").replace(xwalk),
        team_id_source=pl.when(pl.col("nhl_team_id").is_in(list(xwalk)))
        .then(pl.lit("espn"))
        .otherwise(pl.lit("nhl")),
        subdivision_id=pl.lit(None, pl.Utf8),
        source=pl.lit("nhl"),
        notes=pl.format("nhl_team_id={}", "nhl_team_id"),
    )


def build() -> dict[str, pl.DataFrame]:
    """The four nhl_groups tables, offline from raw/nhl/ and curated/."""
    eras = _curated()
    tgs = _team_seasons(eras)
    espn_rows = read_json_gz(RAW / "espn_groups.json.gz")
    group_map = dict(
        eras.filter(pl.col("espn_group_id").is_not_null())
        .select("espn_group_id", "group_id")
        .unique()
        .iter_rows()
    )
    tgs, summary = espn_check(tgs, espn_rows, group_map)

    # names come from the curated era rows, for every season each group has members
    seasons = pl.DataFrame({"season": tgs["season"].unique().sort()})
    names = (
        eras.join(seasons, how="cross")
        .filter(
            pl.col("season").is_between(
                pl.col("valid_from"), pl.col("valid_to").fill_null(9999)
            )
        )
        .select("group_id", "season", "name", "short_name", "abbreviation")
    )
    # an era row can't describe a season twice
    dup = names.filter(names.select("group_id", "season").is_duplicated())
    if dup.height:
        raise ValueError(
            f"nhl: overlapping name eras in curated/nhl_groups.csv: {dup.rows()[:3]}"
        )
    notes = {
        g: "; ".join(n)
        for g, n in eras.filter(pl.col("notes").is_not_null())
        .group_by("group_id", maintain_order=True)
        .agg("notes")
        .iter_rows()
    }
    notes["nhl:nhl"] = f"{notes.get('nhl:nhl', '')}. {summary}"

    # aliases: the NHL's own labels per era (valid over the seasons observed), plus ESPN's
    member_seasons = pl.concat(
        [
            tgs.select(pl.col(c).alias("group_id"), "season").drop_nulls()
            for c in ("conference_id", "division_id")
        ]
        + [tgs.select(pl.lit("nhl:nhl").alias("group_id"), "season")]
    ).unique()
    latest = tgs["season"].max()
    nhl_alias = (
        names.join(member_seasons, on=["group_id", "season"], how="semi")
        .unpivot(
            index=["group_id", "season"],
            on=["name", "short_name", "abbreviation"],
            variable_name="name_kind",
            value_name="value",
        )
        .group_by("group_id", "name_kind", "value")
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
        )
        .with_columns(
            source=pl.lit("nhl"),
            valid_to=pl.when(pl.col("valid_to") < latest).then(pl.col("valid_to")),
        )
    )
    aliases = pl.concat(
        [nhl_alias, espn_aliases(espn_rows, group_map, member_seasons)], how="diagonal"
    )
    return assemble(LEAGUE, tgs, names, notes, aliases)


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
