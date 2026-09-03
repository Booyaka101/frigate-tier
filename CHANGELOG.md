# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/Booyaka101/frigate-tier/releases/tag/v1.0.0
