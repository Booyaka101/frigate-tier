#!/bin/sh
# End to end run against a realistic Frigate tree, inside a Linux container.
#
#   docker build -t frigate-tier:e2e .
#   docker run --rm -v "$PWD:/src" -v "$SOMEWHERE:/work" frigate-tier:e2e-harness \
#       /src/tests/e2e/run.sh
#
# Needs ffmpeg, ffprobe, python and the frigate-tier CLI on PATH, and a writable
# /work. Exits non-zero on the first failure, so a green run means every check
# below passed.
set -eu

WORK="${WORK:-/work}"
SRC="${SRC:-/src}"
DEMO="$WORK/demo"
STATE="$WORK/state"
CAMERAS="${CAMERAS:-driveway front_door side_gate}"
PER_DAY="${PER_DAY:-20}"
DAY_OFFSETS="${DAY_OFFSETS:-9 8 7 6 0}"

HOT="$DEMO/media/frigate/recordings"
PHOT="$DEMO/media/frigate/clips/previews"
COLD="$DEMO/mnt/nas/frigate/recordings"
PCOLD="$DEMO/mnt/nas/frigate/previews"
DB="$DEMO/config/frigate.db"
MAPS="--db-path-prefix $COLD=/media/archive/recordings \
      --db-path-prefix $PCOLD=/media/archive/previews"
CHECK="python $SRC/tests/e2e/mediacheck.py"

step() { printf '\n=== %s ===\n' "$1"; }

rm -rf "$DEMO" "$STATE"
mkdir -p "$STATE"

step "1. generate a realistic tree"
python -m frigate_tier.fixture "$DEMO" \
    --profile realistic \
    --cameras $CAMERAS \
    --day-offsets $DAY_OFFSETS \
    --per-day "$PER_DAY"
du -sh "$DEMO/media"

step "2. fingerprint and decode every original segment"
$CHECK snapshot "$HOT" "$PHOT" > "$STATE/before.json"
$CHECK decode "$HOT" "$PHOT"

step "3. plan"
frigate-tier plan --media all --db "$DB" \
    --hot "$HOT" --cold "$COLD" --preview-hot "$PHOT" --preview-cold "$PCOLD" \
    --older-than 3d $MAPS

step "4. move, both media types, one invocation"
frigate-tier move --media all --db "$DB" \
    --hot "$HOT" --cold "$COLD" --preview-hot "$PHOT" --preview-cold "$PCOLD" \
    --older-than 3d $MAPS --commit --progress-every 50

step "5. verify"
frigate-tier verify --db "$DB" --cold "$COLD" $MAPS
frigate-tier verify --db "$DB" --cold "$PCOLD" $MAPS

step "6. nothing lost, nothing corrupted"
$CHECK snapshot "$HOT" "$PHOT" "$COLD" "$PCOLD" > "$STATE/after.json"
$CHECK compare "$STATE/before.json" "$STATE/after.json"
$CHECK decode "$HOT" "$PHOT" "$COLD" "$PCOLD"

step "7. every row resolves, and playback concatenates across both tiers"
python "$SRC/tests/e2e/checkdb.py" "$DB" "$COLD=/media/archive/recordings" \
    "$PCOLD=/media/archive/previews"

step "8. media sync would delete nothing"
frigate-tier sync-report --db "$DB" --recordings-root "$HOT" --previews-root "$PHOT" $MAPS

step "9. a second move is a no-op"
frigate-tier move --media all --db "$DB" \
    --hot "$HOT" --cold "$COLD" --preview-hot "$PHOT" --preview-cold "$PCOLD" \
    --older-than 3d $MAPS --commit

step "10. restore everything"
frigate-tier restore --media all --db "$DB" \
    --hot "$HOT" --cold "$COLD" --preview-hot "$PHOT" --preview-cold "$PCOLD" \
    $MAPS --commit --progress-every 50

step "11. the tree is byte for byte what it was"
$CHECK snapshot "$HOT" "$PHOT" > "$STATE/restored.json"
$CHECK compare "$STATE/before.json" "$STATE/restored.json"
$CHECK decode "$HOT" "$PHOT"
python "$SRC/tests/e2e/checkdb.py" "$DB"
test -z "$(find "$COLD" "$PCOLD" -type f 2>/dev/null)" || {
    echo "FAIL: files left on the cold tier"; exit 1; }

printf '\n=== e2e passed ===\n'
