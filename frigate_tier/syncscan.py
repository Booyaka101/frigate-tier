"""What Frigate's media sync would delete, worked out without deleting anything.

This mirrors ``sync_recordings`` and ``sync_previews`` in frigate/util/media.py:
a row whose ``path`` does not exist is deleted from the database, and a file
under the media root with no row pointing at it is unlinked. Both halves stop at
a 50% threshold unless the caller passes ``force``.

Running this before clicking the Maintenance pane's sync button is the whole
point: after a tiering move the answer should be "nothing", and if it is not,
the container mapping is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from peewee import Model

from .mover import PART_SUFFIX
from .safety import PathMap, indent

# frigate/util/media.py: SAFETY_THRESHOLD = 0.5
SAFETY_THRESHOLD = 0.5


@dataclass
class SyncScan:
    media: str
    root: Path
    rows: int = 0
    files: int = 0
    orphan_rows: list[str] = field(default_factory=list)
    orphan_files: list[Path] = field(default_factory=list)
    partials: list[Path] = field(default_factory=list)

    @property
    def row_ratio(self) -> float:
        return len(self.orphan_rows) / self.rows if self.rows else 0.0

    @property
    def file_ratio(self) -> float:
        return len(self.orphan_files) / self.files if self.files else 0.0

    @property
    def would_delete(self) -> int:
        return len(self.orphan_rows) + len(self.orphan_files)

    @property
    def would_abort(self) -> bool:
        """Frigate bails out instead of deleting when either side crosses 50%."""
        return self.row_ratio > SAFETY_THRESHOLD or self.file_ratio > SAFETY_THRESHOLD

    def to_dict(self) -> dict:
        return {
            "media": self.media,
            "root": str(self.root),
            "rows": self.rows,
            "files": self.files,
            "orphan_rows": self.orphan_rows,
            "orphan_files": [str(p) for p in self.orphan_files],
            "stale_partials": [str(p) for p in self.partials],
            "row_ratio": round(self.row_ratio, 4),
            "file_ratio": round(self.file_ratio, 4),
            "would_delete": self.would_delete,
            "would_abort": self.would_abort,
        }


def _recording_files(root: Path) -> list[Path]:
    # os.walk over the whole recordings root, matching sync_recordings.
    return [path for path in root.rglob("*") if path.is_file()]


def _preview_files(root: Path) -> list[Path]:
    # sync_previews only lists CLIPS_DIR/previews/<camera>/*.mp4, one level deep.
    files: list[Path] = []
    for camera in sorted(root.iterdir()):
        if camera.is_dir():
            files.extend(sorted(p for p in camera.glob("*.mp4") if p.is_file()))
    return files


_LISTERS = {"recordings": _recording_files, "previews": _preview_files}


def scan(model: type[Model], media: str, root: Path, path_map: PathMap) -> SyncScan:
    """Compare every row of ``model`` against every file under ``root``."""
    result = SyncScan(media=media, root=root)

    stored = {row.path for row in model.select(model.path).iterator()}
    result.rows = len(stored)
    for db_path in sorted(stored):
        if not path_map.from_db(db_path).exists():
            result.orphan_rows.append(db_path)

    if not root.is_dir():
        return result

    for local in _LISTERS[media](root):
        if local.name.endswith(PART_SUFFIX):
            result.partials.append(local)
            continue
        result.files += 1
        if path_map.to_db(local) not in stored:
            result.orphan_files.append(local)

    return result


def _tally(label: str, count: int, ratio: float) -> str:
    return f"  {label:<50}{count:>6}  ({ratio * 100:.1f}%)"


def render(scans: list[SyncScan]) -> list[str]:
    lines: list[str] = []
    for scan_result in scans:
        if lines:
            lines.append("")
        lines.append(
            f"{scan_result.media}: {scan_result.rows} rows, "
            f"{scan_result.files} files under {scan_result.root}"
        )
        lines.append(
            _tally(
                "rows whose file is missing (sync deletes the row):",
                len(scan_result.orphan_rows),
                scan_result.row_ratio,
            )
        )
        lines.append(
            _tally(
                "files with no row (sync unlinks the file):",
                len(scan_result.orphan_files),
                scan_result.file_ratio,
            )
        )
        if scan_result.partials:
            lines.append(
                f"  {'leftover frigate-tier .part files:':<50}"
                f"{len(scan_result.partials):>6}"
            )
        if scan_result.would_abort:
            lines.append(
                indent(
                    "Over the 50% threshold, so a normal media sync would abort "
                    "rather than delete. That usually means a wrong path, not "
                    "genuinely missing media."
                )
            )

    total = sum(s.would_delete for s in scans)
    lines.append("")
    lines.append(
        "media sync would delete nothing"
        if total == 0
        else f"media sync would delete {total} items"
    )
    return lines
