"""Stage 30 uploads only assets whose sha256 differs from the release (one shared 5,000/h API quota)."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1] / "scripts/pipeline/30_publish_releases.sh"

# fake gh: `api` answers from $FAKE_LISTING ("sha256:<hex>  <name>" lines; names only for the
# `.assets[].name` re-list) or a 404; every other call is logged. With $FAKE_FAIL set, the first
# $FAKE_FAIL_TIMES uploads fail with that stderr and empty the listing, as a --clobber delete would.
FAKE_GH = """#!/usr/bin/env bash
if [ "$1" = api ]; then
  [ -f "$FAKE_LISTING" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
  if [[ "$*" == *'.assets[].name'* ]]; then awk '{print $2}' "$FAKE_LISTING"; else cat "$FAKE_LISTING"; fi
  exit 0
fi
echo "$*" >> "$FAKE_LOG"
if [ "$1 $2" = "release upload" ] && [ -n "${FAKE_FAIL:-}" ]; then
  n=$(( $(cat "$FAKE_LOG.n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_LOG.n"
  if [ "$n" -le "${FAKE_FAIL_TIMES:-99}" ]; then : > "$FAKE_LISTING"; echo "$FAKE_FAIL" >&2; exit 1; fi
fi
exit 0
"""


def _run(tmp_path: Path, listing: str | None, rc: int = 0, **fake: str) -> tuple[list[str], str]:
    root = tmp_path / "repo"
    (root / "scripts/pipeline").mkdir(parents=True)
    shutil.copy(STAGE, root / "scripts/pipeline/30_publish_releases.sh")
    build = root / "build/nba"
    build.mkdir(parents=True)
    for name, body in [
        ("nba_groups.csv", "a\n1\n"),
        ("nba_groups.parquet", "PQ"),
        ("nba_x.csv", "b\n2\n"),
    ]:
        (build / name).write_text(body)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "gh").write_text(FAKE_GH)
    (bin_ / "gh").chmod(0o755)
    log = tmp_path / "gh.log"
    env = {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_LISTING": str(tmp_path / "listing"),
        "GH_RETRY_BACKOFF_S": "0",
        **fake,
    }
    if listing is not None:
        digests = {
            f.stem.replace(".", "_") + "_" + f.suffix[1:]: hashlib.sha256(
                f.read_bytes()
            ).hexdigest()
            for f in build.iterdir()
        }
        (tmp_path / "listing").write_text(listing.format(**digests))
    proc = subprocess.run(
        ["bash", str(root / "scripts/pipeline/30_publish_releases.sh"), "nba"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == rc, proc.stdout + proc.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    return calls, proc.stdout + proc.stderr


UNCHANGED = (
    "sha256:{nba_groups_csv}  nba_groups.csv\n"
    "sha256:{nba_groups_parquet}  nba_groups.parquet\n"
    "sha256:{nba_x_csv}  nba_x.csv\n"
)
CHANGED = "sha256:{nba_groups_csv}  nba_groups.csv\nsha256:old  nba_groups.parquet\n"


def test_unchanged_assets_make_no_upload(tmp_path):
    calls, _ = _run(tmp_path, UNCHANGED)
    assert calls == []


def test_only_changed_or_missing_assets_upload_parquet_last(tmp_path):
    calls, _ = _run(tmp_path, CHANGED)
    assert len(calls) == 1
    args = calls[0].split()
    assert args[:3] == ["release", "upload", "nba_groups"]
    files = [Path(a).name for a in args if a.startswith("build/")]
    assert files == ["nba_x.csv", "nba_groups.parquet"]


def test_missing_release_is_created_then_everything_uploads(tmp_path):
    calls, _ = _run(tmp_path, None)
    assert calls[0].startswith("release create nba_groups")
    assert len([a for a in calls[1].split() if a.startswith("build/")]) == 3


# --- bounded retry: a 502 on the delete half of --clobber dropped an asset (2026-10-12) ---


def test_transient_upload_failure_is_retried_then_succeeds(tmp_path):
    calls, _ = _run(tmp_path, CHANGED, FAKE_FAIL="HTTP 502: Server Error", FAKE_FAIL_TIMES="1")
    assert [c.split()[:2] for c in calls] == [["release", "upload"]] * 2


def test_4xx_upload_failure_is_not_retried(tmp_path):
    calls, _ = _run(tmp_path, CHANGED, rc=1, FAKE_FAIL="HTTP 422: Validation Failed")
    assert len(calls) == 1


def test_final_failure_names_the_asset_clobber_deleted(tmp_path):
    calls, out = _run(tmp_path, CHANGED, rc=1, FAKE_FAIL="HTTP 503: Service Unavailable")
    assert len(calls) == 3
    assert "ASSET MISSING: nba_groups:nba_groups.parquet" in out
