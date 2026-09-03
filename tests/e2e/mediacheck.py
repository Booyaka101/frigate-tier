"""Prove no video was lost or corrupted, at the codec level and not just the bytes.

sha256 answers "are these the same bytes". This also asks ffmpeg to decode every
frame of every file, which is what catches a truncated moov atom, a short write
or a half-flushed copy that still happens to hash consistently against itself.

    python mediacheck.py snapshot <root>... > before.json
    python mediacheck.py compare before.json after.json
    python mediacheck.py decode <root>...
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKERS = 8


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def media_files(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(sorted(p for p in root.rglob("*.mp4") if p.is_file()))
    return found


def ffprobe(path: Path, *entries: str, count_frames: bool = False) -> dict:
    """Raw ffprobe JSON for one file. The single place this tool shells out."""
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0"]
    if count_frames:
        command.append("-count_frames")
    for entry in entries:
        command += ["-show_entries", entry]
    command += ["-of", "json", str(path)]
    return json.loads(subprocess.run(command, check=True, capture_output=True).stdout)


def duration(path: Path) -> float:
    return float(ffprobe(path, "format=duration")["format"]["duration"])


def probe(path: Path) -> dict:
    """Frame count and duration, read by decoding rather than trusting the header."""
    parsed = ffprobe(
        path,
        "stream=nb_read_frames,width,height",
        "format=duration",
        count_frames=True,
    )
    stream = parsed["streams"][0]
    return {
        "frames": int(stream["nb_read_frames"]),
        "width": stream["width"],
        "height": stream["height"],
        "duration": round(float(parsed["format"]["duration"]), 3),
    }


def decode(path: Path) -> str:
    """Full decode to null. Any complaint on stderr is a corrupt file."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
    )
    if out.returncode != 0 or out.stderr.strip():
        return out.stderr.decode(errors="replace").strip() or f"exit {out.returncode}"
    return ""


def _entry(path: Path) -> tuple[str, dict]:
    record = {"sha256": sha256(path), "bytes": path.stat().st_size}
    record.update(probe(path))
    return path.name, record


def snapshot(roots: list[Path]) -> dict:
    """Map every segment by filename, so a move between roots is not a difference."""
    files = media_files(roots)
    entries: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for name, record in pool.map(_entry, files):
            key = name
            suffix = 1
            while key in entries:  # two cameras can share a filename
                key = f"{name}#{suffix}"
                suffix += 1
            entries[key] = record
    return entries


def _keyed_by_content(entries: dict) -> dict[str, dict]:
    return {record["sha256"]: record for record in entries.values()}


def compare(before: dict, after: dict) -> list[str]:
    problems: list[str] = []
    left, right = _keyed_by_content(before), _keyed_by_content(after)

    for digest in left.keys() - right.keys():
        problems.append(f"LOST: {digest[:16]} present before, gone after")
    for digest in right.keys() - left.keys():
        problems.append(f"NEW: {digest[:16]} appeared after, was not there before")
    for digest in left.keys() & right.keys():
        for field in ("bytes", "frames", "duration", "width", "height"):
            if left[digest][field] != right[digest][field]:
                problems.append(
                    f"CHANGED: {digest[:16]} {field} "
                    f"{left[digest][field]} -> {right[digest][field]}"
                )
    if len(before) != len(after):
        problems.append(f"COUNT: {len(before)} files before, {len(after)} after")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]

    if command == "snapshot":
        json.dump(snapshot([Path(p) for p in rest]), sys.stdout, indent=2)
        return 0

    if command == "compare":
        with open(rest[0]) as handle:
            before = json.load(handle)
        with open(rest[1]) as handle:
            after = json.load(handle)
        problems = compare(before, after)
        for line in problems:
            print(line)
        print(
            f"{len(before)} segments before, {len(after)} after, "
            f"{len(problems)} problems"
        )
        return 1 if problems else 0

    if command == "decode":
        files = media_files([Path(p) for p in rest])
        bad = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for path, error in zip(files, pool.map(decode, files), strict=True):
                if error:
                    bad += 1
                    print(f"CORRUPT: {path}: {error}")
        print(f"decoded {len(files)} segments, {bad} corrupt")
        return 1 if bad else 0

    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
