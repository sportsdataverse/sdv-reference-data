"""College football groups: CFBD affiliation spells for membership, ESPN's group walk for ids and a cross-check.

    uv run python -m sdv_reference.leagues.cfb fetch   # network: refresh the raw/cfb/ snapshots
    uv run python -m sdv_reference.build cfb            # offline: raw/cfb/ + curated/cfb_*.csv -> build/cfb/

Rules (research notes 2026-09-26, 01-espn-groups and 03-cfb-cfbd):
- Membership: CFBD /conferences/affiliations spells, 1869-present. CFBD team id == ESPN team id.
- Lineage: CFBD mints a new conference id on every rename or tier change. curated/cfb_lineage.csv folds those ids
  into SDV lineages; any id it doesn't list becomes `cfb:{slug of CFBD's name}`, which also folds CFBD's tier
  splits of one body (Big Sky 20/284/285/286, Southern 29/206/362/363, ...).
- Names as of each season: the CFBD id most members used that season, overridden by curated/cfb_group_names.csv.
- Subdivision: CFBD classification by era (curated/cfb_subdivision_eras.csv); CFBD back-applies today's tiers.
- ESPN: group ids as aliases only (its labels and parents are today's). Its group team lists cross-check membership
  from 2014 (sources_agree) and fill D-I programs CFBD drops (2020 opt-outs); the 2001-12 lists are not used.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import polars as pl
import requests

from sdv_reference.schema import conform

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw" / "cfb"
CURATED = ROOT / "curated"
CFBD = "https://api.collegefootballdata.com"
# the published espn_cfb_teams build (season x team universe), snapshotted by fetch()
ESPN_TEAMS_DIR = Path(
    os.environ.get(
        "SDV_CFB_TEAMS_DIR", "/mnt/sdv_repos/cfbfastR-cfb-data/cfb/cfb_teams/parquet"
    )
)
ESPN_SUBDIVISIONS = {"80": "cfb:fbs", "81": "cfb:fcs"}
ESPN_PSEUDO_GROUPS = {"51"}  # "All-star Bowls", 2014-15
CROSS_CHECK_FROM = 2014  # ESPN group lists are anachronistic before ~2013 (note 03 §5)
COMPLETE_FROM = 2014  # CFBD holds every D-II/III program only from 2014 (note 03 §1)
D1 = ["cfb:fbs", "cfb:fcs"]


# ---------------------------------------------------------------------------------------------- io


def _cfbd_key() -> str:
    """CFBD_API_KEY from the environment or ~/.Renviron, read at call time and never echoed."""
    key = os.environ.get("CFBD_API_KEY")
    renviron = Path.home() / ".Renviron"
    if not key and renviron.exists():
        for line in renviron.read_text().splitlines():
            if line.startswith("CFBD_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise RuntimeError("CFBD_API_KEY is not set in the environment or ~/.Renviron")
    return key


def _write_json_gz(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(
        path, "wb", mtime=0
    ) as f:  # mtime=0: same bytes for the same data
        f.write(json.dumps(obj, sort_keys=True, indent=0).encode())


def _read_json_gz(path: Path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def fetch() -> None:
    """Snapshot the sources into raw/cfb/: two CFBD calls (every season each), ESPN's FBS/FCS group walk for the
    refresh.seasons_to_fetch() seasons (the rest of raw/ is kept; the 2001-2026 base is the 2026-09-26 research
    walk), and ESPN's team universe. The manifest carries no fetch timestamp: git log dates the snapshot."""
    from sdv_reference.espn import CORE
    from sdv_reference.leagues.mlb import espn_walk

    headers = {"Authorization": f"Bearer {_cfbd_key()}", "Accept": "application/json"}
    manifest = {"files": {}}
    for name, path in [
        ("cfbd_conferences", "/conferences"),
        ("cfbd_affiliations", "/conferences/affiliations"),
    ]:
        r = requests.get(CFBD + path, headers=headers, timeout=120)
        r.raise_for_status()
        _write_json_gz(RAW / f"{name}.json.gz", r.json())
        manifest["files"][f"{name}.json.gz"] = {
            "source": CFBD + path,
            "rows": len(r.json()),
        }
    # ESPN seasons stop at build()'s last season: an ESPN list for a season CFBD lacks would be all "missing" teams
    aff = _read_json_gz(RAW / "cfbd_affiliations.json.gz")
    last = max(y for r in aff for y in (r["startYear"], r["endYear"]) if y is not None)
    espn = espn_walk(
        "cfb",
        list(range(2001, last + 1)),
        RAW / "espn_groups.json.gz",
        roots=tuple(ESPN_SUBDIVISIONS),
    )
    manifest["files"]["espn_groups.json.gz"] = {
        "source": f"{CORE}/football/leagues/college-football/seasons/{{season}}/types/2/groups/{{80,81}}",
        "rows": len(espn),
    }
    teams = (
        pl.scan_parquet(ESPN_TEAMS_DIR / "cfb_teams_*.parquet")
        .select(pl.col("season").cast(pl.Int32), pl.col("team_id").cast(pl.Utf8))
        .unique()
        .sort("season", "team_id")
        .collect()
    )
    with gzip.GzipFile(RAW / "espn_teams.csv.gz", "wb", mtime=0) as f:
        teams.write_csv(f)
    manifest["files"]["espn_teams.csv.gz"] = {
        "source": str(ESPN_TEAMS_DIR),
        "rows": teams.height,
    }
    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for k, v in manifest["files"].items():
        print(f"{k}: {v['rows']} rows", flush=True)


# ------------------------------------------------------------------------------------------ helpers


def _slug(e: pl.Expr) -> pl.Expr:
    return (
        e.str.to_lowercase()
        .str.replace_all("&", "and", literal=True)
        .str.replace_all("'", "", literal=True)
        .str.replace_all(r"[^a-z0-9]+", "-")
        .str.strip_chars("-")
    )


def _in_window(lo: str = "valid_from", hi: str = "valid_to") -> pl.Expr:
    return (pl.col("season") >= pl.col(lo).fill_null(-9999)) & (
        pl.col("season") <= pl.col(hi).fill_null(9999)
    )


def _curated(name: str) -> pl.DataFrame:
    df = pl.read_csv(CURATED / f"cfb_{name}.csv", infer_schema_length=0)
    types = {
        "cfbd_conference_id": pl.Int64,
        "valid_from": pl.Int32,
        "valid_to": pl.Int32,
    }
    return df.with_columns(
        [pl.col(c).cast(t) for c, t in types.items() if c in df.columns]
    )


def _ranges(df: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """Collapse (keys, season) rows into contiguous (keys, valid_from, valid_to) runs."""
    df = df.select(keys + ["season"]).unique().sort(keys + ["season"])
    brk = (pl.col("season").diff().over(keys) != 1).fill_null(True)
    return (
        df.with_columns(brk.cum_sum().alias("_run"))
        .group_by(keys + ["_run"])
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
        )
        .drop("_run")
    )


def _fmt(seasons) -> str:
    """1978, 1979, 1980, 1990 -> '1978-1980, 1990'."""
    s = sorted(set(seasons))
    out, lo = [], None
    for i, y in enumerate(s):
        lo = y if lo is None else lo
        if i + 1 == len(s) or s[i + 1] != y + 1:
            out.append(str(lo) if lo == y else f"{lo}-{y}")
            lo = None
    return ", ".join(out)


def _mode(df: pl.DataFrame, key: str, val: str) -> pl.DataFrame:
    """The most common non-null `val` per (key, season); ties go to the smallest value."""
    return (
        df.filter(pl.col(val).is_not_null())
        .group_by(key, "season", val)
        .len()
        .sort([key, "season", "len", val], descending=[False, False, True, False])
        .group_by(key, "season", maintain_order=True)
        .first()
        .drop("len")
    )


# ------------------------------------------------------------------------------------------- build


def _team_seasons(
    aff: pl.DataFrame, conf_gid: dict[int, str], eras: pl.DataFrame, last: int
) -> pl.DataFrame:
    tm = (
        aff.with_columns(
            pl.int_ranges("startYear", pl.col("endYear").fill_null(last) + 1).alias(
                "season"
            )
        )
        .explode("season", empty_as_null=False)
        .select(
            pl.col("season").cast(pl.Int32),
            pl.col("teamId").cast(pl.Utf8).alias("team_id"),
            pl.col("team").alias("team_name"),
            pl.col("conferenceId").alias("cfbd_id"),
            "classification",
            pl.col("conferenceDivision").alias("cfbd_division"),
            pl.lit("cfbd").alias("source"),
        )
    )
    dup = tm.filter(tm.select("team_id", "season").is_duplicated())
    if dup.height:
        raise ValueError(f"CFBD spells overlap: {dup.head(3).rows()}")
    tier = (
        tm.select("classification", "season")
        .unique()
        .join(
            eras.select("classification", "valid_from", "valid_to", "subdivision_id"),
            on="classification",
        )
        .filter(_in_window())
        .select("classification", "season", "subdivision_id")
    )
    if tier.select("classification", "season").is_duplicated().any():
        raise ValueError("curated/cfb_subdivision_eras.csv has overlapping windows")
    return (
        tm.join(tier, on=["classification", "season"], how="left")
        .with_columns(
            pl.col("cfbd_id")
            .replace_strict(conf_gid, return_dtype=pl.Utf8)
            .alias("conference_id"),
        )
        .with_columns(
            pl.when(pl.col("cfbd_division").is_not_null())
            .then(pl.col("conference_id") + "-" + _slug(pl.col("cfbd_division")))
            .alias("division_id")
        )
    )


def _apply_membership_overrides(tm: pl.DataFrame, ov: pl.DataFrame) -> pl.DataFrame:
    """Verified corrections to CFBD's membership (curated/cfb_membership_overrides.csv), each proved from that
    season's schedule. An overridden row takes its new group's CFBD conference id (the one that group's other
    members carry that season), so group labels stay the group's own; `_override_note` carries the citation."""
    ov = ov.select(
        pl.col("season").cast(pl.Int32),
        pl.col("team_id").cast(pl.Utf8),
        pl.col("conference_id").alias("_ov_conf"),
        pl.col("division_id").alias("_ov_div"),
        pl.col("notes").alias("_override_note"),
    )
    stale = ov.join(tm, on=["season", "team_id"], how="anti")
    if stale.height:
        raise ValueError(f"cfb membership overrides match no CFBD team-season: {stale.rows()}")
    hit = pl.col("_ov_conf").is_not_null()
    tm = tm.join(ov, on=["season", "team_id"], how="left")
    peers = (
        tm.filter(~hit)
        .group_by("conference_id", "season")
        .agg(pl.col("cfbd_id").mode().first().alias("_peer_cfbd"))
        .rename({"conference_id": "_ov_conf"})
    )
    tm = tm.join(peers, on=["_ov_conf", "season"], how="left")
    if tm.filter(hit & pl.col("_peer_cfbd").is_null()).height:
        raise ValueError("a cfb membership override points at a group with no other members that season")
    return tm.with_columns(
        pl.when(hit).then(pl.col("_ov_conf")).otherwise(pl.col("conference_id")).alias("conference_id"),
        pl.when(hit).then(pl.col("_ov_div")).otherwise(pl.col("division_id")).alias("division_id"),
        pl.when(hit).then(pl.col("_peer_cfbd")).otherwise(pl.col("cfbd_id")).alias("cfbd_id"),
    ).drop("_ov_conf", "_ov_div", "_peer_cfbd")


def _group_seasons(
    tm: pl.DataFrame, conf: pl.DataFrame, names: pl.DataFrame
) -> pl.DataFrame:
    keys = ["group_id", "season"]
    labels = conf.select(
        pl.col("id").alias("cfbd_id"),
        pl.col("shortName").alias("name"),  # CFBD's shortName is the long name
        pl.col("name").alias("short_name"),
        "abbreviation",
    )
    cs = (
        tm.group_by("conference_id", "season")
        .agg(pl.len().alias("n_teams"))
        .join(_mode(tm, "conference_id", "cfbd_id"), on=["conference_id", "season"])
        .join(
            _mode(tm, "conference_id", "subdivision_id").rename(
                {"subdivision_id": "parent_group_id"}
            ),
            on=["conference_id", "season"],
            how="left",
        )
        .join(labels, on="cfbd_id", how="left")
        .rename({"conference_id": "group_id"})
        .with_columns(pl.lit("conference").alias("level"))
    )
    ds = (
        tm.filter(pl.col("division_id").is_not_null())
        .group_by("division_id", "season")
        .agg(
            pl.len().alias("n_teams"),
            pl.col("conference_id").first().alias("parent_group_id"),
            pl.col("cfbd_division").drop_nulls().first().alias("short_name"),
        )
        .rename({"division_id": "group_id"})
        .with_columns(pl.lit("division").alias("level"))
    )
    ss = (
        tm.filter(pl.col("subdivision_id").is_not_null())
        .group_by("subdivision_id", "season")
        .agg(pl.len().alias("n_teams"))
        .rename({"subdivision_id": "group_id"})
        .with_columns(pl.lit("subdivision").alias("level"))
    )
    cols = [
        "group_id",
        "season",
        "level",
        "name",
        "short_name",
        "abbreviation",
        "parent_group_id",
        "n_teams",
    ]
    gs = pl.concat([_cast_cols(df, cols) for df in (ss, cs, ds)])
    hit = (
        gs.select(keys)
        .join(names, on="group_id")
        .filter(_in_window())
        .select(
            *keys,
            pl.lit(True).alias("_hit"),
            pl.col("name").alias("_name"),
            pl.col("short_name").alias("_short"),
            pl.col("abbreviation").alias("_abbr"),
        )
    )
    if hit.select(keys).is_duplicated().any():
        raise ValueError("curated/cfb_group_names.csv has overlapping windows")
    gs = gs.join(hit, on=keys, how="left").with_columns(
        pl.when(pl.col("_hit"))
        .then(pl.col("_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
        pl.when(pl.col("_hit"))
        .then(pl.col("_short"))
        .otherwise(pl.col("short_name"))
        .alias("short_name"),
        pl.when(pl.col("_hit"))
        .then(pl.col("_abbr"))
        .otherwise(pl.col("abbreviation"))
        .alias("abbreviation"),
    )
    # a division is named after its conference as of that season: "Big Ten Legends", "Pac-10" has none
    conf_short = gs.filter(pl.col("level") == "conference").select(
        pl.col("group_id").alias("parent_group_id"),
        "season",
        pl.col("short_name").alias("_conf"),
    )
    gs = (
        gs.join(conf_short, on=["parent_group_id", "season"], how="left")
        .with_columns(
            pl.when(pl.col("level") == "division")
            .then(pl.col("_conf") + " " + pl.col("short_name"))
            .otherwise(pl.col("name"))
            .alias("name")
        )
        .select(cols)
    )
    if gs["name"].null_count():
        raise ValueError(
            f"unnamed group-seasons: {gs.filter(pl.col('name').is_null()).head(3).rows()}"
        )
    return gs


def _cast_cols(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    types = {"season": pl.Int32, "n_teams": pl.Int32}
    return df.select(
        [
            (pl.col(c) if c in df.columns else pl.lit(None))
            .cast(types.get(c, pl.Utf8))
            .alias(c)
            for c in cols
        ]
    )


def _espn_crosswalk(
    tm: pl.DataFrame,
    gs: pl.DataFrame,
    conf: pl.DataFrame,
    conf_gid: dict[int, str],
    walk: pl.DataFrame,
    overrides: pl.DataFrame,
) -> pl.DataFrame:
    """(group_id, season, espn_id) for every ESPN group ESPN had that season and SDV can name."""
    present = walk.select(
        pl.col("id").alias("espn_id"), pl.col("season").cast(pl.Int32)
    )
    espn_confs = walk.filter(pl.col("isConference"))["id"].unique().to_list()
    d1_ids = conf.filter(pl.col("classification").is_in(["fbs", "fcs"]))["id"].to_list()
    pairs = tm.select("conference_id", "season", "cfbd_id").unique()
    ov = overrides.filter(pl.col("cfbd_division").is_null())
    by_override = (
        pairs.join(ov, left_on="cfbd_id", right_on="cfbd_conference_id")
        .filter(_in_window())
        .select(
            "conference_id",
            "season",
            "cfbd_id",
            pl.col("espn_group_id").alias("espn_id"),
        )
    )
    by_identity = (
        pairs.join(by_override, on=["cfbd_id", "season"], how="anti")
        .filter(pl.col("cfbd_id").is_in(d1_ids))
        .with_columns(pl.col("cfbd_id").cast(pl.Utf8).alias("espn_id"))
        .filter(pl.col("espn_id").is_in(espn_confs))
    )
    confs = (
        pl.concat([by_override, by_identity])
        .join(present, on=["espn_id", "season"])
        .select(pl.col("conference_id").alias("group_id"), "season", "espn_id")
        .unique()
    )
    # divisions: ESPN names them "{conference} - {division}"; the Big Ten's 52/53 held Legends/Leaders in 2011-13
    ovd = overrides.filter(pl.col("cfbd_division").is_not_null()).with_columns(
        (
            pl.col("cfbd_conference_id")
            .cast(pl.Int64)
            .replace_strict(conf_gid, return_dtype=pl.Utf8)
            + "-"
            + _slug(pl.col("cfbd_division"))
        ).alias("_div")
    )
    divs = (
        walk.filter(
            ~pl.col("isConference")
            & ~pl.col("id").is_in(list(ESPN_SUBDIVISIONS) + list(ESPN_PSEUDO_GROUPS))
        )
        .select(
            pl.col("id").alias("espn_id"),
            pl.col("season").cast(pl.Int32),
            "parent",
            "name",
        )
        .join(
            confs.rename({"espn_id": "parent", "group_id": "_conf"}),
            on=["parent", "season"],
        )
        .join(
            ovd.select(
                pl.col("espn_group_id").alias("espn_id"),
                "valid_from",
                "valid_to",
                "_div",
            ),
            on="espn_id",
            how="left",
        )
        .with_columns(pl.when(_in_window()).then(pl.col("_div")).alias("_div"))
        .sort("espn_id", "season", "_div", nulls_last=True)
        .unique(["espn_id", "season"], keep="first")
        .with_columns(
            pl.coalesce(
                "_div",
                pl.col("_conf")
                + "-"
                + _slug(pl.col("name").str.split(" - ").list.last()),
            ).alias("group_id")
        )
        .select("group_id", "season", "espn_id")
    )
    subs = present.filter(
        pl.col("espn_id").is_in(list(ESPN_SUBDIVISIONS))
    ).with_columns(
        pl.col("espn_id").replace_strict(ESPN_SUBDIVISIONS).alias("group_id")
    )
    xw = pl.concat([confs, divs, subs.select("group_id", "season", "espn_id")]).join(
        gs.select("group_id", "season"), on=["group_id", "season"]
    )
    clash = xw.filter(xw.select("espn_id", "season").is_duplicated())
    if clash.height:
        raise ValueError(
            f"one ESPN group maps to two SDV groups: {clash.sort('season').head(4).rows()}"
        )
    return xw


def _espn_leaves(walk: pl.DataFrame) -> pl.DataFrame:
    """(season, team_id, espn_leaf, espn_conf, espn_div) from ESPN's group team lists, 2014 on."""
    return (
        walk.filter(
            pl.col("team_ids").is_not_null()
            & (pl.col("season") >= CROSS_CHECK_FROM)
            & ~pl.col("id").is_in(list(ESPN_PSEUDO_GROUPS))
        )
        .select(
            pl.col("season").cast(pl.Int32),
            pl.col("id").alias("espn_leaf"),
            pl.when(pl.col("isConference")).then(pl.col("id")).otherwise(pl.col("parent")).alias("espn_conf"),
            pl.when(~pl.col("isConference")).then(pl.col("id")).alias("espn_div"),
            pl.col("team_ids").alias("team_id"),
        )
        .explode("team_id", empty_as_null=False)
    )


def _espn_fill(tm: pl.DataFrame, walk: pl.DataFrame, xw: pl.DataFrame) -> pl.DataFrame:
    """D-I team-seasons in ESPN's 2014+ group lists with no CFBD spell at all. In practice these are the 2020
    programs that sat the season out (CFBD drops them); ESPN still lists them with their conference."""
    missing = _espn_leaves(walk).join(tm.select("season", "team_id"), on=["season", "team_id"], how="anti")
    near = (pl.col("season") - pl.col("_s")).abs()
    keys = ["season", "team_id"]
    conf = (  # the ESPN conference's SDV lineage that season, else in the nearest season it maps
        missing.join(xw.rename({"espn_id": "espn_conf", "season": "_s", "group_id": "conference_id"}), on="espn_conf")
        .sort(near, "_s")
        .unique(keys, keep="first", maintain_order=True)
        .drop("_s")
    )
    cfbd = (  # CFBD id and name from the team's nearest spell in the same lineage
        conf.join(tm.select("team_id", "conference_id", pl.col("season").alias("_s"), "cfbd_id", "team_name"), on=["team_id", "conference_id"], how="left")
        .sort(near, "_s", nulls_last=True)
        .unique(keys, keep="first", maintain_order=True)
    )
    root = walk.select(pl.col("id").alias("espn_conf"), pl.col("season").cast(pl.Int32), "parent")
    return (
        cfbd.join(xw.rename({"espn_id": "espn_div", "group_id": "division_id"}), on=["espn_div", "season"], how="left")
        .join(root, on=["espn_conf", "season"], how="left")
        .with_columns(
            pl.col("parent").replace_strict(ESPN_SUBDIVISIONS, default=None).alias("subdivision_id"),
            pl.lit("espn").alias("source"),
        )
        .select("season", "team_id", "team_name", "cfbd_id", "subdivision_id", "conference_id", "division_id", "source")
    )


def _cross_check(
    tm: pl.DataFrame, walk: pl.DataFrame, xw: pl.DataFrame, espn_teams: pl.DataFrame
) -> pl.DataFrame:
    """sources_agree from ESPN's D-I group lists (2014+), plus notes on rows the sources dispute."""
    to_sdv = xw.select("espn_id", "season", "group_id")
    lists = (
        _espn_leaves(walk)
        .join(
            to_sdv.rename({"espn_id": "espn_conf", "group_id": "espn_conference_id"}),
            on=["espn_conf", "season"],
            how="left",
        )
        .join(
            to_sdv.rename({"espn_id": "espn_div", "group_id": "espn_division_id"}),
            on=["espn_div", "season"],
            how="left",
        )
    )
    if lists.select("team_id", "season").is_duplicated().any():
        raise ValueError("a team sits in two ESPN leaf groups in one season")
    checked = lists["season"].unique().to_list()
    in_espn = espn_teams.with_columns(pl.lit(True).alias("_in_espn"))
    tm = tm.join(lists, on=["season", "team_id"], how="left").join(
        in_espn, on=["season", "team_id"], how="left"
    )
    listed = pl.col("espn_leaf").is_not_null()
    conf_ok = pl.col("espn_conference_id") == pl.col("conference_id")
    div_ok = (
        pl.col("espn_division_id").is_null()
        | pl.col("division_id").is_null()
        | (pl.col("espn_division_id") == pl.col("division_id"))
    )
    agree = (
        pl.when(~pl.col("season").is_in(checked) | (pl.col("source") == "espn"))
        .then(None)
        .when(listed)
        .then((conf_ok & div_ok).fill_null(False))
        .when(pl.col("subdivision_id").is_in(D1))
        .then(False)
    )
    espn_says = pl.coalesce(
        pl.col("espn_division_id"),
        pl.col("espn_conference_id"),
        pl.lit("unmapped group ") + pl.col("espn_leaf"),
    )
    note_agree = pl.when(pl.col("sources_agree") == False).then(
        pl.when(listed)
        .then(pl.lit("ESPN group lists put it in ") + espn_says)
        .otherwise(pl.lit("missing from ESPN's D-I group lists"))
    )
    note_stale = pl.when(
        (pl.col("season") >= CROSS_CHECK_FROM) & pl.col("_in_espn").is_null()
    ).then(
        pl.lit(
            "not in ESPN's team list this season; the CFBD spell may predate the program or outlive it"
        )
    )
    note_fill = pl.when(pl.col("source") == "espn").then(
        pl.lit("no CFBD spell this season (did not play); membership from ESPN's group list")
    )
    note_tier = pl.when(
        pl.col("subdivision_id").is_null() & (pl.col("season") >= 1956)
    ).then(
        pl.lit("CFBD classification ")
        + pl.col("classification")
        + " has no NCAA tier this season"
    )
    return (
        tm.with_columns(agree.alias("sources_agree"))
        .with_columns(
            pl.concat_str(
                [note_fill, note_agree, note_stale, note_tier], separator="; ", ignore_nulls=True
            ).alias("notes")
        )
        .with_columns(
            pl.when(pl.col("notes") != "").then(pl.col("notes")).alias("notes")
        )
    )


def _aliases(
    tm: pl.DataFrame,
    gs: pl.DataFrame,
    conf: pl.DataFrame,
    walk: pl.DataFrame,
    xw: pl.DataFrame,
) -> pl.DataFrame:
    kinds = ["name", "short_name", "abbreviation"]
    sdv = (
        gs.unpivot(
            index=["group_id", "season"],
            on=kinds,
            variable_name="name_kind",
            value_name="value",
        )
        .drop_nulls("value")
        .pipe(_ranges, ["group_id", "name_kind", "value"])
        .with_columns(
            pl.lit("sdv").alias("source"), pl.lit(None, pl.Utf8).alias("source_id")
        )
    )
    labels = conf.select(
        pl.col("id").alias("cfbd_id"),
        pl.col("shortName").alias("name"),
        pl.col("name").alias("short_name"),
        "abbreviation",
    )
    cfbd_conf = (
        _ranges(tm.rename({"conference_id": "group_id"}), ["group_id", "cfbd_id"])
        .join(labels, on="cfbd_id")
        .unpivot(
            index=["group_id", "cfbd_id", "valid_from", "valid_to"],
            on=kinds,
            variable_name="name_kind",
            value_name="value",
        )
        .drop_nulls("value")
        .with_columns(
            pl.lit("cfbd").alias("source"),
            pl.col("cfbd_id").cast(pl.Utf8).alias("source_id"),
        )
    )
    cfbd_div = _ranges(
        tm.filter(pl.col("division_id").is_not_null() & pl.col("cfbd_division").is_not_null()).rename(
            {"division_id": "group_id", "cfbd_division": "value"}
        ),
        ["group_id", "value"],
    ).with_columns(
        pl.lit("cfbd").alias("source"),
        pl.lit(None, pl.Utf8).alias("source_id"),
        pl.lit("short_name").alias("name_kind"),
    )
    cfbd_tier = _ranges(
        tm.filter(pl.col("subdivision_id").is_not_null() & pl.col("classification").is_not_null()).rename(
            {"subdivision_id": "group_id", "classification": "value"}
        ),
        ["group_id", "value"],
    ).with_columns(
        pl.lit("cfbd").alias("source"),
        pl.lit(None, pl.Utf8).alias("source_id"),
        pl.lit("code").alias("name_kind"),
    )
    espn_labels = (
        walk.sort("season")
        .unique("id", keep="last")
        .select(
            pl.col("id").alias("source_id"),
            "name",
            pl.col("shortName").alias("short_name"),
            "abbreviation",
            "slug",
        )
    )
    espn = (
        _ranges(xw.rename({"espn_id": "source_id"}), ["group_id", "source_id"])
        .join(espn_labels, on="source_id")
        .unpivot(
            index=["group_id", "source_id", "valid_from", "valid_to"],
            on=kinds + ["slug"],
            variable_name="name_kind",
            value_name="value",
        )
        .drop_nulls("value")
        .with_columns(pl.lit("espn").alias("source"))
    )
    cols = [
        "group_id",
        "source",
        "source_id",
        "name_kind",
        "value",
        "valid_from",
        "valid_to",
    ]
    return pl.concat(
        [df.select(cols) for df in (sdv, cfbd_conf, cfbd_div, cfbd_tier, espn)]
    )


def _groups(
    tm: pl.DataFrame,
    gs: pl.DataFrame,
    lineage: pl.DataFrame,
    eras: pl.DataFrame,
    names: pl.DataFrame,
) -> pl.DataFrame:
    notes: dict[str, list[str]] = {}

    def add(gid: str, text: str) -> None:
        if text and text not in notes.setdefault(gid, []):
            notes[gid].append(text)

    for gid, text in (
        lineage.drop_nulls("notes").select("group_id", "notes").iter_rows()
    ):
        add(gid, text)
    for gid, text in (
        eras.drop_nulls("notes").select("subdivision_id", "notes").iter_rows()
    ):
        add(gid, text)
    for gid, text in names.drop_nulls("notes").select("group_id", "notes").iter_rows():
        add(gid, text)
    ids = (
        tm.drop_nulls("cfbd_id")
        .group_by("conference_id", "cfbd_id")
        .agg(pl.col("season").min().alias("lo"), pl.col("season").max().alias("hi"))
        .sort("conference_id", "lo", "cfbd_id")
    )
    for gid, part in ids.group_by("conference_id", maintain_order=True):
        add(
            gid[0],
            "CFBD conference ids: "
            + ", ".join(f"{c} ({lo}-{hi})" for _, c, lo, hi in part.iter_rows()),
        )
    mixed = (
        tm.drop_nulls("subdivision_id")
        .group_by("conference_id", "season")
        .agg(pl.col("subdivision_id").n_unique().alias("k"))
        .filter(pl.col("k") > 1)
        .group_by("conference_id")
        .agg("season")
    )
    for gid, seasons in mixed.iter_rows():
        add(
            gid,
            f"Members span subdivisions in {_fmt(seasons)}; the parent is the majority tier.",
        )
    lower = tm.filter(
        ~pl.col("subdivision_id").is_in(D1).fill_null(False)
        & (pl.col("season") < COMPLETE_FROM)
    )
    for col in ["subdivision_id", "conference_id", "division_id"]:
        for gid, seasons in (
            lower.drop_nulls(col).group_by(col).agg("season").iter_rows()
        ):
            add(
                gid,
                f"n_teams is a lower bound in {_fmt(seasons)}: before {COMPLETE_FROM} CFBD holds only the "
                "lower-tier programs it tracks for their later D-I history.",
            )
    return (
        gs.group_by("group_id")
        .agg(
            pl.col("level").first(),
            pl.col("season").min().alias("first_season"),
            pl.col("season").max().alias("last_season"),
        )
        .with_columns(
            pl.col("group_id")
            .map_elements(
                lambda g: " ".join(notes.get(g, [])) or None, return_dtype=pl.Utf8
            )
            .alias("notes")
        )
    )


def build() -> dict[str, pl.DataFrame]:
    """The four cfb tables, from raw/cfb/ and curated/cfb_*.csv only (offline, deterministic)."""
    conf = pl.DataFrame(
        _read_json_gz(RAW / "cfbd_conferences.json.gz"), infer_schema_length=None
    )
    aff = pl.DataFrame(
        _read_json_gz(RAW / "cfbd_affiliations.json.gz"), infer_schema_length=None
    )
    walk = pl.DataFrame(
        _read_json_gz(RAW / "espn_groups.json.gz"), infer_schema_length=None
    )
    espn_teams = pl.read_csv(
        RAW / "espn_teams.csv.gz", schema={"season": pl.Int32, "team_id": pl.Utf8}
    )
    lineage = _curated("lineage")
    eras = _curated("subdivision_eras")
    names = _curated("group_names")
    overrides = _curated("espn_overrides")

    last = int(max(aff["startYear"].max(), aff["endYear"].max()))
    curated_gid = dict(
        zip(lineage["cfbd_conference_id"].to_list(), lineage["group_id"].to_list())
    )
    default_gid = conf.select("id", (pl.lit("cfb:") + _slug(pl.col("name"))).alias("g"))
    conf_gid = {cid: curated_gid.get(cid, g) for cid, g in default_gid.iter_rows()}

    tm = _team_seasons(aff, conf_gid, eras, last)
    tm = _apply_membership_overrides(tm, _curated("membership_overrides"))
    gs = _group_seasons(tm, conf, names)
    xw = _espn_crosswalk(tm, gs, conf, conf_gid, walk, overrides)
    fill = _espn_fill(tm, walk, xw)
    if fill.height:  # rebuild with ESPN's rows for programs CFBD drops (2020 opt-outs)
        tm = pl.concat([tm, fill], how="diagonal_relaxed")
        gs = _group_seasons(tm, conf, names)
        xw = _espn_crosswalk(tm, gs, conf, conf_gid, walk, overrides)
    tm = _cross_check(tm, walk, xw, espn_teams)
    tm = tm.with_columns(
        pl.concat_str([pl.col("_override_note"), pl.col("notes")], separator="; ", ignore_nulls=True)
        .replace("", None)
        .alias("notes")
    )
    espn_ids = set(espn_teams["team_id"]) | set(
        walk.drop_nulls("team_ids")["team_ids"].explode(empty_as_null=False)
    )
    tgs = tm.with_columns(
        pl.lit("cfb").alias("league"),
        pl.when(pl.col("team_id").is_in(list(espn_ids)))
        .then(pl.lit("espn"))
        .otherwise(pl.lit("cfbd"))
        .alias("team_id_source"),
    )
    groups = _groups(tm, gs, lineage, eras, names)
    if (
        gs.group_by("group_id")
        .agg(pl.col("level").n_unique())
        .filter(pl.col("level") > 1)
        .height
    ):
        raise ValueError("a group id is used at two levels")
    aliases = _aliases(tm, gs, conf, walk, xw).with_columns(
        pl.when(pl.col("valid_to") < last)
        .then(pl.col("valid_to"))
        .alias("valid_to")  # still valid -> open
    )
    lg = pl.lit("cfb").alias("league")
    return {
        "groups": conform("groups", groups.with_columns(lg)),
        "group_seasons": conform("group_seasons", gs.with_columns(lg)),
        "group_aliases": conform("group_aliases", aliases.with_columns(lg)),
        "team_group_seasons": conform("team_group_seasons", tgs),
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
