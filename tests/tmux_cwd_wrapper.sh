#!/usr/bin/env bash
set -uo pipefail

: "${HUBI_TEST_REAL_TMUX:?}"
: "${HUBI_TEST_CWD_LOG:?}"
: "${HUBI_TEST_CWD_MARKER:?}"
: "${HUBI_TEST_CWD_MODE:?}"

for argument in "$@"; do
    [[ "$argument" == '#{pane_current_path}' ]] || continue
    printf 'observation\n' >>"$HUBI_TEST_CWD_LOG"
    if [[ "$HUBI_TEST_CWD_MODE" == wrong ]]; then
        printf '/tmux-bootstrap-not-ready\n'
        exit 0
    fi
    if [[ ! -e "$HUBI_TEST_CWD_MARKER" ]]; then
        : >"$HUBI_TEST_CWD_MARKER"
        printf '/tmux-bootstrap-not-ready\n'
        exit 0
    fi
    break
done

exec "$HUBI_TEST_REAL_TMUX" "$@"
