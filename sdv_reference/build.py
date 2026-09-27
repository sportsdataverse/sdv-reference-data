"""Build and validate leagues' {league}_groups tables into build/{league}/, and each {league}_parks table into
build/{league}_parks/.

    uv run python -m sdv_reference.build            # every league and parks module
    uv run python -m sdv_reference.build cfb nhl mlb_parks

Each league module in sdv_reference/leagues/ exposes build() -> dict of the four tables (see CONTRACT.md); each
module in sdv_reference/parks/ exposes write(out_dir), which builds, validates and writes its own table.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

from sdv_reference import leagues, parks
from sdv_reference.schema import write_league

OUT = Path(__file__).resolve().parent.parent / "build"


def available() -> list[str]:
    """Every buildable name: leagues, then `{league}_parks`. A leading underscore marks a shared helper module."""
    def mods(pkg: ModuleType) -> list[str]:
        return sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_"))

    return mods(leagues) + [f"{m}_parks" for m in mods(parks)]


def module(name: str) -> ModuleType:
    """The module that fetches and builds `name`: sdv_reference.parks.{league} for `{league}_parks`, else the
    league's module."""
    if name.endswith("_parks"):
        return importlib.import_module(f"sdv_reference.parks.{name.removesuffix('_parks')}")
    return importlib.import_module(f"sdv_reference.leagues.{name}")


def main(names: list[str]) -> int:
    chosen = names or available()
    failed = []
    for league in chosen:
        try:
            mod = module(league)
            if league.endswith("_parks"):
                mod.write(OUT / league)
                continue
            tables = mod.build()
            files = write_league(league, tables, OUT / league)
        except Exception as e:  # noqa: BLE001 - report every league, then fail
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
