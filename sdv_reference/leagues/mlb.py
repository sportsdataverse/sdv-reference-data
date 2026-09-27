"""MLB groups reference: the MLB Stats API teams by season (membership, names, native ids), cross-checked with ESPN.

uv run python -m sdv_reference.leagues.mlb fetch   # network: refresh raw/mlb/ snapshots
uv run python -m sdv_reference.build mlb            # offline: raw/mlb/ + curated/ -> build/mlb/

Season key: the single year. Leagues 1901+ (AL 103, NL 104, and the Federal League 106 in 1914-15, which the
Stats API files under sportId 1); divisions 1969+ (200-205).
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
from pathlib import Path

import polars as pl
import requests

from sdv_reference.schema import conform

LEAGUE = "mlb"
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw" / LEAGUE
CURATED = ROOT / "curated"
# https with a query string returns 406 from the droplet; the API refuses the decodo proxy
STATSAPI = "http://statsapi.mlb.com/api/v1"
UA = {
    "User-Agent": "sdv-reference-data/0.1 (+https://github.com/sportsdataverse/sdv-reference-data)"
}
FIRST_SEASON = 1901


# -- raw snapshot helpers (shared with nhl.py) ------------------------------------------------------------------


def write_json_gz(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the bytes stable across re-fetches of unchanged data
    with gzip.GzipFile(path, "wb", mtime=0) as f:
        f.write(json.dumps(obj, sort_keys=True, indent=0).encode())


def read_json_gz(path: Path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def espn_walk(
    league: str, seasons: list[int], path: Path, roots: tuple[str, ...] = ()
) -> list[dict]:
    """Walk ESPN's core group tree per season into `path`, in the research cache's row format (leaves carry
    `team_ids`), from the top-level groups or from `roots`. Only refresh.seasons_to_fetch() seasons are walked; the
    rest of `path` is kept, and a season whose top-level group list is empty (ESPN doesn't have it yet) stays absent.
    A first fetch (no `path`) takes seasons before the refresh window from the research cache when it has them.
    The file is rewritten after every season."""
    from sdv_reference.espn import CORE, LEAGUE_PATHS, cached_groups
    from sdv_reference.espn import get_json as _get_json
    from sdv_reference.refresh import seasons_to_fetch

    def get_json(url: str) -> dict:
        # the proxy drops the odd TLS handshake; espn.get_json only retries HTTP status codes
        for attempt in range(4):
            try:
                return _get_json(url)
            except requests.ConnectionError:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))

    rows = read_json_gz(path) if path.exists() else []
    todo = seasons_to_fetch(seasons, {r["season"] for r in rows})
    live = set(todo) if rows else set(seasons_to_fetch(seasons, seasons))
    old = {s: [r for r in rows if r["season"] == s] for s in todo}
    rows = [r for r in rows if r["season"] not in todo]
    cache = {}
    for r in cached_groups(league):
        cache.setdefault(r["season"], []).append(r)
    sport, lg = LEAGUE_PATHS[league]
    base = f"{CORE}/{sport}/leagues/{lg}/seasons"

    def ids(d: dict, pat: str) -> list[str]:
        return [re.search(pat, i["$ref"]).group(1) for i in d.get("items", [])]

    def walk(season: int, gid: str, depth: int) -> None:
        url = f"{base}/{season}/types/2/groups/{gid}"
        g = get_json(url)
        row = {
            k: g.get(k)
            for k in (
                "id",
                "uid",
                "name",
                "abbreviation",
                "shortName",
                "midsizeName",
                "slug",
                "isConference",
            )
        }
        parent = (g.get("parent") or {}).get("$ref", "")
        m = re.search(r"/groups/(\d+)", parent)
        row.update(
            season=season, parent=m.group(1) if m else None, depth=depth, children=[]
        )
        if "children" in g:
            row["children"] = ids(
                get_json(f"{url}/children?limit=100"), r"/groups/(\d+)"
            )
        if not row["children"]:
            row["team_ids"] = ids(get_json(f"{url}/teams?limit=1000"), r"/teams/(\d+)")
        rows.append(row)
        for k in row["children"]:
            walk(season, k, depth + 1)

    for s in todo:
        n = len(rows)
        cached = cache.get(s, [])
        if (
            s not in live
            and cached
            and all("team_ids" in r for r in cached if not r.get("children"))
        ):
            rows.extend(cached)
        else:
            top = ids(
                get_json(f"{base}/{s}/types/2/groups?limit=100"), r"/groups/(\d+)"
            )
            for gid in roots if top and roots else top:
                walk(s, gid, 0)
        if len(rows) == n:  # ESPN has nothing for the season: keep what raw/ had, if anything
            rows.extend(old[s])
        # seasons still to walk keep their old rows, so an interrupted refresh never drops one
        write_json_gz(path, rows + [r for t in todo if t > s for r in old[t]])
        print(
            f"espn {league} {s}: {sum(r['season'] == s for r in rows)} groups",
            flush=True,
        )
    return rows


# -- build helpers (shared with nhl.py) -------------------------------------------------------------------------


def espn_check(
    tgs: pl.DataFrame, espn_rows: list[dict], group_map: dict[str, str]
) -> tuple[pl.DataFrame, str]:
    """Cross-check each ESPN-keyed team-season against ESPN's walk and set `sources_agree` and `notes`.

    Seasons ESPN has no teams for stay null. ESPN group ids map to SDV ids through `group_map`; an unmapped id
    becomes `espn:{id}` so it can't agree. Where ESPN has divisions that season both levels are compared,
    otherwise the conference only. Returns the table and a one-line summary for the league's notes."""
    mem = []
    for r in espn_rows:
        for t in r.get("team_ids") or []:
            leaf = group_map.get(r["id"], f"espn:{r['id']}")
            parent = group_map.get(r["parent"], f"espn:{r['parent']}")
            is_div = r["depth"] == 1
            mem.append(
                (r["season"], t, parent if is_div else leaf, leaf if is_div else None)
            )
    espn = pl.DataFrame(
        mem,
        schema={
            "season": pl.Int32,
            "team_id": pl.Utf8,
            "e_conf": pl.Utf8,
            "e_div": pl.Utf8,
        },
        orient="row",
    ).unique()
    has_div = espn.group_by("season").agg(
        pl.col("e_div").is_not_null().any().alias("has_div")
    )
    # a team listed in two groups in one season can't agree with anything
    espn = espn.group_by(["season", "team_id"]).agg(
        pl.when(pl.len() == 1)
        .then(pl.col("e_conf").first())
        .otherwise(pl.lit("espn:several"))
        .alias("e_conf"),
        pl.col("e_div").first(),
    )
    t = tgs.join(has_div, on="season", how="left").join(
        espn, on=["season", "team_id"], how="left"
    )
    checked = (pl.col("team_id_source") == "espn") & pl.col("has_div").is_not_null()
    agree = pl.col("conference_id").eq_missing(pl.col("e_conf")) & (
        ~pl.col("has_div") | pl.col("division_id").eq_missing(pl.col("e_div"))
    )
    t = t.with_columns(
        pl.when(checked)
        .then(pl.col("e_conf").is_not_null() & agree)
        .alias("sources_agree")
    ).with_columns(
        pl.when(pl.col("sources_agree") == False)
        .then(
            pl.col("notes")
            + pl.when(pl.col("e_conf").is_null())
            .then(pl.lit("; espn: absent from its groups this season"))
            .otherwise(
                pl.lit("; espn: ")
                + pl.col("e_conf")
                + pl.lit(" / ")
                + pl.col("e_div").fill_null("-")
            )
        )
        .otherwise(pl.col("notes"))
        .alias("notes")
    )
    extra = espn.join(
        tgs.select("season", "team_id"), on=["season", "team_id"], how="anti"
    ).filter(pl.col("season").is_in(tgs["season"].unique().implode()))
    n_ok = int((t["sources_agree"] == True).sum())
    n_bad = int((t["sources_agree"] == False).sum())
    seasons = t.filter(checked)["season"]
    eg = (
        extra.sort("season")
        .head(4)
        .select(pl.format("{} espn {}", "season", "team_id").alias("eg"))["eg"]
        .to_list()
    )
    summary = (
        f"ESPN cross-check {seasons.min()}-{seasons.max()}: {n_ok} team-seasons agree, {n_bad} disagree, "
        f"{extra.height} ESPN-only team-seasons (e.g. {', '.join(eg) or 'none'})"
        if seasons.len()
        else "ESPN cross-check: no ESPN seasons in raw/"
    )
    return t.drop("has_div", "e_conf", "e_div"), summary


def assemble(
    league: str,
    tgs: pl.DataFrame,
    names: pl.DataFrame,
    group_notes: dict[str, str],
    aliases: pl.DataFrame,
) -> dict[str, pl.DataFrame]:
    """The four tables from team-seasons, per-(group_id, season) names and aliases. Division parents come from
    their members' conference (the league root where there is none); conference parents are the league root."""
    root = f"{league}:{league}"
    lg = (
        tgs.group_by("season")
        .agg(pl.len().alias("n_teams"))
        .with_columns(
            group_id=pl.lit(root),
            level=pl.lit("league"),
            parent_group_id=pl.lit(None, pl.Utf8),
        )
    )
    conf = (
        tgs.filter(pl.col("conference_id").is_not_null())
        .group_by("conference_id", "season")
        .agg(pl.len().alias("n_teams"))
        .rename({"conference_id": "group_id"})
        .with_columns(level=pl.lit("conference"), parent_group_id=pl.lit(root))
    )
    div = (
        tgs.filter(pl.col("division_id").is_not_null())
        .group_by("division_id", "season")
        .agg(
            pl.len().alias("n_teams"), pl.col("conference_id").unique().alias("parents")
        )
        .rename({"division_id": "group_id"})
    )
    split = div.filter(pl.col("parents").list.len() > 1)
    if split.height:
        raise ValueError(
            f"{league}: divisions spanning conferences: {split.rows()[:3]}"
        )
    div = div.with_columns(
        level=pl.lit("division"),
        parent_group_id=pl.col("parents").list.first().fill_null(root),
    ).drop("parents")
    gs = pl.concat([lg, conf, div], how="diagonal").join(
        names, on=["group_id", "season"], how="left"
    )
    unnamed = gs.filter(pl.col("name").is_null())
    if unnamed.height:
        raise ValueError(
            f"{league}: group-seasons without a name: {unnamed.select('group_id', 'season').rows()[:5]}"
        )
    gs = gs.with_columns(league=pl.lit(league))
    groups = (
        gs.group_by("group_id", "level")
        .agg(
            pl.col("season").min().alias("first_season"),
            pl.col("season").max().alias("last_season"),
        )
        .with_columns(
            league=pl.lit(league),
            notes=pl.col("group_id").replace_strict(
                group_notes, default=None, return_dtype=pl.Utf8
            ),
        )
    )
    return {
        "groups": conform("groups", groups).sort("level", "group_id"),
        "group_seasons": conform("group_seasons", gs).sort("group_id", "season"),
        "group_aliases": conform(
            "group_aliases", aliases.with_columns(league=pl.lit(league))
        )
        .unique()
        .sort(
            "group_id", "source", "name_kind", "valid_from", "value", nulls_last=True
        ),
        "team_group_seasons": conform(
            "team_group_seasons", tgs.with_columns(league=pl.lit(league))
        ).sort("season", "team_id"),
    }


def espn_aliases(
    espn_rows: list[dict], group_map: dict[str, str], gs_seasons: pl.DataFrame
) -> pl.DataFrame:
    """ESPN's labels (today's strings) per mapped group id, valid over the seasons both ESPN and the group have;
    valid_to is null when that reaches the latest season."""
    rows = [
        (group_map[r["id"]], r["id"], r["season"], kind, r.get(key))
        for r in espn_rows
        if r["id"] in group_map
        for kind, key in (
            ("name", "name"),
            ("short_name", "shortName"),
            ("abbreviation", "abbreviation"),
        )
        if r.get(key)
    ]
    df = pl.DataFrame(
        rows,
        schema={
            "group_id": pl.Utf8,
            "source_id": pl.Utf8,
            "season": pl.Int32,
            "name_kind": pl.Utf8,
            "value": pl.Utf8,
        },
        orient="row",
    ).join(gs_seasons, on=["group_id", "season"], how="semi")
    latest = gs_seasons["season"].max()
    return (
        df.group_by("group_id", "source_id", "name_kind", "value")
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
        )
        .with_columns(
            source=pl.lit("espn"),
            valid_to=pl.when(pl.col("valid_to") < latest).then(pl.col("valid_to")),
        )
    )


# -- fetch ------------------------------------------------------------------------------------------------------


def _statsapi(path: str, session: requests.Session) -> dict:
    for attempt in range(4):
        r = session.get(f"{STATSAPI}/{path}", headers=UA, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.5)
        return r.json()
    r.raise_for_status()
    return r.json()


def fetch(last_season: int | None = None) -> None:
    """Snapshot raw/mlb/: Stats API teams, divisions and leagues per season, then ESPN's group walk. Only the
    refresh.seasons_to_fetch() seasons are requested; a season the Stats API has no teams for stays absent."""
    from sdv_reference.refresh import current_season, seasons_to_fetch

    last = last_season or current_season(ending_year=False)
    seasons = list(range(FIRST_SEASON, last + 1))
    s = requests.Session()
    path = RAW / "statsapi_teams.json.gz"
    out = read_json_gz(path) if path.exists() else {}
    for y in seasons_to_fetch(seasons, map(int, out)):
        teams = _statsapi(f"teams?sportId=1&season={y}", s).get("teams", [])
        if not teams:
            continue
        out[str(y)] = {
            "teams": teams,
            "leagues": _statsapi(f"league?sportId=1&seasons={y}", s).get("leagues", []),
            "divisions": _statsapi(f"divisions?sportId=1&season={y}", s).get(
                "divisions", []
            )
            if y >= 1969
            else [],
        }
        print(f"statsapi {y}: {len(out[str(y)]['teams'])} teams", flush=True)
    write_json_gz(path, out)
    espn_walk(LEAGUE, seasons, RAW / "espn_groups.json.gz")


# -- build ------------------------------------------------------------------------------------------------------


def build() -> dict[str, pl.DataFrame]:
    """The four mlb_groups tables, offline from raw/mlb/ and curated/."""
    cur = pl.read_csv(CURATED / "mlb_groups.csv", infer_schema_length=0)
    by_mlb = dict(cur.select("mlb_id", "group_id").iter_rows())
    xwalk = dict(
        pl.read_csv(CURATED / "mlb_team_espn.csv", infer_schema_length=0)
        .select("mlb_team_id", "espn_team_id")
        .iter_rows()
    )
    raw = read_json_gz(RAW / "statsapi_teams.json.gz")

    teams, names = [], []
    for season, d in raw.items():
        y = int(season)
        for t in d["teams"]:
            # expansion clubs are listed a season or two early with no league (1968 KC/MON/SEA, 1992 COL/FLA, ...)
            if (
                not (t.get("league") or {}).get("id")
                or t.get("allStarStatus", "N") != "N"
            ):
                continue
            teams.append(
                (
                    y,
                    str(t["id"]),
                    t["name"].removesuffix(" - Deprecate"),  # the Stats API's flag on 6144 Cleveland Cubs
                    str(t["league"]["id"]),
                    str((t.get("division") or {}).get("id") or "") or None,
                )
            )
        for g in d["leagues"] + d["divisions"]:
            if str(g["id"]) in by_mlb and g.get("name"):
                names.append(
                    (
                        by_mlb[str(g["id"])],
                        y,
                        g["name"],
                        g.get("nameShort"),
                        g.get("abbreviation"),
                    )
                )
    tgs = pl.DataFrame(
        teams,
        schema={
            "season": pl.Int32,
            "mlb_team_id": pl.Utf8,
            "team_name": pl.Utf8,
            "league_id": pl.Utf8,
            "division_id_mlb": pl.Utf8,
        },
        orient="row",
    )
    unknown = set(tgs["league_id"]) | set(tgs["division_id_mlb"].drop_nulls())
    if unknown - set(by_mlb):
        raise ValueError(
            f"mlb: league/division ids missing from curated/mlb_groups.csv: {sorted(unknown - set(by_mlb))}"
        )
    tgs = tgs.with_columns(
        pl.col("season").cast(pl.Int32),
        conference_id=pl.col("league_id").replace_strict(by_mlb),
        division_id=pl.col("division_id_mlb").replace_strict(by_mlb, default=None),
        team_id=pl.col("mlb_team_id").replace(xwalk),
        team_id_source=pl.when(pl.col("mlb_team_id").is_in(list(xwalk)))
        .then(pl.lit("espn"))
        .otherwise(pl.lit("mlb")),
        subdivision_id=pl.lit(None, pl.Utf8),
        source=pl.lit("mlb"),
        notes=pl.format("mlb_team_id={}", "mlb_team_id"),
    )
    # every AL/NL club belongs to a franchise ESPN carries; a new MLB id must be curated, not fall back silently
    stray = tgs.filter(pl.col("conference_id").is_in(["mlb:al", "mlb:nl"]) & (pl.col("team_id_source") == "mlb"))
    if stray.height:
        raise ValueError(f"mlb: AL/NL team ids missing from curated/mlb_team_espn.csv: "
                         f"{stray.select('mlb_team_id', 'team_name').unique().rows()[:5]}")

    espn_rows = read_json_gz(RAW / "espn_groups.json.gz")
    group_map = dict(
        cur.filter(pl.col("espn_group_id").is_not_null())
        .select("espn_group_id", "group_id")
        .iter_rows()
    )
    tgs, summary = espn_check(tgs, espn_rows, group_map)

    root = cur.filter(pl.col("level") == "league").row(0, named=True)
    seasons = tgs["season"].unique().sort()
    names = pl.concat(
        [
            pl.DataFrame(
                names,
                schema={
                    "group_id": pl.Utf8,
                    "season": pl.Int32,
                    "name": pl.Utf8,
                    "short_name": pl.Utf8,
                    "abbreviation": pl.Utf8,
                },
                orient="row",
            ),
            pl.DataFrame(
                {
                    "group_id": root["group_id"],
                    "season": seasons,
                    "name": root["name"],
                    "short_name": root["short_name"],
                    "abbreviation": root["abbreviation"],
                }
            ),
        ]
    ).with_columns(pl.col("season").cast(pl.Int32))
    notes = dict(cur.select("group_id", "notes").iter_rows())
    notes[root["group_id"]] = f"{notes[root['group_id']]}. {summary}"

    member_seasons = pl.concat(
        [
            tgs.select(pl.col(c).alias("group_id"), "season").drop_nulls()
            for c in ("conference_id", "division_id")
        ]
        + [tgs.select(pl.lit(root["group_id"]).alias("group_id"), "season")]
    ).unique()
    latest = tgs["season"].max()
    mlb_alias = (
        names.join(member_seasons, on=["group_id", "season"], how="semi")
        .unpivot(
            index=["group_id", "season"],
            on=["name", "short_name", "abbreviation"],
            variable_name="name_kind",
            value_name="value",
        )
        .drop_nulls("value")
        .group_by("group_id", "name_kind", "value")
        .agg(
            pl.col("season").min().alias("valid_from"),
            pl.col("season").max().alias("valid_to"),
        )
        .join(
            cur.select("group_id", pl.col("mlb_id").alias("source_id")), on="group_id"
        )
        .with_columns(
            source=pl.lit("mlb"),
            valid_to=pl.when(pl.col("valid_to") < latest).then(pl.col("valid_to")),
        )
    )
    aliases = pl.concat(
        [mlb_alias, espn_aliases(espn_rows, group_map, member_seasons)], how="diagonal"
    )
    return assemble(LEAGUE, tgs, names, notes, aliases)


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
