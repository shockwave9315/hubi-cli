#!/usr/bin/env bash
set -uo pipefail

if [[ "${1:-}" == --hubi-test-worker ]]; then
    shift
    lock_file="$1"
    shift
    : "${HUBI_TEST_FLOCK_BARRIER_DIR:?}"
    marker="$HUBI_TEST_FLOCK_BARRIER_DIR/$(basename "$lock_file").$BASHPID"
    : >"$marker"
    for _ in {1..1000}; do
        [[ -e "$marker.release" ]] && exec "$@"
        sleep 0.01
    done
    printf 'Timed out at deterministic flock barrier: %s\n' "$lock_file" >&2
    exit 124
fi

if (( $# < 8 )) || [[ "$1" != --exclusive || "$2" != --close \
    || "$3" != --wait || "$5" != --conflict-exit-code ]]; then
    printf 'Unexpected flock invocation in test wrapper.\n' >&2
    exit 2
fi

lock_file="$7"
exec /usr/bin/flock "$1" "$2" "$3" "$4" "$5" "$6" "$lock_file" \
    "$0" --hubi-test-worker "$lock_file" "${@:8}"
