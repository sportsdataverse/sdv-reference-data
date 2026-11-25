"""The one incremental-refresh rule every league's fetch() routes its per-season sources through.

Seasons already in raw/ are kept. Only the newest $SDV_REF_REFRESH_SEASONS (default 2) seasons in raw/, and any
season newer than raw/, are fetched again. A season the source doesn't have yet comes back empty and stays absent.
A season missing from raw/ that is older than that window is a gap in the source (ESPN has no NHL groups for
1974-92), not a hole to refill, so it is never re-probed. To re-walk everything, set a large window.

Published release assets (the sportsdataverse-data `*_crosswalk` tags that team_aliases and player_aliases snapshot)
don't use the window: the release lists every season it holds with a sha256 digest, so release_seasons() +
seasons_changed() fetch every published season once, then only a new season or one whose digest changed. A
backfilled old season is picked up on the next run.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

import requests

RELEASE_API = "https://api.github.com/repos/sportsdataverse/sportsdataverse-data/releases/tags/{tag}"


def window() -> int:
    return int(os.environ.get("SDV_REF_REFRESH_SEASONS", "2"))


def current_season(ending_year: bool, today: dt.date | None = None) -> int:
    """The newest season key a source can have today: the calendar year, or for ending-year leagues (NBA, NHL,
    college basketball) next year from July on, when the next season's schedules and group trees appear."""
    today = today or dt.date.today()
    return today.year + int(ending_year and today.month >= 7)


def seasons_to_fetch(candidates: Iterable[int], have: Iterable[int]) -> list[int]:
    """The candidates to (re)fetch, ascending: all of them when raw/ has none, else those newer than the newest
    raw season minus the window."""
    have = set(have)
    floor = max(have) - window() if have else None
    return sorted(s for s in set(candidates) if floor is None or s > floor)


def release_assets(tag: str) -> dict[str, str]:
    """name -> sha256 digest of every asset on the sportsdataverse-data release `tag`: one GitHub API call (the
    release object lists all its assets). $GITHUB_TOKEN / $GH_TOKEN is sent when set; without one the 60/hour
    anonymous quota covers a full stage 10 (11 calls: team and player aliases for 5 tags, cfb's two player stems)."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"} | (
        {"Authorization": f"Bearer {token}"} if token else {}
    )
    r = requests.get(RELEASE_API.format(tag=tag), headers=headers, timeout=60)
    r.raise_for_status()
    return {a["name"]: a.get("digest") or a["updated_at"] for a in r.json()["assets"]}


def release_seasons(assets: dict[str, str], stem: str) -> dict[int, str]:
    """season -> digest of each `{stem}_{season}.parquet` in a release_assets() listing."""
    pat = re.compile(rf"{re.escape(stem)}_(\d{{4}})\.parquet")
    return {int(m[1]): d for name, d in assets.items() if (m := pat.fullmatch(name))}


def seasons_changed(published: dict[int, str], seen: dict[int, str]) -> list[int]:
    """The published seasons to (re)fetch, ascending: every one whose digest isn't the one recorded at the last
    fetch (`seen`), so a new season, a re-uploaded one and a backfilled old one all qualify, at any age."""
    return sorted(s for s, d in published.items() if seen.get(s) != d)


def read_digests(path: Path, stem: str) -> dict[int, str]:
    """The `seen` digests of `stem` recorded by write_digests() in `path` (release_digests.json)."""
    rec = json.loads(path.read_text()) if path.exists() else {}
    return release_seasons(rec, stem)


def write_digests(path: Path, stem: str, published: dict[int, str]) -> None:
    """Record `stem`'s published digests after a fetch; unchanged digests rewrite the same bytes."""
    rec = json.loads(path.read_text()) if path.exists() else {}
    rec = {k: v for k, v in rec.items() if not release_seasons({k: v}, stem)}
    rec |= {f"{stem}_{s}.parquet": d for s, d in published.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(rec, indent=1, sort_keys=True) + "\n", newline="\n"
    )  # LF, as on the droplet
