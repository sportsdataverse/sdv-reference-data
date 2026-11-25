"""run_pipeline.sh: one league's stage-10 failure still builds and publishes the rest, then fails the run.

The 2026-11-09 weekly run fetched every league but cfb (CFBD 429), so stage 10 exited 1 and nothing was built,
committed or published: mbb/wbb player_aliases missed that week's 2026-27 NCAA crosswalk.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_pipeline.sh"


def _run(
    tmp_path: Path, rcs: dict[str, int], out: list[str] | None = None
) -> tuple[int, list[str]]:
    root = tmp_path / "repo"
    (root / "scripts/pipeline").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts/run_pipeline.sh")
    log = tmp_path / "ran"
    for stage in (
        "10_fetch_sources",
        "20_build_tables",
        "25_commit_raw",
        "30_publish_releases",
    ):
        body = f'#!/usr/bin/env bash\necho {stage} >> "{log.as_posix()}"\nexit {rcs.get(stage, 0)}\n'
        (root / f"scripts/pipeline/{stage}.sh").write_text(body, newline="\n")
    proc = subprocess.run(
        [shutil.which("bash"), (root / "scripts/run_pipeline.sh").as_posix()],
        capture_output=True,
        text=True,
    )
    if out is not None:
        out.append(proc.stdout)
    return proc.returncode, log.read_text().split()


@pytest.mark.parametrize(
    ("rcs", "rc", "ran"),
    [
        (
            {},
            0,
            [
                "10_fetch_sources",
                "20_build_tables",
                "25_commit_raw",
                "30_publish_releases",
            ],
        ),
        # a league's fetch failed: the rest still build and publish, and the run fails
        (
            {"10_fetch_sources": 1},
            1,
            [
                "10_fetch_sources",
                "20_build_tables",
                "25_commit_raw",
                "30_publish_releases",
            ],
        ),
        # ... and when nothing else changed, it stops before publishing, still failing
        (
            {"10_fetch_sources": 1, "25_commit_raw": 3},
            1,
            ["10_fetch_sources", "20_build_tables", "25_commit_raw"],
        ),
        (
            {"25_commit_raw": 3},
            0,
            ["10_fetch_sources", "20_build_tables", "25_commit_raw"],
        ),
        # a build or test failure still stops the run
        ({"20_build_tables": 2}, 2, ["10_fetch_sources", "20_build_tables"]),
    ],
)
def test_stage_flow(
    tmp_path: Path, rcs: dict[str, int], rc: int, ran: list[str]
) -> None:
    assert _run(tmp_path, rcs) == (rc, ran)


def test_nothing_to_publish_says_why_it_passed_or_failed(tmp_path: Path) -> None:
    """sdv-reference-data#8: "nothing to publish" then EXIT=1 read as a false alarm; the failing stage was 10."""
    out: list[str] = []
    assert _run(tmp_path / "ok", {"25_commit_raw": 3}, out)[0] == 0
    assert "nothing to publish: raw/ unchanged upstream, run succeeded" in out[0]
    assert (
        _run(tmp_path / "bad", {"10_fetch_sources": 1, "25_commit_raw": 3}, out)[0] == 1
    )
    assert (
        "nothing to publish, but stage 10 (fetch) failed: run fails with EXIT=1"
        in out[1]
    )


FETCH = Path(__file__).resolve().parents[1] / "scripts/pipeline/10_fetch_sources.sh"

# fake uv: `uv run python -c "...fetch('<league>')"` rewrites the league's snapshots, then cfb fails half way
FAKE_UV = """#!/usr/bin/env bash
l=$(echo "$*" | sed -n "s/.*fetch('\\([a-z_]*\\)').*/\\1/p")
mkdir -p "raw/$l" "raw/player_aliases/$l"
echo new > "raw/$l/groups.csv"
[ "$l" = cfb ] && exit 1
echo new > "raw/player_aliases/$l/ncaa_2027.csv"
"""


def test_failed_league_raw_reverted(tmp_path: Path) -> None:
    """A league whose fetch failed part way keeps its committed raw/; the others keep their refresh."""
    root = tmp_path / "repo"
    (root / "scripts/pipeline").mkdir(parents=True)
    shutil.copy(FETCH, root / "scripts/pipeline/10_fetch_sources.sh")
    for lg in ("cfb", "mbb"):
        (root / f"raw/{lg}").mkdir(parents=True)
        (root / f"raw/{lg}/groups.csv").write_text("old\n")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "uv").write_text(FAKE_UV, newline="\n")
    (bin_ / "uv").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            shutil.which("bash"),
            (root / "scripts/pipeline/10_fetch_sources.sh").as_posix(),
            "cfb",
            "mbb",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "fetch failed: cfb" in proc.stdout
    assert (root / "raw/cfb/groups.csv").read_text() == "old\n"
    assert not (root / "raw/player_aliases/cfb").exists()
    assert (root / "raw/mbb/groups.csv").read_text() == "new\n"
    assert (root / "raw/player_aliases/mbb/ncaa_2027.csv").read_text() == "new\n"
