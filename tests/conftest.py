from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from frigate_tier import db as ftdb
from frigate_tier import fixture as ftfixture


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


def missing_files(db_path: Path) -> list[str]:
    out = []
    with sqlite3.connect(db_path) as conn:
        for table in ("recordings", "previews"):
            for (path,) in conn.execute(f"select path from {table}"):
                if not Path(path).exists():
                    out.append(path)
    return out
