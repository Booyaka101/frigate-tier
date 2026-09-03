"""frigate-tier command line interface."""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import click

from . import __version__, syncscan
from .db import (
    MEDIA_MODELS,
    FrigateDatabaseError,
    Previews,
    Recordings,
    close_database,
    model_for,
    open_database,
)
from .mover import (
    DatabaseBusy,
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
from .report import (
    VerifyProblem,
    candidates_payload,
    failure_lines,
    human_bytes,
    missing_note,
    move_payload,
    move_summary,
    plan_table,
    verify_lines,
)
from .safety import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Finding,
    PathMap,
    SafetyRefusal,
    audit_roots,
    indent,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 3

# Findings that only matter when something is being written into the cold tier.
_COLD_WRITE_CODES = {"unverified-container-path", "cold-under-media-root"}

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 1}

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*(%?)\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

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


def parse_size(text: str) -> tuple[str, float]:
    """``("bytes", n)`` or ``("percent", p)``. Suffixes are binary, like the report."""
    match = _SIZE.match(text or "")
    if not match:
        raise click.BadParameter(
            f"{text!r} is not a size. Use a number with K, M, G or T, or a "
            "percentage, for example 500G or 20%."
        )
    value, unit, percent = float(match.group(1)), match.group(2).upper(), match.group(3)
    if percent:
        if unit or not 0 <= value <= 100:
            raise click.BadParameter(f"{text!r} is not a percentage between 0 and 100")
        return "percent", value
    return "bytes", value * _SIZE_UNITS[unit]


def _size_spec(flag: str, text: str | None) -> tuple[str, float] | None:
    if text is None:
        return None
    try:
        return parse_size(text)
    except click.BadParameter as exc:
        raise click.BadParameter(f"{flag}: {exc.message}") from None


def _bytes_only(flag: str, text: str) -> float:
    kind, value = _size_spec(flag, text)
    if kind != "bytes":
        raise click.BadParameter(f"{flag} takes a size, not a percentage")
    return value


def _resolve_size(spec: tuple[str, float], root: Path) -> int:
    kind, value = spec
    if kind == "bytes":
        return int(value)
    return int(total_bytes(root) * value / 100)


def _echo_findings(findings, *, refuse: bool) -> None:
    for finding in findings:
        click.echo(finding.render(refuse), err=True)


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
                type=click.Choice([*sorted(MEDIA_MODELS), "all"]),
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


def relocation_options(
    *, older_than_required: bool, writes: bool, free_space: bool, bandwidth: bool
):
    """The --hot/--cold/--older-than trio, plus whichever knobs the verb supports."""

    options = [
        click.option("--hot", required=True, type=click.Path(path_type=Path)),
        click.option("--cold", required=True, type=click.Path(path_type=Path)),
        click.option(
            "--preview-hot",
            type=click.Path(path_type=Path),
            default=None,
            help="Preview tree, required by --media all.",
        ),
        click.option(
            "--preview-cold",
            type=click.Path(path_type=Path),
            default=None,
            help="Where preview clips go, required by --media all.",
        ),
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
    if free_space:
        options += [
            click.option(
                "--until-free",
                default=None,
                metavar="SIZE",
                help=(
                    "Stop once the hot filesystem has this much free, oldest "
                    "segments first. Takes 500G or 20%."
                ),
            ),
            click.option(
                "--min-free-on-cold",
                default=None,
                metavar="SIZE",
                help=(
                    "Refuse the move unless the cold tier keeps this much free "
                    "afterwards. Defaults to refusing only if it would not fit."
                ),
            ),
        ]
    if bandwidth:
        options.append(
            click.option(
                "--bandwidth-limit",
                default=None,
                metavar="SIZE",
                help=(
                    "Cap the write rate to the cold tier, for example 50M "
                    "(bytes per second)."
                ),
            )
        )
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
@relocation_options(
    older_than_required=True, writes=False, free_space=True, bandwidth=False
)
def plan(**options):
    """Show what would move, without touching anything."""
    _relocation(
        **options, commit=False, i_know=False, progress_every=500, restore=False
    )


@cli.command()
@common_options
@relocation_options(
    older_than_required=True, writes=True, free_space=True, bandwidth=True
)
def move(**options):
    """Move segments from the hot tier to the cold tier."""
    _relocation(**options, restore=False)


@cli.command()
@common_options
@relocation_options(
    older_than_required=False, writes=True, free_space=False, bandwidth=True
)
def restore(**options):
    """Move segments back from the cold tier to the hot tier."""
    _relocation(**options, restore=True)


@dataclass
class _Pass:
    """One media type with its own pair of roots."""

    media: str
    hot: Path
    cold: Path

    def roots(self, restore: bool) -> tuple[Path, Path]:
        return (self.cold, self.hot) if restore else (self.hot, self.cold)


def _plan_passes(media, hot, cold, preview_hot, preview_cold) -> list[_Pass]:
    if media != "all":
        return [_Pass(media, Path(hot), Path(cold))]
    if not preview_hot or not preview_cold:
        raise click.UsageError(
            "--media all needs --preview-hot and --preview-cold, because preview "
            "clips live under clips/previews and not under the recordings root."
        )
    return [
        _Pass("recordings", Path(hot), Path(cold)),
        _Pass("previews", Path(preview_hot), Path(preview_cold)),
    ]


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
    preview_hot,
    preview_cold,
    older_than,
    commit,
    i_know,
    progress_every,
    restore,
    until_free=None,
    min_free_on_cold=None,
    bandwidth_limit=None,
):
    path_map = _path_map(prefixes)
    passes = _plan_passes(media, hot, cold, preview_hot, preview_cold)

    # Audit every pass before touching anything, so --media all cannot half-run.
    findings = [
        finding
        for job in passes
        for finding in audit_roots(job.hot, job.cold, path_map, job.media).findings
        if not (restore and finding.code in _COLD_WRITE_CODES)
    ]
    if commit:
        blocking = [f for f in findings if not (i_know and f.overridable)]
        if blocking:
            _echo_findings(blocking, refuse=True)
            click.echo("nothing was moved.", err=True)
            raise SystemExit(EXIT_REFUSED)
    elif findings:
        _echo_findings(findings, refuse=False)

    before = time.time() - parse_duration(older_than) if older_than else None
    free_target = _size_spec("--until-free", until_free)
    cold_floor = _size_spec("--min-free-on-cold", min_free_on_cold)
    limiter = (
        RateLimiter(_bytes_only("--bandwidth-limit", bandwidth_limit))
        if bandwidth_limit
        else None
    )

    database = _open(db_path)
    payloads: list[dict] = []
    worst = EXIT_OK
    try:
        for job in passes:
            code, payload = _one_pass(
                database,
                job,
                path_map,
                before=before,
                cameras=cameras,
                limit=limit,
                commit=commit,
                i_know=i_know,
                as_json=as_json,
                restore=restore,
                progress_every=progress_every,
                free_target=free_target,
                cold_floor=cold_floor,
                limiter=limiter,
                labelled=len(passes) > 1,
            )
            payloads.append(payload)
            worst = max(worst, code)
    except DatabaseBusy as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        click.echo("\ninterrupted.", err=True)
        click.echo(
            indent(
                "Nothing was left half-moved: every committed row has its file, and "
                "every uncommitted segment is still in place. Re-run the same command "
                "to pick up where this stopped."
            ),
            err=True,
        )
        raise SystemExit(EXIT_FAILURE) from None
    finally:
        close_database()

    if as_json:
        document = (
            payloads[0] if len(payloads) == 1 else {"media": "all", "passes": payloads}
        )
        click.echo(json.dumps(document, indent=2))
    raise SystemExit(worst)


def _one_pass(
    database,
    job: _Pass,
    path_map: PathMap,
    *,
    before,
    cameras,
    limit,
    commit,
    i_know,
    as_json,
    restore,
    progress_every,
    free_target,
    cold_floor,
    limiter,
    labelled,
) -> tuple[int, dict]:
    """Select, report and maybe move one media type. Returns (exit code, payload)."""
    source_root, dest_root = job.roots(restore)
    model = model_for(job.media)
    db_root = _db_root(source_root, path_map)
    if labelled and not as_json:
        click.echo(f"== {job.media} ==")

    candidates = scan(
        select_rows(model, db_root, before=before, cameras=cameras, limit=limit),
        path_map,
    )

    if free_target is not None:
        if same_filesystem(source_root, dest_root):
            click.echo(
                Finding(
                    SEVERITY_WARNING,
                    "one-filesystem",
                    "both tiers are on the same filesystem.",
                    f"{source_root} and {dest_root} share a device, so moving between "
                    "them frees no space and --until-free can never be satisfied.",
                ).render(False),
                err=True,
            )
        available = free_bytes(source_root)
        candidates = trim_to_free_target(
            candidates, available, _resolve_size(free_target, source_root)
        )
        if not candidates:
            note = (
                f"{human_bytes(available)} already free on {source_root}, "
                "nothing to move"
            )
            _echo_empty(note, as_json, table=not commit)
            return EXIT_OK, {"segments": 0, "cameras": [], "note": note}

    if not candidates:
        note = _no_rows_hint(
            db_root,
            model,
            path_map,
            "--cold" if restore else "--hot",
            destination=_db_root(dest_root, path_map),
        )
        _echo_empty(note, as_json, table=not commit)
        return EXIT_OK, {"segments": 0, "cameras": [], "note": note}

    shortfall = _cold_headroom(candidates, dest_root, cold_floor)
    if shortfall:
        blocked = commit and not i_know
        click.echo(shortfall.render(blocked), err=True)
        if blocked:
            click.echo("nothing was moved. Pass --i-know to go ahead anyway.", err=True)
            return EXIT_REFUSED, {
                "segments": len(candidates),
                "refused": shortfall.message,
            }

    if not commit:
        payload = _print_plan(candidates, as_json, job.media, restore)
        return EXIT_OK, payload

    note = missing_note(candidates)
    if note and not as_json:
        click.echo(note, err=True)

    swept = sweep_stale_parts(destination_dirs(candidates, source_root, dest_root))
    if swept and not as_json:
        click.echo(f"removed {len(swept)} stale .part files from an earlier run")

    def on_progress(done: int, total: int) -> None:
        click.echo(f"  {done}/{total} segments")

    report = relocate(
        database,
        model,
        candidates,
        source_root,
        dest_root,
        path_map,
        on_progress=None if as_json else on_progress,
        progress_every=max(1, progress_every),
        limiter=limiter,
    )

    payload = move_payload(report)
    payload["direction"] = "restore" if restore else "move"
    payload["media"] = job.media
    payload["swept_partials"] = [str(p) for p in swept]
    if not as_json:
        for line in failure_lines(report):
            click.echo(line, err=True)
        if report.pruned_dirs:
            click.echo(f"pruned {len(report.pruned_dirs)} empty directories")
        click.echo(move_summary(report))
    return (EXIT_FAILURE if report.failures else EXIT_OK), payload


def _echo_empty(note: str, as_json: bool, table: bool) -> None:
    """An empty result: a dry run wants the table, a move just wants the reason."""
    if as_json:
        return
    if table:
        click.echo(plan_table([]))
    click.echo(note)


def _cold_headroom(candidates, dest_root: Path, floor: tuple[str, float] | None):
    """A Finding explaining why the destination cannot take this, or None."""
    payload = sum(c.size for c in candidates)
    try:
        available = free_bytes(dest_root)
    except OSError as exc:
        return Finding(
            SEVERITY_ERROR,
            "cold-space-unknown",
            "cannot read the free space on the cold tier.",
            f"{dest_root}: {exc}",
            overridable=True,
        )
    wanted = _resolve_size(floor, dest_root) if floor else 0
    if available - payload >= wanted:
        return None
    return Finding(
        SEVERITY_ERROR,
        "cold-tier-too-small",
        "the cold tier does not have room for this move.",
        f"{dest_root} has {human_bytes(available)} free and this move would write "
        f"{human_bytes(payload)}"
        + (f", leaving less than the {human_bytes(wanted)} floor" if wanted else "")
        + ". Narrow it with --limit, a longer --older-than or --until-free.",
        overridable=True,
    )


def _print_plan(candidates, as_json: bool, media: str, restore: bool) -> dict:
    payload = candidates_payload(candidates)
    payload["dry_run"] = True
    payload["direction"] = "restore" if restore else "move"
    payload["media"] = media
    if as_json:
        return payload
    click.echo(plan_table(candidates))
    note = missing_note(candidates)
    if note:
        click.echo(note, err=True)
    click.echo("dry run - nothing moved")
    return payload


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


@cli.command("sync-report")
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Path to frigate.db.",
)
@click.option(
    "--recordings-root",
    required=True,
    type=click.Path(path_type=Path),
    help="Frigate's recordings root, as this machine sees it.",
)
@click.option(
    "--previews-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Frigate's clips/previews root, as this machine sees it.",
)
@click.option("--db-path-prefix", "prefixes", multiple=True, metavar="LOCAL=DATABASE")
@click.option("--json", "as_json", is_flag=True)
def sync_report(db_path, recordings_root, previews_root, prefixes, as_json):
    """Show what Frigate's media sync would delete, without deleting anything.

    Run this after a move and before touching the Maintenance pane. A healthy
    tiered setup reports nothing on both sides.
    """
    path_map = _path_map(prefixes)
    _open(db_path)
    roots = [("recordings", Recordings, Path(recordings_root))]
    if previews_root:
        roots.append(("previews", Previews, Path(previews_root)))

    try:
        scans = [
            syncscan.scan(model, media, root, path_map) for media, model, root in roots
        ]
    except DatabaseBusy as exc:
        _fail(str(exc))
    finally:
        close_database()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "scans": [s.to_dict() for s in scans],
                    "would_delete": sum(s.would_delete for s in scans),
                },
                indent=2,
            )
        )
    else:
        for line in syncscan.render(scans):
            click.echo(line)
    raise SystemExit(EXIT_FAILURE if any(s.would_delete for s in scans) else EXIT_OK)


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
