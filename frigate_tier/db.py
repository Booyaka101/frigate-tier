"""Peewee models mirroring Frigate's own tables.

The models are copied field for field from frigate/models.py so that this tool
reads and writes exactly the columns Frigate reads and writes. Nothing here ever
creates, alters or migrates a table in a real Frigate database.
"""

from __future__ import annotations

import os
from pathlib import Path

from peewee import (
    CharField,
    DatabaseError,
    DateTimeField,
    FloatField,
    IntegerField,
    Model,
    OperationalError,
    SqliteDatabase,
)
from playhouse.sqlite_ext import JSONField

# WAL is what Frigate itself uses; the busy timeout is what lets us wait out a
# recording maintainer that is mid-insert instead of failing immediately.
PRAGMAS = {"journal_mode": "wal", "busy_timeout": 15000}

database = SqliteDatabase(None)


class FrigateDatabaseError(Exception):
    """The database could not be opened, or is not a Frigate database."""


class _BaseModel(Model):
    class Meta:
        database = database


class Recordings(_BaseModel):
    id = CharField(null=False, primary_key=True, max_length=30)
    camera = CharField(index=True, max_length=20)
    path = CharField(unique=True)
    start_time = DateTimeField()
    end_time = DateTimeField()
    duration = FloatField()
    motion = IntegerField(null=True)
    objects = IntegerField(null=True)
    dBFS = IntegerField(null=True)
    segment_size = FloatField(default=0)  # stored as MB, rounded to 2 decimals
    regions = IntegerField(null=True)
    motion_heatmap = JSONField(null=True)

    class Meta:
        table_name = "recordings"


class Previews(_BaseModel):
    id = CharField(null=False, primary_key=True, max_length=30)
    camera = CharField(index=True, max_length=20)
    path = CharField(unique=True)
    start_time = DateTimeField()
    end_time = DateTimeField()
    duration = FloatField()

    class Meta:
        table_name = "previews"


MEDIA_MODELS = {"recordings": Recordings, "previews": Previews}


def model_for(media: str) -> type[Model]:
    try:
        return MEDIA_MODELS[media]
    except KeyError:
        raise FrigateDatabaseError(
            f"unknown media type {media!r}, expected one of {sorted(MEDIA_MODELS)}"
        ) from None


def open_database(db_path: str | os.PathLike[str]) -> SqliteDatabase:
    """Open a Frigate database read-write and check it looks like one."""
    path = Path(db_path)
    if not path.exists():
        raise FrigateDatabaseError(f"database not found: {path}")
    if path.is_dir():
        raise FrigateDatabaseError(f"database path is a directory: {path}")
    if not os.access(path, os.W_OK):
        raise FrigateDatabaseError(
            f"database is not writable: {path} "
            "(frigate-tier has to UPDATE the path column)"
        )

    if not database.is_closed():
        database.close()
    database.init(str(path), pragmas=PRAGMAS)
    try:
        database.connect(reuse_if_open=True)
    except (OperationalError, DatabaseError) as exc:
        raise FrigateDatabaseError(f"could not open {path}: {exc}") from exc

    missing = [name for name in MEDIA_MODELS if not database.table_exists(name)]
    if missing:
        database.close()
        raise FrigateDatabaseError(
            f"{path} has no {', '.join(missing)} table - is this a Frigate database?"
        )
    return database


def close_database() -> None:
    if not database.is_closed():
        database.close()
