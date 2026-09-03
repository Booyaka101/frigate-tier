"""Human tables and the --json payloads."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .mover import STATUS_MISSING, Candidate, RelocationReport, SegmentResult

# Frigate reports storage in binary megabytes but labels them MB (see
# frigate/record/maintainer.py); this matches so the two agree.
_UNITS = (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10))

CAMERA_WIDTH = 14
COUNT_WIDTH = 8
SIZE_WIDTH = 10
TIME_WIDTH = 22


def human_bytes(size: int | float) -> str:
    for label, factor in _UNITS:
        if size >= factor:
            return f"{size / factor:.1f} {label}"
    return f"{int(size)} B"


def format_ts(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%d %H:%MZ")


@dataclass
class CameraSummary:
    camera: str
    segments: int
    size: int
    oldest: float
    newest: float


def summarise(candidates: Sequence[Candidate]) -> list[CameraSummary]:
    by_camera: dict[str, CameraSummary] = {}
    for candidate in candidates:
        summary = by_camera.get(candidate.camera)
        if summary is None:
            by_camera[candidate.camera] = CameraSummary(
                candidate.camera,
                1,
                candidate.size,
                candidate.start_time,
                candidate.end_time,
            )
            continue
        summary.segments += 1
        summary.size += candidate.size
        summary.oldest = min(summary.oldest, candidate.start_time)
        summary.newest = max(summary.newest, candidate.end_time)
    return sorted(by_camera.values(), key=lambda s: s.camera)


def _row(
    camera: str, count: str, size: str, oldest: str, newest: str, width: int
) -> str:
    line = (
        f"{camera:<{width}}{count:>{COUNT_WIDTH}}   "
        f"{size:<{SIZE_WIDTH}}{oldest:<{TIME_WIDTH}}{newest}"
    )
    return line.rstrip()


def plan_table(candidates: Sequence[Candidate]) -> str:
    summaries = summarise(candidates)
    width = max(CAMERA_WIDTH, max((len(s.camera) + 2 for s in summaries), default=0))
    lines = [_row("camera", "segments", "size", "oldest", "newest", width)]
    for summary in summaries:
        lines.append(
            _row(
                summary.camera,
                str(summary.segments),
                human_bytes(summary.size),
                format_ts(summary.oldest),
                format_ts(summary.newest),
                width,
            )
        )
    total_size = sum(s.size for s in summaries)
    total_count = sum(s.segments for s in summaries)
    lines.append(
        _row("total", str(total_count), human_bytes(total_size), "", "", width)
    )
    return "\n".join(lines)


def missing_note(candidates: Sequence[Candidate]) -> str:
    missing = [c for c in candidates if not c.exists]
    if not missing:
        return ""
    shown = "\n".join(f"  {c.db_path}" for c in missing[:10])
    more = f"\n  ... and {len(missing) - 10} more" if len(missing) > 10 else ""
    return (
        f"{len(missing)} row(s) point at a file that is already gone. "
        f"They will be reported and skipped, never deleted:\n{shown}{more}"
    )


def move_summary(report: RelocationReport) -> str:
    return (
        f"moved {report.moved} segments, {human_bytes(report.bytes_moved)}, "
        f"{report.failures} failures, {report.rows_updated} rows updated"
    )


def failure_lines(report: RelocationReport, limit: int = 20) -> list[str]:
    problems = [r for r in report.results if r.failed or r.status == STATUS_MISSING]
    lines = [
        f"{r.status}: {r.candidate.db_path} - {r.detail}" for r in problems[:limit]
    ]
    if len(problems) > limit:
        lines.append(f"... and {len(problems) - limit} more")
    return lines


@dataclass
class VerifyProblem:
    db_path: str
    local_path: Path
    kind: str
    detail: str


def verify_lines(problems: Sequence[VerifyProblem], limit: int = 20) -> list[str]:
    lines = [f"{p.kind}: {p.db_path} - {p.detail}" for p in problems[:limit]]
    if len(problems) > limit:
        lines.append(f"... and {len(problems) - limit} more")
    return lines


def candidates_payload(candidates: Sequence[Candidate]) -> dict:
    summaries = summarise(candidates)
    return {
        "segments": len(candidates),
        "bytes": sum(c.size for c in candidates),
        "missing_files": sum(1 for c in candidates if not c.exists),
        "cameras": [
            {
                "camera": s.camera,
                "segments": s.segments,
                "bytes": s.size,
                "oldest": format_ts(s.oldest),
                "newest": format_ts(s.newest),
            }
            for s in summaries
        ],
    }


def results_payload(results: Iterable[SegmentResult]) -> list[dict]:
    return [
        {
            "id": r.candidate.row_id,
            "camera": r.candidate.camera,
            "status": r.status,
            "source": str(r.candidate.local_path),
            "destination": str(r.destination) if r.destination else None,
            "db_path": r.candidate.db_path,
            "new_db_path": r.new_db_path,
            "bytes": r.candidate.size,
            "detail": r.detail,
        }
        for r in results
    ]


def move_payload(report: RelocationReport) -> dict:
    return {
        "moved": report.moved,
        "bytes": report.bytes_moved,
        "failures": report.failures,
        "missing": report.missing,
        "rows_updated": report.rows_updated,
        "pruned_dirs": [str(p) for p in report.pruned_dirs],
        "orphaned_sources": [str(p) for p in report.orphaned_sources],
        "segments": results_payload(report.results),
    }
