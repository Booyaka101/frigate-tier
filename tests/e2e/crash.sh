#!/bin/sh
# SIGKILL a move in flight, then prove no segment was lost and the run resumes.
#
# SIGKILL is the worst case on purpose: no handler runs, no cleanup happens, the
# process simply stops between a copy and its commit. Frigate's own crash, an
# OOM kill or the host losing power all land in the same place.
set -eu

WORK="${WORK:-/work}"
SRC="${SRC:-/src}"
DEMO="$WORK/crashdemo"
STATE="$WORK/crashstate"

HOT="$DEMO/media/frigate/recordings"
PHOT="$DEMO/media/frigate/clips/previews"
COLD="$DEMO/mnt/nas/frigate/recordings"
DB="$DEMO/config/frigate.db"
MAP="--db-path-prefix $COLD=/media/archive/recordings"
CHECK="python $SRC/tests/e2e/mediacheck.py"

step() { printf '\n=== %s ===\n' "$1"; }

rm -rf "$DEMO" "$STATE"
mkdir -p "$STATE"

step "1. build a tree and fingerprint it"
python -m frigate_tier.fixture "$DEMO" --profile realistic \
    --cameras driveway front_door --day-offsets 9 8 0 --per-day 20 >/dev/null
$CHECK snapshot "$HOT" "$PHOT" > "$STATE/before.json"
echo "$(find "$HOT" -name '*.mp4' | wc -l) segments on the hot tier"

step "2. start a move and SIGKILL it mid-flight"
frigate-tier move --db "$DB" --hot "$HOT" --cold "$COLD" \
    --older-than 3d $MAP --commit --bandwidth-limit 8M --progress-every 5 &
MOVER=$!
sleep 4
kill -9 "$MOVER" 2>/dev/null || true
wait "$MOVER" 2>/dev/null || true
echo "killed pid $MOVER"

step "3. state right after the kill"
python "$SRC/tests/e2e/tiers.py" "$DB"
echo "part files left   : $(find "$COLD" -name '*.frigate-tier.part' 2>/dev/null | wc -l)"
echo "files on cold     : $(find "$COLD" -name '*.mp4' | wc -l)"

step "4. no row points at a missing file, and no segment was lost"
python "$SRC/tests/e2e/checkdb.py" "$DB" "$COLD=/media/archive/recordings"
$CHECK snapshot "$HOT" "$PHOT" "$COLD" > "$STATE/after_kill.json"
$CHECK compare "$STATE/before.json" "$STATE/after_kill.json"
$CHECK decode "$HOT" "$PHOT" "$COLD"

step "5. resume, and finish the job"
frigate-tier move --db "$DB" --hot "$HOT" --cold "$COLD" \
    --older-than 3d $MAP --commit --progress-every 20
frigate-tier verify --db "$DB" --cold "$COLD" $MAP

step "6. still nothing lost, nothing corrupt, no leftovers"
$CHECK snapshot "$HOT" "$PHOT" "$COLD" > "$STATE/after_resume.json"
$CHECK compare "$STATE/before.json" "$STATE/after_resume.json"
$CHECK decode "$HOT" "$PHOT" "$COLD"
python "$SRC/tests/e2e/checkdb.py" "$DB" "$COLD=/media/archive/recordings"
test -z "$(find "$COLD" -name '*.frigate-tier.part' 2>/dev/null)" || {
    echo "FAIL: a .part file survived the resumed run"; exit 1; }
frigate-tier sync-report --db "$DB" --recordings-root "$HOT" --previews-root "$PHOT" $MAP

printf '\n=== crash test passed ===\n'
