"""Division I men's college basketball groups by season (ending-year keys: 2025 = 2024-25).

Shared with `wbb`, which imports `fetch_league` / `build_league` from here. The shared code lives in this module
rather than a `_college_basketball.py` helper because `build.py` discovers leagues with an unfiltered
`pkgutil.iter_modules`, so any helper module in `leagues/` would be run as a league.

Sources (see ClaudeCowork notes/2026-09-26-conference-reference/01, 02, 04):
- membership: the ESPN core group tree per season (leaf `/teams`); pre-2014 leaves that come back empty
  (Big South, Mid-Con, OVC, Southland, Great West) are filled from ESPN site.web standings for that season;
- cross-checks: KenPom team -> conference (hoopR `teams_links`, MBB 2002-2026) and the NCAA <-> ESPN crosswalk
  (`ncaa_conference`; historical for MBB 2017-18+ only, for WBB 2009-10..2024-25);
- teams no ESPN source lists that season come from KenPom / NCAA (no division);
- corrections: curated/{league}_membership_overrides.csv holds verified ESPN errors (MBB 2003 is a copy of 2004;
  WBB 2002 lists the Southland inside the Horizon League); ESPN's claim stays in notes with sources_agree false;
- lineage: curated/{league}_groups.csv maps ESPN conference ids to SDV group ids; divisions are derived from ESPN;
- names: ESPN's labels are today's for every season, so every name comes from curated/{league}_group_names.csv.

`fetch()` writes raw/{league}/ (network); `build()` reads only raw/ and curated/.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
import time
from pathlib import Path

import polars as pl
import requests

from sdv_reference.espn import CORE, LEAGUE_PATHS, cached_groups, get_json, group_url
from sdv_reference.refresh import current_season, seasons_to_fetch
from sdv_reference.schema import conform

ROOT = Path(__file__).resolve().parents[2]
RAW, CURATED = ROOT / "raw", ROOT / "curated"
# ESPN's WBB 2001 membership is an exact copy of 2002 (TCU already in C-USA, Louisiana Tech in the WAC), so WBB starts
# in 2002; MBB 2001 has no conferences at all.
SEASONS = {lg: range(2002, current_season(ending_year=True) + 1) for lg in ("mbb", "wbb")}
D1 = "50"  # ESPN "NCAA Division I" in both leagues
STANDINGS = "https://site.web.api.espn.com/apis/v2/sports/basketball/{lg}/standings?season={s}&group=50"
SDV_PY = Path("/mnt/sdv_repos/sdv-py")
GIT_FILES = {
    "mbb": {
        "kenpom_team_info.csv.gz": "sportsdataverse/mbb/data/kp_team_info.csv",
        "ncaa_espn_team_crosswalk.csv.gz": "sportsdataverse/mbb/data/ncaa_espn_team_crosswalk_mbb.csv",
    },
    "wbb": {
        "ncaa_espn_team_crosswalk.csv.gz": "sportsdataverse/wbb/data/ncaa_espn_team_crosswalk_wbb.csv",
    },
}
# the NCAA crosswalk's ncaa_conference is a backfill before 2017-18 in MBB and carried forward in WBB 2025-26
NCAA_HISTORICAL = {"mbb": range(2018, 2027), "wbb": range(2010, 2026)}


# ---------------------------------------------------------------- fetch (network)


def _get(url: str, session: requests.Session) -> dict:
    """espn.get_json, plus retries on proxy timeouts and dropped connections (it retries only HTTP statuses)."""
    for attempt in range(5):
        try:
            return get_json(
                url, session, pause=0.4
            )  # + proxy latency ~= 1 request/second
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))
    raise AssertionError("unreachable")


def _ref_id(ref: dict | None) -> str | None:
    return re.search(r"/(\d+)(?:\?|$)", ref["$ref"]).group(1) if ref else None


def _team_ids(
    league: str, season: int, gid: str, session: requests.Session
) -> list[str]:
    d = _get(group_url(league, season, gid, "/teams?limit=1000"), session)
    assert d.get("pageCount", 1) <= 1, f"{league} {season} group {gid}: paged team list"
    return [re.search(r"/teams/(\d+)", i["$ref"]).group(1) for i in d.get("items", [])]


def _walk(league: str, season: int, session: requests.Session) -> list[dict]:
    """One season's ESPN core tree under D-I, in the research-cache row shape (see espn.cached_groups)."""
    rows = []

    def visit(gid: str, depth: int) -> None:
        g = _get(group_url(league, season, gid), session)
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
        row |= {
            "season": season,
            "parent": _ref_id(g.get("parent")),
            "depth": depth,
            "logos": [
                {"href": x.get("href"), "rel": x.get("rel")} for x in g.get("logos", [])
            ],
        }
        kids = []
        if "children" in g:
            kids = [
                _ref_id(i)
                for i in _get(
                    group_url(league, season, gid, "/children?limit=100"), session
                )["items"]
            ]
        row["children"] = kids
        if not kids:
            row["team_ids"] = _team_ids(league, season, gid, session)
        rows.append(row)
        for k in kids:
            visit(k, depth + 1)

    visit(D1, 0)
    return rows


def _standings_season(d: dict) -> int | None:
    """The season a standings payload actually holds: out-of-coverage requests silently return the current one."""
    for c in d.get("children", []):
        if "standings" in c:
            return c["standings"].get("season")
        for cc in c.get("children", []):
            if "standings" in cc:
                return cc["standings"].get("season")
    return None


def _trim(x):
    """Drop the per-team stats, links and logos from a standings payload (95% of its bytes; never read), and sort
    each group's entries by team id: the standing order changes every game day, membership doesn't."""
    if isinstance(x, dict):
        out = {
            k: _trim(v) for k, v in x.items() if k not in ("stats", "links", "logos")
        }
        if isinstance(out.get("entries"), list):
            out["entries"].sort(key=lambda e: str((e.get("team") or {}).get("id")))
        return out
    return [_trim(v) for v in x] if isinstance(x, list) else x


def _write_gz(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    with gzip.GzipFile(
        tmp, "wb", mtime=0
    ) as fh:  # mtime=0: same bytes for the same data
        fh.write(text.encode())
    tmp.rename(path)


def _seasons(league: str) -> list[int]:
    """Seasons with an ESPN group tree in raw/{league}/."""
    return sorted(
        int(p.name.removeprefix("espn_groups_").split(".")[0])
        for p in (RAW / league).glob("espn_groups_*.jsonl.gz")
    )


def fetch_league(league: str) -> None:
    """Snapshot every source into raw/{league}/. ESPN's group tree and standings are fetched only for the
    refresh.seasons_to_fetch() seasons, and the other season files are kept; a season ESPN has no groups for yet
    gets no files. A first fetch takes seasons before the refresh window from the research cache when it has them.
    Live ESPN calls go through $SDV_API_PROXY at ~1 request/second. Provenance carries no fetch timestamps, so an
    unchanged season rewrites the same bytes (git log dates the snapshot)."""
    out = RAW / league
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    cached: dict[int, list[dict]] = {}
    for r in cached_groups(league):
        cached.setdefault(r["season"], []).append(r)
    prov_path = out / "provenance.json"
    prov = json.loads(prov_path.read_text()) if prov_path.exists() else {}
    have = _seasons(league)
    todo = seasons_to_fetch(SEASONS[league], have)
    live = set(todo) if have else set(seasons_to_fetch(SEASONS[league], SEASONS[league]))
    sport, lg = LEAGUE_PATHS[league]
    for s in todo:
        p = out / f"espn_groups_{s}.jsonl.gz"
        rows = [] if s in live else cached.get(s, [])
        src = "research cache 2026-09-26 walk"
        if not rows:
            top = _get(f"{CORE}/{sport}/leagues/{lg}/seasons/{s}/types/2/groups?limit=100", session)
            if not top.get("items"):  # the D-I group answers for any season; the season list doesn't
                print(f"{league} {s}: absent (ESPN has no groups yet)", flush=True)
                continue
            rows, src = _walk(league, s, session), "live core walk"
        elif not any(
            "team_ids" in r for r in rows
        ):  # cached tree, no team lists: fetch the leaves' lists
            for r in rows:
                if not r["children"]:
                    r["team_ids"] = _team_ids(league, s, r["id"], session)
            src = "research cache 2026-09-26 tree + live leaf team lists"
        _write_gz(p, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        prov[p.name] = src
        print(f"{league} {s}: {len(rows)} groups ({src})", flush=True)
        q = out / f"espn_standings_{s}.json.gz"
        d = _get(STANDINGS.format(lg=lg, s=s), session)
        _write_gz(q, json.dumps(_trim(d), sort_keys=True))
        prov[q.name] = f"live site.web standings; echoed season {_standings_season(d)}"
        print(f"{league} {s}: standings (echo {_standings_season(d)})", flush=True)
        prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True))
    for name, path in GIT_FILES[league].items():
        git = ["git", "-C", str(SDV_PY)]
        text = subprocess.run(
            [*git, "show", f"origin/main:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # the commit that last changed the file, not origin/main's head: provenance moves only with the content
        sha = subprocess.run(
            [*git, "log", "-1", "--format=%H", "origin/main", "--", path],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _write_gz(out / name, text)
        prov[name] = f"sportsdataverse-py origin/main {sha}:{path}"
    prov_path.write_text(json.dumps(prov, indent=1, sort_keys=True))


def fetch() -> None:
    fetch_league("mbb")


# ---------------------------------------------------------------- build (offline: raw/ + curated/)


def _csv(path: Path) -> pl.DataFrame:
    """Every column as Utf8: ids keep one dtype from the boundary on."""
    return pl.read_csv(
        gzip.open(path).read() if path.suffix == ".gz" else path, infer_schema=False
    )


def _espn_season(league: str, s: int) -> tuple[dict, dict, dict, dict, dict]:
    """One season of ESPN: (tree rows by id, team -> (conf, div) from core leaves, the same from standings,
    standings group labels by id, team display names)."""
    tree = {}
    with gzip.open(RAW / league / f"espn_groups_{s}.jsonl.gz", "rt") as fh:
        for line in fh:
            r = json.loads(line)
            tree[r["id"]] = r
    core = {}
    for r in tree.values():
        if r["id"] == D1:
            continue
        where = (r["id"], None) if r["parent"] == D1 else (r["parent"], r["id"])
        for t in r.get("team_ids") or []:
            assert t not in core, f"{league} {s}: team {t} in two ESPN leaves"
            core[t] = where
    stand, labels, names = {}, {}, {}
    with gzip.open(RAW / league / f"espn_standings_{s}.json.gz", "rt") as fh:
        d = json.load(fh)
    if (
        _standings_season(d) == s
    ):  # out-of-coverage requests silently return the current season
        for conf in d.get("children", []):
            for node, where in [(conf, (conf["id"], None))] + [
                (c, (conf["id"], c["id"])) for c in conf.get("children", [])
            ]:
                labels[node["id"]] = node
                for e in node.get("standings", {}).get("entries", []):
                    stand[e["team"]["id"]] = where
                    names[e["team"]["id"]] = e["team"].get("displayName")
    return tree, core, stand, labels, names


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _in_window(df: pl.DataFrame, s: int) -> pl.DataFrame:
    lo, hi = pl.col("valid_from").cast(pl.Int32), pl.col("valid_to").cast(pl.Int32)
    return df.filter((lo.is_null() | (lo <= s)) & (hi.is_null() | (hi >= s)))


def build_league(league: str) -> dict[str, pl.DataFrame]:
    d1 = f"{league}:d1"
    lineage = _csv(CURATED / f"{league}_groups.csv")
    conf_of = dict(zip(lineage["espn_group_id"], lineage["group_id"]))
    names = _csv(CURATED / f"{league}_group_names.csv")
    codes = _csv(CURATED / f"{league}_source_codes.csv")
    overrides = {
        (int(r["season"]), r["team_id"]): r
        for r in _csv(CURATED / f"{league}_membership_overrides.csv").iter_rows(
            named=True
        )
    }
    code_of = {
        (r["source"], r["value"]): r["group_id"] for r in codes.iter_rows(named=True)
    }

    # second sources, keyed (season, espn team id) -> group id
    kenpom: dict[tuple[int, str], str] = {}
    if league == "mbb":
        kp_team = dict(
            _csv(CURATED / "mbb_kenpom_teams.csv")
            .select("kenpom_team", "espn_team_id")
            .iter_rows()
        )
        for team, conf, year in (
            _csv(RAW / league / "kenpom_team_info.csv.gz")
            .select("Team", "Conf", "Year")
            .iter_rows()
        ):
            assert (int(year), kp_team[team]) not in kenpom, (
                f"KenPom {year}: two names map to ESPN {kp_team[team]}"
            )
            kenpom[(int(year), kp_team[team])] = code_of[("kenpom", conf)]
    ncaa: dict[tuple[int, str], str] = {}
    xw = _csv(RAW / league / "ncaa_espn_team_crosswalk.csv.gz")
    fixes = (
        CURATED / f"{league}_team_id_fixes.csv"
    )  # second-source ESPN ids that differ from ESPN's own that season
    for r in (
        _csv(fixes).filter(pl.col("source") == "ncaa").iter_rows(named=True)
        if fixes.exists()
        else []
    ):
        season_end = pl.col("season").str.slice(0, 4).cast(pl.Int32) + 1
        xw = xw.with_columns(
            pl.when(
                (pl.col("espn_team_id") == r["source_espn_team_id"])
                & season_end.is_between(int(r["season_from"]), int(r["season_to"]))
            )
            .then(pl.lit(r["espn_team_id"]))
            .otherwise(pl.col("espn_team_id"))
            .alias("espn_team_id")
        )
    ncaa_names: dict[str, str] = {}
    for season, espn_id, label, disp in xw.select(
        "season", "espn_team_id", "ncaa_conference", "espn_display_name"
    ).iter_rows():
        if espn_id:
            ncaa_names[espn_id] = disp
        s = int(season[:4]) + 1  # "2009-10" -> 2010
        if espn_id and s in NCAA_HISTORICAL[league] and ("ncaa", label) in code_of:
            ncaa[(s, espn_id)] = code_of[("ncaa", label)]

    tgs, div_label, espn_seen, team_names = [], {}, {}, {}
    for s in _seasons(league):
        tree, core, stand, labels, names_s = _espn_season(league, s)
        team_names |= names_s
        for gid, r in tree.items():
            espn_seen.setdefault(gid, []).append((s, r))

        def place(
            conf: str, div: str | None, s=s, tree=tree, labels=labels
        ) -> tuple[str, str | None]:
            """ESPN (conference, division) ids -> SDV group ids; divisions are named from their ESPN label."""
            if conf not in conf_of:
                raise KeyError(
                    f"{league} {s}: ESPN group {conf} is not in curated/{league}_groups.csv"
                )
            cid = conf_of[conf]
            if not div:
                return cid, None
            node = tree.get(div) or labels[div]
            did = f"{cid}-{_slug(node['name'].rsplit(' - ', 1)[-1])}"
            div_label[(did, s)] = (
                div,
                node["name"],
                node.get("shortName"),
                node.get("abbreviation"),
                cid,
            )
            return cid, did

        members = {t: (*place(*w), "espn_core_groups", []) for t, w in core.items()}
        for t, w in stand.items():
            cid, did = place(*w)
            if t not in members:
                members[t] = (
                    cid,
                    did,
                    "espn_standings",
                    ["not in any ESPN core group team list this season"],
                )
            elif (cid, did) != members[t][:2]:
                members[t][3].append(f"ESPN standings have {did or cid}")
        # D-I teams ESPN lists nowhere this season: take the second source's conference (no division)
        for src, table in (("kenpom", kenpom), ("ncaa", ncaa)):
            for (ss, t), g in table.items():
                if ss == s and t not in members:
                    members[t] = (
                        g,
                        None,
                        src,
                        ["in neither ESPN core group lists nor standings this season"],
                    )
        for t, (cid, did, source, notes) in members.items():
            others = {}
            if (
                (s, t) in overrides
            ):  # a verified correction; ESPN's claim is kept as a disagreeing source
                o = overrides[(s, t)]
                others["espn"], notes = cid, [f"{source} had {did or cid}", o["notes"]]
                cid, did, source = (
                    o["conference_id"],
                    o["division_id"] or None,
                    o["source"],
                )
            others |= {
                src: g
                for src, g in (
                    ("kenpom", kenpom.get((s, t))),
                    ("ncaa", ncaa.get((s, t))),
                )
                if g and src != source
            }
            notes = notes + [
                f"{src} has {g}"
                for src, g in others.items()
                if g != cid and src != "espn"
            ]
            tgs.append(
                {
                    "league": league,
                    "season": s,
                    "team_id": t,
                    "team_id_source": "espn",
                    "team_name": names_s.get(t),
                    "subdivision_id": d1,
                    "conference_id": cid,
                    "division_id": did,
                    "source": source,
                    "sources_agree": all(g == cid for g in others.values())
                    if others
                    else None,
                    "notes": "; ".join(notes) or None,
                }
            )
    tgs = conform(
        "team_group_seasons", pl.DataFrame(tgs, infer_schema_length=None)
    ).with_columns(
        # names come from that season's standings; fall back to any season's, then the NCAA crosswalk's ESPN name
        pl.col("team_name")
        .fill_null(pl.col("team_id").replace_strict(team_names, default=None))
        .fill_null(pl.col("team_id").replace_strict(ncaa_names, default=None))
    )

    # group_seasons: every group with at least one member that season
    counts = pl.concat(
        [
            tgs.group_by("season")
            .agg(pl.len().alias("n"))
            .with_columns(
                pl.lit(d1).alias("group_id"), pl.lit("subdivision").alias("level")
            ),
            tgs.group_by("conference_id", "season")
            .agg(pl.len().alias("n"))
            .rename({"conference_id": "group_id"})
            .with_columns(pl.lit("conference").alias("level")),
            tgs.filter(pl.col("division_id").is_not_null())
            .group_by("division_id", "season")
            .agg(pl.len().alias("n"))
            .rename({"division_id": "group_id"})
            .with_columns(pl.lit("division").alias("level")),
        ],
        how="diagonal",
    )
    gs = []
    for gid, s, n, level in counts.select(
        "group_id", "season", "n", "level"
    ).iter_rows():
        if level == "division":
            _, name, short, abbr, parent = div_label[(gid, s)]
        else:
            row = _in_window(names.filter(pl.col("group_id") == gid), s)
            if row.height != 1:
                raise ValueError(
                    f"{league}: {row.height} curated names for {gid} in {s}"
                )
            name, short, abbr = row.row(0)[3:6]
            parent = None if level == "subdivision" else d1
        gs.append(
            {
                "league": league,
                "group_id": gid,
                "season": s,
                "level": level,
                "name": name,
                "short_name": short,
                "abbreviation": abbr,
                "parent_group_id": parent,
                "n_teams": n,
            }
        )
    gs = conform("group_seasons", pl.DataFrame(gs))

    notes_of = dict(zip(lineage["group_id"], lineage["notes"]))
    for (did, _), (espn_id, label, *_rest, cid) in div_label.items():
        notes_of.setdefault(
            did,
            f"ESPN division {espn_id} '{label}' of {cid}; division names are ESPN's labels "
            "(no division was renamed while it existed)",
        )
    groups = conform(
        "groups",
        gs.group_by("group_id", "level")
        .agg(
            pl.lit(league).alias("league"),
            pl.col("season").min().alias("first_season"),
            pl.col("season").max().alias("last_season"),
        )
        .with_columns(
            pl.col("group_id").replace_strict(notes_of, default=None).alias("notes")
        ),
    )

    # aliases: ESPN ids/labels (today's labels, valid for the seasons the id is in the tree), SDV names, codes
    espn_group = {**conf_of, **{e: did for (did, _), (e, *_r) in div_label.items()}}
    known = set(groups["group_id"])
    aliases = []
    for gid, seen in espn_seen.items():
        if espn_group.get(gid) not in known:
            continue
        r = seen[-1][1]
        for kind, key in (
            ("name", "name"),
            ("short_name", "shortName"),
            ("abbreviation", "abbreviation"),
            ("slug", "slug"),
        ):
            aliases.append(
                (
                    espn_group[gid],
                    "espn",
                    gid,
                    kind,
                    r.get(key),
                    seen[0][0],
                    seen[-1][0],
                )
            )
    for r in names.iter_rows(named=True):
        for kind in ("name", "short_name", "abbreviation"):
            lo, hi = (int(r[k]) if r[k] else None for k in ("valid_from", "valid_to"))
            aliases.append((r["group_id"], "sdv", None, kind, r[kind], lo, hi))
    observed = (
        [
            ("kenpom", c, int(y))
            for c, y in _csv(RAW / league / "kenpom_team_info.csv.gz")
            .select("Conf", "Year")
            .iter_rows()
        ]
        if league == "mbb"
        else []
    )
    observed += [
        ("ncaa", lab, int(se[:4]) + 1)
        for se, lab in xw.select("season", "ncaa_conference").iter_rows()
        if int(se[:4]) + 1 in NCAA_HISTORICAL[league]
    ]
    window: dict[tuple[str, str], list[int]] = {}
    for src, value, y in observed:
        window.setdefault((src, value), []).append(y)
    for r in codes.iter_rows(named=True):
        ys = window.get((r["source"], r["value"]))
        if ys:
            kind = "code" if r["source"] == "kenpom" else "short_name"
            aliases.append(
                (
                    r["group_id"],
                    r["source"],
                    r["source_id"] or None,
                    kind,
                    r["value"],
                    min(ys),
                    max(ys),
                )
            )
    ga = conform(
        "group_aliases",
        pl.DataFrame(
            aliases,
            schema=[
                "group_id",
                "source",
                "source_id",
                "name_kind",
                "value",
                "valid_from",
                "valid_to",
            ],
            orient="row",
        )
        .with_columns(pl.lit(league).alias("league"))
        .filter(
            pl.col("value").is_not_null()
            & (pl.col("value") != "")
            & pl.col("group_id").is_in(list(known))
        ),
    )

    return {
        "groups": groups,
        "group_seasons": gs,
        "group_aliases": ga,
        "team_group_seasons": tgs,
    }


def build() -> dict[str, pl.DataFrame]:
    return build_league("mbb")
