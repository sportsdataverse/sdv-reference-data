# Runbook

`scripts/run_pipeline.sh [league ...]` runs every stage in order (all leagues when none are named).

| Stage | Script | Frequency | Idempotency | Typical duration |
|---|---|---|---|---|
| 10 | `scripts/pipeline/10_fetch_sources.sh [league ...]` | when sources change (new season, realignment) | overwrites `raw/{league}/` snapshots; commit them | seconds (CFBD, MLB) to ~30 min (ESPN walks at 1 req/s) |
| 20 | `scripts/pipeline/20_build_tables.sh [league ...]` | every run | offline and deterministic from `raw/` + `curated/`; refuses tables that fail `CONTRACT.md` | ~10 s for all leagues |
| 30 | `scripts/pipeline/30_publish_releases.sh [league ...]` | after a reviewed build | `--clobber` per asset; creates `{league}_groups` once | ~1 min per league |

Stage 10 changes committed data: review the `raw/` diff and rebuild before publishing. Run stages individually when
you only need to rebuild or republish.
