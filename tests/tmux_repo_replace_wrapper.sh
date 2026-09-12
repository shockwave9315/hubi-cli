#!/usr/bin/env bash
set -uo pipefail

: "${HUBI_TEST_REAL_TMUX:?}"
: "${HUBI_TEST_REPO_PATH:?}"
: "${HUBI_TEST_REPLACE_MARKER:?}"

is_new_session=0
for argument in "$@"; do
    if [[ "$argument" == new-session ]]; then
        is_new_session=1
        break
    fi
done

if (( is_new_session == 0 )) || [[ -e "$HUBI_TEST_REPLACE_MARKER" ]]; then
    exec "$HUBI_TEST_REAL_TMUX" "$@"
fi

output="$("$HUBI_TEST_REAL_TMUX" "$@")"
rc=$?
(( rc == 0 )) || exit "$rc"
mv -- "$HUBI_TEST_REPO_PATH" "$HUBI_TEST_REPO_PATH-old"
mkdir -- "$HUBI_TEST_REPO_PATH"
git init -q "$HUBI_TEST_REPO_PATH"
: >"$HUBI_TEST_REPLACE_MARKER"
printf '%s\n' "$output"
