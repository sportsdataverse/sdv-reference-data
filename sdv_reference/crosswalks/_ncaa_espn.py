"""NCAA school <-> ESPN team crosswalk for college basketball, keyed on the NCAA school (org id).

    uv run python -m sdv_reference.build mbb_crosswalk wbb_crosswalk   # offline: raw/ + curated/ -> build/

Moved here from sdv-py's tools/crosswalk/build_ncaa_espn_crosswalk.py (ClaudeCowork plans/
2026-10-11-provider-crosswalk-jobs.md, J3), so a new season no longer needs an sdv-py release.

Inputs:
- raw/{league}_crosswalk/ncaa_teamids.csv.gz: stats.ncaa.org's per-season team list (Team, Conference, ID, Season),
  snapshotted from sdv-py's bundled ncaa_teamids_{league}.csv (the bigballR teamids port);
- raw/{league}_crosswalk/ncaa_org_ids.csv.gz: (season, ncaa_team_id, org_id), read from the "Team History" link of
  each team's committed schedule page in ncaa-{league}-hoops-raw (no stats.ncaa.org requests);
- curated/ncaa_espn_teams.csv: the ESPN school pool (ids are school-level, shared by both leagues);
- curated/ncaa_espn_hoopr_dict.csv, curated/ncaa_espn_aliases.csv: hoopR's name dictionary and the hand-verified
  aliases; curated/ncaa_bb_org_id_fixes.csv: verified corrections to the org id of a season's team id;
- the {league}_groups tables, built in-process from raw/{league}/, for the per-season conference columns.

Match order per NCAA name (a candidate counts only when it resolves to exactly one ESPN id): `alias`, then `exact`
(normalized name), then `dict` (hoopR spellings). No fuzzy matching. Then the school key: every org must resolve to
one ESPN id across seasons and both leagues, and a row its name left unresolved takes its org's id (`org`).
College baseball is out of scope: ESPN's baseball team ids are a separate id system.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import subprocess
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
RAW, CURATED = ROOT / "raw", ROOT / "curated"
SDV_PY = Path(os.environ.get("SDV_PY_CHECKOUT", "/mnt/sdv_repos/sdv-py"))
HOOPS_RAW = (
    "/mnt/sdv_repos/ncaa-{league}-hoops-raw"  # $SDV_NCAA_{LEAGUE}_HOOPS_RAW overrides
)
LEAGUES = ("mbb", "wbb")

#: AP-style abbreviations stats.ncaa.org uses that ESPN spells out; each retired at least one real miss.
_ABBREV = {
    "st.": "state", "u.": "university", "univ.": "university", "ala.": "alabama", "ark.": "arkansas",
    "ariz.": "arizona", "caro.": "carolina", "colo.": "colorado", "conn.": "connecticut", "fla.": "florida",
    "ga.": "georgia", "ill.": "illinois", "ind.": "indiana", "ky.": "kentucky", "la.": "louisiana",
    "md.": "maryland", "mass.": "massachusetts", "mich.": "michigan", "minn.": "minnesota", "miss.": "mississippi",
    "mo.": "missouri", "n.c.": "north carolina", "n.d.": "north dakota", "n.j.": "new jersey", "n.m.": "new mexico",
    "n.y.": "new york", "neb.": "nebraska", "okla.": "oklahoma", "ore.": "oregon", "pa.": "pennsylvania",
    "s.c.": "south carolina", "s.d.": "south dakota", "tenn.": "tennessee", "tex.": "texas", "va.": "virginia",
    "vt.": "vermont", "w.va.": "west virginia", "wash.": "washington", "wis.": "wisconsin",
}  # fmt: skip
# a LEADING "St. " is Saint (St. John's); a trailing/medial "St." is State (Ohio St.)
_LEADING_SAINT = re.compile(r"^st\.\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ORG = re.compile(r"/teams/history/[A-Z]+/(\d+)")

COLUMNS = [
    "season", "ncaa_team_id", "ncaa_org_id", "ncaa_team", "ncaa_conference", "conference_id", "espn_team_id",
    "espn_display_name", "espn_location", "espn_mascot", "espn_abbreviation", "espn_conference_name",
    "espn_conference_id", "match_method",
]  # fmt: skip


def normalize(name: str | None) -> str:
    """Contract an NCAA/ESPN school name to a comparable key: AP abbreviations expanded, case and punctuation
    folded; "" for null."""
    if not name:
        return ""
    text = _LEADING_SAINT.sub("saint ", name.lower().strip())
    text = " ".join(_ABBREV.get(tok, tok) for tok in text.split())
    return _NON_ALNUM.sub(" ", text.replace("&", " and ")).strip()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


# ---------------------------------------------------------------- fetch (local checkouts; no network)


def fetch_league(league: str) -> None:
    """Snapshot the NCAA team list and its org ids into raw/{league}_crosswalk/. A file whose data is unchanged
    keeps its provenance line, so an unchanged snapshot rewrites nothing (git log dates the snapshot)."""
    out = RAW / f"{league}_crosswalk"
    prov_path = out / "provenance.json"
    old = json.loads(prov_path.read_text()) if prov_path.exists() else {}
    prov: dict[str, str] = {}

    def snapshot(name: str, df: pl.DataFrame, source) -> None:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:  # mtime=0: unchanged data, unchanged bytes
            df.write_csv(fh)
        path = out / name
        if path.exists() and path.read_bytes() == buf.getvalue() and name in old:
            prov[name] = old[name]
            return
        out.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buf.getvalue())
        prov[name] = source()

    path = f"sportsdataverse/{league}/data/ncaa_teamids_{league}.csv"
    teams = pl.read_csv(_git(SDV_PY, "show", f"origin/main:{path}").encode(), infer_schema=False)
    teams = teams.sort("Season", pl.col("ID").cast(pl.Int64))
    # the commit that last changed the file, not origin/main's head
    snapshot("ncaa_teamids.csv.gz", teams, lambda: "sportsdataverse-py origin/main "
             f"{_git(SDV_PY, 'log', '-1', '--format=%H', 'origin/main', '--', path).strip()}:{path}")  # fmt: skip

    hoops = Path(os.environ.get(f"SDV_NCAA_{league.upper()}_HOOPS_RAW", HOOPS_RAW.format(league=league)))
    rows, missing = [], 0
    for season, team_id in teams.select("Season", "ID").iter_rows():
        end = int(season[:4]) + 1  # "2009-10" -> 2010, the hoops-raw directory key
        page = hoops / league / "schedules" / "html" / str(end) / f"{team_id}.html"
        m = _ORG.search(page.read_text(errors="ignore")) if page.exists() else None
        missing += m is None
        rows.append({"season": season, "ncaa_team_id": team_id, "org_id": m.group(1) if m else None})
    orgs = pl.DataFrame(rows, schema={"season": pl.Utf8, "ncaa_team_id": pl.Utf8, "org_id": pl.Utf8})
    html = f"{league}/schedules/html"
    snapshot("ncaa_org_ids.csv.gz", orgs, lambda: f"{hoops.name} {_git(hoops, 'log', '-1', '--format=%H', '--', html).strip()}"
             f":{html}/<end season>/<team id>.html Team History link")  # fmt: skip
    prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n")
    print(f"{league}_crosswalk: {teams.height} team-seasons, {missing} without an org id", flush=True)


# ---------------------------------------------------------------- build (offline: raw/ + curated/)


def _csv(path: Path) -> pl.DataFrame:
    """Every column as Utf8: ids keep one dtype from the boundary on."""
    return pl.read_csv(
        gzip.open(path).read() if path.suffix == ".gz" else path, infer_schema=False
    )


def _espn_index(espn: pl.DataFrame) -> dict[str, set[str]]:
    """normalized name -> {ESPN team id}; an ambiguous key keeps every id."""
    index: dict[str, set[str]] = {}
    for row in espn.iter_rows(named=True):
        keys = {
            normalize(row[c])
            for c in (
                "location",
                "display_name",
                "short_name",
                "nickname",
                "abbreviation",
            )
        }
        display, mascot = row["display_name"], row["mascot"]
        if display and mascot and display.endswith(mascot):
            keys.add(normalize(display[: -len(mascot)]))
        for key in keys - {""}:
            index.setdefault(key, set()).add(row["team_id"])
    return index


def _resolver(espn: pl.DataFrame):
    index = _espn_index(espn)
    bridge: dict[str, set[str]] = {}
    for ncaa, *spellings in (
        _csv(CURATED / "ncaa_espn_hoopr_dict.csv")
        .select("NCAA", "ESPN", "ESPN_PBP")
        .iter_rows()
    ):
        if ncaa and any(spellings):
            bridge.setdefault(normalize(ncaa), set()).update(s for s in spellings if s)
    alias = dict(
        _csv(CURATED / "ncaa_espn_aliases.csv")
        .select("ncaa_team", "espn_team_id")
        .iter_rows()
    )

    def resolve(team: str) -> tuple[str | None, str]:
        if team in alias:
            return alias[team], "alias"
        key = normalize(team)
        hit = index.get(key, set())
        if len(hit) == 1:
            return next(iter(hit)), "exact"
        ids = set().union(
            *(index.get(normalize(s), set()) for s in bridge.get(key, set()))
        )
        if len(ids) == 1:
            return next(iter(ids)), "dict"
        return None, "ambiguous" if (hit or ids) else "unmatched"

    return resolve


def _name_matches(league: str, resolve) -> pl.DataFrame:
    """One league's NCAA team-seasons with their org id and name-resolved ESPN id."""
    raw = RAW / f"{league}_crosswalk"
    teams = _csv(raw / "ncaa_teamids.csv.gz").select(
        pl.col("Season").alias("season"),
        pl.col("ID").cast(pl.Int64).alias("ncaa_team_id"),
        pl.col("Team").alias("ncaa_team"),
        pl.col("Conference").alias("ncaa_conference"),
    )
    orgs = _csv(raw / "ncaa_org_ids.csv.gz").with_columns(
        pl.col("ncaa_team_id", "org_id").cast(pl.Int64)
    )
    fixes = _csv(CURATED / "ncaa_bb_org_id_fixes.csv").filter(
        pl.col("league") == league
    )
    fixes = fixes.select(
        "season", pl.col("ncaa_team_id", "org_id").cast(pl.Int64).name.suffix("_fix")
    )
    orgs = orgs.join(
        fixes,
        left_on=["season", "ncaa_team_id"],
        right_on=["season", "ncaa_team_id_fix"],
        how="left",
    ).select(
        "season",
        "ncaa_team_id",
        pl.coalesce("org_id_fix", "org_id").alias("ncaa_org_id"),
    )
    assert teams.schema["ncaa_team_id"] == orgs.schema["ncaa_team_id"] == pl.Int64
    resolved = {t: resolve(t) for t in teams["ncaa_team"].unique().to_list()}
    return teams.join(orgs, on=["season", "ncaa_team_id"], how="left").with_columns(
        pl.col("ncaa_team").replace_strict({t: v[0] for t, v in resolved.items()}, return_dtype=pl.Utf8)
        .alias("espn_team_id"),
        pl.col("ncaa_team").replace_strict({t: v[1] for t, v in resolved.items()}, return_dtype=pl.Utf8)
        .alias("match_method"),
        pl.lit(league).alias("sport"),
    )  # fmt: skip


def _school_key(matches: pl.DataFrame) -> pl.DataFrame:
    """Each org's one ESPN id, from its name-resolved rows across seasons and both leagues. An org that resolves
    to two ESPN ids, or an ESPN id claimed by two orgs, is a matching error and fails the build."""
    key = (
        matches.drop_nulls(["ncaa_org_id", "espn_team_id"])
        .group_by("ncaa_org_id")
        .agg(
            pl.col("espn_team_id").unique().alias("ids"),
            pl.col("ncaa_team").unique().alias("names"),
        )
    )
    split = key.filter(pl.col("ids").list.len() > 1)
    assert split.height == 0, (
        f"an NCAA school resolves to several ESPN ids: {split.to_dicts()}"
    )
    key = key.select(
        "ncaa_org_id", pl.col("ids").list.first().alias("org_espn_team_id")
    )
    shared = (
        key.group_by("org_espn_team_id")
        .agg("ncaa_org_id")
        .filter(pl.col("ncaa_org_id").list.len() > 1)
    )
    assert shared.height == 0, (
        f"an ESPN id is claimed by several NCAA schools: {shared.to_dicts()}"
    )
    return key


def _conferences(league: str) -> pl.DataFrame:
    """Per (ending-year season, ESPN team id): the SDV conference id, ESPN's group id and name that season, and the
    NCAA label (the nearest-window `ncaa` alias, else the SDV abbreviation), from the {league}_groups tables."""
    from sdv_reference.leagues.mbb import build_league

    t = build_league(league)
    teams = (
        t["team_group_seasons"]
        .filter(pl.col("team_id_source") == "espn")
        .select(
            pl.col("season").cast(pl.Int64),
            pl.col("team_id").alias("espn_team_id"),
            "conference_id",
        )
    )
    names = t["group_seasons"].select(
        pl.col("group_id").alias("conference_id"),
        pl.col("season").cast(pl.Int64),
        pl.col("name").alias("espn_name"),
    )
    aliases = (
        t["group_aliases"]
        .rename({"group_id": "conference_id"})
        .with_columns(
            pl.col("valid_from").cast(pl.Int64).fill_null(0),
            pl.col("valid_to").cast(pl.Int64).fill_null(9999),
        )
    )  # a null bound is an open window
    keys = teams.select("conference_id", "season").unique()

    def nearest(src: pl.DataFrame, value: str, name: str) -> pl.DataFrame:
        # distance 0 inside [valid_from, valid_to], so an in-window alias always wins
        return (
            keys.join(src, on="conference_id")
            .with_columns(
                pl.max_horizontal(pl.col("valid_from") - pl.col("season"), pl.col("season") - pl.col("valid_to"), 0)
                .alias("_dist")
            )
            .sort("conference_id", "season", "_dist", "valid_from", descending=[False, False, False, True])
            .unique(subset=["conference_id", "season"], keep="first", maintain_order=True)
            .select("conference_id", "season", pl.col(value).alias(name))
        )  # fmt: skip

    on = ["conference_id", "season"]
    out = (
        teams.join(names, on=on, how="left")
        .join(nearest(aliases.filter((pl.col("source") == "espn") & (pl.col("name_kind") == "name")), "source_id",
                      "espn_conference_id"), on=on, how="left")
        .join(nearest(aliases.filter(pl.col("source") == "ncaa"), "value", "ncaa_label"), on=on, how="left")
        .join(nearest(aliases.filter((pl.col("source") == "sdv") & (pl.col("name_kind") == "abbreviation")), "value",
                      "sdv_abbreviation"), on=on, how="left")
        .select(
            pl.col("season").alias("_end_season"), "espn_team_id", "conference_id", "espn_conference_id",
            pl.col("espn_name").alias("espn_conference_name"),
            pl.coalesce("ncaa_label", "sdv_abbreviation").alias("_ncaa_conference"),
        )
    )  # fmt: skip
    assert out.select("_end_season", "espn_team_id").is_duplicated().sum() == 0, (
        f"{league}: a team in two conferences"
    )
    return out


def build() -> tuple[dict[str, pl.DataFrame], pl.DataFrame, pl.DataFrame]:
    """(per-league season crosswalk, the school key table, the per-sport season presence table)."""
    espn = _csv(CURATED / "ncaa_espn_teams.csv")
    resolve = _resolver(espn)
    matches = pl.concat([_name_matches(lg, resolve) for lg in LEAGUES])
    assert matches["ncaa_org_id"].null_count() == 0, (
        matches.filter(pl.col("ncaa_org_id").is_null()).head().to_dicts()
    )
    key = _school_key(matches)
    matches = (
        matches.join(key, on="ncaa_org_id", how="left")
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
        # "2009-10" -> 2010: the groups tables key seasons by the ENDING year
        .with_columns((pl.col("season").str.slice(0, 4).cast(pl.Int64) + 1).alias("_end_season"))
    )  # fmt: skip
    crosswalks = {}
    for lg in LEAGUES:
        conf = _conferences(lg)
        for k in ("_end_season", "espn_team_id"):
            assert matches.schema[k] == conf.schema[k], (
                f"{k}: {matches.schema[k]} != {conf.schema[k]}"
            )
        crosswalks[lg] = (
            matches.filter(pl.col("sport") == lg)
            .join(conf, on=["_end_season", "espn_team_id"], how="left")
            # the NCAA label follows the season's membership; a team the groups don't carry keeps stats.ncaa.org's
            .with_columns(
                pl.coalesce("_ncaa_conference", "ncaa_conference").alias(
                    "ncaa_conference"
                )
            )
            .select(COLUMNS)
            .sort("season", "ncaa_team")
        )
    presence = (
        pl.concat(
            [crosswalks[lg].with_columns(pl.lit(lg).alias("sport")) for lg in LEAGUES]
        )
        .select("ncaa_org_id", "sport", "season", "ncaa_team_id", "espn_team_id")
        .sort("ncaa_org_id", "sport", "season")
    )
    latest = pl.concat([crosswalks[lg] for lg in LEAGUES]).sort("season").group_by("ncaa_org_id").agg(
        pl.col("ncaa_team").last(), pl.col("espn_team_id").drop_nulls().last(),
        pl.col("espn_display_name").drop_nulls().last(),
    )  # fmt: skip
    schools = latest.select(
        "ncaa_org_id", "ncaa_team", "espn_team_id", "espn_display_name"
    ).sort("ncaa_org_id")
    return crosswalks, schools, presence


def validate(crosswalk: pl.DataFrame, league: str) -> list[str]:
    """Contract problems in one league's season crosswalk (CONTRACT.md); [] when it passes."""
    problems = []
    teams = _csv(RAW / f"{league}_crosswalk" / "ncaa_teamids.csv.gz")
    if crosswalk.height != teams.height:
        problems.append(f"{crosswalk.height} rows for {teams.height} NCAA team-seasons")
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
        != (pl.col("match_method").is_in(["unmatched", "ambiguous"]))
    )
    if bad.height:
        problems.append(
            f"match_method disagrees with espn_team_id: {bad.head(5).to_dicts()}"
        )
    return problems


def write(league: str, out_dir: Path) -> list[Path]:
    """Build, validate and write build/{league}_crosswalk/: the season crosswalk, plus the school key and the
    presence table (both leagues, identical in each tag)."""
    crosswalks, schools, presence = build()
    df = crosswalks[league]
    problems = validate(df, league)
    if problems:
        raise ValueError(
            f"{league}_crosswalk fails the contract:\n  " + "\n  ".join(problems)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name, table in (("ncaa_espn_team_crosswalk", df), ("ncaa_espn_schools", schools),
                        ("ncaa_espn_school_seasons", presence)):  # fmt: skip
        for ext in ("parquet", "csv"):
            p = out_dir / f"{name}.{ext}"
            table.write_parquet(p) if ext == "parquet" else table.write_csv(p)
            files.append(p)
    methods = dict(df["match_method"].value_counts().iter_rows())
    print(
        f"{league}_crosswalk: {df.height} team-seasons ({df['season'].min()}..{df['season'].max()}), "
        f"{df['espn_team_id'].is_not_null().sum()} matched {methods}; {schools.height} schools",
        flush=True,
    )
    return files
