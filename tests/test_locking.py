"""Writer locks must exclude other processes and recover after termination."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from frigate_tier.locking import writer_lock

PROBE = """
import sys
from pathlib import Path
from frigate_tier.locking import writer_lock
from frigate_tier.mover import DatabaseBusy
try:
    with writer_lock(Path(sys.argv[1])):
        print('locked', flush=True)
        if len(sys.argv) > 2:
            sys.stdin.read()
except DatabaseBusy as exc:
    print(exc)
    sys.exit(1)
"""


def probe(path: Path):
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_only_one_writer_can_hold_the_database_lock(tmp_path):
    path = tmp_path / "frigate.db"
    with writer_lock(path):
        result = probe(path)
        assert result.returncode == 1, result.stderr
        assert "another frigate-tier writer" in result.stdout
    assert probe(path).returncode == 0


def test_terminated_writer_does_not_leave_a_stale_lock(tmp_path):
    path = tmp_path / "frigate.db"
    child = subprocess.Popen(
        [sys.executable, "-c", PROBE, str(path), "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        assert probe(path).returncode == 1
    finally:
        child.kill()
        child.communicate(timeout=15)
    assert probe(path).returncode == 0


def test_exception_releases_writer_lock(tmp_path):
    path = tmp_path / "frigate.db"
    with pytest.raises(KeyboardInterrupt), writer_lock(path):
        raise KeyboardInterrupt
    assert probe(path).returncode == 0
