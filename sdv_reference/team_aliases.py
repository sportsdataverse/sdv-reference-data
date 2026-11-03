"""{league}_team_aliases: every provider's id (and name) for a team, keyed on the ESPN team id, with season windows.
Published as the fifth table of each `{league}_groups` tag. See CONTRACT.md.

    uv run python -c "from sdv_reference.build import fetch; fetch('nba')"   # snapshots, incl. this table's
    uv run python -m sdv_reference.build nba                                # offline: raw/ + curated/ -> build/nba/

It consolidates legs SDV already has; nothing here asks a provider for anything:
- the Fox / Yahoo / Torvik columns of the published team crosswalks ({nba,wnba,mbb,wbb,cfb}_crosswalk), snapshotted
  into raw/team_aliases/{league}/ by fetch();
- nfl-raw's Fox / Yahoo / CBS player crosswalks (each player row names both teams), also snapshotted;
- curated maps: NBA / WNBA Stats (curated/{nba,wnba}_teams.csv), MLB / NHL (curated/{mlb,nhl}_team_espn.csv, read
  from the league's team_group_seasons notes, which carry the native id per season), KenPom (curated/mbb_kenpom_teams.csv
  over raw/mbb/kenpom_team_info.csv.gz), CBS college football (curated/cfb_cbs_teams.csv, from sdv-py);
- deterministic keys: CFBD team ids are ESPN ids; Yahoo NBA / NFL ids are `{lg}.t.{espn id}` (asserted on every row);
  stats.ncaa.org org ids from this repo's NCAA <-> ESPN basketball crosswalk.

No fuzzy matching and no guessing: a row whose team-season is not in the league's team_group_seasons, or an id two
teams claim in one season (or a team with two ids of one source in a season), is dropped and written to
{league}_team_aliases_unmatched.csv with the reason.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
from functools import cache
from pathlib import Path

import polars as pl
import requests

from sdv_reference.refresh import current_season, seasons_to_fetch
from sdv_reference.schema import (
    ENDING_YEAR_LEAGUES,
    TEAM_ALIASES,
    validate_team_aliases,
)

ROOT = Path(__file__).resolve().parent.parent
RAW, CURATED = ROOT / "raw" / "team_aliases", ROOT / "curated"
LEAGUES = ("nfl", "cfb", "nba", "wnba", "mbb", "wbb", "mlb", "nhl")
RELEASES = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
NFL_RAW = Path(os.environ.get("SDV_NFL_RAW_CHECKOUT", "/mnt/sdv_repos/nfl-raw"))

#: league -> (release tag, asset stem, first season published)
XWALKS = {
    "nba": ("nba_crosswalk", "nba_team_crosswalk", 2002),
    "wnba": ("wnba_crosswalk", "wnba_team_crosswalk", 2002),
    "mbb": ("mbb_crosswalk", "mbb_team_crosswalk", 2002),
    "wbb": ("wbb_crosswalk", "wbb_team_crosswalk", 2002),
    "cfb": ("cfb_crosswalk", "cfb_teams_crosswalk", 2014),
}
#: source -> (id column, {name_kind: column}); a column an asset lacks is skipped. Torvik has names only.
XWALK_LEGS = {
    "fox": (
        "fox_team_id",
        {"name": ["fox_team_name", "fox_team"], "abbreviation": ["fox_abbreviation"]},
    ),
    "yahoo": (
        "yahoo_team_id",
        {
            "name": ["yahoo_team_name", "yahoo_team"],
            "abbreviation": ["yahoo_team_abbreviation", "yahoo_abbreviation"],
        },
    ),
    "torvik": (None, {"name": ["bart_team"]}),
}
NFL_LEGS = ("fox", "yahoo", "cbs")
#: Yahoo's team id is `{prefix}.t.{ESPN id}` in these leagues (asserted on every row)
YAHOO_IS_ESPN = {"nba": "nba", "nfl": "nfl"}

LONG = {
    "season": pl.Int32,
    "team_id": pl.Utf8,
    "source": pl.Utf8,
    "source_id": pl.Utf8,
    "id_kind": pl.Utf8,
    "name_kind": pl.Utf8,
    "value": pl.Utf8,
    "match_method": pl.Utf8,
    "evidence": pl.Utf8,
}
#: the output order: every column, so the bytes are the same for the same rows
SORT = ["team_id", "source", "id_kind", "source_id", "name_kind", "value", "valid_from", "valid_to", "match_method", "evidence"]
UNMATCHED = {
    "league": pl.Utf8,
    "source": pl.Utf8,
    "source_id": pl.Utf8,
    "id_kind": pl.Utf8,
    "name_kind": pl.Utf8,
    "value": pl.Utf8,
    "team_id": pl.Utf8,
    "season_from": pl.Int32,
    "season_to": pl.Int32,
    "reason": pl.Utf8,
    "evidence": pl.Utf8,
}


# ---------------------------------------------------------------- fetch (published assets + local checkouts)


def _gz_csv(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(
        fileobj=buf, mode="wb", mtime=0
    ) as fh:  # mtime=0: unchanged data, unchanged bytes
        df.write_csv(fh)
    return buf.getvalue()


def _read_gz_csv(path: Path) -> pl.DataFrame:
    """Every column Utf8 except season: ids keep one dtype from the boundary on."""
    df = pl.read_csv(gzip.open(path).read(), infer_schema=False)
    return df.with_columns(pl.col("season").cast(pl.Int32))


def _snapshot(league: str, name: str, df: pl.DataFrame, source: str) -> None:
    """Write raw/team_aliases/{league}/{name}; its provenance line changes only when the bytes do."""
    out = RAW / league
    out.mkdir(parents=True, exist_ok=True)
    prov_path = out / "provenance.json"
    prov = json.loads(prov_path.read_text()) if prov_path.exists() else {}
    data, path = _gz_csv(df), out / name
    if path.exists() and path.read_bytes() == data and name in prov:
        return
    path.write_bytes(data)
    prov[name] = source
    prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n")


def _fetch_crosswalk(league: str) -> None:
    tag, stem, first = XWALKS[league]
    name = f"{stem}.csv.gz"
    path = RAW / league / name
    old = _read_gz_csv(path) if path.exists() else None
    have = old["season"].unique().to_list() if old is not None else []
    todo = seasons_to_fetch(
        range(first, current_season(league in ENDING_YEAR_LEAGUES) + 1), have
    )
    keep = [
        c
        for _, (i, names) in XWALK_LEGS.items()
        for c in ([i] if i else []) + sum(names.values(), [])
    ]
    parts, session = [], requests.Session()
    for season in todo:
        r = session.get(f"{RELEASES}/{tag}/{stem}_{season}.parquet", timeout=120)
        if r.status_code == 404:  # not published (yet)
            continue
        r.raise_for_status()
        df = pl.read_parquet(io.BytesIO(r.content))
        if "season" not in df.columns:  # cfb_teams_crosswalk has no season column
            df = df.with_columns(pl.lit(season).alias("season"))
        parts.append(
            df.select(
                pl.lit(season).cast(pl.Int32).alias("season"),
                pl.col("espn_team_id").cast(pl.Utf8),
                *[pl.col(c).cast(pl.Utf8) for c in keep if c in df.columns],
            )
        )
    if old is not None:
        parts.insert(0, old.filter(~pl.col("season").is_in(todo)))
    if not parts:
        raise ValueError(f"{league}: no {tag}/{stem}_*.parquet found")
    snap = pl.concat(parts, how="diagonal_relaxed")
    snap = snap.select(
        "season",
        "espn_team_id",
        *sorted(c for c in snap.columns if c not in ("season", "espn_team_id")),
    )
    snap = snap.unique().sort(pl.all())
    lo, hi = snap["season"].min(), snap["season"].max()
    _snapshot(
        league,
        name,
        snap,
        f"sportsdataverse-data release {tag}: {stem}_{{{lo}..{hi}}}.parquet",
    )


def _fetch_nfl() -> None:
    """The (season, ESPN team, provider team) pairs of nfl-raw's player crosswalks, as committed on origin/main."""
    for p in NFL_LEGS:
        rel = f"nfl/espn/crosswalk/players_{p}.json"
        raw = subprocess.run(
            ["git", "-C", str(NFL_RAW), "show", f"origin/main:{rel}"],
            capture_output=True,
            check=True,
        )
        rows = json.loads(raw.stdout.decode("utf-8"))
        pairs = (
            pl.DataFrame(
                [(r["season"], r["espn_team_id"], r[f"{p}_team_id"]) for r in rows],
                schema={
                    "season": pl.Int32,
                    "espn_team_id": pl.Utf8,
                    "source_id": pl.Utf8,
                },
                orient="row",
            )
            .drop_nulls()
            .group_by("season", "espn_team_id", "source_id")
            .agg(pl.len().cast(pl.Int32).alias("n_players"))
            .sort("season", "espn_team_id", "source_id")
        )
        commit = subprocess.run(
            ["git", "-C", str(NFL_RAW), "log", "-1", "--format=%H", "origin/main", "--", rel],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        _snapshot(
            "nfl", f"{p}_teams.csv.gz", pairs, f"nfl-raw origin/main {commit}:{rel}"
        )


def fetch(league: str) -> None:
    if league in XWALKS:
        _fetch_crosswalk(league)
    elif league == "nfl":
        _fetch_nfl()


# ---------------------------------------------------------------- build (offline: raw/ + curated/)


def _clean_id(c: str) -> pl.Expr:
    """An id with surrounding blanks stripped; "", "0" and "None" are no id."""
    s = pl.col(c).cast(pl.Utf8).str.strip_chars()
    return pl.when(s.is_in(["", "0", "None", "null"])).then(None).otherwise(s)


def _long(
    df: pl.DataFrame,
    source: str,
    id_col: str | None,
    names: dict[str, str],
    method: str,
    evidence: str,
    id_kind: str = "team",
) -> pl.DataFrame:
    """One row per (season, team, id, name): each name the source gives, and a bare id row when it gives none.
    `df` has season and team_id."""
    base = df.select(
        pl.col("season").cast(pl.Int32),
        pl.col("team_id").cast(pl.Utf8),
        (_clean_id(id_col) if id_col else pl.lit(None, pl.Utf8)).alias("source_id"),
        *[pl.col(c).cast(pl.Utf8).str.strip_chars().alias(k) for k, c in names.items()],
    ).with_columns(pl.when(pl.col(k) != "").then(pl.col(k)).alias(k) for k in names)
    parts = [
        base.filter(pl.col(k).is_not_null()).select(
            "season",
            "team_id",
            "source_id",
            pl.lit(k).alias("name_kind"),
            pl.col(k).alias("value"),
        )
        for k in names
    ]
    no_name = (
        pl.all_horizontal([pl.col(k).is_null() for k in names])
        if names
        else pl.lit(True)
    )
    parts.append(
        base.filter(pl.col("source_id").is_not_null() & no_name).select(
            "season",
            "team_id",
            "source_id",
            pl.lit(None, pl.Utf8).alias("name_kind"),
            pl.lit(None, pl.Utf8).alias("value"),
        )
    )
    return (
        pl.concat(parts)
        .with_columns(
            pl.lit(source).alias("source"),
            pl.lit(id_kind).alias("id_kind"),
            pl.lit(method).alias("match_method"),
            pl.lit(evidence).alias("evidence"),
        )
        .select(list(LONG))
        .cast(LONG)
    )


def _read_curated(name: str) -> pl.DataFrame:
    return pl.read_csv(CURATED / name, infer_schema=False)


def _crosswalk_legs(league: str) -> list[pl.DataFrame]:
    tag, stem, _ = XWALKS[league]
    snap = _read_gz_csv(RAW / league / f"{stem}.csv.gz").rename(
        {"espn_team_id": "team_id"}
    )
    out = []
    for source, (id_col, kinds) in XWALK_LEGS.items():
        names = {
            k: next(c for c in cols if c in snap.columns)
            for k, cols in kinds.items()
            if any(c in snap.columns for c in cols)
        }
        if (id_col and id_col not in snap.columns) or (not id_col and not names):
            continue
        method, ev = (
            "crosswalk",
            f"{tag}/{stem}_{{season}}.parquet ({id_col or names['name']}); raw/team_aliases/{league}/{stem}.csv.gz",
        )
        if source == "yahoo" and league in YAHOO_IS_ESPN:
            method = "deterministic"
        out.append(_long(snap, source, id_col, names, method, ev))
    return out


def _nfl_legs() -> list[pl.DataFrame]:
    out = []
    for p in NFL_LEGS:
        pairs = _read_gz_csv(RAW / "nfl" / f"{p}_teams.csv.gz").rename(
            {"espn_team_id": "team_id"}
        )
        method = "deterministic" if p == "yahoo" else "crosswalk"
        ev = f"nfl-raw nfl/espn/crosswalk/players_{p}.json team pairs; raw/team_aliases/nfl/{p}_teams.csv.gz"
        out.append(_long(pairs, p, "source_id", {}, method, ev))
    return out


def _stats_legs(league: str) -> pl.DataFrame:
    """NBA / WNBA Stats TeamID windows -> ESPN id, from the curated map the league build uses."""
    c = _read_curated(f"{league}_teams.csv")
    rows = [
        (s, espn, sid)
        for sid, espn, lo, hi in c.select(
            f"{league}_stats_team_id", "espn_team_id", "first_season", "last_season"
        ).iter_rows()
        for s in range(int(lo), int(hi) + 1)
    ]
    df = pl.DataFrame(
        rows,
        schema={"season": pl.Int32, "team_id": pl.Utf8, "sid": pl.Utf8},
        orient="row",
    )
    return _long(
        df, f"{league}_stats", "sid", {}, "curated", f"curated/{league}_teams.csv"
    )


def _native_from_notes(league: str, tgs: pl.DataFrame) -> pl.DataFrame:
    """MLB / NHL native team ids: the league build writes `{league}_team_id=N` into each ESPN team-season's notes from
    curated/{league}_team_espn.csv."""
    df = tgs.filter(pl.col("team_id_source") == "espn").with_columns(
        pl.col("notes").str.extract(rf"\b{league}_team_id=(\d+)").alias("sid")
    )
    return _long(
        df,
        league,
        "sid",
        {},
        "curated",
        f"curated/{league}_team_espn.csv (per season: {league}_team_group_seasons notes)",
    )


def _cfbd(tgs: pl.DataFrame) -> pl.DataFrame:
    """CFBD's team id is the ESPN id, for every team-season whose membership came from CFBD."""
    df = tgs.filter(
        (pl.col("team_id_source") == "espn") & (pl.col("source") == "cfbd")
    ).with_columns(sid=pl.col("team_id"))
    return _long(
        df,
        "cfbd",
        "sid",
        {},
        "deterministic",
        "CFBD team ids are ESPN ids (cfb_team_group_seasons source cfbd)",
    )


def _cbs_cfb() -> pl.DataFrame:
    c = (
        _read_curated("cfb_cbs_teams.csv")
        .rename({"espn_team_id": "team_id"})
        .with_columns(pl.lit(None, pl.Int32).alias("season"))
    )
    return _long(
        c, "cbs", "cbs_team_id", {}, "curated", "curated/cfb_cbs_teams.csv"
    )  # undated: season null


def _kenpom() -> pl.DataFrame:
    names = dict(
        _read_curated("mbb_kenpom_teams.csv")
        .select("kenpom_team", "espn_team_id")
        .iter_rows()
    )
    kp = pl.read_csv(
        gzip.open(ROOT / "raw" / "mbb" / "kenpom_team_info.csv.gz").read(),
        infer_schema=False,
    )
    df = kp.select(
        pl.col("Year").cast(pl.Int32).alias("season"),
        pl.col("Team").replace_strict(names, default=None).alias("team_id"),
        pl.col("Team").alias("kp"),
    )
    return _long(
        df,
        "kenpom",
        None,
        {"name": "kp"},
        "curated",
        "curated/mbb_kenpom_teams.csv over raw/mbb/kenpom_team_info.csv.gz",
    )


@cache
def _ncaa_presence() -> pl.DataFrame:
    from sdv_reference.crosswalks import _ncaa_espn

    return _ncaa_espn.build()[2]


def _ncaa(league: str) -> pl.DataFrame:
    """stats.ncaa.org's school (org) id per D-I season, from the NCAA <-> ESPN basketball crosswalk."""
    p = _ncaa_presence().filter(pl.col("sport") == league)
    df = p.select(
        (pl.col("season").str.slice(0, 4).cast(pl.Int32) + 1).alias(
            "season"
        ),  # "2025-26" -> 2026
        pl.col("espn_team_id").alias("team_id"),
        pl.col("ncaa_org_id").cast(pl.Utf8).alias("sid"),
    )
    return _long(
        df,
        "ncaa",
        "sid",
        {},
        "crosswalk",
        f"{league}_crosswalk ncaa_espn_school_seasons",
        id_kind="school",
    )


def legs(league: str, tgs: pl.DataFrame) -> pl.DataFrame:
    """Every candidate row for the league, one per season (season null for an undated curated map)."""
    parts: list[pl.DataFrame] = []
    if league in XWALKS:
        parts += _crosswalk_legs(league)
    if league == "nfl":
        parts += _nfl_legs()
    if league in ("nba", "wnba"):
        parts.append(_stats_legs(league))
    if league in ("mlb", "nhl"):
        parts.append(_native_from_notes(league, tgs))
    if league == "cfb":
        parts += [_cfbd(tgs), _cbs_cfb()]
    if league == "mbb":
        parts.append(_kenpom())
    if league in ("mbb", "wbb"):
        parts.append(_ncaa(league))
    return pl.concat(parts).unique()


def _windows(df: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """Collapse per-season rows into runs of consecutive seasons: valid_from / valid_to (null for rows from an
    undated map, `_u`)."""
    dated = df.filter(~pl.col("_u")).sort(*keys, "season")
    runs = (
        dated.with_columns(
            (
                pl.col("season") - pl.int_range(pl.len()).over(keys, order_by="season")
            ).alias("_run")
        )
        .group_by(*keys, "_run")
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
        )
        .drop("_run")
    )
    undated = df.filter(pl.col("_u")).unique(keys).select(
        *keys,
        pl.lit(None, pl.Int32).alias("valid_from"),
        pl.lit(None, pl.Int32).alias("valid_to"),
    )
    return pl.concat([runs, undated])


def build(league: str, tgs: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(team_aliases, unmatched) for a league, given its built team_group_seasons."""
    hub = (
        tgs.filter(pl.col("team_id_source") == "espn")
        .select("season", "team_id")
        .unique()
    )
    rows = legs(league, tgs)
    for k in ("season", "team_id"):
        assert rows.schema[k] == hub.schema[k], (
            f"{k}: {rows.schema[k]} != {hub.schema[k]}"
        )
    undated = rows.filter(pl.col("season").is_null())
    # an undated map is checked in every season the team is in the hub, and published with open bounds (`_u`)
    rows = pl.concat([
        rows.filter(pl.col("season").is_not_null()).with_columns(pl.lit(False).alias("_u")),
        undated.drop("season").join(hub, on="team_id").select(*LONG, pl.lit(True).alias("_u")),
    ])  # fmt: skip
    drops: list[pl.DataFrame] = []

    def drop(bad: pl.DataFrame, reason: str) -> None:
        drops.append(bad.with_columns(pl.lit(reason).alias("reason")))

    no_team = rows.filter(pl.col("team_id").is_null())
    drop(no_team, "no ESPN team for this id in the source")
    rows = rows.filter(pl.col("team_id").is_not_null())
    orphan = rows.join(hub, on=["season", "team_id"], how="anti")
    drop(orphan, f"team-season not in {league}_team_group_seasons (ESPN)")
    rows = rows.join(hub, on=["season", "team_id"], how="semi")

    # Yahoo is the ESPN id in these leagues: fail rather than publish a row that breaks the rule
    if league in YAHOO_IS_ESPN:
        y = rows.filter(pl.col("source") == "yahoo")
        bad = y.filter(
            pl.col("source_id")
            != pl.lit(f"{YAHOO_IS_ESPN[league]}.t.") + pl.col("team_id")
        )
        assert bad.height == 0, (
            f"{league}: Yahoo ids that are not {YAHOO_IS_ESPN[league]}.t.{{espn}}: {bad.head(5).rows()}"
        )

    # one id -> one team, and one team -> one id, per (season, source, id_kind); names alike for name-only sources
    ids = rows.filter(pl.col("source_id").is_not_null())
    many_teams = (
        ids.group_by("season", "source", "id_kind", "source_id")
        .agg(pl.col("team_id").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    many_ids = (
        ids.group_by("season", "source", "id_kind", "team_id")
        .agg(pl.col("source_id").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    names = rows.filter(pl.col("source_id").is_null())
    many_names = (
        names.group_by("season", "source", "name_kind", "value")
        .agg(pl.col("team_id").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    # the whole id (every season) goes: a conflicted id is not trusted in its other seasons either
    bad_ids = pl.concat([
        many_teams.select("source", "id_kind", "source_id"),
        ids.join(many_ids.select("season", "source", "id_kind", "team_id"), on=["season", "source", "id_kind", "team_id"])
        .select("source", "id_kind", "source_id"),
    ]).unique()  # fmt: skip
    hit = rows.join(bad_ids, on=["source", "id_kind", "source_id"], how="semi")
    drop(
        hit,
        "an id two teams claim in one season, or one of two ids a team carries in one season",
    )
    rows = rows.join(bad_ids, on=["source", "id_kind", "source_id"], how="anti")
    bad_names = many_names.select("source", "name_kind", "value").unique()
    hit = rows.filter(pl.col("source_id").is_null()).join(
        bad_names, on=["source", "name_kind", "value"], how="semi"
    )
    drop(hit, "a name two teams carry in one season")
    rows = rows.join(hit, on=list(LONG), how="anti", nulls_equal=True)

    keys = [
        "team_id",
        "source",
        "source_id",
        "id_kind",
        "name_kind",
        "value",
        "match_method",
        "evidence",
    ]
    aliases = (
        _windows(rows, keys)
        .with_columns(
            pl.lit(league).alias("league"), pl.lit("espn").alias("team_id_source")
        )
        .select(list(TEAM_ALIASES))
        .cast(TEAM_ALIASES)
        .sort(SORT, nulls_last=True)
    )
    ukeys = [
        "source",
        "source_id",
        "id_kind",
        "name_kind",
        "value",
        "team_id",
        "reason",
        "evidence",
    ]
    unmatched = (
        _windows(pl.concat(drops) if drops else pl.DataFrame(schema={**LONG, "_u": pl.Boolean, "reason": pl.Utf8}), ukeys)
        .rename({"valid_from": "season_from", "valid_to": "season_to"})
        .with_columns(pl.lit(league).alias("league"))
        .select(list(UNMATCHED)).cast(UNMATCHED)
        .sort(list(UNMATCHED), nulls_last=True)
    )  # fmt: skip
    return aliases, unmatched


def write(
    league: str, tgs: pl.DataFrame, out_dir: Path
) -> tuple[list[Path], pl.DataFrame, pl.DataFrame]:
    aliases, unmatched = build(league, tgs)
    problems = validate_team_aliases(league, aliases, tgs)
    if problems:
        raise ValueError(
            f"{league}_team_aliases fails the contract:\n  " + "\n  ".join(problems)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [out_dir / f"{league}_team_aliases.parquet", out_dir / f"{league}_team_aliases.csv",
             out_dir / f"{league}_team_aliases_unmatched.csv"]  # fmt: skip
    aliases.write_parquet(files[0])
    aliases.write_csv(files[1])
    unmatched.write_csv(files[2])
    return files, aliases, unmatched
