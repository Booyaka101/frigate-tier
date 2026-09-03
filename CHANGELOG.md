# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-03

### Added

- `sync-report`, a read-only reimplementation of Frigate's own media sync. It tells you exactly
  what the Maintenance pane button would delete, on both sides, before you press it. Exits
  non-zero if anything would go.
- `--until-free 500G` or `--until-free 20%`: move oldest segments first until the hot filesystem
  has that much space, instead of a fixed age. Warns when both tiers are on one device, since
  moving between them frees nothing.
- `--min-free-on-cold`: refuse a move that would leave the cold tier below a floor. A move that
  plainly would not fit is now refused even without the flag.
- `--bandwidth-limit 50M`: cap the copy rate so a first run does not saturate the link the cameras
  are writing over.
- `--media all` with `--preview-hot` / `--preview-cold`, so recordings and previews move in one
  invocation. Every root is audited before anything moves.
- Stale `.frigate-tier.part` files older than an hour are swept at the start of a move. The age
  guard keeps a second frigate-tier from deleting a copy that is still in flight.
- `tests/e2e/`: a containerised end to end run over 315 real 10-second 720p segments, and a crash
  test that `SIGKILL`s a move in flight and proves nothing was lost. Both check every file by
  sha256, decoded frame count and a full `ffmpeg -f null` decode.
- `python -m frigate_tier.fixture --profile realistic`, plus `--cameras`, `--day-offsets` and
  `--per-day`, for building a tree that weighs what a real one weighs.

### Changed

- Refusals and warnings now print a one-line summary followed by a wrapped, indented explanation.
  They used to be a single long line that the terminal broke mid-word.
- `sync-report` and the empty-result messages read better: a run with nothing to do now says the
  rows are already archived rather than blaming the flags.
- The Docker entrypoint uses `--media all` for a single pass, and gained
  `FRIGATE_TIER_UNTIL_FREE`, `FRIGATE_TIER_MIN_FREE_ON_COLD`, `FRIGATE_TIER_BANDWIDTH_LIMIT` and
  `FRIGATE_TIER_SYNC_REPORT`.
- Row selection names the six columns it reads instead of `SELECT *`, so an older Frigate schema
  without `motion_heatmap` still works.

## [1.0.0] - 2026-09-03

First release.

### Added

- `plan`, `move`, `verify` and `restore` commands over Frigate's `recordings` and `previews`
  tables, with `--json` reports on all four.
- Per-segment copy, fsync, read-back size and sha256 check, path UPDATE inside `db.atomic()`,
  then unlink. One transaction per segment, so an interrupted run is resumable and never leaves a
  row pointing at a missing file.
- `--db-path-prefix LOCAL=DATABASE` for the common case where the tool runs on the host and Frigate
  sees the same storage at a different path inside its container.
- Refusals for the layouts Frigate's media sync would destroy: a cold tier under the hot recordings
  root, a cold tier mapped back under Frigate's own recordings path, and an unmapped cold tier whose
  visibility to the container cannot be proved. The last two are overridable with `--i-know`.
- Empty source directories are pruned after a move, walking up but never past `--hot`.
- Docker image whose entrypoint loops on `FRIGATE_TIER_INTERVAL`, with an optional second pass for
  preview clips and an optional `verify` after each pass.
- 63 pytest tests against 60 real mp4 segments generated with ffmpeg. No mocks. One of them
  rebuilds Frigate's own concat playlist from the post-move rows and runs ffmpeg over it, so
  playback across both tiers is proved rather than asserted.

[1.1.0]: https://github.com/Booyaka101/frigate-tier/releases/tag/v1.1.0
[1.0.0]: https://github.com/Booyaka101/frigate-tier/releases/tag/v1.0.0
