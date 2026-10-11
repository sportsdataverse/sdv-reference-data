"""NCAA school <-> ESPN team crosswalk, men's college basketball (tag mbb_crosswalk). See _ncaa_espn.py."""

from pathlib import Path

from sdv_reference.crosswalks._ncaa_espn import fetch_league, write as _write


def fetch() -> None:
    fetch_league("mbb")


def write(out_dir: Path) -> list[Path]:
    return _write("mbb", out_dir)
