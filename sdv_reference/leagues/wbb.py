"""Division I women's college basketball groups by season (ending-year keys). Shares mbb's builder."""

from sdv_reference.leagues.mbb import build_league, fetch_league


def fetch() -> None:
    fetch_league("wbb")


def build():
    return build_league("wbb")
