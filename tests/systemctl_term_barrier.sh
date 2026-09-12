#!/usr/bin/env bash
set -uo pipefail

: "${HUBI_TEST_SCOPE_BARRIER_UNIT:?}"
: "${HUBI_TEST_SCOPE_BARRIER_READY:?}"
: "${HUBI_TEST_SCOPE_BARRIER_RELEASE:?}"

if [[ " $* " == *" --user kill --kill-whom=all --signal=TERM $HUBI_TEST_SCOPE_BARRIER_UNIT "* ]]; then
    /usr/bin/systemctl "$@"
    rc=$?
    : >"$HUBI_TEST_SCOPE_BARRIER_READY"
    for _ in {1..1500}; do
        [[ -e "$HUBI_TEST_SCOPE_BARRIER_RELEASE" ]] && exit "$rc"
        sleep 0.01
    done
    printf 'Timed out at deterministic post-TERM systemctl barrier.\n' >&2
    exit 124
fi

exec /usr/bin/systemctl "$@"
