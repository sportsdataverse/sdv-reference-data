"""The one incremental-refresh rule every league's fetch() routes its per-season sources through.

Seasons already in raw/ are kept. Only the newest $SDV_REF_REFRESH_SEASONS (default 2) seasons in raw/, and any
season newer than raw/, are fetched again. A season the source doesn't have yet comes back empty and stays absent.
A season missing from raw/ that is older than that window is a gap in the source (ESPN has no NHL groups for
1974-92), not a hole to refill, so it is never re-probed. To re-walk everything, set a large window.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable


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
