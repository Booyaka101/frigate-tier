"""Process-scoped writer exclusion, separate from Frigate's SQLite locks."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .mover import DatabaseBusy


@contextmanager
def writer_lock(db_path: Path) -> Iterator[None]:
    """Serialize this tool's writers without blocking Frigate or read-only checks.

    Keep the sidecar in place: unlinking a locked file lets another process lock a
    different inode at the same path. The OS releases the lock even after SIGKILL.
    """
    path = db_path.resolve()
    lock_path = path.with_name(path.name + ".frigate-tier.lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise DatabaseBusy(
                    f"another frigate-tier writer is using {path}; try again later"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DatabaseBusy(
                    f"another frigate-tier writer is using {path}; try again later"
                ) from exc
        yield
