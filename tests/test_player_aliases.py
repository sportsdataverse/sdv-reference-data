"""{league}_player_aliases: offline (raw/ + curated/), the contract, the match-rate floors and the no-guessing rules."""

import importlib
from functools import cache

import polars as pl
import pytest
import requests

from sdv_reference import player_aliases, team_aliases
from sdv_reference.schema import validate_player_aliases


@cache
def tgs(league: str) -> pl.DataFrame:
    return importlib.import_module(f"sdv_reference.leagues.{league}").build()[
        "team_group_seasons"
    ]


@cache
def built(league: str):
    return player_aliases.build(
        league, tgs(league), team_aliases.build(league, tgs(league))[0]
    )


@pytest.mark.parametrize("league", player_aliases.LEAGUES)
def test_contract_and_floors(league):
    aliases, _, rates = built(league)
    assert validate_player_aliases(league, aliases, tgs(league)) == []
    assert player_aliases.gate(league, rates) == []


@pytest.mark.parametrize("league", ["mbb", "wbb", "cfb"])
def test_ncaa_leg_is_published(league):
    a = built(league)[0].filter(pl.col("source") == "ncaa")
    assert a.height > 50_000
    # stats.ncaa.org roster ids are per season: every NCAA row is one season on one NCAA team
    assert (a["valid_from"] == a["valid_to"]).all() and a[
        "source_team_id"
    ].null_count() == 0
    # transfers link through the athlete: some athletes carry NCAA ids on two ESPN teams
    assert (
        a.group_by("athlete_id")
        .agg(pl.col("team_id").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
        .height
        > 1000
    )


def test_nba_yahoo_player_ids_are_dotted():
    y = built("nba")[0].filter(pl.col("source") == "yahoo")
    assert y["source_player_id"].str.starts_with("nba.p.").all()


def test_every_dropped_row_has_a_reason():
    for league in ("nfl", "mbb"):
        assert built(league)[1]["reason"].null_count() == 0


def _row(season, team, athlete, pid, method="exact_name"):
    return dict(season=season, team_id=team, athlete_id=athlete, source="fox", source_player_id=pid, source_team_id=None,
                match_method=method, match_confidence=1.0, unmatched_reason=None if athlete else "no ESPN athlete in the leg",
                evidence="test")  # fmt: skip


def test_rules_on_a_synthetic_league(monkeypatch, tmp_path):
    rows = [
        _row(2024, "1", "a1", "f1"), _row(2025, "2", None, "f1"),  # f1 carried to 2025 (a transfer to team 2)
        _row(2024, "1", "a2", "f2"), _row(2025, "1", "a3", "f2"),  # f2 on two athletes: dropped every season
        _row(2025, "1", "a4", "f4"), _row(2025, "1", None, "f5"),  # f5: a miss with no pair anywhere
    ]  # fmt: skip
    monkeypatch.setattr(
        player_aliases,
        "legs",
        lambda league: pl.DataFrame(rows, schema=player_aliases.LONG),
    )
    hub = pl.DataFrame({"season": [2024, 2025, 2024, 2025], "team_id": ["1", "1", "2", "2"],
                        "team_id_source": ["espn"] * 4}, schema_overrides={"season": pl.Int32})  # fmt: skip
    (tmp_path / "player_overrides.csv").write_text(
        "league,source,source_player_id,athlete_id,verified_by,note\n"
    )
    (tmp_path / "player_match_floors.csv").write_text(
        "league,source,season_from,season_to,floor_pct,note\nmbb,fox,2024,2025,90.0,test\n"
    )
    monkeypatch.setattr(player_aliases, "CURATED", tmp_path)
    aliases, unmatched, rates = player_aliases.build("mbb", hub, pl.DataFrame())
    got = {
        (
            r["source_player_id"],
            r["athlete_id"],
            r["team_id"],
            r["valid_from"],
            r["match_method"],
        )
        for r in aliases.to_dicts()
    }
    assert got == {
        ("f1", "a1", "1", 2024, "exact_name"),
        ("f1", "a1", "2", 2025, "propagated"),
        ("f4", "a4", "1", 2025, "exact_name"),
    }
    assert set(
        unmatched.filter(pl.col("source_player_id") == "f2")["reason"].str.slice(0, 8)
    ) == {"conflict"}
    assert player_aliases.gate("mbb", rates)  # 2024: 1 of 2 rows kept -> 50% < 90%

    # an override that changes nothing fails the build; one that does is applied
    (tmp_path / "player_overrides.csv").write_text(
        "league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f4,a4,x,y\n"
    )
    with pytest.raises(AssertionError, match="change nothing"):
        player_aliases.build("mbb", hub, pl.DataFrame())
    (tmp_path / "player_overrides.csv").write_text(
        "league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f4,a9,x,y\n"
    )
    a = player_aliases.build("mbb", hub, pl.DataFrame())[0].filter(
        pl.col("source_player_id") == "f4"
    )
    assert a.select("athlete_id", "match_method").rows() == [("a9", "override")]

    # a blank athlete forces the id unmatched; on an id that is already unmatched it changes nothing
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f4,,x,y\n")
    a, u, _ = player_aliases.build("mbb", hub, pl.DataFrame())
    assert a.filter(pl.col("source_player_id") == "f4").height == 0
    assert u.filter(pl.col("source_player_id") == "f4")["reason"].to_list() == ["curated override: no ESPN athlete"]
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f5,,x,y\n")
    with pytest.raises(AssertionError, match="change nothing"):
        player_aliases.build("mbb", hub, pl.DataFrame())
    # an override can set the athlete of a leg's miss (team-block checks still apply)
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\nmbb,fox,f5,a5,x,y\n")
    a = player_aliases.build("mbb", hub, pl.DataFrame())[0].filter(pl.col("source_player_id") == "f5")
    assert a.select("athlete_id", "team_id", "match_method").rows() == [("a5", "1", "override")]


@pytest.mark.parametrize("league", ["nba", "cfb"])
def test_windows_span_seasons(league):
    a = built(league)[0].filter(pl.col("source") != "ncaa")
    assert a.filter(pl.col("valid_to") > pl.col("valid_from")).height > 1000


def _cbs_league(monkeypatch, tmp_path, rows, statline):
    """A one-team cfb whose cbs leg is `rows` and whose ESPN stat-line athletes are `statline`."""
    monkeypatch.setattr(player_aliases, "legs", lambda league: pl.DataFrame(rows, schema=player_aliases.LONG))
    monkeypatch.setattr(player_aliases, "statlines", lambda league: pl.DataFrame(
        {"season": [2026] * len(statline), "athlete_id": statline}, schema={"season": pl.Int32, "athlete_id": pl.Utf8}))  # fmt: skip
    (tmp_path / "player_overrides.csv").write_text("league,source,source_player_id,athlete_id,verified_by,note\n")
    (tmp_path / "player_match_floors.csv").write_text(
        "league,source,season_from,season_to,floor_pct,note\ncfb,cbs,2026,2026,98.0,test\n"
    )
    monkeypatch.setattr(player_aliases, "CURATED", tmp_path)
    hub = pl.DataFrame({"season": [2026], "team_id": ["1"], "team_id_source": ["espn"]},
                       schema_overrides={"season": pl.Int32})  # fmt: skip
    return player_aliases.build("cfb", hub, pl.DataFrame())[2]


def _cbs(athlete, pid):
    return {**_row(2026, "1", athlete, pid), "source": "cbs", "unmatched_reason": None}


def test_cfb_cbs_gate_counts_only_athletes_with_an_espn_stat_line(monkeypatch, tmp_path):
    # 10 ESPN athletes with a stat line, all with a CBS id; 10 linemen with none and no CBS id (CBS never lists them)
    rows = [_cbs(f"s{i}", f"c{i}") for i in range(10)] + [_cbs(f"ol{i}", None) for i in range(10)]
    rates = _cbs_league(monkeypatch, tmp_path, rows, [f"s{i}" for i in range(10)])
    r = rates.row(0, named=True)
    assert (r["match_rate"], r["match_rate_statline"], r["n_rows_statline"]) == (50.0, 100.0, 10)
    assert player_aliases.gate("cfb", rates) == []


def test_cfb_cbs_gate_fails_when_stat_line_athletes_lack_cbs_ids(monkeypatch, tmp_path):
    # 98% of all rows match, but 2 of the 50 stat-line athletes have no CBS id (96%): only the stat-line gate catches it
    rows = [_cbs(f"s{i}", f"c{i}" if i < 8 else None) for i in range(10)] + [_cbs(f"x{i}", f"d{i}") for i in range(90)]
    rates = _cbs_league(monkeypatch, tmp_path, rows, [f"s{i}" for i in range(10)] + [f"x{i}" for i in range(40)])
    r = rates.row(0, named=True)
    assert (r["match_rate"], r["match_rate_statline"]) == (98.0, 96.0)
    assert player_aliases.gate("cfb", rates) == ["cfb cbs 2026: match rate 96.0 < floor 98.0"]


@pytest.mark.parametrize("statline", [[], ["zzz"], ["s1"]])
def test_cfb_cbs_gate_never_falls_back_to_all_rows(monkeypatch, tmp_path, statline):
    """No snapshot, one sharing no athlete with the leg (id drift), or one covering under a quarter of it: the build fails."""
    rows = [_cbs(f"s{i}", f"c{i}") for i in range(5)]
    with pytest.raises(ValueError, match="no espn_statline snapshot"):
        _cbs_league(monkeypatch, tmp_path, rows, statline)


def test_other_legs_keep_the_all_rows_gate():
    rates = built("cfb")[2]
    assert rates.filter(pl.col("source") != "cbs")["match_rate_statline"].null_count() == rates.filter(
        pl.col("source") != "cbs"
    ).height
    assert rates.filter(pl.col("source") == "cbs")["match_rate_statline"].null_count() == 0


def test_torvik_fetch_decodes_utf8_and_refuses_a_changed_layout(monkeypatch, tmp_path):
    def row(name, pid, n=67):
        f = ["x"] * n
        for i, v in ((0, name), (1, "Duke"), (27, "1"), (31, "2026"), (32, pid)):
            if i < n:
                f[i] = v
        return ",".join(f'"{v}"' for v in f)

    class R:
        def __init__(self, body):
            self.content = body.encode("utf-8")

        def raise_for_status(self):
            pass

    monkeypatch.setattr(player_aliases, "RAW", tmp_path)
    monkeypatch.setattr(player_aliases, "TORVIK", {"mbb": 2026})
    monkeypatch.setattr(player_aliases, "current_season", lambda ending_year: 2026)
    monkeypatch.setattr(player_aliases.requests, "get", lambda *a, **k: R(row("Mathis Mönninghoff", "7") + "\n"))
    player_aliases._fetch_torvik("mbb", pause=0)
    assert player_aliases._read(tmp_path / "mbb" / "torvik_2026.csv.gz")["player_name"].to_list() == ["Mathis Mönninghoff"]
    monkeypatch.setattr(player_aliases.requests, "get", lambda *a, **k: R(row("A", "7") + "\n" + row("B", "8", n=30)))
    with pytest.raises(ValueError, match="1 of 2 rows have under 33 fields"):
        player_aliases._fetch_torvik("mbb", pause=0)


def _refused(url, params=None, headers=None, timeout=None):
    r = requests.Response()
    r.status_code, r.reason, r.url = 403, "Forbidden", f"{url}?year={params['year']}&csv=1"
    return r


def _unreachable(*a, **k):
    raise requests.ConnectionError("connection reset")


@pytest.mark.parametrize(("get", "why"), [(_refused, "HTTP 403"), (_unreachable, "ConnectionError")])
def test_torvik_refused_keeps_the_committed_snapshot(monkeypatch, tmp_path, capsys, get, why):
    # 2026-11-25: barttorvik.com answered the droplet 403 and failed mbb's whole stage 10 (its raw/ reverted)
    lg = tmp_path / "mbb"
    lg.mkdir()
    snap = team_aliases._gz_csv(pl.DataFrame({"player_id": ["7"], "player_name": ["A"], "team": ["Duke"], "number": ["1"]}))
    (lg / "torvik_2025.csv.gz").write_bytes(snap)
    monkeypatch.setattr(player_aliases, "RAW", tmp_path)
    monkeypatch.setattr(player_aliases, "TORVIK", {"mbb": 2025})
    monkeypatch.setattr(player_aliases, "current_season", lambda ending_year: 2026)
    monkeypatch.setattr(player_aliases.requests, "get", get)
    player_aliases._fetch_torvik("mbb", pause=0)
    assert (lg / "torvik_2025.csv.gz").read_bytes() == snap
    assert sorted(p.name for p in lg.iterdir()) == ["torvik_2025.csv.gz"]
    out = capsys.readouterr().out
    assert f"WARNING: torvik mbb 2025: barttorvik.com answered {why}" in out and "(2025-2025)" in out


def test_torvik_refused_with_no_snapshot_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(player_aliases, "RAW", tmp_path)
    monkeypatch.setattr(player_aliases, "TORVIK", {"mbb": 2026})
    monkeypatch.setattr(player_aliases, "current_season", lambda ending_year: 2026)
    monkeypatch.setattr(player_aliases.requests, "get", _refused)
    with pytest.raises(requests.HTTPError, match="403"):
        player_aliases._fetch_torvik("mbb", pause=0)


def test_torvik_leg_is_published():
    a = built("mbb")[0].filter(pl.col("source") == "torvik")
    assert a.height > 50_000
    assert set(a["match_method"]) <= {"name_jersey", "name", "last_jersey", "propagated"}


def test_torvik_tiers_on_a_synthetic_season(monkeypatch, tmp_path):
    """Name + jersey, then name, then last name + jersey, each only on a key unique on both sides of the block."""
    lg = tmp_path / "mbb"
    lg.mkdir()
    torvik = pl.DataFrame({
        "player_id": ["1", "2", "3", "4", "5", "6", "7", "8"],
        "player_name": ["José Núñez", "Al Smith", "Al Smith", "Bo Jones Jr.", "Cy Young", "Di Lee", "Ed Ray", "Ed Ray"],
        "team": ["Duke", "Duke", "Duke", "Duke", "Duke", "Nowhere St.", "Duke", "Duke"],
        "number": ["01", "", "", "9", "4", "2", "12", "--"],
    })  # fmt: skip
    espn = pl.DataFrame({
        "season": ["2026"] * 7, "espn_team_id": ["150"] * 7,
        "espn_athlete_id": ["a1", "a2", "a3", "a4", "a5", "a7", "a8"],
        "espn_full_name": ["Jose Nunez", "Al Smith", "Al Smith", "Bo Jones", "Cyrus Young", "Ed Ray", "Ed Ray"],
        "espn_jersey": ["1", "3", "5", "9", "4", "12", "20"],
    })  # fmt: skip
    (lg / "torvik_2026.csv.gz").write_bytes(team_aliases._gz_csv(torvik))
    (lg / "mbb_player_crosswalk_2026.csv.gz").write_bytes(team_aliases._gz_csv(espn))
    monkeypatch.setattr(player_aliases, "RAW", tmp_path)
    ta = pl.DataFrame({"source": ["torvik"], "name_kind": ["name"], "value": ["Duke"], "team_id": ["150"],
                       "valid_from": [2026], "valid_to": [2026]}, schema_overrides={"valid_from": pl.Int32, "valid_to": pl.Int32})  # fmt: skip
    got = {
        r["source_player_id"]: (r["athlete_id"], r["match_method"], r["unmatched_reason"])
        for r in pl.concat(player_aliases._torvik_legs("mbb", ta)).to_dicts()
    }
    assert got["1"] == ("a1", "name_jersey", None)  # accents folded, Torvik's "01" is ESPN's "1"
    # two Ed Rays: #12 matches on name + jersey, which leaves the other one unique by name alone
    assert got["7"] == ("a7", "name_jersey", None) and got["8"] == ("a8", "name", None)
    assert got["4"] == ("a4", "name_jersey", None)  # Jr. dropped
    assert got["5"] == ("a5", "last_jersey", None)  # Cy / Cyrus: last name + jersey
    # two Al Smiths with no Torvik jersey: a tie on every tier stays unmatched, never guessed
    assert got["2"][0] is None and got["3"][0] is None and got["2"][2].startswith("no unique name match")
    assert got["6"][0] is None and got["6"][2].startswith("no ESPN team")



def test_jersey_drops_only_a_leading_zero_before_1_to_9():
    df = pl.DataFrame({"j": ["01", "0", "00", "09", "10", "--", " 7 "]})
    assert df.select(player_aliases._jersey("j"))["j"].to_list() == ["1", "0", "00", "9", "10", None, "7"]


def test_recruit_tiers_on_a_synthetic_class(monkeypatch, tmp_path):
    """On3 (with its 247 id) -> CFBD recruit -> ESPN athlete, in the (class, ESPN team) block; ties stay unmatched."""
    lg = tmp_path / "cfb"
    lg.mkdir()
    on3 = pl.DataFrame(
        {
            "on3_person_key": ["o1", "o2", "o3", "o4", "o5", "o6", "o7"],
            "sports247_player_id": ["s1", None, "s3", "s4", "s5", "s6", "s7"],
            "name": [
                "Arch Manning",
                "Bo Jones Jr.",
                "Al Smith",
                "Al Smith",
                "Cy Young",
                "Di Lee",
                "Ed Ray",
            ],
            "position": ["QB", "RB", "WR", "CB", "TE", "OT", "LB"],
            "status": ["Enrolled"] * 7,
            "on3_org_key": ["23628"] * 5 + ["999", "23628"],
            "on3_org_name": ["Texas Longhorns"] * 5 + ["Nowhere", "Texas Longhorns"],
        }
    )
    cfbd = pl.DataFrame(
        {
            "cfbd_recruit_id": ["c1", "c2", "c3", "c4", "c5", "c7", "c8"],
            "athlete_id": ["a1", "a2", "a3", "a4", None, "a7", "a8"],
            "name": [
                "Arch Manning",
                "Bo Jones",
                "Al Smith",
                "Al Smith",
                "Cy Young",
                "Ed Ray",
                "Ed Ray",
            ],
            "committed_to": ["Texas"] * 7,
            "position": ["PRO", "RB", "WR", "S", "TE", "OLB", "ILB"],
        }
    )
    (lg / "on3_recruits_2023.csv.gz").write_bytes(team_aliases._gz_csv(on3))
    (lg / "cfbd_recruits_2023.csv.gz").write_bytes(team_aliases._gz_csv(cfbd))
    monkeypatch.setattr(player_aliases, "RAW", tmp_path)
    teams = pl.DataFrame({"committed_to": ["Texas"], "team_id": ["251"]})
    monkeypatch.setattr(player_aliases, "_cfbd_team_ids", lambda: teams)
    ta = pl.DataFrame(
        {
            "source": ["on3"],
            "id_kind": ["school"],
            "source_id": ["23628"],
            "team_id": ["251"],
        }
    )
    rows = pl.concat(player_aliases._recruit_legs("cfb", ta))
    assert rows.select("source", "source_player_id").is_unique().all() and rows.height == 11  # 6 On3 + 5 with a 247 id
    got = {
        (r["source"], r["source_player_id"]): (
            r["athlete_id"],
            r["match_method"],
            r["unmatched_reason"],
        )
        for r in rows.to_dicts()
    }
    assert got[("on3", "o1")] == ("a1", "name", None)  # QB vs CFBD's PRO: the name tier
    assert got[("sports247", "s1")] == ("a1", "name", None)
    assert got[("on3", "o2")] == (
        "a2",
        "name_position",
        None,
    )  # Jr. dropped; no 247 id, so no sports247 row
    assert ("sports247", None) not in got
    # two Al Smiths at Texas: the WR matches on name + position, which leaves the other unique by name alone
    assert got[("on3", "o3")] == ("a3", "name_position", None)
    assert got[("on3", "o4")] == ("a4", "name", None)
    # two CFBD Ed Rays and neither position agrees: a tie on every tier stays unmatched, never guessed
    assert got[("on3", "o7")][0] is None
    assert got[("on3", "o7")][2].startswith("no unique name match")
    assert (
        got[("on3", "o5")][0] is None and "no ESPN athlete" in got[("on3", "o5")][2]
    )  # no CFBD athleteId
    assert (
        "on3",
        "o6",
    ) not in got  # committed to a school the On3 directory doesn't map: not a candidate
    assert rows.schema["season"] == pl.Int32 and rows.schema["athlete_id"] == pl.Utf8


def test_on3_rows_take_the_247_id_from_its_rating_link():
    item = {
        "person": {"key": 7353, "fullName": "Arch Manning", "position": {"abbr": "QB"}},
        "ratings": [
            {"type": "On3", "link": None},
            {"type": "247", "link": "https://247sports.com/player/46080533/"},
        ],
        "status": {
            "type": "Enrolled",
            "committedAsset": {
                "key": 23628,
                "fullName": "Texas Longhorns",
                "orgType": "College",
            },
        },
    }
    r = player_aliases._on3_rows(item)
    assert (r["on3_person_key"], r["sports247_player_id"], r["on3_org_key"]) == (
        7353,
        "46080533",
        23628,
    )
    item["status"]["committedAsset"]["orgType"] = "HighSchool"
    assert player_aliases._on3_rows(item)["on3_org_key"] is None
