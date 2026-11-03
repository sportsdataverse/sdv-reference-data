"""{league}_player_aliases: every provider's player id for an ESPN athlete, with the ESPN team and seasons it held.
Published beside team_aliases in each `{league}_groups` tag. See CONTRACT.md.

    uv run python -c "from sdv_reference.build import fetch; fetch('mbb')"   # snapshots, incl. this table's
    uv run python -m sdv_reference.build mbb                                # offline: raw/ + curated/ -> build/mbb/

Each row's evidence names its source file; raw/player_aliases/{league}/provenance.json maps each snapshot to the
release asset or commit. It consolidates player legs SDV has already matched; nothing here asks a provider for anything, and nothing here
re-matches names:
- the published ESPN-anchored player crosswalks ({nba,wnba,mbb,wbb}_player_crosswalk, cfb_players_crosswalk,
  cfb_rosters_crosswalk), snapshotted into raw/player_aliases/{league}/ by fetch();
- nfl-raw's Fox / Yahoo / CBS player crosswalks (`players_{fox,yahoo,cbs}.json`, origin/main);
- the NCAA raw repos' stats.ncaa.org roster id -> ESPN athlete maps (`{lg}/xwalk/espn_athlete_id/{season}.json`,
  origin/main of ncaa-mbb-hoops-raw, ncaa-wbb-hoops-raw and ncaa-mfb-football-raw), built there by sdv-py's
  `scrape/ncaa/espn_player_xwalk.py`: blocked by (season, ESPN team) through the NCAA team crosswalk, then normalized
  name + jersey, name, last name + jersey, and a guarded Jaro-Winkler >= 0.92 step (one candidate in the block, claimed
  by no other player). Its unmatched rows carry their reason.

On top of the legs, the build:
1. applies curated/player_overrides.csv (hand-verified; it can set a leg's miss; one that changes nothing fails);
2. keeps a row only when its (season, ESPN team) is an ESPN team-season of team_group_seasons, and (NCAA) the leg's
   ESPN team is the one the NCAA team crosswalk gives that NCAA team; (NFL) the provider team maps to that ESPN team
   in team_aliases that season;
3. holds each provider id to one athlete across every season (a conflict drops the id, every season) and each athlete
   to one id per source and team per season; a pair seen in one season is carried to the same provider id's unmatched
   rows in other seasons (`propagated`), which links a player across teams;
4. fails when a (league, source, season) match rate falls below curated/player_match_floors.csv.
Everything dropped goes to {league}_player_aliases_unmatched.csv with its reason.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
from pathlib import Path

import polars as pl
import requests

from sdv_reference.refresh import current_season, seasons_to_fetch
from sdv_reference.schema import (
    ENDING_YEAR_LEAGUES,
    PLAYER_ALIASES,
    validate_player_aliases,
)
from sdv_reference.team_aliases import NFL_RAW, RELEASES, _gz_csv

ROOT = Path(__file__).resolve().parent.parent
RAW, CURATED = ROOT / "raw" / "player_aliases", ROOT / "curated"
LEAGUES = ("nfl", "cfb", "nba", "wnba", "mbb", "wbb")

#: league -> [(release tag, asset stem, first season)] of the published ESPN-anchored player crosswalks
XWALKS = {
    "nba": [("nba_crosswalk", "nba_player_crosswalk", 2002)],
    "wnba": [("wnba_crosswalk", "wnba_player_crosswalk", 2002)],
    "mbb": [("mbb_crosswalk", "mbb_player_crosswalk", 2002)],
    "wbb": [("wbb_crosswalk", "wbb_player_crosswalk", 2002)],
    "cfb": [
        ("cfb_crosswalk", "cfb_players_crosswalk", 2014),
        ("cfb_crosswalk", "cfb_rosters_crosswalk", 2014),
    ],
}
#: asset stem -> {source: (id column, name column)}; a column an asset lacks is skipped
LEGS = {
    "player_crosswalk": {
        "nba_stats": ("nba_player_id", "nba_player_name"),
        "wnba_stats": ("wnba_player_id", "wnba_player_name"),
        "fox": ("fox_athlete_id", "fox_player"),
        "yahoo": ("yahoo_player_id", "yahoo_player_name"),
        "kenpom": ("kp_player_id", None),
    },
    "cfb_players_crosswalk": {
        "cbs": ("cbs_player_id", "cbs_player_name"),
        "fox": ("fox_athlete_id", "fox_player_name"),
        "yahoo": ("yahoo_player_id", "yahoo_player_name"),
    },
    "cfb_rosters_crosswalk": {
        "fox": ("fox_athlete_id", None),
        "yahoo": ("yahoo_athlete_id", None),
    },
}
#: the source whose match an asset's match_method / match_confidence describe (sdv-py _common_crosswalk_basketball)
METHOD_LEG = {"nba": "nba_stats", "wnba": "wnba_stats", "mbb": "fox", "wbb": "fox"}
NFL_LEGS = ("fox", "yahoo", "cbs")
#: league -> (raw repo env var, default checkout, tree league); the NCAA repos' season key is the ENDING year for
#: hoops and the academic year (fall + 1) for football
NCAA = {
    "mbb": ("SDV_NCAA_MBB_HOOPS_RAW", "/mnt/sdv_repos/ncaa-mbb-hoops-raw", "mbb"),
    "wbb": ("SDV_NCAA_WBB_HOOPS_RAW", "/mnt/sdv_repos/ncaa-wbb-hoops-raw", "wbb"),
    "cfb": ("SDV_NCAA_MFB_FOOTBALL_RAW", "/mnt/sdv_repos/ncaa-mfb-football-raw", "mfb"),
}
#: an ordinal tier score for legs that give no confidence of their own (sdv-py espn_player_xwalk tiers)
TIER_CONFIDENCE = {
    "name_jersey": 1.0,
    "name": 0.95,
    "last_jersey": 0.9,
    "jaro_winkler": 0.85,
}

LONG = {
    "season": pl.Int32,
    "team_id": pl.Utf8,
    "athlete_id": pl.Utf8,
    "source": pl.Utf8,
    "source_player_id": pl.Utf8,
    "source_team_id": pl.Utf8,
    "match_method": pl.Utf8,
    "match_confidence": pl.Float64,
    "unmatched_reason": pl.Utf8,
    "evidence": pl.Utf8,
}
UNMATCHED = {
    "league": pl.Utf8,
    "source": pl.Utf8,
    "source_player_id": pl.Utf8,
    "source_team_id": pl.Utf8,
    "team_id": pl.Utf8,
    "athlete_id": pl.Utf8,
    "season_from": pl.Int32,
    "season_to": pl.Int32,
    "reason": pl.Utf8,
    "evidence": pl.Utf8,
}
RATES = {
    "league": pl.Utf8,
    "source": pl.Utf8,
    "season": pl.Int32,
    "n_rows": pl.UInt32,
    "n_matched": pl.UInt32,
    "match_rate": pl.Float64,
}


# ---------------------------------------------------------------- fetch (published assets + local checkouts)


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True
    ).stdout


def _snapshot(league: str, name: str, df: pl.DataFrame, source: str) -> None:
    """Write raw/player_aliases/{league}/{name}; its provenance line changes only when the bytes do."""
    out = RAW / league
    out.mkdir(parents=True, exist_ok=True)
    prov_path = out / "provenance.json"
    prov = json.loads(prov_path.read_text()) if prov_path.exists() else {}
    data, path = _gz_csv(df.sort(pl.all())), out / name
    if path.exists() and path.read_bytes() == data and name in prov:
        return
    path.write_bytes(data)
    prov[name] = source
    prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n")


def _have(league: str, prefix: str) -> list[int]:
    return [
        int(p.name.removesuffix(".csv.gz").rsplit("_", 1)[1])
        for p in (RAW / league).glob(f"{prefix}_*.csv.gz")
    ]


def _fetch_crosswalks(league: str) -> None:
    """One file per season: a published season that changes rewrites only its own snapshot."""
    session = requests.Session()
    for tag, stem, first in XWALKS[league]:
        legs = LEGS["player_crosswalk" if stem.endswith("_player_crosswalk") else stem]
        keep = ["espn_full_name", "espn_player_name", "name", "espn_jersey", "fox_team_id", "match_method",
                "match_confidence", "match_keys"] + [c for i, n in legs.values() for c in (i, n) if c]  # fmt: skip
        todo = seasons_to_fetch(
            range(first, current_season(league in ENDING_YEAR_LEAGUES) + 1),
            _have(league, stem),
        )
        for season in todo:
            r = session.get(f"{RELEASES}/{tag}/{stem}_{season}.parquet", timeout=120)
            if r.status_code == 404:  # not published (yet)
                continue
            r.raise_for_status()
            df = pl.read_parquet(io.BytesIO(r.content))
            snap = df.select(
                pl.lit(season).cast(pl.Int32).alias("season"),
                pl.col("espn_team_id").cast(pl.Int64, strict=False).cast(pl.Utf8),
                pl.col("espn_athlete_id").cast(pl.Int64, strict=False).cast(pl.Utf8),
                *[pl.col(c).cast(pl.Utf8) for c in keep if c in df.columns],
            )
            _snapshot(
                league,
                f"{stem}_{season}.csv.gz",
                snap,
                f"sportsdataverse-data release {tag}: {stem}_{season}.parquet",
            )


def _fetch_nfl() -> None:
    for p in NFL_LEGS:
        rel = f"nfl/espn/crosswalk/players_{p}.json"
        rows = json.loads(_git(NFL_RAW, "show", f"origin/main:{rel}").decode("utf-8"))
        cols = ["season", "espn_team_id", "espn_athlete_id", "espn_full_name", f"{p}_team_id", f"{p}_player_id",
                f"{p}_player_name", "match_method", "match_confidence"]  # fmt: skip
        df = pl.DataFrame(
            [[r.get(c) for c in cols] for r in rows],
            schema={c: pl.Utf8 for c in cols},
            orient="row",
        )
        commit = (
            _git(NFL_RAW, "log", "-1", "--format=%H", "origin/main", "--", rel)
            .decode()
            .strip()
        )
        _snapshot(
            "nfl", f"players_{p}.csv.gz", df, f"nfl-raw origin/main {commit}:{rel}"
        )


def _fetch_ncaa(league: str) -> None:
    env, default, lg = NCAA[league]
    repo = Path(os.environ.get(env, default))
    shift = (
        1 if lg == "mfb" else 0
    )  # mfb's tree key is the academic year: cfb season = ay - 1
    for path in (
        _git(
            repo,
            "ls-tree",
            "--name-only",
            "origin/main",
            f"{lg}/xwalk/espn_athlete_id/",
        )
        .decode()
        .split()
    ):
        key = int(Path(path).stem)
        rows = json.loads(_git(repo, "show", f"origin/main:{path}").decode("utf-8"))
        cols = [
            "ncaa_team_id",
            "ncaa_player_id",
            "espn_team_id",
            "espn_athlete_id",
            "match_method",
            "unmatched_reason",
        ]
        df = pl.DataFrame(
            [[r.get(c) for c in cols] for r in rows],
            schema={c: pl.Utf8 for c in cols},
            orient="row",
        )
        commit = (
            _git(repo, "log", "-1", "--format=%H", "origin/main", "--", path)
            .decode()
            .strip()
        )
        _snapshot(
            league,
            f"ncaa_{key - shift}.csv.gz",
            df,
            f"{repo.name} origin/main {commit}:{path}",
        )


def fetch(league: str) -> None:
    if league in XWALKS:
        _fetch_crosswalks(league)
    if league == "nfl":
        _fetch_nfl()
    if league in NCAA:
        _fetch_ncaa(league)


# ---------------------------------------------------------------- build (offline: raw/ + curated/)


def _read(path: Path) -> pl.DataFrame:
    return pl.read_csv(gzip.open(path).read(), infer_schema=False)


def _clean(c: str) -> pl.Expr:
    s = pl.col(c).cast(pl.Utf8).str.strip_chars()
    return pl.when(s.is_in(["", "0", "None", "null", "nan"])).then(None).otherwise(s)


def _rows(df: pl.DataFrame, **exprs: pl.Expr) -> pl.DataFrame:
    """`df` -> LONG; columns not given are null."""
    return df.select(
        *[exprs.get(k, pl.lit(None)).cast(t).alias(k) for k, t in LONG.items()]
    )


def _season_files(league: str, prefix: str) -> list[tuple[int, Path]]:
    return sorted(
        (int(p.name.removesuffix(".csv.gz").rsplit("_", 1)[1]), p)
        for p in (RAW / league).glob(f"{prefix}_*.csv.gz")
    )


def _crosswalk_legs(league: str) -> list[pl.DataFrame]:
    out = []
    for tag, stem, _ in XWALKS.get(league, []):
        legs = LEGS["player_crosswalk" if stem.endswith("_player_crosswalk") else stem]
        for season, path in _season_files(league, stem):
            df = _read(path)
            for source, (id_col, _name) in legs.items():
                if id_col not in df.columns:
                    continue
                ev = f"{tag}/{stem}_{{season}}.parquet ({id_col})"
                own = METHOD_LEG.get(league) == source and "match_method" in df.columns
                # every row: one with no id of this source still counts in its match rate
                rows = df.filter(
                    _clean(id_col).is_not_null()
                    | _clean("espn_athlete_id").is_not_null()
                )
                if (
                    stem == "cfb_rosters_crosswalk"
                ):  # name-joined roster rows: an id with no ESPN athlete is a miss
                    method, conf = pl.lit("name"), pl.lit(TIER_CONFIDENCE["name"])
                    team = pl.col("fox_team_id") if source == "fox" else pl.lit(None)
                else:
                    method = (
                        pl.col("match_method").str.to_lowercase()
                        if own
                        else pl.lit("crosswalk")
                    )
                    conf = (
                        pl.col("match_confidence").cast(pl.Float64, strict=False)
                        if own
                        else pl.lit(None)
                    )
                    team = pl.lit(None)
                out.append(_rows(
                    rows, season=pl.col("season"), team_id=_clean("espn_team_id"), athlete_id=_clean("espn_athlete_id"),
                    source=pl.lit(source), source_player_id=_clean(id_col), source_team_id=team, match_method=method,
                    match_confidence=conf, evidence=pl.lit(ev),
                    unmatched_reason=pl.when(_clean("espn_athlete_id").is_null()).then(pl.lit("no ESPN athlete in the leg")),
                ))  # fmt: skip
    return out


def _nfl_legs() -> list[pl.DataFrame]:
    out = []
    for p in NFL_LEGS:
        df = _read(RAW / "nfl" / f"players_{p}.csv.gz")
        out.append(_rows(
            df, season=pl.col("season"), team_id=_clean("espn_team_id"), athlete_id=_clean("espn_athlete_id"),
            source=pl.lit(p), source_player_id=_clean(f"{p}_player_id"), source_team_id=_clean(f"{p}_team_id"),
            match_method=pl.col("match_method"), match_confidence=pl.col("match_confidence").cast(pl.Float64),
            evidence=pl.lit(f"nfl-raw nfl/espn/crosswalk/players_{p}.json"),
        ).filter(pl.col("source_player_id").is_not_null()))  # fmt: skip
    return out


def _ncaa_legs(league: str) -> list[pl.DataFrame]:
    _, _, lg = NCAA[league]
    out = []
    for season, path in _season_files(league, "ncaa"):
        df = _read(path)
        ev = f"{Path(NCAA[league][1]).name} {lg}/xwalk/espn_athlete_id/" + ("{season + 1}.json" if lg == "mfb" else "{season}.json")
        out.append(_rows(
            df, season=pl.lit(season), team_id=_clean("espn_team_id"), athlete_id=_clean("espn_athlete_id"),
            source=pl.lit("ncaa"), source_player_id=_clean("ncaa_player_id"), source_team_id=_clean("ncaa_team_id"),
            match_method=pl.col("match_method"), match_confidence=pl.col("match_method").replace_strict(TIER_CONFIDENCE, default=None),
            unmatched_reason=pl.col("unmatched_reason"), evidence=pl.lit(ev),
        ))  # fmt: skip
    return out


def legs(league: str) -> pl.DataFrame:
    """Every candidate row: matched (athlete_id set) and the legs' own misses (unmatched_reason set)."""
    parts = _crosswalk_legs(league)
    if league == "nfl":
        parts += _nfl_legs()
    if league in NCAA:
        parts += _ncaa_legs(league)
    return pl.concat(parts).unique() if parts else pl.DataFrame(schema=LONG)


def _ncaa_teams(league: str) -> pl.DataFrame | None:
    """(season, NCAA team id, ESPN team id) from this repo's NCAA <-> ESPN basketball crosswalk; None for football,
    whose team map lives in the raw repo (the hub check still applies)."""
    if league not in ("mbb", "wbb"):
        return None
    from sdv_reference.team_aliases import _ncaa_presence

    p = _ncaa_presence().filter(pl.col("sport") == league)
    return p.select(
        (pl.col("season").str.slice(0, 4).cast(pl.Int32) + 1).alias("season"),
        pl.col("ncaa_team_id").cast(pl.Utf8).alias("source_team_id"),
        pl.col("espn_team_id").alias("xw_team"),
    ).unique()


def _overrides(league: str) -> pl.DataFrame:
    o = pl.read_csv(CURATED / "player_overrides.csv", infer_schema=False)
    return o.filter(pl.col("league") == league).with_columns(
        pl.col("athlete_id").str.strip_chars().replace("", None)
    )


def build(
    league: str, tgs: pl.DataFrame, team_aliases: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """(player_aliases, unmatched, rates) for a league, given its team_group_seasons and team_aliases."""
    hub = (
        tgs.filter(pl.col("team_id_source") == "espn")
        .select("season", "team_id")
        .unique()
    )
    rows = legs(league)
    for k in ("season", "team_id"):
        assert rows.schema[k] == hub.schema[k], (
            f"{k}: {rows.schema[k]} != {hub.schema[k]}"
        )
    # the match-rate denominator: every leg row (an ESPN athlete the leg gives no id of this source counts as a miss,
    # but is no unmatched provider id)
    total = rows
    rows = rows.filter(pl.col("source_player_id").is_not_null())
    pid = ["source", "source_player_id"]

    # 1. curated overrides, first: they can set the athlete of a leg's miss, and the team-block checks still apply
    ov = _overrides(league)
    if ov.height:
        assert ov.select(pid).is_duplicated().sum() == 0, f"{league}: a provider id twice in player_overrides.csv"
        have = ov.join(rows.group_by(pid).agg(pl.col("athlete_id").drop_nulls().unique().alias("_a")), on=pid, how="left")
        noop = have.filter(
            pl.col("_a").is_null()  # the id is in no leg
            | (pl.col("athlete_id").is_null() & (pl.col("_a").list.len() == 0))  # already unmatched
            | ((pl.col("_a").list.len() == 1) & (pl.col("_a").list.first() == pl.col("athlete_id"))))  # fmt: skip
        assert noop.height == 0, f"{league}: player overrides that change nothing: {noop.select(pid).rows()}"
        rows = rows.join(ov.select(*pid, pl.col("athlete_id").alias("_ov"), pl.lit(True).alias("_o")), on=pid, how="left")
        o = pl.col("_o").fill_null(False)
        rows = rows.with_columns(
            pl.when(o).then(pl.col("_ov")).otherwise("athlete_id").alias("athlete_id"),
            pl.when(o).then(pl.lit("override")).otherwise("match_method").alias("match_method"),
            pl.when(o).then(pl.lit(None, pl.Float64)).otherwise("match_confidence").alias("match_confidence"),
            pl.when(o & pl.col("_ov").is_null()).then(pl.lit("curated override: no ESPN athlete"))
            .when(o).then(None).otherwise("unmatched_reason").alias("unmatched_reason"),
            pl.when(o).then(pl.lit("curated/player_overrides.csv")).otherwise("evidence").alias("evidence"),
        ).drop("_ov", "_o")  # fmt: skip

    drops: list[pl.DataFrame] = [rows.filter(pl.col("athlete_id").is_null())]
    rows = rows.filter(pl.col("athlete_id").is_not_null())

    def drop(mask: pl.Expr, reason: str) -> None:
        nonlocal rows
        drops.append(rows.filter(mask).with_columns(pl.lit(reason).alias("unmatched_reason")))
        rows = rows.filter(~mask)

    def in_hub() -> pl.Expr:
        return pl.struct("season", "team_id").is_in(hub.to_struct().implode())

    # 2. the team block: an ESPN team-season of the league, and the team the NCAA / provider team says
    drop(pl.col("team_id").is_null(), "no ESPN team")
    drop(~in_hub(), f"team-season not in {league}_team_group_seasons (ESPN)")
    if (xw := _ncaa_teams(league)) is not None:
        assert not xw.select("season", "source_team_id").is_duplicated().any(), f"{league}: an NCAA team on two ESPN teams"
        rows = rows.join(xw, on=["season", "source_team_id"], how="left")
        drop((pl.col("source") == "ncaa") & (pl.col("xw_team").is_null() | (pl.col("xw_team") != pl.col("team_id"))),
             f"the NCAA team's ESPN team disagrees with {league}_crosswalk ncaa_espn_team_crosswalk")  # fmt: skip
        rows = rows.drop("xw_team")
    if league == "nfl":
        ta = team_aliases.filter(pl.col("source").is_in(NFL_LEGS) & pl.col("source_id").is_not_null()).select(
            "source", pl.col("source_id").alias("source_team_id"), pl.col("team_id").alias("ta_team"),
            pl.int_ranges("valid_from", pl.col("valid_to") + 1).alias("season")).explode("season").with_columns(
            pl.col("season").cast(pl.Int32))  # fmt: skip
        assert not ta.select("source", "source_team_id", "season").is_duplicated().any(), "nfl: a provider team on two ESPN teams"
        rows = rows.join(ta, on=["source", "source_team_id", "season"], how="left")
        drop(pl.col("ta_team").is_null() | (pl.col("ta_team") != pl.col("team_id")),
             f"the provider team is not this ESPN team in {league}_team_aliases")  # fmt: skip
        rows = rows.drop("ta_team")

    # 3. one athlete per provider id (every season), one id per (season, team, source, athlete); an override is never
    # dropped here: if it breaks either rule, validate_player_aliases fails the build
    def conflicted(df: pl.DataFrame) -> pl.DataFrame:
        two_ath = df.group_by(pid).agg(pl.col("athlete_id").n_unique().alias("n")).filter(pl.col("n") > 1).select(pid)
        two_ids = df.group_by("season", "team_id", "source", "athlete_id").agg(
            pl.col("source_player_id").n_unique().alias("n")).filter(pl.col("n") > 1)  # fmt: skip
        return pl.concat([two_ath, df.join(two_ids, on=["season", "team_id", "source", "athlete_id"], how="semi").select(pid)]).unique()

    conflict = "conflict: a provider id on two ESPN athletes, or one of two ids an athlete carries on a team in a season"
    drop(pl.struct(pid).is_in(conflicted(rows).to_struct().implode()) & (pl.col("match_method") != "override"), conflict)
    # carry an established pair to the same provider id's misses in other seasons
    pairs = rows.select(*pid, "athlete_id").unique()
    misses = pl.concat(drops, how="diagonal_relaxed").filter(pl.col("athlete_id").is_null() & pl.col("team_id").is_not_null())
    carried = (
        misses.drop("athlete_id").join(pairs, on=pid, how="inner").filter(in_hub())
        .join(rows.select("season", "source", "athlete_id"), on=["season", "source", "athlete_id"], how="anti")
        .with_columns(pl.lit("propagated").alias("match_method"), pl.lit(None, pl.Float64).alias("match_confidence"),
                      pl.lit(None, pl.Utf8).alias("unmatched_reason"))
        .select(list(LONG))
    )  # fmt: skip
    # two carried ids landing on one athlete-team-season stay misses
    carried = carried.join(conflicted(carried), on=pid, how="anti")
    rows = pl.concat([rows, carried])
    left = pl.concat(drops, how="diagonal_relaxed").select(list(LONG)).join(carried.select("season", *pid), on=["season", *pid], how="anti")

    keys = [
        "athlete_id",
        "source",
        "source_player_id",
        "team_id",
        "source_team_id",
        "match_method",
    ]
    aliases = (
        _windows(rows, keys, pl.col("match_confidence").min().alias("match_confidence"), pl.col("evidence").min())
        .with_columns(pl.lit(league).alias("league"), pl.lit("espn").alias("athlete_id_source"))
        .select(list(PLAYER_ALIASES)).cast(PLAYER_ALIASES)
        .sort(["athlete_id", "source", "source_player_id", "valid_from", "team_id", "source_team_id"], nulls_last=True)
    )  # fmt: skip
    ukeys = [
        "source",
        "source_player_id",
        "source_team_id",
        "team_id",
        "athlete_id",
        "unmatched_reason",
    ]
    unmatched = (
        _windows(left, ukeys, pl.col("evidence").min())
        .rename({"valid_from": "season_from", "valid_to": "season_to", "unmatched_reason": "reason"})
        .with_columns(pl.lit(league).alias("league"))
        .select(list(UNMATCHED)).cast(UNMATCHED)
        .sort(list(UNMATCHED), nulls_last=True)
    )  # fmt: skip
    matched = rows.group_by("source", "season").agg(pl.len().alias("n_matched"))
    rates = (
        total.group_by("source", "season").agg(pl.len().alias("n_rows"))
        .join(matched, on=["source", "season"], how="left").fill_null(0)
        .with_columns(pl.lit(league).alias("league"), (100 * pl.col("n_matched") / pl.col("n_rows")).round(2).alias("match_rate"))
        .select(list(RATES)).cast(RATES).sort("source", "season")
    )  # fmt: skip
    return aliases, unmatched, rates


def _windows(df: pl.DataFrame, keys: list[str], *aggs: pl.Expr) -> pl.DataFrame:
    """Per-season rows -> runs of consecutive seasons (valid_from / valid_to)."""
    return (
        df.sort(*keys, "season", nulls_last=True)
        .with_columns(
            (pl.col("season") - pl.col("season").rank("dense").over(keys)).alias("_run")
        )
        .group_by(*keys, "_run")
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
            *aggs,
        )
        .drop("_run")
    )


def gate(league: str, rates: pl.DataFrame) -> list[str]:
    """Seasons whose match rate sits below its curated floor."""
    floors = pl.read_csv(CURATED / "player_match_floors.csv", schema_overrides={
        "season_from": pl.Int32, "season_to": pl.Int32, "floor_pct": pl.Float64}).filter(pl.col("league") == league)  # fmt: skip
    low = rates.join(floors, on=["league", "source"]).filter(
        pl.col("season").is_between("season_from", "season_to") & (pl.col("match_rate") < pl.col("floor_pct")))  # fmt: skip
    return [
        f"{league} {s} {y}: match rate {r} < floor {f}"
        for s, y, r, f in low.select(
            "source", "season", "match_rate", "floor_pct"
        ).iter_rows()
    ]


def write(
    league: str, tgs: pl.DataFrame, team_aliases: pl.DataFrame, out_dir: Path
) -> tuple[list[Path], pl.DataFrame, pl.DataFrame]:
    aliases, unmatched, rates = build(league, tgs, team_aliases)
    problems = validate_player_aliases(league, aliases, tgs) + gate(league, rates)
    if problems:
        raise ValueError(
            f"{league}_player_aliases fails the contract or its match-rate floors:\n  "
            + "\n  ".join(problems)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [out_dir / f"{league}_player_aliases.parquet", out_dir / f"{league}_player_aliases.csv",
             out_dir / f"{league}_player_aliases_unmatched.csv", out_dir / f"{league}_player_aliases_rates.csv"]  # fmt: skip
    aliases.write_parquet(files[0])
    aliases.write_csv(files[1])
    unmatched.write_csv(files[2])
    rates.write_csv(files[3])
    return files, aliases, rates
