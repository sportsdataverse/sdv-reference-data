"""NBA groups by season (ENDING-year keys: 2025 = 2024-25).

Also hosts the helpers shared by `nfl` and `wnba` (ESPN pro-league walk, table assembly, alias windows): they live
here rather than in a helper module so this builder stays inside its own files.

Sources (ClaudeCowork notes/2026-09-26-conference-reference/05-pro-leagues.md, 01-espn-groups.md):
- membership 1996-97 → 2026-27: the captured stats.nba.com `leaguestandingsv3` JSON (hoopR-nba-stats-raw, one
  directory per START year; `SeasonID = "2" + start year`), `Conference`/`Division` per `TeamID`;
- membership 1970-71 → 1995-96: curated/nba_membership_static.csv (cited per row);
- cross-check: ESPN core groups per season (1984-85+), mapped to SDV ids through curated/nba_groups.csv;
- team ids: ESPN ids through curated/nba_teams.csv (stats TeamID windows → ESPN id); names from the stats feed
  (as of the season) or the static table.

`fetch()` writes raw/nba/ (copies of the captured JSON + live ESPN walks); `build()` reads only raw/ and curated/.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import requests

from sdv_reference.espn import CORE, LEAGUE_PATHS, get_json
from sdv_reference.schema import conform

ROOT = Path(__file__).resolve().parents[2]
RAW, CURATED = ROOT / "raw", ROOT / "curated"

LEAGUE = "nba"
STATS_RAW = Path(
    os.environ.get(
        "SDV_NBA_STATS_RAW",
        "/mnt/sdv_repos/hoopR-nba-stats-raw/nba_stats/json/leaguestandingsv3",
    )
)
STATS_SEASONS = range(1997, 2028)  # ending years; directory = season - 1
ESPN_SEASONS = range(1985, 2028)  # ESPN NBA groups start with 1984-85


# ================================================================ shared helpers (nfl, wnba import these)


def write_gz(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = obj if isinstance(obj, bytes) else json.dumps(obj, sort_keys=True).encode()
    path.write_bytes(gzip.compress(data, mtime=0))


def read_gz_json(path: Path):
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


def _get(url: str, session: requests.Session) -> dict:
    """espn.get_json plus retries on proxy timeouts and dropped connections."""
    for attempt in range(5):
        try:
            return get_json(url, session, pause=0.6)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))
    raise AssertionError("unreachable")


def _ref_id(ref: dict | None) -> str | None:
    return re.search(r"/(\d+)(?:\?|$)", ref["$ref"]).group(1) if ref else None


def _item_ids(d: dict, pattern: str = r"/(\d+)(?:\?|$)") -> list[str]:
    assert d.get("pageCount", 1) <= 1, "paged ESPN list"
    return [re.search(pattern, i["$ref"]).group(1) for i in d.get("items", [])]


def espn_walk(
    league: str, season: int, session: requests.Session, team_objects: bool = False
) -> dict:
    """One season of an ESPN pro league: every group (conference → division) with its leaf team ids, the season's
    team list, and optionally each team-season object (name / abbreviation as ESPN has it for that season)."""
    sport, lg = LEAGUE_PATHS[league]
    base = f"{CORE}/{sport}/leagues/{lg}/seasons/{season}"
    groups = []

    def visit(gid: str, depth: int) -> None:
        g = _get(f"{base}/types/2/groups/{gid}", session)
        row = {
            k: g.get(k)
            for k in ("id", "name", "abbreviation", "shortName", "slug", "isConference")
        }
        row |= {"season": season, "parent": _ref_id(g.get("parent")), "depth": depth}
        kids = (
            _item_ids(_get(f"{base}/types/2/groups/{gid}/children?limit=100", session))
            if "children" in g
            else []
        )
        row["children"] = kids
        if not kids:
            row["team_ids"] = _item_ids(
                _get(f"{base}/types/2/groups/{gid}/teams?limit=1000", session),
                r"/teams/(\d+)",
            )
        groups.append(row)
        for k in kids:
            visit(k, depth + 1)

    top = _get(f"{base}/types/2/groups?limit=100", session)
    for gid in _item_ids(top):
        visit(gid, 0)
    season_teams = _item_ids(_get(f"{base}/teams?limit=1000", session), r"/teams/(\d+)")
    out = {
        "league": league,
        "season": season,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "groups": groups,
        "season_team_ids": season_teams,
    }
    if team_objects:
        teams = {}
        for tid in sorted(
            set(season_teams) | {t for g in groups for t in g.get("team_ids", [])},
            key=int,
        ):
            t = _get(f"{base}/teams/{tid}", session)
            teams[tid] = {
                k: t.get(k) for k in ("location", "name", "displayName", "abbreviation")
            } | {"group": _ref_id(t.get("groups"))}
        out["teams"] = teams
    return out


def fetch_espn(
    league: str, seasons, team_objects: bool = False, refresh_from: int | None = None
) -> None:
    """Walk ESPN for each season into raw/{league}/espn/{season}.json.gz; closed seasons already on disk are kept."""
    session = requests.Session()
    for s in seasons:
        p = RAW / league / "espn" / f"{s}.json.gz"
        if p.exists() and (refresh_from is None or s < refresh_from):
            continue
        write_gz(p, espn_walk(league, s, session, team_objects))
        print(f"{league}: espn {s}", flush=True)


def espn_frames(league: str) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """From raw/{league}/espn: (groups: season, espn_group_id, name, abbreviation, slug, parent, is_conference),
    (members: season, espn_group_id, espn_team_id), (teams: season, espn_team_id, location, name, display_name,
    abbreviation, group)."""
    g_rows, m_rows, t_rows = [], [], []
    for p in sorted((RAW / league / "espn").glob("*.json.gz")):
        d = read_gz_json(p)
        s = d["season"]
        for g in d["groups"]:
            g_rows.append(
                (
                    s,
                    g["id"],
                    g["name"],
                    g["abbreviation"],
                    g["slug"],
                    g["parent"],
                    bool(g["isConference"]),
                )
            )
            m_rows += [(s, g["id"], t) for t in g.get("team_ids", [])]
        for tid, t in d.get("teams", {}).items():
            t_rows.append(
                (
                    s,
                    tid,
                    t["location"],
                    t["name"],
                    t["displayName"],
                    t["abbreviation"],
                    t["group"],
                )
            )
    groups = pl.DataFrame(
        g_rows,
        schema={
            "season": pl.Int32,
            "espn_group_id": pl.Utf8,
            "name": pl.Utf8,
            "abbreviation": pl.Utf8,
            "slug": pl.Utf8,
            "parent": pl.Utf8,
            "is_conference": pl.Boolean,
        },
        orient="row",
    )
    members = pl.DataFrame(
        m_rows,
        schema={"season": pl.Int32, "espn_group_id": pl.Utf8, "espn_team_id": pl.Utf8},
        orient="row",
    )
    teams = pl.DataFrame(
        t_rows,
        schema={
            "season": pl.Int32,
            "espn_team_id": pl.Utf8,
            "location": pl.Utf8,
            "name": pl.Utf8,
            "display_name": pl.Utf8,
            "abbreviation": pl.Utf8,
            "group": pl.Utf8,
        },
        orient="row",
    )
    return groups, members, teams


def copy_stats_standings(league: str, src: Path, dirs) -> None:
    """Snapshot the captured stats `leaguestandingsv3` regular-season JSON into raw/{league}/stats/{start}.json.gz."""
    for d in dirs:
        f = src / str(d) / "regular-season.json"
        write_gz(RAW / league / "stats" / f"{d}.json.gz", f.read_bytes())


# ================================================================ NBA fetch / build


def fetch() -> None:
    """Network + sibling-repo reads: raw/nba/stats/{start}.json.gz and raw/nba/espn/{season}.json.gz."""
    copy_stats_standings(LEAGUE, STATS_RAW, [s - 1 for s in STATS_SEASONS])
    fetch_espn(LEAGUE, ESPN_SEASONS, refresh_from=max(ESPN_SEASONS) - 1)


def read_curated(name: str) -> pl.DataFrame:
    """A curated CSV with every column as Utf8 except *_season (Int32)."""
    df = pl.read_csv(CURATED / name, infer_schema=False)
    return df.with_columns(
        pl.col(c).cast(pl.Int32) for c in df.columns if c.endswith("_season")
    )


def in_window(df: pl.DataFrame, season: str = "season") -> pl.DataFrame:
    return df.filter(
        pl.col(season).is_between(pl.col("first_season"), pl.col("last_season"))
    )


def expand_seasons(df: pl.DataFrame) -> pl.DataFrame:
    """One row per season in each row's [first_season, last_season] (window columns dropped)."""
    lo, hi = int(df["first_season"].min()), int(df["last_season"].max())
    seasons = pl.DataFrame({"season": range(lo, hi + 1)}, schema={"season": pl.Int32})
    return in_window(df.join(seasons, how="cross")).drop("first_season", "last_season")


def join_window(
    left: pl.DataFrame, right: pl.DataFrame, on: str | list[str], what: str
) -> pl.DataFrame:
    """Join each left row to the one right row whose [first_season, last_season] holds its season; fail loudly
    on a miss or an overlap."""
    on = [on] if isinstance(on, str) else on
    out = in_window(left.join(right, on=on, how="left")).drop(
        "first_season", "last_season"
    )
    if out.height != left.height:
        missed = left.join(out.select(on + ["season"]), on=on + ["season"], how="anti")
        raise ValueError(
            f"{what}: {left.height - out.height:+d} rows off after the windowed join, e.g. {missed.head(3).rows()}"
        )
    return out


def espn_membership(
    league: str, gdef: pl.DataFrame, team_alias: dict[str, str] | None = None
) -> tuple[pl.DataFrame, pl.DataFrame, set[int]]:
    """ESPN leaf membership mapped to SDV ids: (season, espn_team_id, conference_id, division_id) with one row per
    team-season ("|"-joined when ESPN lists a team twice), the ghost rows (groups outside their SDV window, e.g.
    NFL 2001 'NFC North' = Pro Bowl teams, NBA 2003-04 'Southeast' = Charlotte), and the seasons ESPN covers."""
    _, members, _ = espn_frames(league)
    if team_alias:  # ESPN duplicates of one team (e.g. WNBA Sacramento 2793 = 13)
        members = members.with_columns(
            pl.col("espn_team_id").replace(team_alias)
        ).unique()
    seasons = set(members["season"].unique())
    m = gdef.filter(pl.col("espn_group_id").is_not_null()).select(
        "espn_group_id",
        "group_id",
        "level",
        "parent_group_id",
        "first_season",
        "last_season",
    )
    j = members.join(m, on="espn_group_id", how="left")
    ok = in_window(j)
    ghosts = j.join(
        ok.select("season", "espn_group_id", "espn_team_id"),
        on=["season", "espn_group_id", "espn_team_id"],
        how="anti",
    )
    mapped = (
        ok.with_columns(
            conference_id=pl.when(pl.col("level") == "conference")
            .then(pl.col("group_id"))
            .otherwise(pl.col("parent_group_id")),
            division_id=pl.when(pl.col("level") == "division").then(pl.col("group_id")),
        )
        .group_by("season", "espn_team_id")
        .agg(
            pl.col("conference_id").unique().sort().str.join("|"),
            pl.col("division_id").drop_nulls().unique().sort().str.join("|"),
        )
    )
    return mapped, ghosts, seasons


def cross_check(
    tgs: pl.DataFrame, espn: pl.DataFrame, espn_seasons: set[int]
) -> pl.DataFrame:
    """Set sources_agree against ESPN: null when ESPN doesn't cover the season or doesn't list the team."""
    j = tgs.join(
        espn.rename(
            {
                "espn_team_id": "team_id",
                "conference_id": "espn_conf",
                "division_id": "espn_div",
            }
        ),
        on=["season", "team_id"],
        how="left",
    )
    same = (pl.col("conference_id").fill_null("") == pl.col("espn_conf")) & (
        pl.col("division_id").fill_null("") == pl.col("espn_div").fill_null("")
    )
    covered = pl.col("season").is_in(sorted(espn_seasons))
    note = (
        pl.when(covered & pl.col("espn_conf").is_null())
        .then(pl.lit("not in ESPN's group lists"))
        .when(covered & ~same)
        .then(
            pl.lit("ESPN: ")
            + pl.when(pl.col("espn_div") != "")
            .then(pl.col("espn_div"))
            .otherwise(pl.col("espn_conf"))
        )
    )
    return j.with_columns(
        sources_agree=pl.when(covered & pl.col("espn_conf").is_not_null()).then(same),
        notes=pl.concat_str([pl.col("notes"), note], separator="; ", ignore_nulls=True),
    ).drop("espn_conf", "espn_div")


def windows(df: pl.DataFrame, last: int) -> pl.DataFrame:
    """(group_id, source, source_id, name_kind, value, season) → one row per consecutive run of seasons;
    valid_to is null when the run reaches the league's latest season (still in use)."""
    keys = ["group_id", "source", "source_id", "name_kind", "value"]
    return (
        df.select(keys + ["season"])
        .unique()
        .sort(keys + ["season"], nulls_last=True)
        .with_columns(
            run=(pl.col("season").diff().over(keys).fill_null(1) != 1)
            .cum_sum()
            .over(keys)
        )
        .group_by(keys + ["run"])
        .agg(valid_from=pl.col("season").min(), valid_to=pl.col("season").max())
        .with_columns(
            valid_to=pl.when(pl.col("valid_to") < last).then(pl.col("valid_to"))
        )
        .drop("run")
    )


def espn_aliases(league: str, gdef: pl.DataFrame, gs: pl.DataFrame) -> pl.DataFrame:
    """ESPN's id, name, abbreviation and slug for each SDV group, in the seasons both have it (ghosts dropped)."""
    groups, _, _ = espn_frames(league)
    m = (
        gdef.filter(pl.col("espn_group_id").is_not_null())
        .select("espn_group_id", "group_id")
        .unique()
    )
    g = groups.join(m, on="espn_group_id").join(
        gs.select("group_id", "season"), on=["group_id", "season"]
    )
    return (
        pl.concat(
            [
                g.select(
                    "group_id",
                    "season",
                    source_id="espn_group_id",
                    value=pl.col(c),
                    name_kind=pl.lit(k),
                )
                for c, k in (
                    ("name", "name"),
                    ("abbreviation", "abbreviation"),
                    ("slug", "slug"),
                )
            ]
        )
        .filter(pl.col("value").is_not_null())
        .with_columns(source=pl.lit("espn"))
    )


def assemble(
    league: str, tgs: pl.DataFrame, gdef: pl.DataFrame, aliases: list[pl.DataFrame]
) -> dict[str, pl.DataFrame]:
    """The four contract tables from team-seasons + curated group definitions (+ source alias rows with seasons)."""
    root = gdef.filter(pl.col("level") == "league")["group_id"].item()
    counts = pl.concat(
        [
            tgs.filter(pl.col(c).is_not_null())
            .group_by(c, "season")
            .agg(n_teams=pl.len())
            .select(group_id=c, season="season", n_teams="n_teams")
            for c in ("division_id", "conference_id")
        ]
        + [
            tgs.group_by("season")
            .agg(n_teams=pl.len())
            .select(group_id=pl.lit(root), season="season", n_teams="n_teams")
        ]
    )
    labels = gdef.select(
        "group_id",
        "level",
        "parent_group_id",
        "name",
        "short_name",
        "abbreviation",
        "first_season",
        "last_season",
    )
    gs = join_window(counts, labels, "group_id", f"{league} group labels")
    last = int(gs["season"].max())
    groups = (
        gs.group_by("group_id", "level")
        .agg(first_season=pl.col("season").min(), last_season=pl.col("season").max())
        .join(
            gdef.group_by("group_id").agg(
                pl.col("notes").drop_nulls().unique(maintain_order=True).str.join(" ")
            ),
            on="group_id",
        )
        .with_columns(notes=pl.when(pl.col("notes") != "").then(pl.col("notes")))
    )
    sdv = pl.concat(
        [
            gs.select("group_id", "season", value=pl.col(c), name_kind=pl.lit(c))
            for c in ("name", "short_name", "abbreviation")
        ]
    ).with_columns(source=pl.lit("sdv"), source_id=pl.lit(None, pl.Utf8))
    cols = ["group_id", "season", "source", "source_id", "name_kind", "value"]
    al = windows(pl.concat([a.select(cols) for a in [sdv, *aliases]]), last)
    lit = pl.lit(league).alias("league")
    return {
        "groups": conform("groups", groups.with_columns(lit)),
        "group_seasons": conform("group_seasons", gs.with_columns(lit)),
        "group_aliases": conform("group_aliases", al.with_columns(lit)),
        "team_group_seasons": conform("team_group_seasons", tgs.with_columns(lit)),
    }


def stats_rows(league: str) -> pl.DataFrame:
    """Every team-season in raw/{league}/stats: dir (start year), team id, name, Conference, Division."""
    rows = []
    for p in sorted((RAW / league / "stats").glob("*.json.gz")):
        rs = read_gz_json(p)["resultSets"][0]
        h = rs["headers"]
        for r in rs["rowSet"]:
            x = dict(zip(h, r, strict=True))
            rows.append(
                (
                    int(p.name.split(".")[0]),
                    str(x["TeamID"]),
                    f"{x['TeamCity']} {x['TeamName']}",
                    x["Conference"],
                    x["Division"],
                )
            )
    return pl.DataFrame(
        rows,
        schema={
            "dir": pl.Int32,
            "native_id": pl.Utf8,
            "team_name": pl.Utf8,
            "conference": pl.Utf8,
            "division": pl.Utf8,
        },
        orient="row",
    )


def stats_members(
    league: str, gdef: pl.DataFrame, xwalk: pl.DataFrame, season_offset: int
) -> pl.DataFrame:
    """Stats standings → team-seasons with SDV group ids and ESPN team ids (crosswalk windows), plus the raw labels."""
    st = stats_rows(league).with_columns(
        season=(pl.col("dir") + season_offset).cast(pl.Int32)
    )
    name_map = gdef.filter(pl.col("stats_name").is_not_null()).select(
        "level", "stats_name", "group_id"
    )
    conf = name_map.filter(pl.col("level") == "conference").select(
        conference="stats_name", conference_id="group_id"
    )
    div = name_map.filter(pl.col("level") == "division").select(
        division="stats_name", division_id="group_id"
    )
    st = st.join(conf, on="conference", how="left").join(div, on="division", how="left")
    bad = st.filter(
        pl.col("conference_id").is_null()
        | (pl.col("division").is_not_null() & pl.col("division_id").is_null())
    )
    if bad.height:
        raise ValueError(
            f"{league}: unmapped stats labels {bad.select('conference', 'division').unique().rows()}"
        )
    x = xwalk.select(
        pl.col(f"{league}_stats_team_id").alias("native_id"),
        team_id="espn_team_id",
        first_season="first_season",
        last_season="last_season",
    )
    return join_window(st, x, "native_id", f"{league} stats → ESPN team ids")


# ================================================================ NBA build


def build() -> dict[str, pl.DataFrame]:
    gdef = read_curated("nba_groups.csv")
    stats = stats_members(
        LEAGUE, gdef, read_curated("nba_teams.csv"), season_offset=1
    )  # dir = START year
    tgs_stats = stats.select(
        "season",
        "team_id",
        "team_name",
        "conference_id",
        "division_id",
        source=pl.lit("nba_stats"),
        notes=pl.lit("nba_stats_team_id=") + pl.col("native_id"),
    )
    static = read_curated("nba_membership_static.csv")
    static = (
        expand_seasons(static)
        .join(
            gdef.select(division_id="group_id", conference_id="parent_group_id"),
            on="division_id",
        )
        .select(
            "season",
            team_id="espn_team_id",
            team_name="team_name",
            conference_id="conference_id",
            division_id="division_id",
            source=pl.lit("curated"),
            notes=pl.lit("abbr=") + pl.col("team_abbr"),
        )
    )
    overlap = set(static["season"]) & set(tgs_stats["season"])
    assert not overlap, f"static and stats seasons overlap: {sorted(overlap)}"
    tgs = pl.concat([static, tgs_stats]).with_columns(team_id_source=pl.lit("espn"))
    espn, _, espn_seasons = espn_membership(LEAGUE, gdef)
    tgs = cross_check(tgs, espn, espn_seasons)
    tables = assemble(LEAGUE, tgs, gdef, [])
    gs = tables["group_seasons"]
    native = (
        stats.select("season", group_id="conference_id", value="conference")
        .vstack(stats.select("season", group_id="division_id", value="division"))
        .with_columns(
            source=pl.lit("nba_stats"),
            source_id=pl.lit(None, pl.Utf8),
            name_kind=pl.lit("name"),
        )
    )
    return assemble(LEAGUE, tgs, gdef, [native, espn_aliases(LEAGUE, gdef, gs)])
