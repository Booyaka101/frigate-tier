"""Prove a moved segment still plays the way Frigate plays it.

frigate/api/media.py builds playback and exports by writing the stored path into
an ffmpeg concat playlist, one `file '{clip.path}'` line per row, then handing
that to ffmpeg. This does the same thing with the paths the database holds after
a move, so a pass here means Frigate's own playback path resolves.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import paths_under

from frigate_tier.__main__ import cli


def _playlist(db_path: Path, camera: str, target: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        clips = conn.execute(
            "select path from recordings where camera = ? order by start_time",
            (camera,),
        ).fetchall()
    with open(target, "w") as handle:
        for (path,) in clips:
            # Frigate writes clip.path verbatim; on Linux that is this string.
            # The concat demuxer treats a backslash as an escape, so the Windows
            # test run needs forward slashes to reach the same file.
            handle.write(f"file '{Path(path).as_posix()}'\n")
    return len(clips)


def _duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


@pytest.fixture
def ffprobe():
    binary = shutil.which("ffprobe")
    if binary is None:
        pytest.skip("ffprobe is not on PATH")
    return binary


def test_a_camera_still_concatenates_after_a_move(
    tree, runner, ffmpeg, ffprobe, tmp_path
):
    playlist = tmp_path / "before.txt"
    clips = _playlist(tree.db_path, "driveway", playlist)
    before = tmp_path / "before.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(playlist),
            "-c",
            "copy",
            "-y",
            str(before),
        ],
        check=True,
        capture_output=True,
    )

    result = runner.invoke(
        cli,
        [
            "move",
            "--db",
            str(tree.db_path),
            "--hot",
            str(tree.hot),
            "--cold",
            str(tree.cold),
            "--older-than",
            "3d",
            "--commit",
            "--i-know",
        ],
    )
    assert result.exit_code == 0, result.output
    assert paths_under(tree.db_path, str(tree.cold))

    after_playlist = tmp_path / "after.txt"
    assert _playlist(tree.db_path, "driveway", after_playlist) == clips
    assert "mnt" in after_playlist.read_text()  # the playlist now spans both tiers

    after = tmp_path / "after.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(after_playlist),
            "-c",
            "copy",
            "-y",
            str(after),
        ],
        check=True,
        capture_output=True,
    )

    assert after.exists()
    assert _duration(after) == pytest.approx(_duration(before), abs=0.05)
