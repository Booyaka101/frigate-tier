# PROGRESS

**Status: v1.1.0 complete and verified locally. Not published.** The owner ships it.

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

Two things found beyond the brief that changed the design:

- `frigate/util/media.py` `sync_recordings` also **deletes database rows** whose path does not exist
  from inside the container, not just orphaned files. That makes a wrong `--db-path-prefix` a
  data-loss bug, not a playback bug, which is why the missing mapping is a refusal. In 1.1.0 it is
  also why `sync-report` exists.
- Both halves of media sync abort at a 50% threshold (`SAFETY_THRESHOLD = 0.5`) unless `force: true`.

No cost barrier: Python, peewee, click, ffmpeg and Docker, all free, all already on this machine.

## What is VERIFIED working

Verified by running it, not by reading the code. Every number below came off this machine.

### The end to end run, in Linux, on real video

`tests/e2e/run.sh` inside a container: 3 cameras, 5 days, 315 real 10-second 720p H.264 segments,
729 MB. Every segment gets its own hue and marker box so a mixed-up file changes its sha256.

- 240 recordings (554.5 MB) and 12 preview clips (27.6 MB) moved in one `--media all` invocation,
  0 failures, 252 rows updated, 20 empty directories pruned.
- `mediacheck.py compare`, keyed by content so a move between roots is not a difference:
  **315 segments before, 315 after, 0 problems.** Nothing lost, nothing new, no changed byte count,
  frame count, duration or resolution.
- `mediacheck.py decode`, a full `ffmpeg -f null` decode of every file: **0 corrupt**, before and
  after.
- `checkdb.py` rebuilds Frigate's own concat playlist from the post-move rows and runs ffmpeg over
  it. Each camera: **100 clips concat to 1000.00s (sum of parts 1000.00s) ok**, spanning both tiers.
- `sync-report`: media sync would delete nothing, on both sides, both media types.
- A second move is a no-op that says so.
- `restore` puts all 315 back, byte for byte, 0 corrupt, 0 files left on cold.

### The crash test

`tests/e2e/crash.sh`: start a move, `SIGKILL` it four seconds in, inspect the wreckage.

- At the moment of the kill: 13 rows committed to cold, 107 still hot, 1 orphaned `.part` file.
- **0 rows pointing at a missing file. 126 segments before, 126 after, 0 problems, 0 corrupt.**
  Playback still concatenates to exactly 600.00s per camera.
- Resuming moved the remaining 67, consumed the `.part`, and `verify` passed on all 80.
- `sync-report` afterwards: nothing would be deleted.

### Everything else

- 106 pytest tests green on Python 3.11 against **both** peewee 4.4.0 and peewee 3.17.9, which is
  Frigate's own pin (`docker/main/requirements-wheels.txt`: `peewee == 3.17.*`).
- `ruff check` and `ruff format --check` clean under `E,W,F,I,UP,B,SIM,C4`.
- Clone check (difflib on line lists, every function of 4+ lines in `frigate_tier/`, `tests/` and
  `tests/e2e/`): 171 functions, **no pair at or above 60%**.
- Wheel and sdist build; the wheel installs into a clean venv and `frigate-tier --version` runs. The
  console script name matches the distribution name, so `uvx frigate-tier` resolves.
- Docker image builds and runs: `--media all` in one pass with verify and sync-report, bandwidth
  limiting, interval loop that stops cleanly on SIGTERM, failing pass propagates its exit code,
  missing `FRIGATE_TIER_COLD` prints a message and exits 1.
- Screenshots in `docs/screenshots/` are genuine captures of a real console window running the
  installed 1.1.0 wheel against a 444 MB tree, not rendered text.

## Commands to reproduce

```bash
pip install -e ".[dev]"
pytest -q                       # 106 tests, needs ffmpeg on PATH
ruff check frigate_tier tests

docker build -f tests/e2e/Dockerfile -t frigate-tier:e2e .
docker run --rm -v "$PWD:/src" -v /tmp/ft-e2e:/work frigate-tier:e2e sh /src/tests/e2e/run.sh
docker run --rm -v "$PWD:/src" -v /tmp/ft-e2e:/work frigate-tier:e2e sh /src/tests/e2e/crash.sh
```

CI runs all of it: the suite on 3.11/3.12/3.13 against two peewee versions, a clean-venv wheel
install, the Docker build, and both end to end scripts.

## Left for the owner

Publishing only. `.github/workflows/release.yml` fires on a `v*` tag:

1. PyPI via trusted publishing. Needs the `frigate-tier` project claimed on PyPI with this repo as
   the trusted publisher, and a `pypi` GitHub environment.
2. GHCR via `GITHUB_TOKEN`, multi-arch amd64 + arm64.

## Distribution

Discovery is passive by choice: the PyPI name, the GHCR image, and the README's opening
paragraph. Announced once on r/frigate_nvr, which has no rules and takes tool posts well
(a comment there is at
https://www.reddit.com/r/frigate_nvr/comments/1w5y9wc/made_a_tool_for_the_move_old_frigate_recordings/).

Deliberately NOT frigate#3673, though it is the open request this answers. It is pinned and
marked planned, NickM-27 has twice asked people to stop advocating in it (the second time six
weeks ago, and nobody has posted since), and its author moderates it and is openly wary of AI
contributions. A third-party tool posted there reads as ignoring the maintainers or capturing
their audience. The README used to name it as the first distribution step; that was wrong and
the section is gone.

A four-line entry on Frigate's third-party extensions docs page was prepared and then
dropped. The page invites PRs and its recent entries are four-line additions by each tool's own
author, but Frigate's AI policy (added 2026-07-25) requires a person to read and send the PR, and
every AI-disclosed precedent on that page predates the policy. Not worth the effort for a docs
line. The fork was deleted.

## Next steps, if there is a v1.2

Everything from the 1.0.0 list is now built. What is left is smaller and less certain.

1. **Read the retention config.** frigate-tier does not know what Frigate would delete tomorrow, so
   it can archive a segment that expires in an hour. Parsing `record.retain.days` out of
   `config.yml` would let it skip that work. Deliberately not done in 1.1.0 because reading the
   user's Frigate config is one step beyond the non-goals in the brief, and it needs the same YAML
   include handling Frigate does.
2. **Tier by camera.** One `--older-than` for every camera is coarse; a doorbell and a driveway
   have different value. Wants a small config file rather than more flags, which is a design
   decision worth taking with a real user rather than guessing.
3. **Resume ordering by size.** With `--until-free`, taking the largest old segments first reaches
   the target with fewer copies. Oldest-first is the safer default; this would be a
   `--shed largest` opt-in.
4. **A `--check-container` probe.** If given a Frigate base URL, ask its API to confirm it can see a
   moved segment, which would turn the unverifiable container-mapping refusal into a real check.
   Needs an auth story, and `sync-report` already covers most of the value.
5. **Prometheus output.** `--json` already carries the numbers; a `--metrics` flag emitting
   textfile-collector format would drop straight into node_exporter.
