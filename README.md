# frigate-tier

Move old [Frigate NVR](https://github.com/blakeblackshear/frigate) recording segments off a fast disk
onto a slow, large one, and keep every one of them playable in the Frigate UI.

Frigate writes everything to a single `/media/frigate` volume and has no notion of storage tiers.
[Issue #3673](https://github.com/blakeblackshear/frigate/issues/3673) has asked for them since 2022
(68 reactions, 93 comments) and the answer has consistently been that this belongs outside Frigate:
"We don't want frigate managing the native filesystems, we want one volume mount for recordings
without caring what the actual file system is behind that." The usual workaround is a mergerfs setup
plus a nightly `cp`/`sqlite3` script, written up in
[discussion #18343](https://github.com/blakeblackshear/frigate/discussions/18343), where a user
answers the "it is quite achievable outside of Frigate" line with "it isn't for most people. This is
pretty complex."

frigate-tier is that job done properly: one command, per-segment verification, a transaction per
file, and refusals for the two layouts that quietly destroy footage.

## Why moving a segment works at all

Frigate resolves recordings through the database, not through a fixed directory:

- `frigate/models.py` stores the full path on the row: `path = CharField(unique=True)` on both
  `Recordings` and `Previews`.
- `frigate/api/media.py` builds playback and exports by writing that stored value straight into an
  ffmpeg concat playlist: `file.write(f"file '{clip.path}'\n")`. Whatever path the row holds is the
  path ffmpeg opens.
- `frigate/record/cleanup.py` expires footage with `Path(recording.path).unlink(missing_ok=True)`,
  so retention keeps working after a move and cannot crash on a file that is mid-flight.

So a segment plays from any path the Frigate container can see, as long as the row is updated to
match. That is the whole trick, and it is why `--db-path-prefix` below matters so much.

The test suite proves it rather than asserting it. `tests/test_playback.py` builds the same concat
playlist Frigate builds, from the paths in the database after a move, and hands it to ffmpeg. Here
is that playlist for one camera, spanning both tiers after `move --older-than 3d`:

```
file 'D:/tmp/ft-demo/mnt/nas/frigate/recordings/2026-08-29/02/driveway/00.00.mp4'
file 'D:/tmp/ft-demo/mnt/nas/frigate/recordings/2026-08-29/02/driveway/00.10.mp4'
...
file 'D:/tmp/ft-demo/media/frigate/recordings/2026-09-03/00/driveway/01.20.mp4'
file 'D:/tmp/ft-demo/media/frigate/recordings/2026-09-03/00/driveway/01.30.mp4'
```

30 clips, 20 of them on the cold tier, `ffmpeg -f concat -c copy` produces one 84.97 second mp4.
Same duration as before the move.

## Install

```bash
pip install frigate-tier
# or, without installing:
uvx frigate-tier --help
```

Python 3.11 or newer. The only runtime dependencies are peewee (Frigate's own ORM) and click.

## Quick start

Look before you leap. `plan` never writes anything:

```
$ frigate-tier plan --db /config/frigate.db \
      --hot /media/frigate/recordings \
      --cold /mnt/nas/frigate/recordings \
      --older-than 3d \
      --db-path-prefix /mnt/nas/frigate/recordings=/media/archive/recordings
camera        segments   size      oldest                newest
driveway            20   415.4 KB  2026-08-29 02:00Z     2026-08-30 02:01Z
front_door          20   415.4 KB  2026-08-29 02:00Z     2026-08-30 02:01Z
total               40   830.9 KB
dry run - nothing moved
```

Add `--commit` to do it:

```
$ frigate-tier move ... --commit
  20/40 segments
  40/40 segments
pruned 8 empty directories
moved 40 segments, 830.9 KB, 0 failures, 40 rows updated
```

Then confirm every archived row still has its file at the size Frigate recorded:

```
$ frigate-tier verify --db /config/frigate.db --cold /mnt/nas/frigate/recordings \
      --db-path-prefix /mnt/nas/frigate/recordings=/media/archive/recordings
checked 40 rows under /media/archive/recordings, 0 problems
```

`verify` exits non-zero on the first mismatch, so it works as a cron health check.

`restore` reverses a move, file for file and row for row:

```
$ frigate-tier restore --db /config/frigate.db \
      --hot /media/frigate/recordings --cold /mnt/nas/frigate/recordings \
      --db-path-prefix /mnt/nas/frigate/recordings=/media/archive/recordings --commit
```

Every command takes `--json` for a machine-readable report.

## Container path mapping

This is the part people get wrong.

frigate-tier normally runs on the host, where the NAS is mounted at something like
`/mnt/nas/frigate/recordings`. Frigate runs in a container, where that same storage is passed
through as, say, `/media/archive/recordings`. The database has to hold the path **Frigate** sees,
not the path the tool sees:

```
--db-path-prefix /mnt/nas/frigate/recordings=/media/archive/recordings
```

The left side is where frigate-tier finds the files. The right side is what gets written into
`Recordings.path`. The flag is repeatable if the hot tier also needs remapping.

You also need the matching bind mount on the Frigate container, or Frigate cannot open the files:

```yaml
services:
  frigate:
    volumes:
      - /mnt/nas/frigate/recordings:/media/archive/recordings
```

If you leave `--db-path-prefix` off, frigate-tier writes its own paths into the database. That is
correct only when the tool and the container see the cold tier at exactly the same path, which is
true if you run the tool inside the Frigate container itself. Because the tool cannot prove that
from outside, `plan` warns and `move` refuses until you pass either the mapping or `--i-know`.

## The Media Sync hazard

Frigate 0.18 added `POST /api/media/sync` and a Maintenance pane button that reconcile the database
against the disk. From `frigate/util/media.py`, `sync_recordings` does two destructive things:

1. It walks `/media/frigate/recordings` and `os.unlink`s every file with no matching
   `Recordings.path` row.
2. It deletes every `Recordings` row whose `path` does not exist **as seen from inside the Frigate
   container**.

Both abort at a 50% threshold, and `force: true` removes even that.

Consequences, all enforced in `frigate_tier/safety.py`:

- **A cold tier under the hot recordings root will be eaten.** frigate-tier refuses that layout
  outright, with no override. A cold tier elsewhere under the Frigate media root is refused unless
  you pass `--i-know`.
- **A cold tier Frigate cannot see means the rows get deleted, not just unplayable.** This is why
  the container mapping is a refusal and not a note in the docs. Get the bind mount right, then run
  `frigate-tier verify` from the host and confirm playback in the UI before you run a large move.
- **The source file is never unlinked before its UPDATE has committed**, so a sync can never catch a
  segment in a state where neither the row nor a file refers to it.
- **A failed byte check leaves the source in place** and skips the row.

Run a media sync yourself, after a move, if you want to see that nothing is orphaned. It should
report zero orphans in either direction.

## Commands

All four take `--db`, `--camera` (repeatable), `--limit`, `--db-path-prefix` (repeatable) and
`--json`.

| Command   | Roots            | What it does                                                |
| --------- | ---------------- | ----------------------------------------------------------- |
| `plan`    | `--hot --cold`   | Prints the table above. Reads only.                          |
| `move`    | `--hot --cold`   | Hot to cold. A dry run without `--commit`.                   |
| `restore` | `--hot --cold`   | Cold back to hot. A dry run without `--commit`.              |
| `verify`  | `--cold`         | Every row under the cold root still has its file, right size |

`plan`, `move` and `restore` also take `--media`. `verify` does not: it checks both tables for rows
under `--cold`, so one call covers recordings and previews if they share a root.

`--media` picks the table and tree: `recordings` (default) or `previews`. Preview clips live under
`/media/frigate/clips/previews/<camera>/`, a different tree from recordings, so archive them with a
second run:

```bash
frigate-tier move --media previews \
    --db /config/frigate.db \
    --hot /media/frigate/clips/previews \
    --cold /mnt/nas/frigate/previews \
    --older-than 3d \
    --db-path-prefix /mnt/nas/frigate/previews=/media/archive/previews \
    --commit
```

`--older-than` takes `s`, `m`, `h`, `d` or `w` suffixes, and filters on `end_time`, so a segment
Frigate is still writing is never a candidate.

Exit codes: `0` success, `1` an operational failure (unreadable database, failed segments, a verify
mismatch), `2` a bad command line, `3` a safety refusal.

## Docker

The image loops on an interval. It is meant to sit beside Frigate in the same compose file.

```yaml
services:
  frigate-tier:
    image: ghcr.io/booyaka101/frigate-tier:1.0.0
    restart: unless-stopped
    environment:
      FRIGATE_TIER_COLD: /mnt/archive/recordings
      FRIGATE_TIER_OLDER_THAN: 3d
      FRIGATE_TIER_INTERVAL: 3600
      FRIGATE_TIER_DB_PATH_PREFIX: /mnt/archive/recordings=/media/archive/recordings
      FRIGATE_TIER_VERIFY: "1"
    volumes:
      - /path/to/frigate/config:/config
      - /path/to/fast/media:/media/frigate
      - /mnt/nas/frigate/recordings:/mnt/archive/recordings
```

| Variable                      | Default                       |
| ----------------------------- | ----------------------------- |
| `FRIGATE_TIER_DB`             | `/config/frigate.db`          |
| `FRIGATE_TIER_HOT`            | `/media/frigate/recordings`   |
| `FRIGATE_TIER_COLD`           | required                      |
| `FRIGATE_TIER_OLDER_THAN`     | `3d`                          |
| `FRIGATE_TIER_INTERVAL`       | `3600` seconds, `0` runs once |
| `FRIGATE_TIER_DB_PATH_PREFIX` | unset, space separated        |
| `FRIGATE_TIER_PREVIEW_HOT`    | unset, enables a second pass  |
| `FRIGATE_TIER_PREVIEW_COLD`   | unset                         |
| `FRIGATE_TIER_VERIFY`         | `0`                           |
| `FRIGATE_TIER_DRY_RUN`        | `0`                           |
| `FRIGATE_TIER_I_KNOW`         | `0`                           |
| `FRIGATE_TIER_ARGS`           | unset, appended verbatim      |

Note the compose example above: because the container sees the archive at `/mnt/archive/recordings`
and Frigate sees it at `/media/archive/recordings`, the prefix is still required. If you mount it at
the same path in both containers, drop the prefix and set `FRIGATE_TIER_I_KNOW=1`.

Passing arguments to the container skips the loop and runs the CLI once:

```bash
docker run --rm -v /path/to/config:/config ghcr.io/booyaka101/frigate-tier:1.0.0 --help
```

## What one segment actually does

1. `stat` the file the row points at. Gone already? Report it and skip. Nothing is ever deleted
   because the file is missing.
2. Copy to `<destination>.frigate-tier.part`, `fsync`, then read the written file back off the disk
   and compare its size and sha256 against the source. A mismatch removes the part file and leaves
   the source alone.
3. `os.replace` the part file into place, `fsync` the directory.
4. `UPDATE ... SET path = ? WHERE id = ? AND path = ?` inside `db.atomic()`. A unique-constraint
   collision aborts that one segment, logs it, removes the copy and moves on to the next.
5. Only then unlink the source.
6. After the run, prune source directories that are now empty, walking up but never past `--hot`.

Because step 4 is the commit point and each segment is its own transaction, an interrupted run
leaves zero rows pointing at a missing file, and re-running picks up where it stopped.

The database is opened with `PRAGMA journal_mode=WAL` and `busy_timeout=15000`, matching Frigate.
A database that stays locked past that fails loudly rather than silently skipping rows. frigate-tier
never creates, alters or migrates a table.

## Not in scope

Exports, snapshots, event thumbnails, S3 or any cloud target, a web UI, changes to your Frigate
config, and Home Assistant. frigate-tier also never deletes a recording. Retention stays Frigate's
job, and it keeps working across the move because `cleanup.py` unlinks by stored path.

## Limitations

- One media type per run. Recordings and previews live in different trees, so they need different
  `--hot`/`--cold` pairs.
- Sizes are compared against `segment_size`, which Frigate stores as `round(bytes / 2**20, 2)`.
  A byte-identical file reproduces that exactly. If your storage does something exotic, loosen it
  with `verify --tolerance-mb`.
- `verify` only looks at rows under `--cold`. Rows still on the hot tier are Frigate's business.
- Nothing here detects that you have pointed Frigate at the wrong mount. It can only refuse the
  layouts it can see are wrong, which is why the container mapping is a refusal and why you should
  confirm playback in the UI after the first move.
- Moving across filesystems is a full copy. A large first run is bounded by your NAS write speed,
  not by the tool.
- A `SIGKILL` in the middle of a copy can leave one `<segment>.frigate-tier.part` file on the cold
  tier. Nothing reads it and the next run over that segment overwrites it, but it is not swept up
  automatically. `find <cold> -name '*.frigate-tier.part' -delete` if you care.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The suite is 63 tests against real media. `frigate_tier/fixture.py` renders 60 real mp4 segments
with `ffmpeg -f lavfi -i testsrc`, laid out as `YYYY-MM-DD/HH/<camera>/MM.SS.mp4` across two cameras
and three days, plus preview clips, then writes a SQLite database with the mirrored models pointing
at them. There are no mocks: the tests move real files and read the real database back with plain
`sqlite3`. ffmpeg has to be on PATH or the suite skips.

`ruff check frigate_tier tests` has to be clean too; CI runs it, plus the suite on Python 3.11,
3.12 and 3.13 against both peewee 3.17 (Frigate's own pin) and the current release.

To poke at it by hand:

```bash
python -m frigate_tier.fixture /tmp/frigate-demo
frigate-tier plan --db /tmp/frigate-demo/config/frigate.db \
    --hot /tmp/frigate-demo/media/frigate/recordings \
    --cold /tmp/frigate-demo/mnt/nas/frigate/recordings \
    --older-than 3d
```

## Where to tell people about it

One place, first: a comment on
[frigate#3673](https://github.com/blakeblackshear/frigate/issues/3673). It is the open request this
tool answers, it has been collecting subscribers since 2022, and the people in it have already said
what they need. A short comment saying what the tool does, that it refuses the two layouts media
sync will eat, and linking here will reach them without a separate announcement. The mergerfs
write-up in [discussion #18343](https://github.com/blakeblackshear/frigate/discussions/18343) is the
obvious second stop, since everyone there is already running a hand-rolled version of this.

## License

MIT.
