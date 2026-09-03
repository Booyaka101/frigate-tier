"""Build a throwaway Frigate media tree with real mp4 segments.

Development and test use only: the CLI never imports this, and this is the only
module in the package that creates database tables. Requires ffmpeg on PATH.

    python -m frigate_tier.fixture /tmp/frigate-demo
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import shutil
import string
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import db as ftdb

CAMERAS = ("driveway", "front_door")
DAY_OFFSETS = (5, 4, 0)  # the newest day is today, so --older-than has work to do
SEGMENTS_PER_DAY = 10
SEGMENT_SECONDS = 10


@dataclass
class Fixture:
    root: Path
    db_path: Path
    hot: Path
    preview_hot: Path
    cold: Path
    preview_cold: Path
    recordings: list[dict] = field(default_factory=list)
    previews: list[dict] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(r["bytes"] for r in self.recordings)


def _rand_id(start_time: float) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{start_time}-{suffix}"


def _render(ffmpeg: str, target: Path, duration: float, rate: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x480:rate={rate}:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
    )


def ffmpeg_path(candidate: str = "ffmpeg") -> str | None:
    return shutil.which(candidate)


def build(
    root: Path,
    *,
    cameras: tuple[str, ...] = CAMERAS,
    day_offsets: tuple[int, ...] = DAY_OFFSETS,
    per_day: int = SEGMENTS_PER_DAY,
    ffmpeg: str = "ffmpeg",
    workers: int = 8,
) -> Fixture:
    """Write the media tree, then a database whose rows point at it."""
    binary = ffmpeg_path(ffmpeg)
    if binary is None:
        raise RuntimeError(
            f"{ffmpeg} is not on PATH; the fixture generates real mp4 segments"
        )

    root = Path(root)
    fixture = Fixture(
        root=root,
        db_path=root / "config" / "frigate.db",
        hot=root / "media" / "frigate" / "recordings",
        preview_hot=root / "media" / "frigate" / "clips" / "previews",
        cold=root / "mnt" / "nas" / "frigate" / "recordings",
        preview_cold=root / "mnt" / "nas" / "frigate" / "previews",
    )
    fixture.db_path.parent.mkdir(parents=True, exist_ok=True)
    fixture.cold.mkdir(parents=True, exist_ok=True)
    fixture.preview_cold.mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    jobs: list[tuple[Path, float, int]] = []

    for offset in day_offsets:
        base = now - dt.timedelta(days=offset)
        # the newest day sits an hour back so it is always inside --older-than 3d
        base = base - dt.timedelta(hours=1) if offset == 0 else base.replace(hour=2)
        for camera in cameras:
            for index in range(per_day):
                start = base + dt.timedelta(seconds=index * SEGMENT_SECONDS)
                end = start + dt.timedelta(seconds=SEGMENT_SECONDS)
                target = (
                    fixture.hot
                    / start.strftime("%Y-%m-%d")
                    / start.strftime("%H")
                    / camera
                    / f"{start.strftime('%M.%S')}.mp4"
                )
                duration = 2.0 + (index % 3)
                rate = 15 + (index % 2) * 5
                jobs.append((target, duration, rate))
                fixture.recordings.append(
                    {
                        "camera": camera,
                        "path": target,
                        "start_time": start.timestamp(),
                        "end_time": end.timestamp(),
                        "duration": float(SEGMENT_SECONDS),
                        "motion": index * 3,
                        "objects": index % 2,
                        "dBFS": -30 + index,
                        "regions": index % 4,
                    }
                )

            preview_start = base
            preview_end = base + dt.timedelta(seconds=per_day * SEGMENT_SECONDS)
            preview = (
                fixture.preview_hot
                / camera
                / f"{preview_start.timestamp()}-{preview_end.timestamp()}.mp4"
            )
            jobs.append((preview, 2.0, 15))
            fixture.previews.append(
                {
                    "camera": camera,
                    "path": preview,
                    "start_time": preview_start.timestamp(),
                    "end_time": preview_end.timestamp(),
                    "duration": float(per_day * SEGMENT_SECONDS),
                }
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda job: _render(binary, *job), jobs))

    for record in fixture.recordings:
        record["bytes"] = record["path"].stat().st_size
    for record in fixture.previews:
        record["bytes"] = record["path"].stat().st_size

    _write_database(fixture)
    return fixture


def _write_database(fixture: Fixture) -> None:
    database = ftdb.database
    if not database.is_closed():
        database.close()
    database.init(str(fixture.db_path), pragmas=ftdb.PRAGMAS)
    database.connect(reuse_if_open=True)
    database.create_tables([ftdb.Recordings, ftdb.Previews])
    with database.atomic():
        for record in fixture.recordings:
            ftdb.Recordings.create(
                id=_rand_id(record["start_time"]),
                camera=record["camera"],
                path=str(record["path"]),
                start_time=record["start_time"],
                end_time=record["end_time"],
                duration=record["duration"],
                motion=record["motion"],
                objects=record["objects"],
                dBFS=record["dBFS"],
                segment_size=round(record["bytes"] / (1 << 20), 2),
                regions=record["regions"],
                motion_heatmap=None,
            )
        for record in fixture.previews:
            ftdb.Previews.create(
                id=_rand_id(record["start_time"]),
                camera=record["camera"],
                path=str(record["path"]),
                start_time=record["start_time"],
                end_time=record["end_time"],
                duration=record["duration"],
            )
    database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="directory to build the tree in")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.root.exists() and any(args.root.iterdir()):
        parser.error(f"{args.root} is not empty")

    try:
        fixture = build(args.root, ffmpeg=args.ffmpeg)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"error: ffmpeg failed: {exc.stderr.decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1

    summary = {
        "db": str(fixture.db_path),
        "hot": str(fixture.hot),
        "cold": str(fixture.cold),
        "preview_hot": str(fixture.preview_hot),
        "preview_cold": str(fixture.preview_cold),
        "recordings": len(fixture.recordings),
        "previews": len(fixture.previews),
        "bytes": fixture.total_bytes,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
