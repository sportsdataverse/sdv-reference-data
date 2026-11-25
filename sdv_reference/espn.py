"""ESPN core API groups by season, read from the research cache first and fetched live only when missing.

The cache (the 2026-09-26 walk, one JSON object per group per season, leaves carrying `team_ids`) lives at
$SDV_ESPN_GROUP_CACHE (default /mnt/sdv_repos/tmp/conf-research-2026-09-26). Live calls go through
$SDV_API_PROXY when set: ESPN blocks this droplet's IP after heavy traffic.

ESPN's names, parents and logos are TODAY's values for every season; use it for ids and membership only.
"""

from __future__ import annotations

import json
import os
import time
from functools import cache
from pathlib import Path

import requests

CACHE = Path(
    os.environ.get(
        "SDV_ESPN_GROUP_CACHE", "/mnt/sdv_repos/tmp/conf-research-2026-09-26"
    )
)
CORE = "https://sports.core.api.espn.com/v2/sports"
UA = {
    "User-Agent": "sdv-reference-data/0.1 (+https://github.com/sportsdataverse/sdv-reference-data)"
}
CACHE_FILES = {
    "cfb": "cfb.jsonl",
    "mbb": "mbb.jsonl",
    "wbb": "wbb.jsonl",
    "nfl": "pro_nfl.jsonl",
    "nba": "pro_nba.jsonl",
    "wnba": "pro_wnba.jsonl",
    "mlb": "pro_mlb.jsonl",
    "nhl": "pro_nhl.jsonl",
}
LEAGUE_PATHS = {
    "cfb": ("football", "college-football"),
    "mbb": ("basketball", "mens-college-basketball"),
    "wbb": ("basketball", "womens-college-basketball"),
    "ncaa_mhockey": ("hockey", "mens-college-hockey"),
    "ncaa_softball": ("baseball", "college-softball"),
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
}


@cache
def cached_groups(league: str) -> list[dict]:
    """Every cached group record for a league: id, name, shortName, abbreviation, season, parent, isConference,
    children, logos, and team_ids on leaves."""
    path = CACHE / CACHE_FILES[league]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open()]


# set once the proxy refuses us (a lapsed account answers 407): the rest of the run's calls go direct
_PROXY_DOWN: list[bool] = []


def get_json(url: str, session: requests.Session | None = None, pause: float = 1.0) -> dict:
    """One polite live call (about 1 request/second), through $SDV_API_PROXY when set. Retries rate limits,
    server errors and dropped connections (the proxy occasionally drops a TLS handshake). An unreachable proxy
    switches the run to direct calls: ESPN answers the droplet directly at this run's volume."""
    s = session or requests
    for attempt in range(4):
        proxy = None if _PROXY_DOWN else os.environ.get("SDV_API_PROXY")
        try:
            r = s.get(url, headers=UA, timeout=60, proxies={"http": proxy, "https": proxy} if proxy else None)
        except requests.exceptions.ProxyError as e:
            if not proxy:
                raise
            # the Decodo account lapsed on 2026-11-02 and 2026-11-23 (407), failing every league's fetch
            print(f"proxy unreachable ({type(e).__name__}); ESPN calls go direct for the rest of this run")
            _PROXY_DOWN.append(True)
            continue
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(pause)
        return r.json()
    raise RuntimeError(f"unreachable: {url}")


def group_url(league: str, season: int, group_id: str, suffix: str = "") -> str:
    sport, lg = LEAGUE_PATHS[league]
    return f"{CORE}/{sport}/leagues/{lg}/seasons/{season}/types/2/groups/{group_id}{suffix}"
