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
- Torvik's player ids (barttorvik.com `getadvstats.php?year=&csv=1`, MBB 2008 on), snapshotted by fetch() and matched
  here, offline, the NCAA leg's way: blocked by (season, ESPN team) through mbb_team_aliases' Torvik team names, then
  normalized name + jersey, name, last name + jersey, each tier joining only on a key unique on both sides (no fuzzy
  step), against the ESPN athletes of mbb_player_crosswalk;
- CFB recruits' 247Sports and On3 ids (On3's public industry-comparison list, which carries each recruit's 247
  player id), tied to the ESPN athlete through CFBD's /recruiting/players (`athleteId` is the ESPN athlete id): both
  snapshotted by fetch() and matched here, offline, by class + committed school (as an ESPN team) + normalized name;
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

import csv
import gzip
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

import polars as pl
import requests

from sdv_reference.refresh import (current_season, read_digests, release_assets, release_seasons, seasons_changed,
                                   seasons_to_fetch, write_digests)  # fmt: skip
from sdv_reference.schema import (
    PLAYER_ALIASES,
    validate_player_aliases,
)
from sdv_reference.team_aliases import NFL_RAW, RELEASES, _gz_csv

ROOT = Path(__file__).resolve().parent.parent
RAW, CURATED = ROOT / "raw" / "player_aliases", ROOT / "curated"
LEAGUES = ("nfl", "cfb", "nba", "wnba", "mbb", "wbb")

#: league -> [(release tag, asset stem)] of the published ESPN-anchored player crosswalks; every season the release
#: lists is snapshotted (refresh.release_seasons)
XWALKS = {
    "nba": [("nba_crosswalk", "nba_player_crosswalk")],
    "wnba": [("wnba_crosswalk", "wnba_player_crosswalk")],
    "mbb": [("mbb_crosswalk", "mbb_player_crosswalk")],
    "wbb": [("wbb_crosswalk", "wbb_player_crosswalk")],
    "cfb": [
        ("cfb_crosswalk", "cfb_players_crosswalk"),
        ("cfb_crosswalk", "cfb_rosters_crosswalk"),
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
#: (league, source) -> (release tag, asset stem, first season) of the ESPN player box scores whose (season, athlete)
#: set is that leg's gated denominator: CBS box scores list only players with a stat line, and cfb_players_crosswalk
#: gained Fox/Yahoo-only rows (mostly linemen, kickers, long snappers) on 2026-11-13 that CBS can never match
STATLINE = {("cfb", "cbs"): ("espn_cfb_player_box", "player_box", 2016)}
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
#: Torvik's public player table: league -> first season; getadvstats.php's positional fields this leg keeps
TORVIK = {"mbb": 2008}
TORVIK_URL = "https://barttorvik.com/getadvstats.php"
TORVIK_FIELDS = {0: "player_name", 1: "team", 27: "number", 31: "year", 32: "player_id"}
UA = {"User-Agent": "sdv-reference-data/0.1 (+https://github.com/sportsdataverse/sdv-reference-data)"}


#: CFB recruiting classes snapshotted for the 247 / On3 leg (CFBD /recruiting/players + On3's industry list)
RECRUIT_CLASSES = {"cfb": 2014}
ON3_LIST = "https://api.on3.com/public/rdb/v1/players/industry-comparision-list"  # sic: On3's spelling
#: an ordinal tier score for legs that give no confidence of their own (sdv-py espn_player_xwalk tiers)
TIER_CONFIDENCE = {
    "name_jersey": 1.0,
    "name": 0.95,
    "last_jersey": 0.9,
    "jaro_winkler": 0.85,
    "name_position": 1.0,
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
    # STATLINE legs only (else null): the same counts over the athletes with an ESPN box-score stat line that
    # season; the gate checks this rate for them
    "n_rows_statline": pl.UInt32,
    "n_matched_statline": pl.UInt32,
    "match_rate_statline": pl.Float64,
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
    prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n", newline="\n")


def _fetch_crosswalks(league: str) -> None:
    """One file per season, for every season the release lists: a season is downloaded only when it is new or its
    digest differs from raw/player_aliases/{league}/release_digests.json, and a changed one rewrites only its own
    snapshot."""
    session = requests.Session()
    for tag, stem in XWALKS[league]:
        legs = LEGS["player_crosswalk" if stem.endswith("_player_crosswalk") else stem]
        keep = ["espn_full_name", "espn_player_name", "name", "espn_jersey", "fox_team_id", "match_method",
                "match_confidence", "match_keys"] + [c for i, n in legs.values() for c in (i, n) if c]  # fmt: skip
        digests = RAW / league / "release_digests.json"
        published = release_seasons(release_assets(tag), stem)
        for season in seasons_changed(published, read_digests(digests, stem)):
            r = session.get(f"{RELEASES}/{tag}/{stem}_{season}.parquet", timeout=120)
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
        write_digests(digests, stem, published)


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


def _fetch_statlines(league: str) -> None:
    """raw/player_aliases/{league}/espn_statline_{season}.csv.gz: the athletes with an ESPN box-score row that
    season, for each of the league's STATLINE legs from its first season; refetched by release digest, like
    _fetch_crosswalks()."""
    session = requests.Session()
    for tag, stem, first in {v for (lg, _), v in STATLINE.items() if lg == league}:
        digests = RAW / league / "release_digests.json"
        published = {s: d for s, d in release_seasons(release_assets(tag), stem).items() if s >= first}
        for season in seasons_changed(published, read_digests(digests, stem)):
            r = session.get(f"{RELEASES}/{tag}/{stem}_{season}.parquet", timeout=120)
            r.raise_for_status()
            df = pl.read_parquet(io.BytesIO(r.content), columns=["athlete_id"])
            # Int64 -> Utf8, the cast _fetch_crosswalks gives espn_athlete_id
            snap = df.select(pl.col("athlete_id").cast(pl.Int64, strict=False).cast(pl.Utf8)).drop_nulls().unique()
            _snapshot(league, f"espn_statline_{season}.csv.gz", snap,
                      f"sportsdataverse-data release {tag}: {stem}_{season}.parquet (athlete_id)")  # fmt: skip
        write_digests(digests, stem, published)


def _fetch_torvik(league: str, pause: float = 2.0) -> None:
    """raw/player_aliases/{league}/torvik_{season}.csv.gz (player_id, player_name, team, number), one public CSV per
    season, sequential and paced; the newest seasons are refetched (refresh.seasons_to_fetch). A 403 / 429 raises."""
    have = [s for s, _ in _season_files(league, "torvik")]
    for season in seasons_to_fetch(range(TORVIK[league], current_season(ending_year=True) + 1), have):
        r = requests.get(TORVIK_URL, params={"year": season, "csv": 1}, headers=UA, timeout=120)
        r.raise_for_status()
        time.sleep(pause)
        # UTF-8, not requests' per-response charset guess (a few names are already mojibake at the source: Mรถnninghoff)
        rows = [row for row in csv.reader(io.StringIO(r.content.decode("utf-8"))) if any(c.strip() for c in row)]
        if not rows:  # a season Torvik has no players for yet
            continue
        if short := sum(len(row) <= max(TORVIK_FIELDS) for row in rows):
            raise ValueError(f"torvik {season}: {short} of {len(rows)} rows have under {max(TORVIK_FIELDS) + 1} fields")
        df = pl.DataFrame([[row[i].strip() for i in TORVIK_FIELDS] for row in rows], schema=list(TORVIK_FIELDS.values()),
                          orient="row")  # fmt: skip
        assert (df["year"] == str(season)).all(), f"torvik {season}: rows for {df['year'].unique().to_list()}"
        assert df["player_id"].str.contains(r"^\d+$").all(), f"torvik {season}: a player_id that is not a number"
        _snapshot(league, f"torvik_{season}.csv.gz", df.drop("year").unique(),
                  f"barttorvik.com getadvstats.php?year={season}&csv=1")  # fmt: skip


def _on3_rows(item: dict) -> dict:
    """One On3 industry-list recruit -> its ids, name, position and commitment; the 247 id is the number in its
    247 rating's link (https://247sports.com/player/{id}/)."""
    person, status = item.get("person") or {}, item.get("status") or {}
    link = next((r.get("link") for r in item.get("ratings") or [] if r.get("type") == "247" and r.get("link")), None)
    m = re.search(r"247sports\.com/player/(?:[^/]*-)?(\d+)", link or "")
    org = status.get("committedAsset") or {}
    return {"on3_person_key": person.get("key"), "sports247_player_id": m[1] if m else None,
            "name": person.get("fullName"), "position": (person.get("position") or {}).get("abbr"),
            "status": status.get("type"), "on3_org_key": org.get("key") if org.get("orgType") == "College" else None,
            "on3_org_name": org.get("fullName") if org.get("orgType") == "College" else None}  # fmt: skip


def _fetch_recruits(league: str, pause: float = 1.5) -> None:
    """raw/player_aliases/{league}/cfbd_recruits_{class}.csv.gz and on3_recruits_{class}.csv.gz: per recruiting class,
    one CFBD call (the key from leagues.cfb._cfbd_key, never echoed) and On3's industry list in pages of 250, sequential
    and paced. A non-200 (403 / 429 included) raises and stops the fetch; the newest classes are refetched."""
    from sdv_reference.leagues.cfb import CFBD, _cfbd_key

    have = [s for s, _ in _season_files(league, "on3_recruits")]
    todo = seasons_to_fetch(range(RECRUIT_CLASSES[league], current_season(ending_year=False) + 1), have)
    if not todo:
        return
    headers = {"Authorization": f"Bearer {_cfbd_key()}", "Accept": "application/json"}
    session = requests.Session()
    for year in todo:
        r = session.get(f"{CFBD}/recruiting/players", params={"year": year, "classification": "HighSchool"},
                         headers=headers, timeout=120)  # fmt: skip
        r.raise_for_status()
        cfbd = pl.DataFrame(
            [{"cfbd_recruit_id": x.get("id"), "athlete_id": x.get("athleteId"), "name": x.get("name"),
              "committed_to": x.get("committedTo"), "position": x.get("position")} for x in r.json()],
            schema={"cfbd_recruit_id": pl.Utf8, "athlete_id": pl.Utf8, "name": pl.Utf8, "committed_to": pl.Utf8, "position": pl.Utf8},
        )  # fmt: skip
        on3, page, pages = [], 1, 1
        while page <= pages:
            time.sleep(pause)
            o = session.get(ON3_LIST, params={"sportKey": 1, "year": year, "page": page, "limit": 250},
                            headers={**UA, "Accept": "application/json"},
                            timeout=120)  # fmt: skip
            o.raise_for_status()
            body = o.json()
            pages = body["pagination"]["pageCount"]
            on3 += [_on3_rows(x) for x in body["list"]]
            page += 1
        # the list is live: a shift between pages would skip or repeat recruits
        assert len(on3) == body["pagination"]["count"], f"on3 {year}: {len(on3)} rows for {body['pagination']['count']}"
        on3_df = pl.DataFrame(on3, schema={k: pl.Utf8 for k in _on3_rows({})})
        if not on3_df.height:  # a class On3 has no recruits for yet
            continue
        _snapshot(league, f"cfbd_recruits_{year}.csv.gz", cfbd.unique(),
                  f"CFBD /recruiting/players?year={year}&classification=HighSchool")  # fmt: skip
        _snapshot(league, f"on3_recruits_{year}.csv.gz", on3_df.unique(),
                  f"On3 {ON3_LIST}?sportKey=1&year={year} (every page)")  # fmt: skip
        time.sleep(pause)


def fetch(league: str) -> None:
    if league in XWALKS:
        _fetch_crosswalks(league)
    if any(lg == league for lg, _ in STATLINE):
        _fetch_statlines(league)
    if league in TORVIK:
        _fetch_torvik(league)
    if league in NCAA:
        _fetch_ncaa(league)
    if league in RECRUIT_CLASSES:  # last: it needs the CFBD key, which the other legs don't
        _fetch_recruits(league)
    if league == "nfl":
        _fetch_nfl()


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
    for tag, stem in XWALKS.get(league, []):
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


def statlines(league: str) -> pl.DataFrame:
    """(season, athlete_id): the athletes with an ESPN box-score stat line, from the espn_statline snapshots."""
    parts = [_read(p).select(pl.lit(s).cast(pl.Int32).alias("season"), "athlete_id")
             for s, p in _season_files(league, "espn_statline")]  # fmt: skip
    return pl.concat(parts) if parts else pl.DataFrame(schema={"season": pl.Int32, "athlete_id": pl.Utf8})


_SUFFIX = r"\b(jr|sr|ii|iii|iv|v)\b"


def _norm_name(c: str) -> pl.Expr:
    """ASCII-folded, lower-cased, apostrophes and periods dropped, other punctuation a space, Jr./Sr./II-V dropped."""
    s = pl.col(c).str.normalize("NFKD").str.replace_all(r"[^\x00-\x7f]", "").str.to_lowercase()
    s = s.str.replace_all(r"['.`]", "").str.replace_all(r"[^a-z0-9]+", " ").str.replace_all(_SUFFIX, " ")
    return s.str.replace_all(r"\s+", " ").str.strip_chars().replace("", None)


def _jersey(c: str) -> pl.Expr:
    """Torvik writes `01` where ESPN writes `1` (`0` and `00` stay apart); `--` is no jersey."""
    s = pl.col(c).cast(pl.Utf8).str.strip_chars().str.replace(r"^0([1-9])$", "$1")
    return pl.when(s.str.contains(r"^\d+$")).then(s)


def _torvik_legs(league: str, team_aliases: pl.DataFrame) -> list[pl.DataFrame]:
    """Torvik players -> ESPN athletes, per (season, ESPN team) block: the team through team_aliases' Torvik names,
    then name + jersey, name, last name + jersey, each tier only on a key unique on both sides; a tie or no candidate
    stays unmatched. Only seasons with both a Torvik snapshot and an ESPN roster (mbb_player_crosswalk) count."""
    stem = XWALKS[league][0][1]
    espn_files = dict(_season_files(league, stem))
    names = (
        team_aliases.filter((pl.col("source") == "torvik") & (pl.col("name_kind") == "name"))
        .select(pl.col("value").alias("team"), "team_id",
                pl.int_ranges("valid_from", pl.col("valid_to") + 1, dtype=pl.Int32).alias("season"))
        .explode("season").drop_nulls("season").unique()
    )  # fmt: skip
    assert not names.select("season", "team").is_duplicated().any(), f"{league}: a Torvik team name on two ESPN teams"
    out = []
    for season, path in _season_files(league, "torvik"):
        if season not in espn_files:
            continue
        tv = _read(path).with_columns(pl.lit(season, pl.Int32).alias("season"))
        assert tv["player_id"].is_unique().all(), f"torvik {season}: a player_id twice"
        tv = tv.join(names, on=["season", "team"], how="left").with_columns(
            _norm_name("player_name").alias("full"), _jersey("number").alias("jersey"))  # fmt: skip
        espn = _read(espn_files[season]).select(
            pl.lit(season, pl.Int32).alias("season"), _clean("espn_team_id").alias("team_id"),
            _clean("espn_athlete_id").alias("athlete_id"), _norm_name("espn_full_name").alias("full"),
            _jersey("espn_jersey").alias("jersey"),
        ).drop_nulls(["team_id", "athlete_id"]).unique(["season", "team_id", "athlete_id"])  # fmt: skip
        for k in ("season", "team_id"):
            assert tv.schema[k] == espn.schema[k], f"{k}: {tv.schema[k]} != {espn.schema[k]}"
        tv, espn = (d.with_columns(pl.col("full").str.split(" ").list.last().alias("last")) for d in (tv, espn))
        todo, pool, matched = tv.filter(pl.col("team_id").is_not_null()), espn, []
        for tier, keys in (("name_jersey", ["full", "jersey"]), ("name", ["full"]), ("last_jersey", ["last", "jersey"])):
            k = ["season", "team_id", *keys]
            a = todo.drop_nulls(keys).filter(pl.struct(k).is_unique())
            b = pool.drop_nulls(keys).filter(pl.struct(k).is_unique()).select(*k, "athlete_id")
            m = a.join(b, on=k).with_columns(pl.lit(tier).alias("match_method"))
            matched.append(m)
            todo = todo.join(m.select("player_id"), on="player_id", how="anti")
            pool = pool.join(m.select("athlete_id"), on="athlete_id", how="anti")
        ev = f"barttorvik.com getadvstats.php?year={season} (raw/player_aliases/{league}/torvik_{season}.csv.gz)"
        miss = pl.concat([
            todo.with_columns(pl.lit("no unique name match in the (season, ESPN team) block").alias("why")),
            tv.filter(pl.col("team_id").is_null()).with_columns(
                pl.lit("no ESPN team: the Torvik team name is not in mbb_team_aliases that season").alias("why")),
        ], how="diagonal_relaxed")  # fmt: skip
        both = pl.concat([*matched, miss], how="diagonal_relaxed")
        out.append(_rows(
            both, season=pl.col("season"), team_id=pl.col("team_id"), athlete_id=pl.col("athlete_id"), source=pl.lit("torvik"),
            source_player_id=_clean("player_id"), match_method=pl.col("match_method"),
            match_confidence=pl.col("match_method").replace_strict(TIER_CONFIDENCE, default=None),
            unmatched_reason=pl.col("why"), evidence=pl.lit(ev),
        ))  # fmt: skip
    return out


def _cfbd_team_ids() -> pl.DataFrame:
    """CFBD school name -> CFBD team id (the ESPN id), from the committed affiliations snapshot; a name on two ids is
    left out."""
    rows = json.loads(gzip.open(ROOT / "raw" / "cfb" / "cfbd_affiliations.json.gz").read())
    m = pl.DataFrame([(r["team"], str(r["teamId"])) for r in rows], schema={"committed_to": pl.Utf8, "team_id": pl.Utf8},
                     orient="row").unique()  # fmt: skip
    return m.filter(pl.col("committed_to").is_duplicated() == False)  # noqa: E712


def _recruit_legs(league: str, team_aliases: pl.DataFrame) -> list[pl.DataFrame]:
    """On3 recruits (with their 247 ids) -> ESPN athletes through CFBD's recruit list, per (class, ESPN team) block:
    On3's commitment through team_aliases' On3 school keys, CFBD's through its own team names (CFBD ids are ESPN ids);
    then normalized name + position, then the name alone, each only on a key unique on both sides. A tie stays
    unmatched, and so does a match to a CFBD recruit with no athleteId (one who never played in CFBD's data). Only
    recruits committed to a school the On3 directory maps to an ESPN team are candidates."""
    orgs = (
        team_aliases.filter((pl.col("source") == "on3") & (pl.col("id_kind") == "school") & pl.col("source_id").is_not_null())
        .select(pl.col("source_id").alias("on3_org_key"), "team_id").unique()
    )  # fmt: skip
    assert not orgs["on3_org_key"].is_duplicated().any(), f"{league}: an On3 school key on two ESPN teams"
    cfbd_ids, out = _cfbd_team_ids(), []
    cfbd_files = dict(_season_files(league, "cfbd_recruits"))
    for year, path in _season_files(league, "on3_recruits"):
        if year not in cfbd_files:
            continue
        on3 = _read(path).join(orgs, on="on3_org_key", how="inner").with_columns(
            pl.lit(year, pl.Int32).alias("season"), _norm_name("name").alias("full"))  # fmt: skip
        assert on3["on3_person_key"].is_not_null().all() and on3["on3_person_key"].is_unique().all(), f"on3 {year}: person keys"
        cfbd = _read(cfbd_files[year]).join(cfbd_ids, on="committed_to", how="inner").select(
            pl.lit(year, pl.Int32).alias("season"), "team_id", _clean("athlete_id").alias("athlete_id"),
            "cfbd_recruit_id", _norm_name("name").alias("full"), "position")  # fmt: skip
        assert cfbd["cfbd_recruit_id"].is_not_null().all() and cfbd["cfbd_recruit_id"].is_unique().all(), f"cfbd {year}: ids"
        for k in ("season", "team_id"):
            assert on3.schema[k] == cfbd.schema[k], f"{k}: {on3.schema[k]} != {cfbd.schema[k]}"
        todo, pool, matched = on3, cfbd, []
        for tier, keys in (("name_position", ["full", "position"]), ("name", ["full"])):
            k = ["season", "team_id", *keys]
            a = todo.drop_nulls(keys).filter(pl.struct(k).is_unique())
            b = pool.drop_nulls(keys).filter(pl.struct(k).is_unique()).select(*k, "athlete_id", "cfbd_recruit_id")
            m = a.join(b, on=k).with_columns(pl.lit(tier).alias("match_method"))
            matched.append(m)
            todo = todo.join(m.select("on3_person_key"), on="on3_person_key", how="anti")
            pool = pool.join(m.select("cfbd_recruit_id"), on="cfbd_recruit_id", how="anti")
        both = pl.concat([
            *[m.with_columns(pl.when(pl.col("athlete_id").is_null())
                             .then(pl.lit("the matched CFBD recruit has no ESPN athlete (athleteId)")).alias("why")) for m in matched],
            todo.with_columns(pl.lit("no unique name match in the (class, ESPN team) block").alias("why")),
        ], how="diagonal_relaxed")  # fmt: skip
        ev = (f"On3 industry-comparision-list sportKey=1 year={year} + CFBD /recruiting/players year={year} "
              f"(raw/player_aliases/{league}/{{on3,cfbd}}_recruits_{year}.csv.gz)")  # fmt: skip
        for source, col in (("on3", "on3_person_key"), ("sports247", "sports247_player_id")):
            out.append(_rows(
                both.filter(pl.col(col).is_not_null()), season=pl.col("season"), team_id=pl.col("team_id"),
                athlete_id=pl.col("athlete_id"), source=pl.lit(source), source_player_id=_clean(col),
                source_team_id=pl.col("on3_org_key") if source == "on3" else pl.lit(None), match_method=pl.col("match_method"),
                match_confidence=pl.col("match_method").replace_strict(TIER_CONFIDENCE, default=None, return_dtype=pl.Float64),
                unmatched_reason=pl.col("why"), evidence=pl.lit(ev),
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
    if league in TORVIK and team_aliases.height:  # the Torvik leg's team block is team_aliases' Torvik names
        rows = pl.concat([rows, *_torvik_legs(league, team_aliases)])
    if league in RECRUIT_CLASSES and team_aliases.height:  # the recruit leg's On3 block is team_aliases' On3 keys
        rows = pl.concat([rows, *_recruit_legs(league, team_aliases)])
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
    def counts(total: pl.DataFrame, matched: pl.DataFrame, sfx: str = "") -> pl.DataFrame:
        k = ["source", "season"]
        return (
            total.group_by(k).agg(pl.len().alias(f"n_rows{sfx}"))
            .join(matched.group_by(k).agg(pl.len().alias(f"n_matched{sfx}")), on=k, how="left").fill_null(0)
            .with_columns((100 * pl.col(f"n_matched{sfx}") / pl.col(f"n_rows{sfx}")).round(2).alias(f"match_rate{sfx}"))
        )  # fmt: skip

    rates = counts(total, rows)
    gated = [s for lg, s in STATLINE if lg == league]
    if gated:
        sl = statlines(league)
        assert sl.schema["athlete_id"] == rows.schema["athlete_id"] == total.schema["athlete_id"], "athlete_id dtype"
        assert sl.schema["season"] == rows.schema["season"], "season dtype"
        on = ["season", "athlete_id"]
        rates = rates.join(counts(total.filter(pl.col("source").is_in(gated)).join(sl, on=on, how="semi"),
                                  rows.filter(pl.col("source").is_in(gated)).join(sl, on=on, how="semi"), "_statline"),
                           on=["source", "season"], how="left")  # fmt: skip
        # a missing snapshot, or one sharing almost no athletes with the leg (an id-format drift; ESPN box scores cover
        # 59-100% of the leg rows in 2016-26), must fail rather
        # than fall back to the all-rows rate or gate a handful of rows
        thin = rates.filter(pl.col("source").is_in(gated) & ~(pl.col("n_rows_statline") >= 0.25 * pl.col("n_rows")).fill_null(False))
        if thin.height:
            raise ValueError(f"{league}: no espn_statline snapshot, or one covering under a quarter of the leg's rows, for "
                             f"{thin.select('source', 'season').rows()}; run fetch('{league}')")  # fmt: skip
    rates = (
        rates.with_columns(pl.lit(league).alias("league"))
        .select([pl.col(c) if c in rates.columns or c == "league" else pl.lit(None).alias(c) for c in RATES])
        .cast(RATES).sort("source", "season")
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
    """Seasons whose match rate sits below its curated floor; a STATLINE leg's floor applies to its stat-line rate."""
    floors = pl.read_csv(CURATED / "player_match_floors.csv", schema_overrides={
        "season_from": pl.Int32, "season_to": pl.Int32, "floor_pct": pl.Float64}).filter(pl.col("league") == league)  # fmt: skip
    rate = pl.coalesce("match_rate_statline", "match_rate").alias("match_rate")
    low = rates.select("league", "source", "season", rate).join(floors, on=["league", "source"]).filter(
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
    for s, y, a, b in rates.filter(pl.col("match_rate_statline").is_not_null()).select(
            "source", "season", "match_rate", "match_rate_statline").iter_rows():  # fmt: skip
        print(f"{league} {s} {y}: {a}% of all rows, {b}% of athletes with an ESPN stat line (gated)")
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
