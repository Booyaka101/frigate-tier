#!/bin/sh
# Loop frigate-tier on an interval. Any arguments passed to the container are
# handed straight to the CLI instead, so `docker run ... plan --help` works.
set -eu

if [ "$#" -gt 0 ]; then
    exec frigate-tier "$@"
fi

DB="${FRIGATE_TIER_DB:-/config/frigate.db}"
HOT="${FRIGATE_TIER_HOT:-/media/frigate/recordings}"
COLD="${FRIGATE_TIER_COLD:-}"
OLDER_THAN="${FRIGATE_TIER_OLDER_THAN:-3d}"
INTERVAL="${FRIGATE_TIER_INTERVAL:-3600}"
PREVIEW_HOT="${FRIGATE_TIER_PREVIEW_HOT:-}"
PREVIEW_COLD="${FRIGATE_TIER_PREVIEW_COLD:-}"
PREFIXES="${FRIGATE_TIER_DB_PATH_PREFIX:-}"
EXTRA="${FRIGATE_TIER_ARGS:-}"

if [ -z "$COLD" ]; then
    echo "FRIGATE_TIER_COLD is not set: there is nowhere to move segments to." >&2
    echo "Set it to the cold tier as this container sees it, for example" >&2
    echo "  -e FRIGATE_TIER_COLD=/mnt/archive/recordings" >&2
    exit 1
fi

prefix_args() {
    for prefix in $PREFIXES; do
        printf ' --db-path-prefix %s' "$prefix"
    done
}

optional() {
    [ -n "$2" ] && printf ' %s %s' "$1" "$2"
    return 0
}

MEDIA="recordings"
PREVIEW_ROOTS=""
if [ -n "$PREVIEW_HOT" ] && [ -n "$PREVIEW_COLD" ]; then
    MEDIA="all"
    PREVIEW_ROOTS="--preview-hot $PREVIEW_HOT --preview-cold $PREVIEW_COLD"
fi

COMMIT="--commit"
[ "${FRIGATE_TIER_DRY_RUN:-0}" = "1" ] && COMMIT=""
ACK=""
[ "${FRIGATE_TIER_I_KNOW:-0}" = "1" ] && ACK="--i-know"

TUNING="$(optional --until-free "${FRIGATE_TIER_UNTIL_FREE:-}")"
TUNING="$TUNING$(optional --min-free-on-cold "${FRIGATE_TIER_MIN_FREE_ON_COLD:-}")"
TUNING="$TUNING$(optional --bandwidth-limit "${FRIGATE_TIER_BANDWIDTH_LIMIT:-}")"

running=1
status=0
trap 'running=0' TERM INT

report() {
    echo "frigate-tier $2 exited $1" >&2
    status="$1"
}

pass() {
    # shellcheck disable=SC2046,SC2086
    frigate-tier move \
        --db "$DB" --media "$MEDIA" --hot "$HOT" --cold "$COLD" $PREVIEW_ROOTS \
        --older-than "$OLDER_THAN" \
        $(prefix_args) $TUNING $COMMIT $ACK $EXTRA || report $? "move"

    if [ "${FRIGATE_TIER_VERIFY:-0}" = "1" ]; then
        # shellcheck disable=SC2046,SC2086
        frigate-tier verify --db "$DB" --cold "$COLD" $(prefix_args) \
            || report $? "verify (recordings)"
        if [ -n "$PREVIEW_COLD" ]; then
            # shellcheck disable=SC2046,SC2086
            frigate-tier verify --db "$DB" --cold "$PREVIEW_COLD" $(prefix_args) \
                || report $? "verify (previews)"
        fi
    fi

    if [ "${FRIGATE_TIER_SYNC_REPORT:-0}" = "1" ]; then
        # shellcheck disable=SC2046,SC2086
        frigate-tier sync-report --db "$DB" --recordings-root "$HOT" \
            $(optional --previews-root "$PREVIEW_HOT") $(prefix_args) \
            || report $? "sync-report"
    fi
}

while [ "$running" = "1" ]; do
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') frigate-tier pass starting"
    pass

    if [ "$INTERVAL" = "0" ]; then
        exit "$status"
    fi
    status=0
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') sleeping ${INTERVAL}s"
    sleep "$INTERVAL" &
    wait $! || true
done
