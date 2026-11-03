"""Division I men's college hockey groups by season (ENDING-year keys: 2026 = 2025-26). ncaa_whockey shares this
builder.

    uv run python -c "from sdv_reference.leagues import ncaa_mhockey; ncaa_mhockey.fetch()"   # network
    uv run python -m sdv_reference.build ncaa_mhockey ncaa_whockey                             # offline

Sources:
- membership 1998-99 on: USCHO's Division I standings, one page per season and gender
  (https://www.uscho.com/standings/division-i-{men,women}/{YYYY-YYYY}/), whose embedded JSON lists every D-I team
  under its conference that season (independents under `i1`). Snapshotted to raw/{league}/uscho_standings.csv.gz,
  identity columns only. USCHO's conference labels are today's, so each season's names come from
  curated/{league}_groups.csv (windows, cited), which maps USCHO's conference ids to SDV lineages. Team names are
  USCHO's labels.
- team ids: ESPN ids through curated/ncaa_hockey_espn_teams.csv (league + USCHO team slug -> ESPN id, checked by
  hand). Usually the school id, but ESPN gives some newer programs a separate id per gender. Every id is in the
  snapshot of ESPN's team list for that sport (raw/{league}/espn_teams.csv.gz) or carries a note saying where ESPN
  uses it (dropped programs, and Maryville / Delaware, which ESPN's game feeds carry but its list doesn't).
- cross-check (men): ESPN core groups, 2024-25 on (raw/ncaa_mhockey/espn/{season}.json.gz). Before that ESPN keeps a
  frozen 2012-13 tree (the old CCHA with Notre Dame and Michigan in 2014-15), and it still lists dropped programs
  (Mercyhurst, AIC) in today's tree, so it is a second source, never the membership. ESPN has no women's groups.

`fetch()` writes raw/{league}/; `build()` reads only raw/ and curated/.
"""

from __future__ import annotations

import html
import json
import re
import time

import polars as pl
import requests

from sdv_reference.espn import UA
from sdv_reference.leagues.nba import (
    RAW,
    cross_check,
    espn_aliases,
    espn_frames,
    espn_membership,
    fetch_espn,
    join_window,
    read_curated,
    windows,
)
from sdv_reference.leagues.ncaa_baseball import write_csv_gz
from sdv_reference.refresh import current_season, seasons_to_fetch
from sdv_reference.schema import conform

LEAGUE = "ncaa_mhockey"
GENDER = {"ncaa_mhockey": "men", "ncaa_whockey": "women"}
ESPN_SLUG = {
    "ncaa_mhockey": "mens-college-hockey",
    "ncaa_whockey": "womens-college-hockey",
}
FIRST_SEASON = 1999  # USCHO's standings archive starts with 1998-99, for both genders
ESPN_FIRST_SEASON = (
    2025  # ESPN core groups track membership from 2024-25 (see the module docstring)
)
USCHO = "https://www.uscho.com/standings/division-i-{gender}/{start}-{end}/"
INDEPENDENT = "i1"  # USCHO's code for Division I independents
ESPN_INDEPENDENT = "59"  # ESPN core group "Independent" (men)
USCHO_COLS = [
    "season",
    "conf_code",
    "conf_id",
    "conf_short",
    "conf_long",
    "team_slug",
    "team",
]


def uscho_season(
    league: str, season: int, session: requests.Session
) -> pl.DataFrame | None:
    """One USCHO standings page -> one row per team: season, conf_code, conf_id, conf_short, conf_long, team_slug,
    team. None when USCHO doesn't list the season yet."""
    r = session.get(
        USCHO.format(gender=GENDER[league], start=season - 1, end=season),
        headers=UA,
        timeout=60,
    )
    r.raise_for_status()
    content = None
    for m in re.finditer(r'=\s*"(\{[^"]*&quot;content&quot;[^"]*\})"', r.text):
        c = json.loads(html.unescape(m.group(1))).get("props", {}).get("content")
        if isinstance(c, dict) and "data" in c:
            content = c
            break
    if content is None:
        raise ValueError(f"{league} {season}: no standings JSON on the USCHO page")
    if f"{season - 1}-{season}" not in {s["text"] for s in content["season"]}:
        return None
    confs = {c["code"]: c for c in content["confs"]}
    rows = []
    for code, teams in content["data"].items():
        c = confs[code]
        for t in teams:
            slug = re.fullmatch(r"/team/([^/]+)/[a-z-]+/", t["link"]).group(1)
            name = re.sub(
                r"^\d+\s+", "", t["team"]
            )  # a poll rank prefixes ranked teams
            rows.append(
                (season, code, c["ID"], c["shortname"], c["longname"], slug, name)
            )
    return pl.DataFrame(rows, schema=USCHO_COLS, orient="row").with_columns(
        pl.col("season").cast(pl.Int32)
    )


def fetch_uscho(league: str) -> None:
    """raw/{league}/uscho_standings.csv.gz: keeps the seasons already there, re-fetches the refresh window."""
    path = RAW / league / "uscho_standings.csv.gz"
    old = (
        read_uscho(league)
        if path.exists()
        else pl.DataFrame(schema=dict.fromkeys(USCHO_COLS, pl.Utf8))
    )
    old = old.with_columns(pl.col("season").cast(pl.Int32))
    todo = seasons_to_fetch(
        range(FIRST_SEASON, current_season(ending_year=True) + 1),
        old["season"].unique(),
    )
    session = requests.Session()
    new = []
    for s in todo:
        df = uscho_season(league, s, session)
        print(
            f"{league}: uscho {s} {df.height if df is not None else 'absent'}",
            flush=True,
        )
        if df is not None:
            new.append(df)
        time.sleep(2)  # one page per two seconds
    out = pl.concat([old.filter(~pl.col("season").is_in(todo)), *new]).sort(
        "season", "conf_code", "team_slug"
    )
    write_csv_gz(out, path)


def fetch_espn_teams(league: str) -> None:
    """raw/{league}/espn_teams.csv.gz: ESPN's site team list for the sport (today's)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/hockey/{ESPN_SLUG[league]}/teams?limit=1000"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    teams = r.json()["sports"][0]["leagues"][0]["teams"]
    df = pl.DataFrame(
        [
            {
                k: t["team"].get(k)
                for k in ("id", "displayName", "location", "abbreviation")
            }
            for t in teams
        ]
    ).rename({"id": "espn_team_id", "displayName": "display_name"})
    write_csv_gz(
        df.sort(pl.col("espn_team_id").cast(pl.Int64)),
        RAW / league / "espn_teams.csv.gz",
    )


def fetch_league(league: str) -> None:
    fetch_uscho(league)
    fetch_espn_teams(league)
    if league == "ncaa_mhockey":
        fetch_espn(
            league, range(ESPN_FIRST_SEASON, current_season(ending_year=True) + 1)
        )


def fetch() -> None:
    fetch_league(LEAGUE)


def read_uscho(league: str) -> pl.DataFrame:
    return pl.read_csv(
        RAW / league / "uscho_standings.csv.gz", infer_schema=False
    ).with_columns(pl.col("season").cast(pl.Int32))


def espn_ids(league: str) -> pl.DataFrame:
    """USCHO team slug -> ESPN team id for one league (curated). Usually the school id, but ESPN gives some newer
    programs a separate id per gender (LIU: men 110133, women 2341)."""
    return read_curated("ncaa_hockey_espn_teams.csv").filter(pl.col("league") == league)


def members(league: str, gdef: pl.DataFrame) -> pl.DataFrame:
    """One row per USCHO team-season: season, team_id (ESPN), team_name, conference_id (null = independent),
    source, notes, team_slug. Fails on a team slug or conference id with no curated mapping."""
    us = read_uscho(league)
    ids = espn_ids(league).select("uscho_slug", "espn_team_id")
    unmapped = us.join(ids, left_on="team_slug", right_on="uscho_slug", how="anti")
    if unmapped.height:
        raise ValueError(
            f"{league}: USCHO teams with no ESPN id in curated/ncaa_hockey_espn_teams.csv: "
            f"{sorted(set(unmapped['team_slug']))}"
        )
    confs = gdef.filter(pl.col("uscho_conf_id").is_not_null()).select(
        "uscho_conf_id", "group_id", "first_season", "last_season"
    )
    # USCHO also lists Merrimack under the independents in every season it was in Hockey East (1998-99 to 2020-21):
    # a conference listing wins over the independents table
    ind = us.filter(pl.col("conf_code") == INDEPENDENT).join(
        us.filter(pl.col("conf_code") != INDEPENDENT),
        on=["season", "team_slug"],
        how="anti",
    )
    conf = join_window(
        us.filter(pl.col("conf_code") != INDEPENDENT).rename(
            {"conf_id": "uscho_conf_id"}
        ),
        confs.unique(),
        "uscho_conf_id",
        f"{league} USCHO conferences",
    ).rename({"uscho_conf_id": "conf_id", "group_id": "conference_id"})
    return (
        pl.concat(
            [conf, ind.with_columns(conference_id=pl.lit(None, pl.Utf8))],
            how="diagonal",
        )
        .join(ids, left_on="team_slug", right_on="uscho_slug")
        .select(
            "season",
            team_id="espn_team_id",
            team_name="team",
            subdivision_id=pl.lit(f"{league}:d1"),
            conference_id="conference_id",
            division_id=pl.lit(None, pl.Utf8),
            source=pl.lit("uscho"),
            notes=pl.format("uscho={}", "team_slug")
            + pl.when(pl.col("conference_id").is_null())
            .then(pl.lit("; independent"))
            .otherwise(pl.lit("")),
            team_id_source=pl.lit("espn"),
            team_slug="team_slug",
        )
    )


def assemble(
    league: str, tgs: pl.DataFrame, gdef: pl.DataFrame, aliases: list[pl.DataFrame]
) -> dict[str, pl.DataFrame]:
    """The four contract tables: nba.assemble's shape, rooted at the `{league}:d1` subdivision (as mbb is)."""
    counts = pl.concat(
        [
            tgs.filter(pl.col(c).is_not_null())
            .group_by(c, "season")
            .agg(n_teams=pl.len())
            .select(group_id=c, season="season", n_teams="n_teams")
            for c in ("subdivision_id", "conference_id")
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
    al = windows(
        pl.concat([a.select(cols) for a in [sdv, *aliases]]), int(gs["season"].max())
    )
    lit = pl.lit(league).alias("league")
    return {
        "groups": conform("groups", groups.with_columns(lit)),
        "group_seasons": conform("group_seasons", gs.with_columns(lit)),
        "group_aliases": conform("group_aliases", al.with_columns(lit)),
        "team_group_seasons": conform("team_group_seasons", tgs.with_columns(lit)),
    }


def uscho_aliases(league: str, tgs: pl.DataFrame) -> pl.DataFrame:
    """USCHO's conference id, long name, short name and code for each SDV conference, in the seasons it lists it."""
    us = read_uscho(league).join(
        tgs.select("season", "conference_id", "team_slug"),
        on=["season", "team_slug"],
    )
    us = us.filter(pl.col("conference_id").is_not_null()).unique(
        ["season", "conference_id"]
    )
    return pl.concat(
        [
            us.select(
                "season",
                group_id="conference_id",
                source_id="conf_id",
                value=c,
                name_kind=pl.lit(k),
            )
            for c, k in (
                ("conf_long", "name"),
                ("conf_short", "short_name"),
                ("conf_code", "code"),
            )
        ]
    ).with_columns(source=pl.lit("uscho"))


def build_league(league: str) -> dict[str, pl.DataFrame]:
    gdef = read_curated(f"{league}_groups.csv")
    tgs = members(league, gdef)
    aliases = [uscho_aliases(league, tgs)]
    if league == "ncaa_mhockey":
        espn, _, espn_seasons = espn_membership(league, gdef)
        tgs = cross_check(tgs, espn, espn_seasons)
        # ESPN files independents under its group 59, which has no SDV conference: agreeing means "59 and no conference"
        _, raw_members, _ = espn_frames(league)
        ind = raw_members.filter(pl.col("espn_group_id") == ESPN_INDEPENDENT).select(
            "season", team_id="espn_team_id", espn_ind=pl.lit(True)
        )
        tgs = (
            tgs.join(ind, on=["season", "team_id"], how="left")
            .with_columns(
                sources_agree=pl.when(
                    pl.col("espn_ind") & pl.col("conference_id").is_null()
                )
                .then(True)
                .otherwise(pl.col("sources_agree")),
                notes=pl.when(pl.col("espn_ind") & pl.col("conference_id").is_null())
                .then(
                    pl.col("notes").str.replace(
                        "; not in ESPN's group lists", "", literal=True
                    )
                )
                .when(pl.col("espn_ind"))
                .then(
                    pl.col("notes").str.replace(
                        "; not in ESPN's group lists",
                        "; ESPN: independent",
                        literal=True,
                    )
                )
                .otherwise(pl.col("notes")),
            )
            .drop("espn_ind")
        )
        gs = assemble(league, tgs, gdef, [])["group_seasons"]
        aliases.append(espn_aliases(league, gdef, gs))
    else:
        tgs = tgs.with_columns(sources_agree=pl.lit(None, pl.Boolean))
    return assemble(league, tgs, gdef, aliases)


def build() -> dict[str, pl.DataFrame]:
    return build_league(LEAGUE)
