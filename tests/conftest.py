from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

# the e2e harness owns the ffprobe helpers; the unit tests borrow them
sys.path.insert(0, str(Path(__file__).parent / "e2e"))

import pytest
from click.testing import CliRunner

from frigate_tier import db as ftdb
from frigate_tier import fixture as ftfixture
from frigate_tier.safety import PathMap


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    binary = ftfixture.ffmpeg_path()
    if binary is None:
        pytest.skip("ffmpeg is not on PATH; the suite runs against real mp4 segments")
    return binary


@pytest.fixture
def tree(tmp_path: Path, ffmpeg: str) -> ftfixture.Fixture:
    """A fresh Frigate media tree with 60 real segments and a matching database."""
    built = ftfixture.build(tmp_path / "root", ffmpeg=ffmpeg)
    yield built
    ftdb.close_database()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def digests(root: Path) -> dict[str, str]:
    """sha256 of every file under root, keyed by its path relative to root."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def rows(db_path: Path, table: str = "recordings") -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"select id, path from {table} order by id").fetchall()


def paths_under(db_path: Path, prefix: str, table: str = "recordings") -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [
            row[0]
            for row in conn.execute(
                f"select path from {table} where substr(path, 1, ?) = ?",
                (len(prefix), prefix),
            )
        ]


def missing_files(db_path: Path, path_map: PathMap | None = None) -> list[str]:
    """Stored paths with no file behind them, resolved through the same map the
    tool would use so container paths are checked where they really live."""
    path_map = path_map or PathMap()
    out = []
    with sqlite3.connect(db_path) as conn:
        for table in ("recordings", "previews"):
            for (path,) in conn.execute(f"select path from {table}"):
                if not path_map.from_db(path).exists():
                    out.append(path)
    return out


def move_json(runner, args, expect=0):
    """Run a committing move and return its JSON report."""
    from frigate_tier.__main__ import cli

    result = runner.invoke(cli, ["move", *args, "--commit", "--json"])
    assert result.exit_code == expect, result.output
    return json.loads(result.stdout)


def assert_refused(result, tree, summary):
    """A refusal that moved nothing and left the database consistent."""
    assert result.exit_code == 3, result.output
    assert f"REFUSING: {summary}" in result.output
    assert not any(tree.cold.rglob("*.mp4"))
    assert missing_files(tree.db_path) == []
