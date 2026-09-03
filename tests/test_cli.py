"""End to end runs of plan, move, verify and restore against real media."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from conftest import digests, missing_files, paths_under

from frigate_tier.__main__ import cli

OLDER_THAN = "3d"


def base_args(tree, *, media="recordings"):
    hot = tree.hot if media == "recordings" else tree.preview_hot
    cold = tree.cold if media == "recordings" else tree.preview_cold
    return [
        "--db",
        str(tree.db_path),
        "--media",
        media,
        "--hot",
        str(hot),
        "--cold",
        str(cold),
    ]


def stale(tree, media="recordings"):
    """The rows plan should pick up: everything that ended before the cutoff."""
    cutoff = time.time() - 3 * 86400
    records = tree.recordings if media == "recordings" else tree.previews
    return [r for r in records if r["end_time"] < cutoff]


def move_everything_stale(tree, runner):
    """The state the verify tests all start from: one committed move."""
    result = runner.invoke(
        cli,
        ["move", *base_args(tree), "--older-than", OLDER_THAN, "--commit", "--i-know"],
    )
    assert result.exit_code == 0, result.output
    return result


def test_plan_matches_the_filesystem(tree, runner):
    result = runner.invoke(
        cli, ["plan", *base_args(tree), "--older-than", OLDER_THAN, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    expected = stale(tree)
    assert payload["segments"] == len(expected)
    assert payload["bytes"] == sum(r["bytes"] for r in expected)
    assert payload["dry_run"] is True

    on_disk = {}
    for record in expected:
        on_disk.setdefault(record["camera"], []).append(record["path"].stat().st_size)
    for camera in payload["cameras"]:
        assert camera["segments"] == len(on_disk[camera["camera"]])
        assert camera["bytes"] == sum(on_disk[camera["camera"]])


def test_plan_skips_segments_newer_than_the_cutoff(tree, runner):
    result = runner.invoke(
        cli, ["plan", *base_args(tree), "--older-than", OLDER_THAN, "--json"]
    )
    payload = json.loads(result.stdout)
    assert payload["segments"] == 40
    assert len(tree.recordings) == 60
    for record in tree.recordings:
        if record not in stale(tree):
            assert record["path"].exists()


def test_plan_prints_the_table(tree, runner):
    result = runner.invoke(cli, ["plan", *base_args(tree), "--older-than", OLDER_THAN])
    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == (
        "camera        segments   size      oldest                newest"
    )
    assert lines[1].startswith("driveway")
    assert lines[3].startswith("total")
    assert lines[-1] == "dry run - nothing moved"


def test_plan_changes_nothing(tree, runner):
    before = digests(tree.hot)
    runner.invoke(cli, ["plan", *base_args(tree), "--older-than", OLDER_THAN])
    assert digests(tree.hot) == before
    assert not any(tree.cold.rglob("*"))


def test_move_rewrites_paths_and_keeps_bytes(tree, runner):
    before = digests(tree.hot)
    moved = {
        r["path"].relative_to(tree.hot).as_posix(): r["bytes"] for r in stale(tree)
    }

    result = runner.invoke(
        cli,
        ["move", *base_args(tree), "--older-than", OLDER_THAN, "--commit", "--i-know"],
    )
    assert result.exit_code == 0, result.output
    assert f"moved {len(moved)} segments" in result.output
    assert "0 failures" in result.output
    assert f"{len(moved)} rows updated" in result.output

    cold_rows = paths_under(tree.db_path, str(tree.cold))
    assert len(cold_rows) == len(moved)
    assert all(Path(p).exists() for p in cold_rows)
    assert not missing_files(tree.db_path)

    after_cold = digests(tree.cold)
    for relative, digest in before.items():
        if relative in moved:
            assert after_cold[relative] == digest
            assert not (tree.hot / relative).exists()
        else:
            assert (tree.hot / relative).exists()


def test_verify_passes_after_a_move(tree, runner):
    move_everything_stale(tree, runner)
    result = runner.invoke(
        cli, ["verify", "--db", str(tree.db_path), "--cold", str(tree.cold)]
    )
    assert result.exit_code == 0, result.output
    assert "0 problems" in result.output


def test_verify_catches_a_truncated_file(tree, runner):
    move_everything_stale(tree, runner)
    victim = next(p for p in tree.cold.rglob("*.mp4"))
    victim.write_bytes(b"")

    result = runner.invoke(
        cli, ["verify", "--db", str(tree.db_path), "--cold", str(tree.cold), "--json"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert [p["kind"] for p in payload["problems"]] == ["size-mismatch"]


def test_verify_catches_a_deleted_file(tree, runner):
    move_everything_stale(tree, runner)
    next(iter(tree.cold.rglob("*.mp4"))).unlink()

    result = runner.invoke(
        cli, ["verify", "--db", str(tree.db_path), "--cold", str(tree.cold)]
    )
    assert result.exit_code == 1
    assert "missing:" in result.output


def test_restore_returns_the_tree_to_its_original_state(tree, runner):
    before_tree = digests(tree.hot)
    before_rows = sorted((r["id"], r["path"]) for r in _rows(tree.db_path))

    move_everything_stale(tree, runner)
    result = runner.invoke(cli, ["restore", *base_args(tree), "--commit", "--i-know"])
    assert result.exit_code == 0, result.output

    assert digests(tree.hot) == before_tree
    assert sorted((r["id"], r["path"]) for r in _rows(tree.db_path)) == before_rows
    assert not any(p.is_file() for p in tree.cold.rglob("*"))


def _rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return [
            {"id": row[0], "path": row[1]}
            for row in conn.execute("select id, path from recordings")
        ]


def test_container_path_prefix_round_trip(tree, runner):
    mapping = f"{tree.cold}=/media/archive/recordings"
    result = runner.invoke(
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
    assert result.exit_code == 0, result.output

    stored = paths_under(tree.db_path, "/media/archive/recordings")
    assert len(stored) == 40
    assert all(path.startswith("/media/archive/recordings/") for path in stored)

    verified = runner.invoke(
        cli,
        [
            "verify",
            "--db",
            str(tree.db_path),
            "--cold",
            str(tree.cold),
            "--db-path-prefix",
            mapping,
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert "checked 40 rows under /media/archive/recordings" in verified.output

    restored = runner.invoke(
        cli,
        ["restore", *base_args(tree), "--db-path-prefix", mapping, "--commit"],
    )
    assert restored.exit_code == 0, restored.output
    assert not paths_under(tree.db_path, "/media/archive/recordings")
    assert not missing_files(tree.db_path)


def test_previews_move_and_come_back(tree, runner):
    args = base_args(tree, media="previews")
    result = runner.invoke(
        cli, ["move", *args, "--older-than", OLDER_THAN, "--commit", "--i-know"]
    )
    assert result.exit_code == 0, result.output
    assert "moved 4 segments" in result.output

    cold_rows = paths_under(tree.db_path, str(tree.preview_cold), table="previews")
    assert len(cold_rows) == 4
    assert all(Path(p).exists() for p in cold_rows)

    verified = runner.invoke(
        cli, ["verify", "--db", str(tree.db_path), "--cold", str(tree.preview_cold)]
    )
    assert verified.exit_code == 0, verified.output

    restored = runner.invoke(cli, ["restore", *args, "--commit", "--i-know"])
    assert restored.exit_code == 0, restored.output
    assert not missing_files(tree.db_path)


def test_move_reports_and_keeps_a_row_whose_file_is_gone(tree, runner):
    victim = stale(tree)[0]["path"]
    victim.unlink()

    result = runner.invoke(
        cli,
        [
            "move",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--commit",
            "--i-know",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["missing"] == 1
    assert payload["moved"] == 39
    assert payload["failures"] == 0

    still_there = paths_under(tree.db_path, str(tree.hot))
    assert str(victim) in still_there


def test_move_refuses_a_cold_path_under_the_hot_root(tree, runner):
    result = runner.invoke(
        cli,
        [
            "move",
            "--db",
            str(tree.db_path),
            "--hot",
            str(tree.hot),
            "--cold",
            str(tree.hot / "archive"),
            "--older-than",
            OLDER_THAN,
            "--commit",
            "--i-know",
        ],
    )
    assert result.exit_code == 3
    assert "REFUSING" in result.output
    assert "media sync" in result.output
    assert not (tree.hot / "archive").exists()
    assert not missing_files(tree.db_path)


def test_move_refuses_without_a_container_mapping(tree, runner):
    result = runner.invoke(
        cli, ["move", *base_args(tree), "--older-than", OLDER_THAN, "--commit"]
    )
    assert result.exit_code == 3
    assert "--db-path-prefix" in result.output
    assert not any(tree.cold.rglob("*"))


def test_plan_warns_instead_of_refusing(tree, runner):
    result = runner.invoke(cli, ["plan", *base_args(tree), "--older-than", OLDER_THAN])
    assert result.exit_code == 0
    assert "warning:" in result.output
    assert "dry run - nothing moved" in result.output


def test_missing_database_is_a_message_not_a_traceback(tmp_path, runner):
    result = runner.invoke(
        cli,
        [
            "plan",
            "--db",
            str(tmp_path / "nope.db"),
            "--hot",
            str(tmp_path / "hot"),
            "--cold",
            str(tmp_path / "cold"),
            "--older-than",
            "1d",
        ],
    )
    assert result.exit_code == 1
    assert "database not found" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_bad_duration_is_a_message_not_a_traceback(tree, runner):
    result = runner.invoke(cli, ["plan", *base_args(tree), "--older-than", "soon"])
    assert result.exit_code == 2
    assert "is not a duration" in result.output


def test_a_bad_path_prefix_is_a_message(tree, runner):
    result = runner.invoke(
        cli,
        [
            "plan",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--db-path-prefix",
            "no-equals-sign",
        ],
    )
    assert result.exit_code == 1
    assert "bad --db-path-prefix" in result.output


def test_empty_result_is_reported_not_crashed(tree, runner):
    result = runner.invoke(
        cli, ["plan", *base_args(tree), "--older-than", "3650d", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["segments"] == 0
    assert "no rows under" in payload["note"]


def test_camera_and_limit_narrow_the_selection(tree, runner):
    result = runner.invoke(
        cli,
        [
            "plan",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--camera",
            "driveway",
            "--limit",
            "5",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["segments"] == 5
    assert [c["camera"] for c in payload["cameras"]] == ["driveway"]


def test_a_second_move_finds_nothing_left_to_do(tree, runner):
    move_everything_stale(tree, runner)
    again = runner.invoke(
        cli,
        ["move", *base_args(tree), "--older-than", OLDER_THAN, "--commit", "--i-know"],
    )
    assert again.exit_code == 0, again.output
    assert "no rows under" in again.stdout
