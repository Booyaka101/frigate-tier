"""Per-segment behaviour: interruption, collisions, pruning, transactions."""

from __future__ import annotations

import sqlite3
import time

import pytest
from conftest import digests, missing_files

from frigate_tier import db as ftdb
from frigate_tier import mover
from frigate_tier.db import Recordings, close_database, open_database
from frigate_tier.mover import (
    STATUS_COLLISION,
    STATUS_MISSING,
    DatabaseBusy,
    relocate,
    scan,
    select_rows,
)
from frigate_tier.safety import PathMap


def _candidates(tree, limit=None):
    return scan(
        select_rows(
            Recordings,
            str(tree.hot),
            before=time.time() - 3 * 86400,
            limit=limit,
        ),
        PathMap(),
    )


def _move_until_interrupted(database, tree, stop_after):
    """Relocate, raising KeyboardInterrupt after the nth file lands but before
    its row is committed. Returns the results seen up to that point."""
    seen = []

    def interrupt(result):
        seen.append(result)
        if len(seen) == stop_after:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        relocate(
            database,
            Recordings,
            _candidates(tree),
            tree.hot,
            tree.cold,
            PathMap(),
            on_copied=interrupt,
        )
    return seen


def test_interrupt_after_copy_leaves_no_row_pointing_at_a_missing_file(tree):
    database = open_database(tree.db_path)
    seen = _move_until_interrupted(database, tree, 3)
    close_database()

    assert missing_files(tree.db_path) == []
    assert len(seen) == 3
    # the two committed segments are on the cold tier, the interrupted one is not
    assert seen[2].destination is not None
    assert not seen[2].destination.exists()
    assert seen[2].candidate.local_path.exists()


def test_an_interrupted_run_resumes(tree):
    database = open_database(tree.db_path)
    _move_until_interrupted(database, tree, 5)

    report = relocate(
        database, Recordings, _candidates(tree), tree.hot, tree.cold, PathMap()
    )
    close_database()

    assert report.failures == 0
    assert report.moved == 36  # 40 minus the 4 that committed before the interrupt
    assert missing_files(tree.db_path) == []


def test_a_destination_collision_skips_one_segment_and_continues(tree):
    database = open_database(tree.db_path)
    candidates = _candidates(tree)
    blocked = candidates[0]
    destination = tree.cold / blocked.local_path.relative_to(tree.hot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"something else entirely")

    report = relocate(database, Recordings, candidates, tree.hot, tree.cold, PathMap())
    close_database()

    assert report.failures == 1
    assert report.by_status(STATUS_COLLISION)[0].candidate.row_id == blocked.row_id
    assert report.moved == len(candidates) - 1
    assert blocked.local_path.exists()
    assert destination.read_bytes() == b"something else entirely"
    assert missing_files(tree.db_path) == []


def test_a_unique_constraint_collision_leaves_the_source_alone(tree):
    database = open_database(tree.db_path)
    candidates = _candidates(tree)
    blocked = candidates[0]
    taken = str(tree.cold / blocked.local_path.relative_to(tree.hot))

    other = (
        Recordings.select()
        .where(Recordings.id != blocked.row_id)
        .order_by(Recordings.start_time.desc())
        .first()
    )
    Recordings.update(path=taken).where(Recordings.id == other.id).execute()

    report = relocate(
        database, Recordings, candidates[:1], tree.hot, tree.cold, PathMap()
    )
    close_database()

    assert report.moved == 0
    assert report.by_status(STATUS_COLLISION)
    assert blocked.local_path.exists()
    with sqlite3.connect(tree.db_path) as conn:
        still = conn.execute(
            "select path from recordings where id = ?", (blocked.row_id,)
        ).fetchone()[0]
    assert still == blocked.db_path


def test_a_missing_source_is_skipped_and_never_deleted(tree):
    database = open_database(tree.db_path)
    candidates = _candidates(tree)
    candidates[0].exists = False

    report = relocate(database, Recordings, candidates, tree.hot, tree.cold, PathMap())
    close_database()

    assert report.missing == 1
    assert report.failures == 0
    assert report.by_status(STATUS_MISSING)[0].candidate.row_id == candidates[0].row_id
    with sqlite3.connect(tree.db_path) as conn:
        unchanged = conn.execute(
            "select path from recordings where id = ?", (candidates[0].row_id,)
        ).fetchone()[0]
    assert unchanged == candidates[0].db_path


def test_empty_directories_are_pruned_only_when_fully_drained(tree):
    database = open_database(tree.db_path)
    candidates = _candidates(tree)
    half = [c for c in candidates if c.camera == "driveway"]

    report = relocate(database, Recordings, half, tree.hot, tree.cold, PathMap())
    close_database()

    assert report.moved == len(half)
    for candidate in half:
        assert not candidate.local_path.parent.exists()
        # the hour directory still holds the other camera
        assert candidate.local_path.parent.parent.exists()
    assert tree.hot.exists()


def test_the_whole_hot_tree_drains_to_the_day_directory(tree):
    database = open_database(tree.db_path)
    report = relocate(
        database, Recordings, _candidates(tree), tree.hot, tree.cold, PathMap()
    )
    close_database()

    assert report.moved == 40
    remaining = sorted(p.name for p in tree.hot.iterdir())
    assert len(remaining) == 1  # only today's recordings survive


def test_bytes_survive_the_round_trip(tree):
    original = digests(tree.hot)
    database = open_database(tree.db_path)
    relocate(database, Recordings, _candidates(tree), tree.hot, tree.cold, PathMap())

    back = scan(
        select_rows(Recordings, str(tree.cold)),
        PathMap(),
    )
    relocate(database, Recordings, back, tree.cold, tree.hot, PathMap())
    close_database()

    assert digests(tree.hot) == original


def test_prune_stops_at_the_root(tmp_path):
    root = tmp_path / "root"
    leaf = root / "a" / "b" / "c"
    leaf.mkdir(parents=True)
    pruned = mover.prune_empty_dirs([leaf], root)
    assert pruned == [leaf, leaf.parent, leaf.parent.parent]
    assert root.exists()


def test_prune_never_climbs_outside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    assert mover.prune_empty_dirs([outside], root) == []
    assert outside.exists()


def test_select_rows_ignores_paths_outside_the_root(tree):
    open_database(tree.db_path)
    everything = list(select_rows(Recordings, str(tree.hot)))
    Recordings.update(path="/somewhere/else/00.00.mp4").where(
        Recordings.id == everything[0].id
    ).execute()
    after = list(select_rows(Recordings, str(tree.hot)))
    close_database()
    assert len(after) == len(everything) - 1


def test_an_unwritable_destination_costs_one_segment_not_the_run(tree, tmp_path):
    database = open_database(tree.db_path)
    candidates = _candidates(tree)
    blocked = candidates[0]
    # a plain file where the destination's day directory needs to be
    (tree.cold / blocked.local_path.relative_to(tree.hot).parts[0]).write_bytes(b"x")

    report = relocate(database, Recordings, candidates, tree.hot, tree.cold, PathMap())
    close_database()

    assert report.failures > 0
    assert report.moved == len(candidates) - report.failures
    assert blocked.local_path.exists()
    assert missing_files(tree.db_path) == []


def test_a_locked_database_fails_loudly(tree, monkeypatch):
    """Frigate mid-write should produce a message, not a peewee traceback."""
    monkeypatch.setitem(ftdb.PRAGMAS, "busy_timeout", 300)
    blocker = sqlite3.connect(tree.db_path, isolation_level=None)
    blocker.execute("pragma busy_timeout = 300")
    blocker.execute("begin exclusive")
    try:
        database = open_database(tree.db_path)
        with pytest.raises(DatabaseBusy) as caught:
            relocate(
                database, Recordings, _candidates(tree), tree.hot, tree.cold, PathMap()
            )
        assert "busy_timeout" in str(caught.value)
    finally:
        blocker.execute("rollback")
        blocker.close()
        close_database()

    assert missing_files(tree.db_path) == []
