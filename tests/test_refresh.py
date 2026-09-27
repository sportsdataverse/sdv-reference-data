import datetime as dt

from sdv_reference.refresh import current_season, seasons_to_fetch


def test_seasons_to_fetch(monkeypatch):
    monkeypatch.delenv("SDV_REF_REFRESH_SEASONS", raising=False)
    seasons = range(2000, 2028)
    assert seasons_to_fetch(seasons, []) == list(seasons)  # first fetch: everything
    have = [s for s in range(2000, 2027) if s != 2010]  # 2010: a gap in the source
    assert seasons_to_fetch(seasons, have) == [2025, 2026, 2027]  # newest 2 in raw/ + anything newer
    monkeypatch.setenv("SDV_REF_REFRESH_SEASONS", "1")
    assert seasons_to_fetch(seasons, have) == [2026, 2027]
    monkeypatch.setenv("SDV_REF_REFRESH_SEASONS", "99")
    assert seasons_to_fetch(seasons, have) == list(seasons)


def test_current_season():
    assert current_season(False, dt.date(2026, 9, 27)) == 2026
    assert current_season(True, dt.date(2026, 9, 27)) == 2027  # NBA/NHL 2026-27
    assert current_season(True, dt.date(2027, 3, 1)) == 2027
