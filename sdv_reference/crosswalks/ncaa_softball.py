"""NCAA <-> ESPN team crosswalk for D-I college softball, keyed on the NCAA school (tag ncaa_softball_crosswalk).

    uv run python -c "from sdv_reference.build import module; module('ncaa_softball_crosswalk').fetch()"  # 1 ESPN call
    uv run python -m sdv_reference.build ncaa_softball_crosswalk   # offline: raw/ + curated/ -> build/

ESPN's college-softball team ids are a third id system (Alabama: 560 in softball, 148 in baseball, 333 in football and
basketball; checked 2026-11-02), so this is its own map. The build is ncaa_baseball's, run over the ncaa_softball
league's D-I team-seasons (1982 on), raw/ncaa_softball_crosswalk/espn_teams.csv.gz and
curated/ncaa_softball_espn_aliases.csv. The softball source has no per-season team id: ncaa_season_team_id is null.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from sdv_reference.crosswalks import ncaa_baseball

LEAGUE = "ncaa_softball"


def fetch() -> None:
    ncaa_baseball.fetch(LEAGUE)


def build(hints: dict[int, list[str]] | None = None) -> pl.DataFrame:
    return ncaa_baseball.build(hints, LEAGUE)


def validate(crosswalk: pl.DataFrame) -> list[str]:
    return ncaa_baseball.validate(crosswalk, LEAGUE)


def write(out_dir: Path) -> list[Path]:
    return ncaa_baseball.write(out_dir, LEAGUE)
