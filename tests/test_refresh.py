import datetime as dt
import io

import polars as pl
import pytest
import requests

from sdv_reference import player_aliases, refresh, team_aliases
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


class _Resp:
    def __init__(self, status_code=200, content=b"", payload=None):
        self.status_code, self.content, self._payload = status_code, content, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.status_code)

    def json(self):
        return self._payload


def _fake_release(monkeypatch, digests: dict[str, str], fail: str = "") -> list[str]:
    """Serve a sportsdataverse-data release holding `digests` (asset name -> digest): its API listing and each asset's
    parquet (`fail` answers 500). Returns the list of asset downloads, in order."""
    gets = []

    def frame(name: str) -> pl.DataFrame:
        season = int(name.removesuffix(".parquet").rsplit("_", 1)[1])
        return pl.DataFrame({"season": [season], "espn_team_id": [7], "espn_athlete_id": [season * 10],
                             "fox_athlete_id": [f"f{season}"], "fox_team_id": ["t7"]})  # fmt: skip

    def download(_session, url, **_kw):
        name = url.rsplit("/", 1)[1]
        gets.append(name)
        if name not in digests or name == fail:
            return _Resp(404 if name not in digests else 500)
        buf = io.BytesIO()
        frame(name).write_parquet(buf)
        return _Resp(content=buf.getvalue())

    def listing(url, **_kw):
        assert url == refresh.RELEASE_API.format(tag=url.rsplit("/", 1)[1])
        return _Resp(
            payload={
                "assets": [
                    {"name": n, "digest": d, "updated_at": "x"}
                    for n, d in digests.items()
                ]
            }
        )

    monkeypatch.setattr(requests.Session, "get", download)
    monkeypatch.setattr(requests, "get", listing)
    return gets


def test_backfilled_old_season_is_fetched(monkeypatch, tmp_path):
    """raw/ holds mbb 2025-27 and the release now lists 2004-27 (the 11-13 backfill): the old season window fetched
    only 2026-27, so 2004-24 never reached player_aliases or team_aliases. Every published season must be fetched,
    then only a new or re-uploaded one."""
    monkeypatch.delenv("SDV_REF_REFRESH_SEASONS", raising=False)
    monkeypatch.setattr(player_aliases, "RAW", tmp_path / "player_aliases")
    monkeypatch.setattr(team_aliases, "RAW", tmp_path / "team_aliases")
    seasons = range(2004, 2028)
    digests = {
        f"mbb_{k}_crosswalk_{s}.parquet": f"sha256:{s}"
        for s in seasons
        for k in ("player", "team")
    }
    gets = _fake_release(
        monkeypatch, {n: d for n, d in digests.items() if int(n[-12:-8]) >= 2025}
    )
    player_aliases._fetch_crosswalks("mbb")
    team_aliases._fetch_crosswalk("mbb")
    gets = _fake_release(monkeypatch, digests)  # the backfill lands upstream
    player_aliases._fetch_crosswalks("mbb")
    team_aliases._fetch_crosswalk("mbb")
    snaps = sorted(
        p.name
        for p in (tmp_path / "player_aliases" / "mbb").glob(
            "mbb_player_crosswalk_*.csv.gz"
        )
    )
    assert snaps == [
        f"mbb_player_crosswalk_{s}.csv.gz" for s in seasons
    ]  # 2004 included
    team = team_aliases._read_gz_csv(
        tmp_path / "team_aliases" / "mbb" / "mbb_team_crosswalk.csv.gz"
    )
    assert sorted(team["season"].to_list()) == list(seasons)
    assert team["espn_team_id"].unique().to_list() == ["7"]  # not "7.0"
    assert min(int(g[-12:-8]) for g in gets) == 2004

    gets = _fake_release(monkeypatch, digests)  # nothing changed upstream: no download
    player_aliases._fetch_crosswalks("mbb")
    team_aliases._fetch_crosswalk("mbb")
    assert gets == []
    gets = _fake_release(
        monkeypatch, digests | {"mbb_player_crosswalk_2010.parquet": "sha256:new"}
    )
    player_aliases._fetch_crosswalks("mbb")
    assert gets == ["mbb_player_crosswalk_2010.parquet"]  # only the re-uploaded season

    seen = (tmp_path / "player_aliases" / "mbb" / "release_digests.json").read_bytes()
    bumped = digests | {"mbb_player_crosswalk_2011.parquet": "sha256:new"}
    _fake_release(monkeypatch, bumped, fail="mbb_player_crosswalk_2011.parquet")
    with pytest.raises(requests.HTTPError):  # a failed download records nothing: the next run retries it
        player_aliases._fetch_crosswalks("mbb")
    assert (tmp_path / "player_aliases" / "mbb" / "release_digests.json").read_bytes() == seen


def test_seasons_changed():
    assets = {"mbb_player_crosswalk_2004.parquet": "a", "mbb_player_crosswalk_2027.parquet": "b",
              "mbb_player_crosswalk_2027.csv": "c", "mbb_team_crosswalk_2027.parquet": "d"}  # fmt: skip
    published = refresh.release_seasons(assets, "mbb_player_crosswalk")
    assert published == {2004: "a", 2027: "b"}
    assert refresh.seasons_changed(published, {}) == [2004, 2027]
    assert refresh.seasons_changed(published, {2027: "b"}) == [
        2004
    ]  # newest is 2027; the old 2004 still qualifies
    assert refresh.seasons_changed(published, {2004: "a", 2027: "b"}) == []
    assert refresh.seasons_changed(published, {2004: "old", 2027: "b"}) == [2004]


def test_current_season():
    assert current_season(False, dt.date(2026, 9, 27)) == 2026
    assert current_season(True, dt.date(2026, 9, 27)) == 2027  # NBA/NHL 2026-27
    assert current_season(True, dt.date(2027, 3, 1)) == 2027
