"""Build and validate leagues' {league}_groups tables into build/{league}/.

    uv run python -m sdv_reference.build            # every league with a builder
    uv run python -m sdv_reference.build cfb nhl

Each league module in sdv_reference/leagues/ exposes build() -> dict of the four tables (see CONTRACT.md).
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

from sdv_reference import leagues
from sdv_reference.schema import write_league

OUT = Path(__file__).resolve().parent.parent / "build"


def main(names: list[str]) -> int:
    available = sorted(m.name for m in pkgutil.iter_modules(leagues.__path__))
    chosen = names or available
    failed = []
    for league in chosen:
        try:
            tables = importlib.import_module(f"sdv_reference.leagues.{league}").build()
            files = write_league(league, tables, OUT / league)
        except Exception as e:  # report every league, then fail
            failed.append(league)
            print(f"{league}: FAILED: {e}", flush=True)
            continue
        tgs = tables["team_group_seasons"]
        print(f"{league}: {tables['groups'].height} groups, {tables['group_seasons'].height} group-seasons, "
              f"{tables['group_aliases'].height} aliases, {tgs.height} team-seasons "
              f"({tgs['season'].min()}-{tgs['season'].max()}), {len(files)} files", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
