"""Provider team directories (247Sports, On3, Fox, Yahoo, CBS) matched to ESPN teams: the directory legs of
{league}_team_aliases. See CONTRACT.md, `team_aliases`.

    uv run --frozen --with curl_cffi python -m sdv_reference.team_directories   # capture (manual, residential only)
    uv run python -m sdv_reference.build cfb mlb                                # offline: raw/ + curated/

Capture is NOT part of stage 10. api.foxsports.com, api.cbssports.com and ipa.247sports.com disallow crawling in
robots.txt; the owner approved (2026-11-03) one-off, low-rate snapshots of their public team directories. So fetch()
is run by hand, from a residential machine: one request at a time, DELAY seconds apart, 11 requests in all. Each
capture is appended to raw/team_aliases/{source}/{file}.csv.gz with its date; raw/team_aliases/{source}/provenance.json
records the URL, capture date and row count. Sports-Reference and PFF are never captured.

A directory lists today's teams, so a capture holds for one season per league: the league's current season on the
capture date. A later capture in a new season extends the window; nothing is carried to seasons nobody captured.

Matching, per directory row and league (no fuzzy step; a candidate counts only when it resolves to one ESPN team):
1. `alias`: curated/team_directory_aliases.csv, (source, id_kind, source_id[, league]) -> ESPN team id; a blank id
   marks a row that is no ESPN team (a D-III school whose location name collides with a D-I one);
2. `exact_name`: the row's names, normalized (sdv_reference.crosswalks._ncaa_espn.normalize, accents folded), against
   the league's ESPN names: the ESPN college pool (curated/ncaa_espn_teams.csv) for cfb / mbb / wbb, plus the league's
   own team_group_seasons names in the capture season;
3. `dict`: curated/team_directory_names.csv, a source spelling -> ESPN team id;
Rows left over go to {league}_team_aliases_unmatched.csv with the reason.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import sys
import time
import unicodedata
from functools import cache
from pathlib import Path

import polars as pl

from sdv_reference.crosswalks._ncaa_espn import _espn_index, normalize
from sdv_reference.refresh import current_season
from sdv_reference.schema import ENDING_YEAR_LEAGUES

ROOT = Path(__file__).resolve().parent.parent
RAW, CURATED = ROOT / "raw" / "team_aliases", ROOT / "curated"
#: seconds between requests; never under 1
DELAY = float(os.environ.get("SDV_REF_DIRECTORY_DELAY", "2"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
FOX_KEY = "jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq"  # the public key in the foxsports.com web bundle (sdv-py fox_api)
COLLEGE = ("cfb", "mbb", "wbb")


def _rows_247(d: list) -> list[dict]:
    return [{"team_id": t["teamId"], "institution_key": t["institutionKey"], "name": t["name"],
             "conference": t.get("conference")} for t in d]  # fmt: skip


def _rows_on3(d: list) -> list[dict]:
    return [{"org_key": t["orgKey"], "org_name": t["orgName"], "mascot": t.get("mascot"), "slug": t.get("slug"),
             "name": " ".join(x for x in (t["orgName"], t.get("mascot")) if x)} for t in d]  # fmt: skip


def _rows_fox(d: dict) -> list[dict]:
    out = []
    for t in d["navItems"]:
        e = t["entityLink"]
        out.append({"team_id": e["layout"]["tokens"]["id"], "abbreviation": t.get("title"),
                    "name": e.get("imageAltText") or e["title"].title(), "uri": e.get("contentUri")})  # fmt: skip
    return out


def _rows_yahoo(d: dict) -> list[dict]:
    return [{"team_id": t["teamId"], "abbreviation": t.get("abbreviation"), "name": t["fullName"],
             "location": t.get("location"), "nickname": t.get("nickname")}
            for lg in d["data"]["leagues"] for t in lg["teams"]]  # fmt: skip


def _rows_cbs(d: dict) -> list[dict]:
    return [{"team_id": t["teamId"], "abbreviation": t.get("abbrev"),
             "name": " ".join(x for x in (t.get("location"), t.get("nickName")) if x), "status": t.get("status")}
            for t in d["data"]]  # fmt: skip


_YH = "https://graphite-secure.sports.yahoo.com/v1/query/shangrila/leagueTeams?league={}&lang=en-US&region=US&tz=America/New_York"
_FOX = (
    "https://api.foxsports.com/bifrost/v1/{}/league/teamnav?apikey="
    + FOX_KEY
    + "&api-version=1.1"
)
_CBS = "https://api.cbssports.com/napi/resource/league/teams/{}"  # CBS league ids: NBA 54, MLB 52, NHL 60
#: (source, file) -> (url, parser, legs: [(league, id_kind, id column)])
DIRECTORIES = {
    ("sports247", "football"): ("https://ipa.247sports.com/rdb/v1/teams/?sportKey=1", _rows_247,
                                [("cfb", "team", "src_team_id"), ("cfb", "school", "institution_key")]),
    ("sports247", "basketball"): ("https://ipa.247sports.com/rdb/v1/teams/?sportKey=2", _rows_247,
                                  [("mbb", "team", "src_team_id"), ("mbb", "school", "institution_key"),
                                   ("wbb", "school", "institution_key")]),
    ("on3", "football"): ("https://api.on3.com/public/rdb/v1/filters/teams?sportKey=1", _rows_on3,
                          [("cfb", "school", "org_key")]),
    ("on3", "basketball"): ("https://api.on3.com/public/rdb/v1/filters/teams?sportKey=2", _rows_on3,
                            [("mbb", "school", "org_key"), ("wbb", "school", "org_key")]),
    ("fox", "mlb"): (_FOX.format("mlb"), _rows_fox, [("mlb", "team", "src_team_id")]),
    ("fox", "nhl"): (_FOX.format("nhl"), _rows_fox, [("nhl", "team", "src_team_id")]),
    ("yahoo", "mlb"): (_YH.format("mlb"), _rows_yahoo, [("mlb", "team", "src_team_id")]),
    ("yahoo", "nhl"): (_YH.format("nhl"), _rows_yahoo, [("nhl", "team", "src_team_id")]),
    ("cbs", "mlb"): (_CBS.format(52), _rows_cbs, [("mlb", "team", "src_team_id")]),
    ("cbs", "nhl"): (_CBS.format(60), _rows_cbs, [("nhl", "team", "src_team_id")]),
    ("cbs", "nba"): (_CBS.format(54), _rows_cbs, [("nba", "team", "src_team_id")]),
}  # fmt: skip
LEAGUES = sorted({lg for *_, legs in DIRECTORIES.values() for lg, _, _ in legs})


# ---------------------------------------------------------------- capture (manual; see the module docstring)


def _path(source: str, name: str) -> Path:
    return RAW / source / f"{name}.csv.gz"


def _read(source: str, name: str) -> pl.DataFrame:
    """Every column Utf8: ids keep one dtype from the boundary on."""
    return pl.read_csv(gzip.open(_path(source, name)).read(), infer_schema=False)


def _get(url: str):
    try:
        from curl_cffi import requests as creq
    except ImportError as e:  # the 247 edge blocks plain requests (sdv-py sports247_runtime)
        raise ImportError("capture needs curl_cffi: uv run --frozen --with curl_cffi python -m "
                          "sdv_reference.team_directories") from e  # fmt: skip
    h = {"User-Agent": UA, "Accept": "application/json"}
    for host, site in (
        ("247sports", "https://247sports.com"),
        ("yahoo", "https://sports.yahoo.com"),
    ):
        if host in url:
            h |= {"Referer": site + "/", "Origin": site}
    r = creq.get(url, headers=h, impersonate="chrome", timeout=60)
    r.raise_for_status()
    return r.json()


def fetch(only: list[str] | None = None, today: dt.date | None = None) -> None:
    """Capture each directory once (or those whose source is in `only`), sequentially, DELAY seconds apart."""
    today = today or dt.date.today()
    first = True
    for (source, name), (url, parse, _) in DIRECTORIES.items():
        if only and source not in only:
            continue
        if not first:
            time.sleep(max(DELAY, 1.0))
        first = False
        rows = parse(_get(url))
        new = pl.DataFrame(rows).select(
            pl.lit(today.isoformat()).alias("captured"), pl.all().cast(pl.Utf8)
        )
        path = _path(source, name)
        old = (
            _read(source, name).filter(pl.col("captured") != today.isoformat())
            if path.exists()
            else None
        )
        df = pl.concat([old, new], how="diagonal_relaxed") if old is not None else new
        df = df.unique().sort(pl.all(), nulls_last=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
            df.write_csv(fh)
        path.write_bytes(buf.getvalue())
        prov_path = path.parent / "provenance.json"
        prov = json.loads(prov_path.read_text()) if prov_path.exists() else {}
        prov[path.name] = {"url": url, "captured": today.isoformat(), "rows": new.height,
                           "note": "one-off directory snapshot (owner decision 2026-11-03); not part of stage 10"}  # fmt: skip
        prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n")
        print(f"{source}/{name}: {new.height} rows ({url})", flush=True)


# ---------------------------------------------------------------- match (offline)


def _fold(s: str | None) -> str | None:
    """Accents and apostrophes dropped (Montréal -> Montreal, Saint Mary's -> Saint Marys, the slug saint-marys)."""
    return (
        unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().replace("'", "") if s else s
    )


def _norm(s: str | None) -> str:
    return normalize(_fold(s))


@cache
def _curated() -> tuple[dict, dict]:
    alias = pl.read_csv(CURATED / "team_directory_aliases.csv", infer_schema=False)
    names = pl.read_csv(CURATED / "team_directory_names.csv", infer_schema=False)
    return (
        {(s, k, i, lg or ""): t or "" for s, k, i, lg, t in alias.select("source", "id_kind", "source_id", "league", "espn_team_id").iter_rows()},
        {(lg or "", _norm(n)): t for lg, n, t in names.select("league", "name", "espn_team_id").iter_rows()},
    )  # fmt: skip


def _index(league: str, tgs: pl.DataFrame, season: int) -> dict[str, set[str]]:
    """normalized ESPN name -> {ESPN team id} for a league in a season; an ambiguous key keeps every id."""
    index: dict[str, set[str]] = {}
    if league in COLLEGE:
        pool = pl.read_csv(CURATED / "ncaa_espn_teams.csv", infer_schema=False)
        pool = pool.with_columns(
            pl.col(c).map_elements(_fold, return_dtype=pl.Utf8)
            for c in pool.columns
            if c != "team_id"
        )
        for k, v in _espn_index(pool).items():
            index.setdefault(k, set()).update(v)
    own = tgs.filter(
        (pl.col("team_id_source") == "espn") & (pl.col("season") == season)
    )
    for t, n in own.select("team_id", "team_name").iter_rows():
        if key := _norm(n):
            index.setdefault(key, set()).add(t)
    return index


def match(league: str, tgs: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(rows, unmatched) for a league's directory legs: rows in team_aliases.LONG; unmatched with a `reason`."""
    from sdv_reference.team_aliases import (  # deferred: team_aliases imports this module
        LONG,
        _long,
    )

    alias, names = _curated()
    found = []
    for (source, name), (url, _, legs) in DIRECTORIES.items():
        if not _path(source, name).exists():
            continue
        snap = _read(source, name).rename(
            {"team_id": "src_team_id"}, strict=False
        )  # team_id is the ESPN id below
        ev = f"raw/team_aliases/{source}/{name}.csv.gz ({url.split('?')[0]})"
        for lg, kind, col in legs:
            if lg != league:
                continue
            for captured in snap["captured"].unique().sort():
                season = current_season(
                    lg in ENDING_YEAR_LEAGUES, dt.date.fromisoformat(captured)
                )
                index = _index(lg, tgs, season)
                part = snap.filter(pl.col("captured") == captured)
                res = []
                for r in part.iter_rows(named=True):
                    cands = [r["name"]] + [r[c] for c in ("org_name", "slug") if r.get(c)]
                    hit = alias.get((source, kind, r[col], lg), alias.get((source, kind, r[col], "")))
                    how = None if hit is None else "alias" if hit else "not_espn"  # a blank alias: not an ESPN team
                    hit = hit or None
                    if not hit and how is None:
                        ids = set().union(*(index.get(_norm(c), set()) for c in cands))
                        hit, how = (
                            (next(iter(ids)), "exact_name")
                            if len(ids) == 1
                            else (None, "ambiguous" if ids else None)
                        )
                    if not hit and how is None:
                        ids = {
                            t
                            for c in cands
                            if (
                                t := names.get((lg, _norm(c)))
                                or names.get(("", _norm(c)))
                            )
                        }
                        hit, how = (
                            (next(iter(ids)), "dict")
                            if len(ids) == 1
                            else (None, "ambiguous" if ids else None)
                        )
                    res.append((hit, how))
                part = part.with_columns(
                    pl.lit(season, pl.Int32).alias("season"),
                    pl.Series("team_id", [h for h, _ in res], pl.Utf8),
                    pl.Series("_how", [m for _, m in res], pl.Utf8),
                    pl.lit(source).alias("_source"), pl.lit(kind).alias("_kind"), pl.lit(col).alias("_col"),
                    pl.lit(ev).alias("_ev"),
                )  # fmt: skip
                found.append(part)
    if not found:
        empty = pl.DataFrame(schema=LONG)
        return empty, empty.with_columns(pl.lit(None, pl.Utf8).alias("reason"))
    allr = pl.concat(found, how="diagonal_relaxed")
    rows, unmatched = [], []
    for (source, kind, col, ev), g in allr.group_by(
        "_source", "_kind", "_col", "_ev", maintain_order=True
    ):
        g = g.with_columns(pl.col(col).alias("_id"))
        names_ = {"name": "name"} | (
            {"abbreviation": "abbreviation"} if "abbreviation" in g.columns else {}
        )
        for how, gg in g.group_by("_how", maintain_order=True):
            how = how[0]
            out = _long(gg, source, "_id", names_, how or "unmatched", ev, id_kind=kind)
            if how in ("alias", "exact_name", "dict"):
                rows.append(out)
            else:
                why = ("the name matches several ESPN teams" if how == "ambiguous" else
                       "curated (team_directory_aliases.csv): not an ESPN team" if how == "not_espn"
                       else f"no ESPN {league} team has this name (no alias, exact name or dict match)")  # fmt: skip
                unmatched.append(out.with_columns(pl.lit(why).alias("reason"), pl.lit(None, pl.Utf8).alias("team_id"),
                                                  pl.lit("unmatched").alias("match_method")))  # fmt: skip
    rows_df = pl.concat(rows) if rows else pl.DataFrame(schema=LONG)
    un_df = (
        pl.concat(unmatched)
        if unmatched
        else pl.DataFrame(schema={**LONG, "reason": pl.Utf8})
    ).cast({**LONG, "reason": pl.Utf8})
    return rows_df, un_df


if __name__ == "__main__":
    fetch(sys.argv[1:] or None)
