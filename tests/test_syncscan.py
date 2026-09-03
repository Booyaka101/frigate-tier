"""sync-report: what Frigate's media sync would delete, before you click it."""

from __future__ import annotations

import json

from conftest import missing_files
from test_cli import OLDER_THAN, base_args

from frigate_tier import syncscan
from frigate_tier.__main__ import cli
from frigate_tier.db import Previews, close_database, open_database
from frigate_tier.mover import PART_SUFFIX
from frigate_tier.safety import PathMap


def report_args(tree, *prefixes):
    args = [
        "sync-report",
        "--db",
        str(tree.db_path),
        "--recordings-root",
        str(tree.hot),
        "--previews-root",
        str(tree.preview_hot),
    ]
    for prefix in prefixes:
        args += ["--db-path-prefix", prefix]
    return args


def test_an_untouched_frigate_tree_is_clean(tree, runner):
    result = runner.invoke(cli, report_args(tree))
    assert result.exit_code == 0, result.output
    assert "media sync would delete nothing" in result.stdout
    assert "60 rows, 60 files" in result.stdout


def test_a_correctly_mapped_move_stays_clean(tree, runner):
    mapping = f"{tree.cold}=/media/archive/recordings"
    moved = runner.invoke(
        cli,
        [
            "move",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--db-path-prefix",
            mapping,
            "--commit",
        ],
    )
    assert moved.exit_code == 0, moved.output

    result = runner.invoke(cli, report_args(tree, mapping))
    assert result.exit_code == 0, result.output
    assert "media sync would delete nothing" in result.stdout
    assert "60 rows, 20 files" in result.stdout


def test_a_move_frigate_cannot_see_is_reported_as_row_deletion(tree, runner):
    """The whole reason the container mapping is a refusal, shown as a number."""
    mapping = f"{tree.cold}=/media/archive/recordings"
    runner.invoke(
        cli,
        [
            "move",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--db-path-prefix",
            mapping,
            "--commit",
        ],
    )

    # no --db-path-prefix here, standing in for a Frigate that cannot see the NAS
    result = runner.invoke(cli, report_args(tree), catch_exceptions=False)
    assert result.exit_code == 1
    payload = json.loads(runner.invoke(cli, [*report_args(tree), "--json"]).stdout)
    recordings = payload["scans"][0]
    assert len(recordings["orphan_rows"]) == 40
    assert recordings["orphan_files"] == []
    assert recordings["would_abort"] is True
    assert "would abort" in result.stdout


def test_an_orphaned_file_is_reported(tree, runner):
    stray = tree.hot / "2026-08-29" / "02" / "driveway" / "99.99.mp4"
    stray.write_bytes(b"not in the database")

    result = runner.invoke(cli, [*report_args(tree), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    recordings = payload["scans"][0]
    assert recordings["orphan_files"] == [str(stray)]
    assert recordings["orphan_rows"] == []
    assert recordings["would_abort"] is False
    assert payload["would_delete"] == 1


def test_a_leftover_part_file_is_counted_separately(tree, runner):
    part = tree.hot / "2026-08-29" / "02" / "driveway" / f"00.00.mp4{PART_SUFFIX}"
    part.write_bytes(b"interrupted")

    result = runner.invoke(cli, [*report_args(tree), "--json"])
    payload = json.loads(result.stdout)
    recordings = payload["scans"][0]
    assert recordings["stale_partials"] == [str(part)]
    assert recordings["orphan_files"] == []
    assert result.exit_code == 0


def test_the_scan_reads_previews_one_level_deep_like_frigate_does(tree):
    open_database(tree.db_path)
    scan = syncscan.scan(Previews, "previews", tree.preview_hot, PathMap())
    close_database()

    assert scan.rows == 6
    assert scan.files == 6
    assert scan.orphan_rows == []
    assert scan.orphan_files == []


def test_the_scan_changes_nothing_on_disk(tree, runner):
    before = sorted(p for p in tree.hot.rglob("*") if p.is_file())
    stray = tree.hot / "2026-08-29" / "02" / "driveway" / "99.99.mp4"
    stray.write_bytes(b"orphan")

    runner.invoke(cli, report_args(tree))

    assert stray.exists()
    assert sorted(p for p in tree.hot.rglob("*") if p.is_file()) == sorted(
        [*before, stray]
    )
    assert missing_files(tree.db_path) == []


def test_the_threshold_matches_frigates_own_constant():
    assert syncscan.SAFETY_THRESHOLD == 0.5
