"""Selecting, copying and re-pointing individual media segments.

One segment is one unit of work and one transaction: copy, verify the bytes that
actually landed, commit the new path, then unlink the source. An interrupted run
leaves the database consistent because nothing is ever deleted before its UPDATE
has committed, and a resumed run simply picks up the rows that are still hot.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from peewee import Database, IntegrityError, Model, OperationalError, fn

from .safety import PathMap, as_prefix

CHUNK = 1 << 20
PART_SUFFIX = ".frigate-tier.part"

STATUS_MOVED = "moved"
STATUS_MISSING = "missing"
STATUS_COLLISION = "collision"
STATUS_VERIFY_FAILED = "verify-failed"
STATUS_OUTSIDE_ROOT = "outside-root"
STATUS_ERROR = "error"
STATUS_ALREADY_THERE = "already-there"

FAILURE_STATUSES = frozenset(
    {STATUS_COLLISION, STATUS_VERIFY_FAILED, STATUS_OUTSIDE_ROOT, STATUS_ERROR}
)


class DatabaseBusy(Exception):
    """The Frigate database stayed locked past busy_timeout."""


@dataclass
class Candidate:
    """A database row plus what the filesystem says about the file it points at."""

    row_id: str
    camera: str
    db_path: str
    local_path: Path
    start_time: float
    end_time: float
    size: int
    exists: bool
    segment_size: float | None = None


@dataclass
class SegmentResult:
    candidate: Candidate
    status: str
    destination: Path | None = None
    new_db_path: str | None = None
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status in FAILURE_STATUSES


@dataclass
class RelocationReport:
    results: list[SegmentResult] = field(default_factory=list)
    rows_updated: int = 0
    bytes_moved: int = 0
    pruned_dirs: list[Path] = field(default_factory=list)
    orphaned_sources: list[Path] = field(default_factory=list)

    @property
    def moved(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_MOVED)

    @property
    def failures(self) -> int:
        return sum(1 for r in self.results if r.failed)

    @property
    def missing(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_MISSING)

    def by_status(self, status: str) -> list[SegmentResult]:
        return [r for r in self.results if r.status == status]


def columns(model: type[Model]) -> list:
    """Only the columns this tool reads, so an older Frigate schema still works."""
    wanted = ("id", "camera", "path", "start_time", "end_time", "segment_size")
    return [getattr(model, name) for name in wanted if hasattr(model, name)]


def select_rows(
    model: type[Model],
    db_root: str,
    *,
    before: float | None = None,
    cameras: Sequence[str] = (),
    limit: int | None = None,
) -> Iterator[Model]:
    """Rows whose stored path sits under ``db_root``, oldest first.

    SUBSTR rather than LIKE because camera names contain underscores, which LIKE
    would treat as single-character wildcards.
    """
    prefix = as_prefix(db_root)
    query = model.select(*columns(model)).where(
        fn.SUBSTR(model.path, 1, len(prefix)) == prefix
    )
    if before is not None:
        query = query.where(model.end_time < before)
    if cameras:
        query = query.where(model.camera.in_(list(cameras)))
    query = query.order_by(model.start_time, model.id)
    if limit:
        query = query.limit(limit)
    try:
        yield from query.iterator()
    except OperationalError as exc:
        raise _busy(exc) from exc


def scan(rows: Iterable[Model], path_map: PathMap) -> list[Candidate]:
    """Turn rows into candidates, stat-ing each file exactly once."""
    candidates: list[Candidate] = []
    for row in rows:
        local = path_map.from_db(row.path)
        try:
            size = local.stat().st_size
            exists = True
        except OSError:
            size = 0
            exists = False
        candidates.append(
            Candidate(
                row_id=row.id,
                camera=row.camera,
                db_path=row.path,
                local_path=local,
                start_time=float(row.start_time),
                end_time=float(row.end_time),
                size=size,
                exists=exists,
                segment_size=getattr(row, "segment_size", None),
            )
        )
    return candidates


def relocate(
    database: Database,
    model: type[Model],
    candidates: Sequence[Candidate],
    source_root: Path,
    dest_root: Path,
    path_map: PathMap,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    progress_every: int = 500,
    on_copied: Callable[[SegmentResult], None] | None = None,
    prune: bool = True,
) -> RelocationReport:
    """Move every candidate from ``source_root`` to ``dest_root``.

    ``on_copied`` runs once the destination file is verified and before the row
    is updated. It is the point at which a run can be interrupted without the
    database ever pointing at a file that is not there.
    """
    report = RelocationReport()
    touched_dirs: set[Path] = set()

    for index, candidate in enumerate(candidates, start=1):
        result = _relocate_one(
            database,
            model,
            candidate,
            source_root,
            dest_root,
            path_map,
            on_copied,
            report,
        )
        report.results.append(result)
        if result.status == STATUS_MOVED:
            touched_dirs.add(candidate.local_path.parent)
        if on_progress and index % progress_every == 0:
            on_progress(index, len(candidates))

    if on_progress and candidates and len(candidates) % progress_every != 0:
        on_progress(len(candidates), len(candidates))

    if prune:
        report.pruned_dirs = prune_empty_dirs(touched_dirs, source_root)
    return report


def _relocate_one(
    database: Database,
    model: type[Model],
    candidate: Candidate,
    source_root: Path,
    dest_root: Path,
    path_map: PathMap,
    on_copied: Callable[[SegmentResult], None] | None,
    report: RelocationReport,
) -> SegmentResult:
    source = candidate.local_path
    if not candidate.exists:
        return SegmentResult(
            candidate,
            STATUS_MISSING,
            detail=f"file is gone, row left untouched: {source}",
        )

    try:
        relative = source.relative_to(source_root)
    except ValueError:
        return SegmentResult(
            candidate,
            STATUS_OUTSIDE_ROOT,
            detail=f"{source} is not under {source_root}",
        )

    destination = dest_root / relative
    new_db_path = path_map.to_db(destination)
    if new_db_path == candidate.db_path:
        return SegmentResult(
            candidate,
            STATUS_ALREADY_THERE,
            destination=destination,
            new_db_path=new_db_path,
            detail="database already points at the destination",
        )

    part = destination.with_name(destination.name + PART_SUFFIX)
    copied_here = False
    try:
        if destination.exists():
            if not _same_bytes(source, destination):
                return SegmentResult(
                    candidate,
                    STATUS_COLLISION,
                    destination=destination,
                    detail=f"{destination} already exists with different contents",
                )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = _copy_and_digest(source, part)
            landed = _digest(part)
            if landed != digest:
                part.unlink(missing_ok=True)
                return SegmentResult(
                    candidate,
                    STATUS_VERIFY_FAILED,
                    destination=destination,
                    detail=(
                        "copy did not match the source after being written to disk "
                        f"({digest.size}/{digest.sha256[:12]} vs "
                        f"{landed.size}/{landed.sha256[:12]}); source left in place"
                    ),
                )
            os.replace(part, destination)
            _fsync_dir(destination.parent)
            copied_here = True

        result = SegmentResult(
            candidate,
            STATUS_MOVED,
            destination=destination,
            new_db_path=new_db_path,
        )
        if on_copied is not None:
            on_copied(result)

        try:
            with database.atomic():
                updated = (
                    model.update(path=new_db_path)
                    .where(
                        (model.id == candidate.row_id)
                        & (model.path == candidate.db_path)
                    )
                    .execute()
                )
        except IntegrityError as exc:
            if copied_here:
                destination.unlink(missing_ok=True)
            return SegmentResult(
                candidate,
                STATUS_COLLISION,
                destination=destination,
                detail=f"another row already claims {new_db_path} ({exc})",
            )
        except OperationalError as exc:
            if copied_here:
                destination.unlink(missing_ok=True)
            raise _busy(exc) from exc

        if updated != 1:
            if copied_here:
                destination.unlink(missing_ok=True)
            return SegmentResult(
                candidate,
                STATUS_ERROR,
                destination=destination,
                detail="row changed underneath us, nothing updated",
            )

        report.rows_updated += updated
        report.bytes_moved += candidate.size

        try:
            source.unlink()
        except OSError as exc:
            report.orphaned_sources.append(source)
            result.detail = f"row updated but the source could not be removed: {exc}"
        return result
    except OSError as exc:
        # A full disk, an unreadable file or a dropped mount should cost one
        # segment, not the rest of the run.
        part.unlink(missing_ok=True)
        if copied_here:
            destination.unlink(missing_ok=True)
        return SegmentResult(
            candidate,
            STATUS_ERROR,
            destination=destination,
            detail=f"{exc}; source left in place",
        )
    except BaseException:
        part.unlink(missing_ok=True)
        if copied_here:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if part.exists():
            part.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Fingerprint:
    size: int
    sha256: str


def _digest(source: Path, sink=None) -> _Fingerprint:
    """Size and sha256 of ``source``, copying into ``sink`` on the way through."""
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            if sink is not None:
                sink.write(chunk)
    return _Fingerprint(size, digest.hexdigest())


def _copy_and_digest(source: Path, destination: Path) -> _Fingerprint:
    with open(destination, "wb") as sink:
        fingerprint = _digest(source, sink)
        sink.flush()
        os.fsync(sink.fileno())
    return fingerprint


def _same_bytes(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return _digest(left) == _digest(right)
    except OSError:
        return False


def _fsync_dir(path: Path) -> None:
    # Directory fsync is a POSIX guarantee; Windows has no equivalent handle.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def prune_empty_dirs(directories: Iterable[Path], root: Path) -> list[Path]:
    """Remove now-empty directories, walking up but never past ``root``."""
    pruned: list[Path] = []
    for directory in sorted(set(directories), key=lambda p: len(p.parts), reverse=True):
        current = directory
        while current != root:
            try:
                current.relative_to(root)
            except ValueError:
                break
            try:
                next(current.iterdir())
                break
            except StopIteration:
                pass
            except OSError:
                break
            try:
                current.rmdir()
            except OSError:
                break
            pruned.append(current)
            current = current.parent
    return pruned


def _busy(exc: OperationalError) -> Exception:
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return DatabaseBusy(
            "the Frigate database stayed locked for longer than busy_timeout "
            "(15s). Frigate is probably mid-write; try again, or run with a "
            "smaller --limit."
        )
    return exc
