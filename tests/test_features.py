"""Free-space targets, bandwidth limiting, both media in one run, part sweeping."""

from __future__ import annotations

import json
import os
import time

import click
import pytest
from conftest import assert_refused, digests, missing_files, move_json, paths_under
from test_cli import OLDER_THAN, base_args, stale

from frigate_tier.__main__ import cli, parse_size
from frigate_tier.db import Recordings, close_database, open_database
from frigate_tier.mover import (
    PART_SUFFIX,
    RateLimiter,
    destination_dirs,
    free_bytes,
    relocate,
    same_filesystem,
    scan,
    select_rows,
    sweep_stale_parts,
    total_bytes,
    trim_to_free_target,
)
from frigate_tier.safety import PathMap


def _candidates(tree):
    return scan(
        select_rows(Recordings, str(tree.hot), before=time.time() - 3 * 86400),
        PathMap(),
    )


def all_args(tree, preview_cold=None, preview_hot=None):
    """--media all needs four roots; the tests vary the preview pair."""
    args = [
        "--db",
        str(tree.db_path),
        "--media",
        "all",
        "--hot",
        str(tree.hot),
        "--cold",
        str(tree.cold),
    ]
    if preview_hot is not False:
        args += ["--preview-hot", str(preview_hot or tree.preview_hot)]
    if preview_cold is not False:
        args += ["--preview-cold", str(preview_cold or tree.preview_cold)]
    return args


# --- size parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500G", ("bytes", 500 * 2**30)),
        ("1.5T", ("bytes", 1.5 * 2**40)),
        ("50M", ("bytes", 50 * 2**20)),
        ("1024", ("bytes", 1024)),
        ("2KB", ("bytes", 2048)),
        ("20%", ("percent", 20.0)),
    ],
)
def test_sizes_parse(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["big", "10X", "", "-5G", "120%"])
def test_bad_sizes_are_rejected(text):
    with pytest.raises(click.BadParameter):
        parse_size(text)


# --- --until-free -----------------------------------------------------------


def test_trim_takes_the_oldest_prefix_that_reaches_the_target(tree):
    open_database(tree.db_path)
    candidates = _candidates(tree)
    close_database()

    wanted = sum(c.size for c in candidates[:5])
    taken = trim_to_free_target(candidates, available=1000, target=1000 + wanted)

    assert taken == candidates[:5]
    assert sum(c.size for c in taken) >= wanted


@pytest.mark.parametrize(
    ("available", "target", "expected"),
    [
        (1000, 1000, "none"),  # already there
        (1000, 999, "none"),  # more than there
        (0, 1 << 60, "all"),  # unreachable, so shed everything eligible
    ],
)
def test_trim_edges(tree, available, target, expected):
    open_database(tree.db_path)
    candidates = _candidates(tree)
    close_database()
    taken = trim_to_free_target(candidates, available=available, target=target)
    assert taken == ([] if expected == "none" else candidates)


def test_until_free_moves_nothing_when_the_disk_is_already_clear(tree, runner):
    available = free_bytes(tree.hot)
    result = runner.invoke(
        cli,
        [
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--until-free",
            str(max(1, available // 2)),
        ][0:0]
        + [
            "plan",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--until-free",
            str(max(1, available // 2)),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "already free" in result.stdout
    assert "total                0" in result.stdout


def test_until_free_as_a_percentage_is_read_against_the_filesystem(tree, runner):
    # 100% of capacity can never be free, so every eligible segment is selected.
    result = runner.invoke(
        cli,
        ["plan", *base_args(tree), "--older-than", OLDER_THAN, "--until-free", "100%"],
    )
    assert result.exit_code == 0, result.output
    assert total_bytes(tree.hot) > 0
    assert "total               40" in result.stdout


def test_until_free_warns_when_both_tiers_share_a_filesystem(tree, runner):
    assert same_filesystem(tree.hot, tree.cold)
    result = runner.invoke(
        cli,
        ["plan", *base_args(tree), "--older-than", OLDER_THAN, "--until-free", "100%"],
    )
    assert "same filesystem" in result.stderr


# --- --min-free-on-cold -----------------------------------------------------


def _floor_args(tree, floor="900T"):
    """A layout with no other outstanding finding, so only the floor can refuse."""
    return [
        *base_args(tree),
        "--db-path-prefix",
        f"{tree.cold}=/media/archive/recordings",
        "--older-than",
        OLDER_THAN,
        "--min-free-on-cold",
        floor,
        "--commit",
    ]


def test_a_cold_tier_that_cannot_hold_the_payload_is_refused(tree, runner):
    result = runner.invoke(cli, ["move", *_floor_args(tree)])

    assert result.exit_code == 3
    assert "REFUSING" in result.output
    assert "free and this move would write" in result.output
    assert not any(tree.cold.rglob("*.mp4"))
    assert not missing_files(tree.db_path)


def test_the_cold_floor_is_a_warning_once_acknowledged(tree, runner):
    result = runner.invoke(cli, ["move", *_floor_args(tree), "--i-know"])

    assert result.exit_code == 0, result.output
    assert "warning:" in result.stderr
    assert "free and this move would write" in result.stderr
    assert len(paths_under(tree.db_path, "/media/archive/recordings")) == 40
    mapped = PathMap.parse([f"{tree.cold}=/media/archive/recordings"])
    assert not missing_files(tree.db_path, mapped)


# --- --bandwidth-limit ------------------------------------------------------


def test_the_rate_limiter_paces_a_run(tree):
    database = open_database(tree.db_path)
    candidates = _candidates(tree)[:8]
    payload = sum(c.size for c in candidates)
    limit = payload / 0.5  # half a second of copying, whatever the fixture weighs

    started = time.monotonic()
    report = relocate(
        database,
        Recordings,
        candidates,
        tree.hot,
        tree.cold,
        PathMap(),
        limiter=RateLimiter(limit),
    )
    elapsed = time.monotonic() - started
    close_database()

    assert report.moved == len(candidates)
    assert elapsed >= 0.4
    assert missing_files(tree.db_path) == []


def test_a_zero_limit_is_treated_as_no_limit():
    limiter = RateLimiter(0)
    started = time.monotonic()
    for _ in range(50):
        limiter.account(1 << 20)
    assert time.monotonic() - started < 0.5


def test_bandwidth_limit_rejects_a_percentage(tree, runner):
    result = runner.invoke(
        cli,
        [
            "move",
            *base_args(tree),
            "--older-than",
            OLDER_THAN,
            "--bandwidth-limit",
            "10%",
            "--commit",
            "--i-know",
        ],
    )
    assert result.exit_code == 2
    assert "takes a size, not a percentage" in result.output


# --- --media all ------------------------------------------------------------


def test_media_all_moves_both_trees_in_one_run(tree, runner):
    before = digests(tree.hot) | digests(tree.preview_hot)

    result = runner.invoke(
        cli,
        ["move", *all_args(tree), "--older-than", OLDER_THAN, "--commit", "--i-know"],
    )
    assert result.exit_code == 0, result.output
    assert "== recordings ==" in result.stdout
    assert "== previews ==" in result.stdout
    assert "moved 40 segments" in result.stdout
    assert "moved 4 segments" in result.stdout

    assert len(paths_under(tree.db_path, str(tree.cold))) == 40
    assert len(paths_under(tree.db_path, str(tree.preview_cold), table="previews")) == 4
    assert not missing_files(tree.db_path)

    restored = runner.invoke(cli, ["restore", *all_args(tree), "--commit", "--i-know"])
    assert restored.exit_code == 0, restored.output
    assert digests(tree.hot) | digests(tree.preview_hot) == before
    assert not missing_files(tree.db_path)


def test_media_all_json_reports_one_entry_per_pass(tree, runner):
    result = runner.invoke(
        cli, ["plan", *all_args(tree), "--older-than", OLDER_THAN, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["media"] == "all"
    assert [p["media"] for p in payload["passes"]] == ["recordings", "previews"]
    assert [p["segments"] for p in payload["passes"]] == [40, 4]


def test_media_all_needs_the_preview_roots(tree, runner):
    args = all_args(tree, preview_hot=False, preview_cold=False)
    result = runner.invoke(cli, ["plan", *args, "--older-than", OLDER_THAN])
    assert result.exit_code == 2
    assert "--preview-hot" in result.output


def test_media_all_refuses_before_moving_either_tree(tree, runner):
    """A bad previews layout must stop the recordings pass too."""
    args = all_args(tree, preview_cold=tree.preview_hot / "archive")
    result = runner.invoke(
        cli, ["move", *args, "--older-than", OLDER_THAN, "--commit", "--i-know"]
    )
    assert_refused(result, tree, "the cold tier is inside the hot tier")
    assert len(stale(tree)) == 40


# --- stale .part sweeping ---------------------------------------------------


def test_a_stale_part_file_is_swept_but_a_fresh_one_is_left(tree):
    old = tree.cold / f"2026-08-29{PART_SUFFIX}"
    fresh = tree.cold / f"2026-08-30{PART_SUFFIX}"
    old.write_bytes(b"interrupted")
    fresh.write_bytes(b"in flight")
    os.utime(old, (time.time() - 7200, time.time() - 7200))

    removed = sweep_stale_parts([tree.cold])

    assert removed == [old]
    assert not old.exists()
    assert fresh.exists()


def test_a_move_sweeps_stale_parts_first(tree, runner):
    open_database(tree.db_path)
    victim = _candidates(tree)[0]
    close_database()

    stale_part = tree.cold / victim.local_path.relative_to(tree.hot)
    stale_part = stale_part.with_name(stale_part.name + PART_SUFFIX)
    stale_part.parent.mkdir(parents=True, exist_ok=True)
    stale_part.write_bytes(b"half a segment from a killed run")
    os.utime(stale_part, (time.time() - 7200, time.time() - 7200))

    payload = move_json(
        runner, [*base_args(tree), "--older-than", OLDER_THAN, "--i-know"]
    )
    assert payload["swept_partials"] == [str(stale_part)]
    assert not stale_part.exists()


def test_sweeping_a_missing_directory_is_not_an_error(tmp_path):
    assert sweep_stale_parts([tmp_path / "nope"]) == []


def test_the_sweep_only_visits_directories_this_run_will_write_to(tree):
    open_database(tree.db_path)
    candidates = _candidates(tree)
    close_database()

    dirs = destination_dirs(candidates, tree.hot, tree.cold)

    assert dirs
    assert all(tree.cold in d.parents or d == tree.cold for d in dirs)
    # one directory per day/hour/camera, not one per segment
    assert len(dirs) < len(candidates)


def test_a_part_outside_this_run_is_left_alone(tree, runner):
    elsewhere = tree.cold / "2099-01-01" / "00" / "attic"
    elsewhere.mkdir(parents=True)
    orphan = elsewhere / f"00.00.mp4{PART_SUFFIX}"
    orphan.write_bytes(b"from some other run")
    os.utime(orphan, (time.time() - 7200, time.time() - 7200))

    move_json(runner, [*base_args(tree), "--older-than", OLDER_THAN, "--i-know"])

    assert orphan.exists()
