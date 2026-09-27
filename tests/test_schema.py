import polars as pl

from sdv_reference.schema import conform, validate


def tiny():
    g = conform("groups", pl.DataFrame({
        "league": ["cfb"] * 3, "group_id": ["cfb:fbs", "cfb:big-ten", "cfb:pac-12"],
        "level": ["subdivision", "conference", "conference"],
        "first_season": [2023, 2023, 2023], "last_season": [2024, 2024, 2023]}))
    gs = conform("group_seasons", pl.DataFrame({
        "league": ["cfb"] * 5, "group_id": ["cfb:fbs", "cfb:fbs", "cfb:big-ten", "cfb:big-ten", "cfb:pac-12"],
        "season": [2023, 2024, 2023, 2024, 2023], "level": ["subdivision", "subdivision", "conference", "conference", "conference"],
        "name": ["FBS", "FBS", "Big Ten Conference", "Big Ten Conference", "Pac-12 Conference"],
        "parent_group_id": [None, None, "cfb:fbs", "cfb:fbs", "cfb:fbs"]}))
    ga = conform("group_aliases", pl.DataFrame({
        "league": ["cfb"], "group_id": ["cfb:big-ten"], "source": ["espn"], "source_id": ["5"],
        "name_kind": ["short_name"], "value": ["Big Ten"]}))
    tgs = conform("team_group_seasons", pl.DataFrame({
        "league": ["cfb"] * 2, "season": [2023, 2024], "team_id": ["30", "30"], "team_id_source": ["espn"] * 2,
        "team_name": ["USC"] * 2, "subdivision_id": ["cfb:fbs"] * 2, "conference_id": ["cfb:pac-12", "cfb:big-ten"],
        "source": ["cfbd"] * 2}))
    return {"groups": g, "group_seasons": gs, "group_aliases": ga, "team_group_seasons": tgs}


def test_valid_league_passes():
    assert validate("cfb", tiny()) == []


def test_contract_violations_are_caught():
    t = tiny()
    t["team_group_seasons"] = t["team_group_seasons"].with_columns(pl.lit("cfb:big-12").alias("conference_id"))
    assert any("not in groups" in p for p in validate("cfb", t))
    t = tiny()
    t["group_seasons"] = pl.concat([t["group_seasons"], t["group_seasons"].head(1)])
    assert any("duplicate (group_id, season)" in p for p in validate("cfb", t))
    t = tiny()
    t["groups"] = t["groups"].with_columns(pl.col("last_season").cast(pl.Int64))
    assert any("schema" in p for p in validate("cfb", t))
    t = tiny()
    t["groups"] = t["groups"].with_columns(pl.when(pl.col("group_id") == "cfb:pac-12").then(2024).otherwise(pl.col("last_season")).cast(pl.Int32).alias("last_season"))
    assert any("first/last_season disagree" in p for p in validate("cfb", t))
    # USC's 2024 row pointing at the Pac-12, which has no 2024 season
    t = tiny()
    t["team_group_seasons"] = t["team_group_seasons"].with_columns(pl.lit("cfb:pac-12").alias("conference_id"))
    assert any("not in group_seasons" in p for p in validate("cfb", t))
