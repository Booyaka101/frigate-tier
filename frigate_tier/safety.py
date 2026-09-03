"""Refusal rules and container path mapping.

Two Frigate behaviours drive everything here, both in frigate/util/media.py:

* ``sync_recordings`` walks ``/media/frigate/recordings`` and unlinks every file
  that has no row with a matching ``Recordings.path``. A cold tier placed under
  the hot recordings root is therefore inside the blast radius.
* the same function deletes every ``Recordings`` row whose ``path`` does not
  exist *from inside the Frigate container*. If the cold tier is not visible to
  Frigate at the path written into the database, one media sync drops the rows.

Both stop at a 50% safety threshold unless the user passes ``force``, which is
enough to make a small mistake survivable and a large one permanent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


class SafetyRefusal(Exception):
    """A layout the tool will not operate on."""


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    overridable: bool = False

    def render(self) -> str:
        label = "REFUSING" if self.severity == SEVERITY_ERROR else "warning"
        return f"{label}: {self.message}"


def _sep_for(text: str) -> str:
    return "\\" if "\\" in text and "/" not in text else "/"


def as_prefix(text: str) -> str:
    """A directory path with the trailing separator its own style implies."""
    stripped = text.rstrip("/\\")
    return stripped + _sep_for(text or stripped)


def _split(text: str) -> list[str]:
    return [part for part in text.replace("\\", "/").split("/") if part]


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class PathMap:
    """Rewrites local filesystem paths into the paths Frigate sees, and back."""

    pairs: tuple[tuple[Path, str], ...] = ()

    @classmethod
    def parse(cls, specs) -> PathMap:
        pairs: list[tuple[Path, str]] = []
        for spec in specs or ():
            local, sep, remote = spec.partition("=")
            if not sep or not local.strip() or not remote.strip():
                raise SafetyRefusal(
                    f"bad --db-path-prefix {spec!r}, expected LOCAL_PATH=DATABASE_PATH "
                    "(for example "
                    "/mnt/nas/frigate/recordings=/media/archive/recordings)"
                )
            pairs.append((Path(local.strip()), remote.strip().rstrip("/\\")))
        pairs.sort(key=lambda pair: len(str(pair[0])), reverse=True)
        return cls(tuple(pairs))

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def covers(self, local: Path) -> bool:
        return any(_is_within(local, root) for root, _ in self.pairs)

    def to_db(self, local: Path) -> str:
        """The string that should be written into Recordings.path / Previews.path."""
        for root, remote in self.pairs:
            if _is_within(local, root):
                relative = local.relative_to(root)
                if not relative.parts:
                    return remote
                return str(PurePosixPath(remote).joinpath(*relative.parts))
        return str(local)

    def from_db(self, stored: str) -> Path:
        """The local path holding the file a database row points at."""
        for root, remote in sorted(
            self.pairs, key=lambda pair: len(pair[1]), reverse=True
        ):
            if stored == remote:
                return root
            prefix = as_prefix(remote)
            if stored.startswith(prefix):
                return root.joinpath(*_split(stored[len(prefix) :]))
        return Path(stored)


@dataclass
class Audit:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    def blocking(self, i_know: bool) -> list[Finding]:
        return [f for f in self.errors if not (i_know and f.overridable)]

    def enforce(self, i_know: bool) -> None:
        blocking = self.blocking(i_know)
        if blocking:
            raise SafetyRefusal("\n".join(f.render() for f in blocking))


def _absolute(path: Path) -> Path:
    # A container path such as /media/frigate is not "absolute" to pathlib on
    # Windows, but it is still not something to resolve against the cwd.
    if path.is_absolute() or str(path).startswith(("/", "\\")):
        return path
    return Path.cwd() / path


def _db_is_within(child: str, parent: str) -> bool:
    child = child.replace("\\", "/")
    parent = parent.replace("\\", "/")
    return child == parent or child.startswith(as_prefix(parent))


def audit_roots(hot: Path, cold: Path, path_map: PathMap, media: str) -> Audit:
    """Check a hot/cold pair against the media sync blast radius."""
    audit = Audit()
    hot = _absolute(hot)
    cold = _absolute(cold)
    scanned = "the recordings root" if media == "recordings" else "the previews root"

    if hot == cold:
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "same-root",
                f"--cold and --hot are the same directory ({hot}); "
                "there is nothing to move.",
            )
        )
        return audit

    if _is_within(cold, hot):
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "cold-under-hot",
                f"--cold {cold} is inside --hot {hot}. Frigate's media sync walks "
                f"{scanned} and unlinks every file with no matching database row, so a "
                "cold tier stored there can be deleted by one click of the Maintenance "
                "pane's sync button.",
            )
        )
    elif _is_within(cold, hot.parent):
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "cold-under-media-root",
                f"--cold {cold} is inside Frigate's media root {hot.parent}. "
                "Media sync cleans several trees under that root; keep the cold "
                "tier on its own mount outside it.",
                overridable=True,
            )
        )

    if _is_within(hot, cold):
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "hot-under-cold",
                f"--hot {hot} is inside --cold {cold}; "
                "the move would recurse into itself.",
            )
        )

    if not path_map:
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "unverified-container-path",
                f"no --db-path-prefix given, so {cold} will be written into the "
                "database verbatim. That is only correct if the Frigate container "
                "sees the cold tier at exactly that path. If it does not, playback "
                "breaks and a media sync deletes the rows. Pass --db-path-prefix "
                f"{cold}=<path inside the container>, or --i-know if the paths really "
                "are identical.",
                overridable=True,
            )
        )
        return audit

    if not path_map.covers(cold):
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "prefix-misses-cold",
                f"--db-path-prefix does not cover --cold {cold}, so the paths "
                "written to the database would still be host paths. Map the cold "
                f"root itself, for example --db-path-prefix "
                f"{cold}=/media/archive/{media}.",
            )
        )
        return audit

    cold_db = path_map.to_db(cold)
    hot_db = path_map.to_db(hot)
    if _db_is_within(cold_db, hot_db):
        audit.findings.append(
            Finding(
                SEVERITY_ERROR,
                "db-cold-under-db-hot",
                f"--db-path-prefix maps --cold to {cold_db}, which is inside the "
                f"path Frigate uses for hot recordings ({hot_db}). Frigate would "
                "look for the files under its own media root, not find them, and "
                "media sync would delete the rows.",
            )
        )
    return audit
