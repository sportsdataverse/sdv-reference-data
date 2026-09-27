"""MLB park dimensions by season (venue x season), from the MLB Stats API's per-season venue `fieldInfo`.

uv run python -m sdv_reference.parks.mlb fetch    # network: refresh raw/mlb_parks/
uv run python -m sdv_reference.build mlb_parks     # offline: raw/mlb_parks/ + curated/ -> build/mlb_parks/

`venues?sportId=1&season=YYYY&hydrate=fieldInfo,location,xrefId` lists every venue MLB used that season (regular
season, spring training, neutral sites) with that season's name, fence distances, capacity, turf and roof.

Coverage starts in 2001. For 1901-2000 the API returns one undated record per venue for every season: no venue's
fieldInfo differs between any two of those seasons, so Yankee Stadium I in 1925 carries its 1988-2008 fences and the
Oakland Coliseum in 1970 its post-1996 capacity. Per-season records begin in 2001 (13 capacities change 2000 -> 2001);
fence changes appear from 2006, and the API lags or misses some, which curated/mlb_park_overrides.csv corrects with
citations (CONTRACT.md, mlb_park_dimensions).

Seamheads' ballpark database is not used: its licence forbids redistribution.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import requests

from sdv_reference.leagues.mlb import _statsapi, read_json_gz, write_json_gz

NAME = "mlb_parks"
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw" / NAME / "statsapi_venues.json.gz"
OVERRIDES = ROOT / "curated" / "mlb_park_overrides.csv"
FIRST_SEASON = 2001
# the API's seven fence markers, in feet from home plate, left to right; each park publishes five to seven of them
DISTANCES = {
    "leftLine": "left_line_ft",
    "left": "left_ft",
    "leftCenter": "left_center_ft",
    "center": "center_ft",
    "rightCenter": "right_center_ft",
    "right": "right_ft",
    "rightLine": "right_line_ft",
}
SCHEMA: dict[str, pl.DataType] = {
    "league": pl.Utf8,
    "season": pl.Int32,
    "venue_id": pl.Utf8,
    "venue_name": pl.Utf8,
    "retro_park_id": pl.Utf8,
    **{c: pl.Int32 for c in DISTANCES.values()},
    "capacity": pl.Int32,
    "turf_type": pl.Utf8,
    "roof_type": pl.Utf8,
    "azimuth_deg": pl.Float64,
    "elevation_ft": pl.Int32,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "notes": pl.Utf8,
}
KEYS = ["league", "season", "venue_id", "venue_name"]
# fences in MLB use run 280 ft (the Alamodome's right-field line, spring 2013-17) to 435 ft (Tal's Hill, 2000-16);
# a value outside this is a unit or key error, not a park
FENCE_FT = (250, 500)


def _slim(v: dict) -> dict:
    """The fields the build reads: no phone numbers, ticketing or weather-station ids, so a re-fetch only diffs
    when a name, a fence, a capacity or a location changes."""
    loc = v.get("location") or {}
    return {
        "id": v["id"],
        "name": v["name"],
        "fieldInfo": v.get("fieldInfo") or {},
        "location": {
            k: loc[k]
            for k in ("azimuthAngle", "elevation", "defaultCoordinates")
            if k in loc
        },
        "retrosheet": next(
            (
                x["xrefId"]
                for x in v.get("xrefIds", [])
                if x["xrefType"] == "retrosheet"
            ),
            None,
        ),
    }


def fetch(last_season: int | None = None) -> None:
    """Snapshot raw/mlb_parks/: one Stats API call per refresh.seasons_to_fetch() season, over http (https with a
    query string returns 406 from the droplet) and never through a proxy (MLB refuses the decodo proxy)."""
    from sdv_reference.refresh import current_season, seasons_to_fetch

    last = last_season or current_season(ending_year=False)
    out = read_json_gz(RAW) if RAW.exists() else {}
    s = requests.Session()
    s.trust_env = False  # no proxy from the environment
    for y in seasons_to_fetch(range(FIRST_SEASON, last + 1), map(int, out)):
        venues = _statsapi(
            f"venues?sportId=1&season={y}&hydrate=fieldInfo,location,xrefId", s
        ).get("venues", [])
        if not venues:
            continue
        out[str(y)] = sorted((_slim(v) for v in venues), key=lambda v: v["id"])
        print(f"statsapi venues {y}: {len(venues)}", flush=True)
        write_json_gz(RAW, out)


def validate(df: pl.DataFrame) -> list[str]:
    """Problems with the table; an empty list means it meets the contract."""
    if dict(df.schema) != SCHEMA:
        return [f"schema {dict(df.schema)} != {SCHEMA}"]
    problems = [
        f"{k}: {n} nulls in a key column" for k in KEYS if (n := df[k].null_count())
    ]
    if (df["league"] != "mlb").any():
        problems.append("rows for another league")
    if df.select("venue_id", "season").is_duplicated().any():
        problems.append("duplicate (venue_id, season)")
    lo, hi = FENCE_FT
    for c in DISTANCES.values():
        bad = df.filter(~pl.col(c).is_between(lo, hi))
        if bad.height:
            problems.append(
                f"{c}: {bad.height} values outside {lo}-{hi} ft, e.g. {bad.select('venue_name', 'season', c).row(0)}"
            )
    return problems


def _apply_overrides(df: pl.DataFrame) -> pl.DataFrame:
    """Replace the API's values with curated/mlb_park_overrides.csv and say so in `notes`, once per correction
    (rows sharing venue, seasons, source and notes). Each correction must still change something: one the API has
    caught up with should be deleted, not carried."""
    cur = pl.read_csv(OVERRIDES, infer_schema_length=0)
    unknown = (
        set(cur["column"])
        - set(DISTANCES.values())
        - {"capacity", "turf_type", "roof_type"}
    )
    if unknown:
        raise ValueError(f"mlb_park_overrides: unknown columns {sorted(unknown)}")
    df = df.with_columns(notes=pl.lit(None, pl.Utf8))
    keys = ["venue_id", "valid_from", "valid_to", "source", "notes"]
    for (vid, lo, hi, source, why), rows in cur.group_by(keys, maintain_order=True):
        hit = (pl.col("venue_id") == vid) & pl.col("season").is_between(
            int(lo), int(hi or 9999)
        )
        new = {
            r["column"]: pl.lit(r["value"]).cast(SCHEMA[r["column"]])
            for r in rows.iter_rows(named=True)
        }
        change = pl.concat_str(
            [
                pl.when(pl.col(c).ne_missing(v)).then(
                    pl.format(
                        "{} {} -> {}",
                        pl.lit(c),
                        pl.col(c).cast(pl.Utf8).fill_null("null"),
                        v.cast(pl.Utf8).fill_null("null"),
                    )
                )
                for c, v in new.items()
            ],
            separator=", ",
            ignore_nulls=True,
        )
        changed = hit & (change != "")
        if not df.filter(changed).height:
            raise ValueError(
                f"mlb_park_overrides: venue {vid} {lo}-{hi or ''} changes nothing"
            )
        note = pl.format("curated {} ({}; {})", change, pl.lit(why), pl.lit(source))
        df = df.with_columns(
            *[
                pl.when(hit).then(v).otherwise(pl.col(c)).alias(c)
                for c, v in new.items()
            ],
            notes=pl.when(changed)
            .then(
                pl.concat_str(
                    [pl.col("notes"), note], separator="; ", ignore_nulls=True
                )
            )
            .otherwise(pl.col("notes")),
        )
    return df


def build() -> pl.DataFrame:
    """mlb_park_dimensions, offline from raw/mlb_parks/ and curated/mlb_park_overrides.csv."""
    rows = []
    for season, venues in read_json_gz(RAW).items():
        for v in venues:
            f, loc = v["fieldInfo"], v["location"]
            xy = loc.get("defaultCoordinates") or {}
            rows.append(
                {
                    "season": int(season),
                    "venue_id": str(v["id"]),
                    "venue_name": v["name"],
                    "retro_park_id": v["retrosheet"],
                    **{c: f.get(k) for k, c in DISTANCES.items()},
                    "capacity": f.get("capacity"),
                    "turf_type": f.get("turfType"),
                    "roof_type": f.get("roofType"),
                    "azimuth_deg": loc.get("azimuthAngle"),
                    "elevation_ft": loc.get("elevation"),
                    "latitude": xy.get("latitude"),
                    "longitude": xy.get("longitude"),
                }
            )
    df = pl.DataFrame(
        rows, schema={c: t for c, t in SCHEMA.items() if c not in ("league", "notes")}
    )
    # placeholder venues (TBD, AL/NL Stadium) and a few with no measurements carry only a default Grass/Open
    has_data = pl.any_horizontal(
        pl.col([*DISTANCES.values(), "capacity"]).is_not_null()
    )
    df = _apply_overrides(df.filter(has_data).with_columns(league=pl.lit("mlb")))
    return df.select(list(SCHEMA)).sort("season", "venue_id")


def write(out_dir: Path) -> list[Path]:
    """Build, validate and write build/mlb_parks/mlb_park_dimensions.{parquet,csv}."""
    df = build()
    problems = validate(df)
    if problems:
        raise ValueError(
            "mlb_park_dimensions fails the contract:\n  " + "\n  ".join(problems)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [
        out_dir / "mlb_park_dimensions.parquet",
        out_dir / "mlb_park_dimensions.csv",
    ]
    df.write_parquet(files[0])
    df.write_csv(files[1])
    print(
        f"{NAME}: {df.height} venue-seasons, {df['venue_id'].n_unique()} venues "
        f"({df['season'].min()}-{df['season'].max()}), {df['notes'].is_not_null().sum()} curated",
        flush=True,
    )
    return files


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
