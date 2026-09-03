"""Path mapping and the refusal rules."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click
import pytest

from frigate_tier.__main__ import parse_duration
from frigate_tier.report import format_ts, human_bytes, plan_table
from frigate_tier.safety import PathMap, SafetyRefusal, audit_roots

HOT = Path("/media/frigate/recordings")
NO_MAP = PathMap()


def codes(hot, cold, path_map=NO_MAP, media="recordings"):
    audit = audit_roots(Path(hot), Path(cold), path_map, media)
    return [f.code for f in audit.findings]


def test_a_cold_root_under_the_hot_root_is_refused_outright():
    audit = audit_roots(HOT, HOT / "archive", PathMap(), "recordings")
    finding = next(f for f in audit.findings if f.code == "cold-under-hot")
    assert not finding.overridable
    with pytest.raises(SafetyRefusal):
        audit.enforce(i_know=True)


def test_a_cold_root_under_the_media_root_can_be_overridden():
    audit = audit_roots(HOT, Path("/media/frigate/archive"), PathMap(), "recordings")
    assert "cold-under-media-root" in [f.code for f in audit.findings]
    assert all(f.overridable for f in audit.errors)
    audit.enforce(i_know=True)


def test_the_same_root_twice_is_refused():
    assert codes(HOT, HOT) == ["same-root"]


def test_a_hot_root_under_the_cold_root_is_refused():
    assert "hot-under-cold" in codes(HOT, Path("/media/frigate"))


def test_a_missing_container_mapping_is_overridable():
    assert codes(HOT, Path("/mnt/nas/recordings")) == ["unverified-container-path"]


def test_a_mapping_that_misses_the_cold_root_is_refused():
    path_map = PathMap.parse(["/mnt/other=/media/archive"])
    assert codes(HOT, Path("/mnt/nas/recordings"), path_map) == ["prefix-misses-cold"]


def test_a_mapping_back_into_the_frigate_recordings_root_is_refused():
    path_map = PathMap.parse(["/mnt/nas/recordings=/media/frigate/recordings/archive"])
    assert codes(HOT, Path("/mnt/nas/recordings"), path_map) == ["db-cold-under-db-hot"]


def test_a_good_layout_has_nothing_to_say():
    path_map = PathMap.parse(["/mnt/nas/recordings=/media/archive/recordings"])
    assert codes(HOT, Path("/mnt/nas/recordings"), path_map) == []


def test_path_map_round_trips():
    path_map = PathMap.parse(["/mnt/nas/recordings=/media/archive/recordings"])
    local = Path("/mnt/nas/recordings/2026-08-01/02/driveway/11.00.mp4")
    stored = path_map.to_db(local)
    assert stored == "/media/archive/recordings/2026-08-01/02/driveway/11.00.mp4"
    assert path_map.from_db(stored) == local


def test_path_map_uses_the_longest_matching_prefix():
    path_map = PathMap.parse(
        ["/mnt/nas=/media/nas", "/mnt/nas/recordings=/media/archive/recordings"]
    )
    assert path_map.to_db(Path("/mnt/nas/recordings/a.mp4")).startswith(
        "/media/archive/recordings"
    )
    assert path_map.to_db(Path("/mnt/nas/other/a.mp4")).startswith("/media/nas")


def test_path_map_leaves_unmapped_paths_alone():
    path_map = PathMap.parse(["/mnt/nas=/media/nas"])
    assert path_map.from_db("/media/frigate/recordings/a.mp4") == Path(
        "/media/frigate/recordings/a.mp4"
    )


@pytest.mark.parametrize("spec", ["no-equals", "=/media/archive", "/mnt/nas=", ""])
def test_a_malformed_prefix_is_rejected(spec):
    with pytest.raises(SafetyRefusal):
        PathMap.parse([spec])


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("3d", 259200), ("36h", 129600), ("90m", 5400), ("2w", 1209600), ("45", 45)],
)
def test_durations_parse(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["soon", "3 days", "-1d", "", "d"])
def test_bad_durations_are_rejected(text):
    with pytest.raises(click.BadParameter):
        parse_duration(text)


def test_human_bytes_matches_frigates_binary_megabytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1 << 20) == "1.0 MB"
    assert human_bytes(41_017_802_752) == "38.2 GB"


def test_format_ts_is_utc():
    epoch = dt.datetime(2026, 8, 1, 16, 0, tzinfo=dt.UTC).timestamp()
    assert format_ts(epoch) == "2026-08-01 16:00Z"


def test_the_empty_plan_still_prints_a_table():
    assert plan_table([]).splitlines() == [
        "camera        segments   size      oldest                newest",
        "total                0   0 B",
    ]
