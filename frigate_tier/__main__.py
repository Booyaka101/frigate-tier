"""frigate-tier command line interface."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import NoReturn

import click

from . import __version__
from .db import (
    MEDIA_MODELS,
    FrigateDatabaseError,
    Recordings,
    close_database,
    model_for,
    open_database,
)
from .mover import DatabaseBusy, relocate, scan, select_rows
from .report import (
    VerifyProblem,
    candidates_payload,
    failure_lines,
    missing_note,
    move_payload,
    move_summary,
    plan_table,
    verify_lines,
)
from .safety import PathMap, SafetyRefusal, audit_roots

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 3

# Findings that only matter when something is being written into the cold tier.
_COLD_WRITE_CODES = {"unverified-container-path", "cold-under-media-root"}

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 1}

# Frigate stores round(bytes / 2**20, 2), so a byte-identical file reproduces the
# recorded value exactly; the epsilon only absorbs float representation noise.
SIZE_EPSILON_MB = 1e-9


def parse_duration(text: str) -> float:
    match = _DURATION.match(text or "")
    if not match:
        raise click.BadParameter(
            f"{text!r} is not a duration. Use a number with s, m, h, d or w, "
            "for example 3d or 36h."
        )
    return float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]


def _echo_findings(findings, *, refuse: bool) -> None:
    for finding in findings:
        click.echo(
            finding.render() if refuse else f"warning: {finding.message}", err=True
        )


def _fail(message: str) -> NoReturn:
    click.echo(f"error: {message}", err=True)
    raise SystemExit(EXIT_FAILURE)


def _open(db_path: Path):
    try:
        return open_database(db_path)
    except FrigateDatabaseError as exc:
        _fail(str(exc))


def _path_map(prefixes) -> PathMap:
    try:
        return PathMap.parse(prefixes)
    except SafetyRefusal as exc:
        _fail(str(exc))


def _db_root(root: Path, path_map: PathMap) -> str:
    return path_map.to_db(root)


def _no_rows_hint(
    db_root: str,
    model,
    path_map: PathMap,
    flag: str,
    destination: str | None = None,
) -> str:
    """Why a run found nothing: already done, or the wrong root."""
    hint = f"no rows under {db_root}"
    if destination and next(select_rows(model, destination, limit=1), None):
        return f"{hint}; the rows already sit under {destination}, nothing to do"
    sample = model.select(model.path).limit(1).scalar()
    if sample:
        fix = (
            "check the LOCAL side of --db-path-prefix"
            if path_map
            else f"check {flag}, or add --db-path-prefix"
        )
        hint += f". The database stores paths like {sample}, so {fix}."
    return hint


def common_options(func):
    for option in reversed(
        [
            click.option(
                "--db",
                "db_path",
                required=True,
                type=click.Path(path_type=Path),
                help="Path to frigate.db (normally /config/frigate.db).",
            ),
            click.option(
                "--media",
                type=click.Choice(sorted(MEDIA_MODELS)),
                default="recordings",
                show_default=True,
                help="Which table and tree to work on.",
            ),
            click.option(
                "--camera",
                "cameras",
                multiple=True,
                help="Limit to a camera. Repeatable.",
            ),
            click.option(
                "--limit", type=int, default=None, help="Stop after this many segments."
            ),
            click.option(
                "--db-path-prefix",
                "prefixes",
                multiple=True,
                metavar="LOCAL=DATABASE",
                help=(
                    "Rewrite paths written to the database, for use when the tool "
                    "and the Frigate container mount the same storage at different "
                    "paths. Repeatable."
                ),
            ),
            click.option(
                "--json", "as_json", is_flag=True, help="Print a JSON report."
            ),
        ]
    ):
        func = option(func)
    return func


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="frigate-tier")
def cli() -> None:
    """Move old Frigate recordings to slower storage, keeping them playable."""


def relocation_options(*, older_than_required: bool, writes: bool):
    """The --hot/--cold/--older-than trio, plus the commit flags where they apply."""

    options = [
        click.option("--hot", required=True, type=click.Path(path_type=Path)),
        click.option("--cold", required=True, type=click.Path(path_type=Path)),
        click.option(
            "--older-than",
            required=older_than_required,
            default=None,
            help=(
                "Only segments that ended longer ago than this, for example 3d."
                if older_than_required
                else "Only segments that ended longer ago than this. "
                "Default: everything in the cold tier."
            ),
        ),
    ]
    if writes:
        options += [
            click.option(
                "--commit",
                is_flag=True,
                help="Actually move the files. Without it this is a dry run.",
            ),
            click.option(
                "--i-know",
                is_flag=True,
                help="Acknowledge the overridable safety findings and proceed anyway.",
            ),
            click.option("--progress-every", type=int, default=500, show_default=True),
        ]

    def wrap(func):
        for option in reversed(options):
            func = option(func)
        return func

    return wrap


@cli.command()
@common_options
@relocation_options(older_than_required=True, writes=False)
def plan(**options):
    """Show what would move, without touching anything."""
    _relocation(
        **options, commit=False, i_know=False, progress_every=500, restore=False
    )


@cli.command()
@common_options
@relocation_options(older_than_required=True, writes=True)
def move(**options):
    """Move segments from the hot tier to the cold tier."""
    _relocation(**options, restore=False)


@cli.command()
@common_options
@relocation_options(older_than_required=False, writes=True)
def restore(**options):
    """Move segments back from the cold tier to the hot tier."""
    _relocation(**options, restore=True)


def _relocation(
    *,
    db_path,
    media,
    cameras,
    limit,
    prefixes,
    as_json,
    hot,
    cold,
    older_than,
    commit,
    i_know,
    progress_every,
    restore,
):
    path_map = _path_map(prefixes)
    audit = audit_roots(Path(hot), Path(cold), path_map, media)
    findings = [
        f for f in audit.findings if not (restore and f.code in _COLD_WRITE_CODES)
    ]

    if commit:
        blocking = [f for f in findings if not (i_know and f.overridable)]
        if blocking:
            _echo_findings(blocking, refuse=True)
            click.echo(
                "nothing was moved. Re-run with --db-path-prefix or --i-know once the "
                "layout is right.",
                err=True,
            )
            raise SystemExit(EXIT_REFUSED)
    elif findings:
        _echo_findings(findings, refuse=False)

    before = time.time() - parse_duration(older_than) if older_than else None

    source_root, dest_root = (
        (Path(cold), Path(hot)) if restore else (Path(hot), Path(cold))
    )
    database = _open(db_path)
    model = model_for(media)
    db_root = _db_root(source_root, path_map)

    def on_progress(done: int, total: int) -> None:
        click.echo(f"  {done}/{total} segments")

    try:
        candidates = scan(
            select_rows(model, db_root, before=before, cameras=cameras, limit=limit),
            path_map,
        )

        if not candidates:
            hint = _no_rows_hint(
                db_root,
                model,
                path_map,
                "--cold" if restore else "--hot",
                destination=_db_root(dest_root, path_map),
            )
            if as_json:
                click.echo(
                    json.dumps({"segments": 0, "cameras": [], "note": hint}, indent=2)
                )
            else:
                click.echo(plan_table([]))
                click.echo(hint)
            raise SystemExit(EXIT_OK)

        if not commit:
            _print_plan(candidates, as_json, media, restore)
            raise SystemExit(EXIT_OK)

        note = missing_note(candidates)
        if note and not as_json:
            click.echo(note, err=True)

        report = relocate(
            database,
            model,
            candidates,
            source_root,
            dest_root,
            path_map,
            on_progress=None if as_json else on_progress,
            progress_every=max(1, progress_every),
        )
    except DatabaseBusy as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        click.echo(
            "\ninterrupted. Nothing was left half-moved: every committed row has its "
            "file, and every uncommitted segment is still in place.",
            err=True,
        )
        raise SystemExit(EXIT_FAILURE) from None
    finally:
        close_database()

    if as_json:
        payload = move_payload(report)
        payload["direction"] = "restore" if restore else "move"
        payload["media"] = media
        click.echo(json.dumps(payload, indent=2))
    else:
        for line in failure_lines(report):
            click.echo(line, err=True)
        if report.pruned_dirs:
            click.echo(f"pruned {len(report.pruned_dirs)} empty directories")
        click.echo(move_summary(report))
    raise SystemExit(EXIT_FAILURE if report.failures else EXIT_OK)


def _print_plan(candidates, as_json: bool, media: str, restore: bool) -> None:
    if as_json:
        payload = candidates_payload(candidates)
        payload["dry_run"] = True
        payload["direction"] = "restore" if restore else "move"
        payload["media"] = media
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(plan_table(candidates))
    note = missing_note(candidates)
    if note:
        click.echo(note, err=True)
    click.echo("dry run - nothing moved")


@cli.command()
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Path to frigate.db.",
)
@click.option("--cold", required=True, type=click.Path(path_type=Path))
@click.option("--camera", "cameras", multiple=True)
@click.option("--limit", type=int, default=None)
@click.option("--db-path-prefix", "prefixes", multiple=True, metavar="LOCAL=DATABASE")
@click.option(
    "--tolerance-mb",
    type=float,
    default=0.0,
    show_default=True,
    help="Allowed difference between the recorded segment_size and the file.",
)
@click.option("--json", "as_json", is_flag=True)
def verify(db_path, cold, cameras, limit, prefixes, tolerance_mb, as_json):
    """Check every row under the cold root still has its file, at its recorded size."""
    path_map = _path_map(prefixes)
    _open(db_path)  # binds the models to this database
    db_root = _db_root(Path(cold), path_map)

    checked = 0
    hint = ""
    problems: list[VerifyProblem] = []
    try:
        for model in MEDIA_MODELS.values():
            for row in select_rows(model, db_root, cameras=cameras, limit=limit):
                checked += 1
                local = path_map.from_db(row.path)
                try:
                    size = local.stat().st_size
                except OSError:
                    problems.append(
                        VerifyProblem(row.path, local, "missing", f"no file at {local}")
                    )
                    continue
                if model is not Recordings or not row.segment_size:
                    continue
                actual_mb = round(size / (1 << 20), 2)
                allowed = max(tolerance_mb, SIZE_EPSILON_MB)
                if abs(actual_mb - float(row.segment_size)) > allowed:
                    problems.append(
                        VerifyProblem(
                            row.path,
                            local,
                            "size-mismatch",
                            f"database says {row.segment_size} MB, "
                            f"file is {actual_mb} MB",
                        )
                    )
        if checked == 0:
            hint = _no_rows_hint(db_root, Recordings, path_map, "--cold")
    except DatabaseBusy as exc:
        close_database()
        _fail(str(exc))
    finally:
        close_database()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "cold": str(cold),
                    "db_root": db_root,
                    "checked": checked,
                    "note": hint,
                    "problems": [
                        {
                            "db_path": p.db_path,
                            "local_path": str(p.local_path),
                            "kind": p.kind,
                            "detail": p.detail,
                        }
                        for p in problems
                    ],
                },
                indent=2,
            )
        )
    else:
        for line in verify_lines(problems):
            click.echo(line, err=True)
        if checked == 0:
            click.echo(f"nothing to verify: {hint}")
        else:
            click.echo(
                f"checked {checked} rows under {db_root}, {len(problems)} problems"
            )
    raise SystemExit(EXIT_FAILURE if problems else EXIT_OK)


def main() -> None:
    """Console entry point: turn the expected failures into one-line messages."""
    try:
        cli.main()
    except SystemExit:
        raise
    except SafetyRefusal as exc:
        click.echo(str(exc), err=True)
        sys.exit(EXIT_REFUSED)
    except FrigateDatabaseError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_FAILURE)
    except DatabaseBusy as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_FAILURE)
    except OSError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_FAILURE)
    except KeyboardInterrupt:
        click.echo("aborted", err=True)
        sys.exit(EXIT_FAILURE)


if __name__ == "__main__":
    main()
