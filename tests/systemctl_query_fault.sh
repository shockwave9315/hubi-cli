#!/usr/bin/env bash
set -uo pipefail

: "${HUBI_TEST_SYSTEMCTL_UNIT:?}"
: "${HUBI_TEST_SYSTEMCTL_COUNTER_DIR:?}"

state_query=0
kill_query=0
has_active_state=0
has_invocation_id=0
has_unit=0

for argument in "$@"; do
    case "$argument" in
        --property=ActiveState) has_active_state=1 ;;
        --property=InvocationID) has_invocation_id=1 ;;
    esac
    [[ "$argument" == "$HUBI_TEST_SYSTEMCTL_UNIT" ]] && has_unit=1
done

if (( has_unit )) && [[ "${1:-}" == --user && "${2:-}" == show ]] \
    && (( has_active_state && ! has_invocation_id )); then
    state_query=1
elif (( has_unit )) && [[ "${1:-}" == --user && "${2:-}" == kill ]]; then
    kill_query=1
fi

if (( state_query )); then
    query_number=1
    while [[ -e "$HUBI_TEST_SYSTEMCTL_COUNTER_DIR/state.$query_number" ]]; do
        ((query_number += 1))
    done
    : >"$HUBI_TEST_SYSTEMCTL_COUNTER_DIR/state.$query_number"
    if [[ "$query_number" == "${HUBI_TEST_SYSTEMCTL_FAIL_STATE_AT:-0}" ]]; then
        printf 'Failed to connect to bus: deterministic test fault\n' >&2
        exit 69
    fi
fi

if (( kill_query )) && [[ "${HUBI_TEST_SYSTEMCTL_FAIL_KILL:-no}" == yes ]]; then
    printf 'Failed to send signal: deterministic test fault\n' >&2
    exit 69
fi

exec /usr/bin/systemctl "$@"
