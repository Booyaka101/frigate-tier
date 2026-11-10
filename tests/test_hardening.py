"""Regression checks from the safety and read-only command review."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import click
import pytest
from peewee import OperationalError

from frigate_tier import __main__ as ftcli
from frigate_tier import db as ftdb
from frigate_tier.__main__ import cli, parse_duration, parse_size
from frigate_tier.locking import writer_lock
from frigate_tier.mover import PART_SUFFIX, relocate, scan, select_rows
from frigate_tier.safety import PathMap, SafetyRefusal, audit_roots


@pytest.mark.parametrize("command", ["plan", "move", "restore", "verify"])
@pytest.mark.parametrize("limit", ["0", "-1"])
def test_nonpositive_limits_are_rejected_before_opening_database(
    runner, command, limit
):
    args = [command, "--db", "missing.db", "--cold", "cold", "--limit", limit]
    if command != "verify":
        args += ["--hot", "hot", "--older-than", "3d"]
    result = runner.invoke(cli, args)
    assert result.exit_code == 2
    assert "--limit" in result.output


def test_read_only_database_preserves_journal_mode_and_rejects_updates(tmp_path):
    name = "database with # and spaces.db" if os.name == "nt" else "database #?.db"
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.execute("create table recordings (path text)")
        conn.execute("create table previews (path text)")
    before = path.read_bytes()
    try:
        database = ftdb.open_database(path, read_only=True)
        assert database.execute_sql("pragma journal_mode").fetchone()[0] == "delete"
        with pytest.raises(OperationalError, match="readonly"):
            database.execute_sql("delete from recordings")
    finally:
        ftdb.close_database()
    assert path.read_bytes() == before
    assert not path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize(
    "command", ["plan", "move", "restore", "verify", "sync-report"]
)
def test_inspection_commands_do_not_require_write_access(
    tree, runner, monkeypatch, command
):
    access = os.access
    monkeypatch.setattr(
        ftdb.os, "access", lambda path, mode: mode != os.W_OK and access(path, mode)
    )
    args = [command, "--db", str(tree.db_path), "--json"]
    if command == "sync-report":
        args += ["--recordings-root", str(tree.hot)]
    else:
        args += ["--cold", str(tree.cold)]
        if command != "verify":
            args += ["--hot", str(tree.hot), "--older-than", "3d"]
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.stdout), dict)


def test_dot_dot_cannot_hide_overlapping_roots(tmp_path):
    hot = tmp_path / "hot"
    cold = tmp_path / "elsewhere" / ".." / "hot" / "archive"
    audit = audit_roots(hot, cold, PathMap(), "recordings")
    assert any(f.code == "cold-under-hot" and not f.overridable for f in audit.errors)


def test_root_database_mapping_round_trips():
    mapping = PathMap.parse(["/mnt/archive=/"])
    local = Path("/mnt/archive/camera/clip.mp4")
    assert mapping.to_db(local) == "/camera/clip.mp4"
    assert mapping.from_db(mapping.to_db(local)) == local


def symlink(link: Path, target: Path, *, directory: bool = False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows requires permission to create symbolic links")
        raise


def test_symlink_alias_cannot_hide_overlapping_roots(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    alias = tmp_path / "alias"
    symlink(alias, hot, directory=True)
    audit = audit_roots(hot, alias / "archive", PathMap(), "recordings")
    assert any(f.code == "cold-under-hot" and not f.overridable for f in audit.errors)


@pytest.mark.parametrize("location", ["source", "destination", "parent", "partial"])
def test_segment_symlinks_cannot_escape_roots_or_alias_the_source(
    tree, tmp_path, location
):
    database = ftdb.open_database(tree.db_path)
    candidate = scan(select_rows(ftdb.Recordings, str(tree.hot), limit=1), PathMap())[0]
    source = candidate.local_path
    destination = tree.cold / source.relative_to(tree.hot)
    destination.parent.mkdir(parents=True)
    original = source.read_bytes()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(original)
    if location == "source":
        source.unlink()
        symlink(source, outside)
    elif location == "destination":
        symlink(destination, source)
    elif location == "partial":
        symlink(destination.with_name(destination.name + PART_SUFFIX), outside)
    else:
        destination.parent.rmdir()
        symlink(destination.parent, source.parent, directory=True)
    report = relocate(
        database, ftdb.Recordings, [candidate], tree.hot, tree.cold, PathMap()
    )
    assert report.failures == 1
    assert report.rows_updated == 0
    assert ftdb.Recordings.get_by_id(candidate.row_id).path == candidate.db_path
    assert source.read_bytes() == original
    assert outside.read_bytes() == original


@pytest.mark.parametrize("parser", [parse_duration, parse_size])
def test_numeric_overflow_is_a_command_line_error(parser):
    with pytest.raises(click.BadParameter, match="finite"):
        parser("9" * 400)


@pytest.mark.parametrize(
    "specs",
    [
        ["/mnt/a=/media/archive", "/mnt/b=/media/archive"],
        ["/mnt/a=/media/archive", "/mnt/a=/media/other"],
    ],
)
def test_ambiguous_path_mappings_are_rejected(specs):
    with pytest.raises(SafetyRefusal, match="ambiguous"):
        PathMap.parse(specs)


def test_failed_source_cleanup_is_visible_and_returns_failure(
    tree, runner, monkeypatch
):
    source = tree.recordings[0]["path"]
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("source removal denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    result = runner.invoke(
        cli,
        [
            "move",
            "--db",
            str(tree.db_path),
            "--hot",
            str(tree.hot),
            "--cold",
            str(tree.cold),
            "--older-than",
            "3d",
            "--commit",
            "--i-know",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "source could not be removed" in result.stderr
    assert "1 failures" in result.stdout
    assert source.exists()
    assert (tree.cold / source.relative_to(tree.hot)).exists()


def test_verify_rejects_a_directory_in_place_of_a_preview(tree, runner):
    preview = tree.previews[0]["path"]
    preview.unlink()
    preview.mkdir()
    result = runner.invoke(
        cli,
        [
            "verify",
            "--db",
            str(tree.db_path),
            "--cold",
            str(tree.preview_hot),
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert [p["kind"] for p in json.loads(result.stdout)["problems"]] == ["not-a-file"]


def test_writer_lock_blocks_move_but_allows_read_only_plan(tree, runner):
    args = [
        "--db",
        str(tree.db_path),
        "--hot",
        str(tree.hot),
        "--cold",
        str(tree.cold),
        "--older-than",
        "3d",
    ]
    with writer_lock(tree.db_path):
        blocked = runner.invoke(cli, ["move", *args, "--commit", "--i-know"])
        planned = runner.invoke(cli, ["plan", *args, "--json"])
    assert blocked.exit_code == 1
    assert "another frigate-tier writer" in blocked.output
    assert planned.exit_code == 0, planned.output
    assert json.loads(planned.stdout)["segments"] == 40


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_verify_tolerance_must_be_finite_and_nonnegative(runner, value):
    result = runner.invoke(
        cli, ["verify", "--db", "missing.db", "--cold", "cold", "--tolerance-mb", value]
    )
    assert result.exit_code == 2
    assert "finite and nonnegative" in result.output


def test_unreachable_free_target_does_not_claim_the_disk_is_already_clear(
    tree, runner, monkeypatch
):
    monkeypatch.setattr(ftcli, "free_bytes", lambda root: 0)
    result = runner.invoke(
        cli,
        [
            "plan",
            "--db",
            str(tree.db_path),
            "--hot",
            str(tree.hot),
            "--cold",
            str(tree.cold),
            "--older-than",
            "3650d",
            "--until-free",
            "1G",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "already free" not in result.stdout
    assert "no rows under" in result.stdout


def test_shadowed_destination_mapping_cannot_update_the_row(tree, tmp_path):
    database = ftdb.open_database(tree.db_path)
    candidate = scan(select_rows(ftdb.Recordings, str(tree.hot), limit=1), PathMap())[0]
    relative = candidate.local_path.relative_to(tree.hot)
    mapping = PathMap.parse(
        [
            f"{tree.cold}=/media/archive",
            f"{tmp_path / 'other'}=/media/archive/{relative.parts[0]}",
        ]
    )
    report = relocate(
        database, ftdb.Recordings, [candidate], tree.hot, tree.cold, mapping
    )
    assert report.failures == 1
    assert "does not round-trip" in report.results[0].detail
    assert report.rows_updated == 0
    assert candidate.local_path.exists()
    assert not (tree.cold / relative).exists()
