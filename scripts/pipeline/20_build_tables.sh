#!/usr/bin/env bash
# Stage 20: build and validate the {league}_groups tables into build/{league}/ (offline, from raw/ + curated/).
set -euo pipefail
cd "$(dirname "$0")/../.."
UV_CACHE_DIR="${UV_CACHE_DIR:-/mnt/sdv_repos/.uv-cache}" uv run python -m sdv_reference.build "$@"
