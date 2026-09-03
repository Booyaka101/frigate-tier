"""Every row resolves to a real file, and playback still concatenates.

Rebuilds the ffmpeg concat playlist exactly as frigate/api/media.py does, one
`file '<path>'` line per row in start_time order, and runs it. A pass means
Frigate's own playback path works over a tree that spans both tiers.

    python checkdb.py <frigate.db> [LOCAL=DATABASE ...]
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from mediacheck import duration


def resolve(stored: str, prefixes: list[tuple[str, str]]) -> Path:
    for local, remote in prefixes:
        if stored.startswith(remote.rstrip("/") + "/"):
            return Path(local.rstrip("/") + stored[len(remote.rstrip("/")) :])
    return Path(stored)


def concat(clips: list[Path], target: Path) -> float:
    playlist = target.with_suffix(".txt")
    with open(playlist, "w") as handle:
        for clip in clips:
            handle.write(f"file '{clip.as_posix()}'\n")
    subprocess.run(
        [
            "ffmpeg",
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
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return duration(target)


def main(argv: list[str]) -> int:
    db_path = Path(argv[0])
    prefixes = [tuple(spec.split("=", 1)) for spec in argv[1:]]
    conn = sqlite3.connect(db_path)

    problems = 0
    tiers: dict[str, int] = {}
    for table in ("recordings", "previews"):
        rows = list(conn.execute(f"select path from {table}"))
        for (stored,) in rows:
            local = resolve(stored, prefixes)
            if not local.exists():
                print(f"MISSING: {table} row points at {stored} -> {local}")
                problems += 1
            tiers[stored.split("/")[2] if "/" in stored else "?"] = (
                tiers.get(stored.split("/")[2] if "/" in stored else "?", 0) + 1
            )
        print(f"{table}: {len(rows)} rows")
    print("rows by second path component:", dict(sorted(tiers.items())))

    cameras = [row[0] for row in conn.execute("select distinct camera from recordings")]
    with tempfile.TemporaryDirectory() as tmp:
        for camera in sorted(cameras):
            clips = [
                resolve(stored, prefixes)
                for (stored,) in conn.execute(
                    "select path from recordings where camera = ? order by start_time",
                    (camera,),
                )
            ]
            expected = sum(duration(clip) for clip in clips)
            actual = concat(clips, Path(tmp) / f"{camera}.mp4")
            spread = abs(actual - expected)
            status = "ok" if spread < 0.5 else "MISMATCH"
            print(
                f"{camera}: {len(clips)} clips concat to {actual:.2f}s "
                f"(sum of parts {expected:.2f}s) {status}"
            )
            if status != "ok":
                problems += 1

    print(f"{problems} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
