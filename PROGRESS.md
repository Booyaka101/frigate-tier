# PROGRESS

**Status: v1.0.0 complete and verified locally. Not published.** The owner ships it.

## Phase 0: every external claim in the brief was fetched and checked

| Claim | Source | Result |
| ----- | ------ | ------ |
| `path = CharField(unique=True)` on `Recordings` and `Previews`, `segment_size = FloatField(default=0)` | [frigate/models.py](https://raw.githubusercontent.com/blakeblackshear/frigate/dev/frigate/models.py) | exact, lines 72 and 130, `segment_size` line 79 with the "stored as MB" comment |
| `file.write(f"file '{clip.path}'\n")` | [frigate/api/media.py](https://raw.githubusercontent.com/blakeblackshear/frigate/dev/frigate/api/media.py) | exact, line 504, inside the concat playlist builder |
| `recording_path.unlink(missing_ok=True)` in `expire_existing_camera_recordings` | [frigate/record/cleanup.py](https://raw.githubusercontent.com/blakeblackshear/frigate/dev/frigate/record/cleanup.py) | exact, line 203, function at line 110 |
| `YYYY-MM-DD/HH/<camera_name>/MM.SS.mp4` in UTC, plus `POST /api/media/sync` | [docs/configuration/record.md](https://raw.githubusercontent.com/blakeblackshear/frigate/dev/docs/docs/configuration/record.md) | exact, lines 10 and 345 |
| issue #3673: 68 reactions, 93 comments, NickM-27's "we don't want frigate managing the native filesystems" | GitHub API | exact, open, reaction total 68, comment count 93, quote in the 2022-08-18 comment |
| issue #3673: fpytloun's "execute `UPDATE` statement to update path in sqlite" | GitHub API | exact, 2023-11-26 comment |
| discussion #18343: mergerfs setup, "it isn't [achievable] for most people. This is pretty complex" | GitHub API | present, with one correction: the quote is DrSpaldo (a user) replying to collaborator NickM-27's "quite achievable outside of Frigate", not the collaborator's own words. The README attributes it correctly. |

Two things I found beyond the brief and built around:

- `frigate/util/media.py` `sync_recordings` also **deletes database rows** whose path does not exist
  from inside the container, not just orphaned files. That makes a wrong `--db-path-prefix` a
  data-loss bug, not a playback bug, which is why the missing-mapping case is a refusal.
- Both halves of media sync abort at a 50% threshold (`SAFETY_THRESHOLD = 0.5`) unless `force: true`.

No cost barrier: Python, peewee, click, ffmpeg and Docker, all free, all already on this machine.

## What is VERIFIED working

Verified by actually running it, not by reading the code.

- `plan`, `move`, `verify`, `restore` on a real 60-segment fixture, from a wheel installed into a
  clean venv (`D:\tmp\ftclean`), for both `--media recordings` and `--media previews`.
- The container mapping round trip: `--db-path-prefix <local>=/media/archive/recordings` writes
  container paths into the database, `verify` and `restore` read them back through the same map.
- Acceptance SQL after `move --commit`: 40 rows under the cold root, all 40 files present, 0 rows in
  the whole table with a missing file, 40 files physically on the cold tier.
- Playback: rebuilt Frigate's own concat playlist from the post-move rows and ran
  `ffmpeg -f concat -c copy` over 30 clips spanning both tiers. One 84.97 second mp4, same duration
  as before the move. This is `tests/test_playback.py` as well as a manual run.
- `restore` returns all 60 recordings and 6 previews to the hot tree, byte-identical (sha256 per
  file), 0 files left on cold, 0 container paths left in the database.
- Refusals: cold under hot exits 3 and moves nothing; a missing container mapping exits 3.
- 63 pytest tests green on Python 3.11 against **both** peewee 4.4.0 and peewee 3.17.9, which is
  Frigate's own pin (`docker/main/requirements-wheels.txt`: `peewee == 3.17.*`).
- `ruff check frigate_tier tests` clean under `E,W,F,I,UP,B,SIM,C4`.
- Docker image builds and runs: one-shot pass (`FRIGATE_TIER_INTERVAL=0`) doing recordings, previews
  and verify; interval loop starts, sleeps and stops cleanly on SIGTERM; a failing pass propagates
  its exit code; a missing `FRIGATE_TIER_COLD` prints a message and exits 1.
- Wheel and sdist build, install into a clean venv, and the `frigate-tier` console script runs. The
  script name matches the distribution name, so `uvx frigate-tier` resolves.

## Commands to reproduce

```bash
pip install -e ".[dev]"
pytest -q                       # 63 tests, needs ffmpeg on PATH
ruff check frigate_tier tests
python -m frigate_tier.fixture /tmp/frigate-demo
docker build -t frigate-tier:test .
```

## Left for the owner

Publishing only. `.github/workflows/release.yml` is wired for both and fires on a `v*` tag:

1. PyPI via trusted publishing. Needs the `frigate-tier` project claimed on PyPI with this repo as
   the trusted publisher, and a `pypi` GitHub environment.
2. GHCR via `GITHUB_TOKEN`, multi-arch amd64 + arm64.

Then the distribution step in the README: a comment on frigate#3673.

## Next steps, if there is a v1.1

Ordered by how often I think someone would actually want them.

1. **A `--free-space` target.** Move oldest-first until the hot tier has N GB or N% free, rather
   than a fixed age. This is what people ask for in #3673 when they describe their setup, and the
   selection code already orders by `start_time`, so it is a stopping condition and nothing else.
2. **`--min-free-on-cold`.** Refuse to start a move that would fill the cold tier. One `statvfs`
   against the summed candidate bytes, which `plan` already computes.
3. **Both media types in one invocation.** `--media all` with `--preview-hot` / `--preview-cold`,
   so the Docker loop does not need a second pass. Deliberately left out of v1 because two trees
   with two mappings made the flags ambiguous.
4. **A `--bandwidth-limit`.** A first run of several hundred GB will saturate a NAS link. A sleep
   between segments sized from the measured copy rate would be a dozen lines.
5. **Sweep stale `.frigate-tier.part` files** at the start of a run. Only reachable by SIGKILL
   mid-copy today, and documented under Limitations, but it is cheap to clean up.
6. **Report what a media sync would do.** frigate-tier already knows every path; it could dry-run
   Frigate's own orphan logic and tell you before you click the button.
