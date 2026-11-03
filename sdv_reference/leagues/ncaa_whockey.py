"""Division I women's college hockey groups by season (ending-year keys). Shares ncaa_mhockey's builder; ESPN has no
women's groups, so there is no second source."""

from sdv_reference.leagues.ncaa_mhockey import build_league, fetch_league


def fetch() -> None:
    fetch_league("ncaa_whockey")


def build():
    return build_league("ncaa_whockey")
