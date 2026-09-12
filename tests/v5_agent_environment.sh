#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
REPO_NAME="hubiv5-test-agent-env-$$"
SOCKET_NAME="hubiv5-test-agent-env-$$"
SOCKET_PATH="/tmp/tmux-$UID/$SOCKET_NAME"
TMUX_SERVICE="hubiv5-test-agent-env-$$.service"
PASS_COUNT=0
FAIL_COUNT=0
INSTANCES=(
    hubiv5-test-a hubiv5-test-u hubiv5-test-p
    hubiv5-test-n hubiv5-test-i hubiv5-test-m
)
declare -a TEST_SCOPES=()

mkdir -p "$REPOS/$REPO_NAME" "$TEST_ROOT/bin" "$TEST_ROOT/locks"
git init -q "$REPOS/$REPO_NAME"

name_for() {
    local kind="$1" instance="$2"
    env -u HUBI_AGENT_INSTANCE HUBI_REPOS="$REPOS" REPO_NAME="$REPO_NAME" \
        HUBI_FILE="$HUBI" KIND="$kind" INSTANCE="$instance" bash -c '
            source "$HUBI_FILE"
            resolve_repo "$REPO_NAME" >/dev/null
            if [[ "$KIND" == scope ]]; then
                agent_scope_name codex "$RESOLVED_REPO_KEY" "$INSTANCE"
            else
                agent_session_name codex "$RESOLVED_REPO_KEY" "$INSTANCE"
            fi
        '
}

for instance in "${INSTANCES[@]}"; do
    TEST_SCOPES+=("$(name_for scope "$instance")")
done

cleanup() {
    local scope
    # Only exact names computed for this test run are eligible for cleanup.
    for scope in "${TEST_SCOPES[@]}"; do
        case "$scope" in
            hubi-codex-*-hubiv5-test-[aupnim].scope)
                systemctl --user kill --kill-whom=all --signal=KILL "$scope" \
                    >/dev/null 2>&1 || true
                ;;
            *)
                printf 'Refusing unsafe test cleanup target: %s\n' "$scope" >&2
                ;;
        esac
    done
    if [[ "$TMUX_SERVICE" == hubiv5-test-agent-env-*.service ]]; then
        systemctl --user stop "$TMUX_SERVICE" >/dev/null 2>&1 || true
    fi
    if [[ -S "$SOCKET_PATH" ]]; then unlink -- "$SOCKET_PATH"; fi
    if [[ "$TEST_ROOT" == /tmp/* && -d "$TEST_ROOT" ]]; then
        find "$TEST_ROOT" -depth -delete
    fi
}
trap cleanup EXIT

pass() { printf 'ok - %s\n' "$1"; ((PASS_COUNT += 1)); }
fail() { printf 'not ok - %s\n' "$1" >&2; ((FAIL_COUNT += 1)); }
check() {
    local name="$1"
    shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}

cat >"$TEST_ROOT/bin/hubi-test-node" <<'EOF'
#!/bin/sh
exec /bin/sh "$@"
EOF

cat >"$TEST_ROOT/probe-agent" <<'EOF'
#!/usr/bin/env hubi-test-node
result="$1"
mode="$2"
case "$mode" in
    allowlisted)
        if [ "${OPENAI_API_KEY-}" = NEW_TEST_SENTINEL ]; then
            printf 'allowlisted=yes\n' >"$result"
        else
            printf 'allowlisted=no\n' >"$result"
        fi
        ;;
    absent)
        if [ "${OPENAI_API_KEY+x}" = x ]; then
            printf 'absent=no\n' >"$result"
        else
            printf 'absent=yes\n' >"$result"
        fi
        ;;
    path)
        printf 'path=yes\n' >"$result"
        ;;
    nonallowlisted)
        if [ "${HUBI_NOT_ALLOWLISTED+x}" = x ]; then
            printf 'nonallowlisted=no\n' >"$result"
        else
            printf 'nonallowlisted=yes\n' >"$result"
        fi
        ;;
    immutable)
        if [ "${OPENAI_API_KEY-}" = ENVIRONMENT_A ] && [ "$PATH" = "$3" ]; then
            printf 'initial=yes\n' >"$result"
        else
            printf 'initial=no\n' >"$result"
        fi
        ;;
    manager)
        printf 'manager=yes\n' >"$result"
        ;;
esac
exec /usr/bin/sleep 120
EOF
chmod +x "$TEST_ROOT/bin/hubi-test-node" "$TEST_ROOT/probe-agent"

systemd-run --user --quiet --collect --service-type=simple --unit="$TMUX_SERVICE" -- \
    /usr/bin/env PATH=/usr/bin OPENAI_API_KEY=STALE_TEST_SENTINEL \
    /usr/bin/tmux -f /dev/null -L "$SOCKET_NAME" -D || exit 1
for _ in {1..100}; do
    if systemctl --user is-active --quiet "$TMUX_SERVICE" \
        && /usr/bin/tmux -N -S "$SOCKET_PATH" display-message -p '#{pid}' >/dev/null 2>&1; then
        break
    fi
    sleep 0.02
done

start_case() {
    local instance="$1" result="$2" mode="$3" path="$4" api_state="$5"
    local executable="/bin/sh"
    local -a command=(
        env -u HUBI_ACTIVE -u HUBI_AGENT_INSTANCE -u TMUX
        -u OPENAI_API_KEY -u HUBI_NOT_ALLOWLISTED
        "PATH=$path"
        "HUBI_REPOS=$REPOS"
        "HUBI_TMUX_SOCKET_PATH=$SOCKET_PATH"
        "HUBI_TMUX_SERVICE=$TMUX_SERVICE"
        "HUBI_LOCK_ROOT=$TEST_ROOT/locks"
        "REPO_NAME=$REPO_NAME" "HUBI_FILE=$HUBI"
        "INSTANCE=$instance" "RESULT=$result" "MODE=$mode"
        "PROBE=$TEST_ROOT/probe-agent"
    )

    case "$api_state" in
        set) command+=(OPENAI_API_KEY=NEW_TEST_SENTINEL) ;;
        immutable) command+=(OPENAI_API_KEY=ENVIRONMENT_A) ;;
        unset) ;;
        *) return 2 ;;
    esac
    if [[ "$mode" == path ]]; then executable="$TEST_ROOT/probe-agent"; fi
    command+=("AGENT_EXEC=$executable")
    if [[ "$mode" == nonallowlisted ]]; then
        command+=(HUBI_NOT_ALLOWLISTED=should-not-cross)
    fi

    "${command[@]}" bash -c '
        source "$HUBI_FILE"
        resolve_repo "$REPO_NAME" >/dev/null
        if [[ "$MODE" == path ]]; then
            HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session codex \
                "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$AGENT_EXEC" "$RESULT" "$MODE"
        else
            HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session codex \
                "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$AGENT_EXEC" \
                "$PROBE" "$RESULT" "$MODE" "$PATH"
        fi
    '
}

wait_for_result() {
    local result="$1"
    for _ in {1..100}; do [[ -s "$result" ]] && return 0; sleep 0.02; done
    return 1
}

test_allowlisted_value() {
    local result="$TEST_ROOT/allowlisted.result"
    start_case hubiv5-test-a "$result" allowlisted /usr/bin set || return 1
    wait_for_result "$result" && [[ "$(cat "$result")" == allowlisted=yes ]]
}
check "launcher allowlisted value replaces stale server value" test_allowlisted_value

test_absent_value() {
    local result="$TEST_ROOT/absent.result" session marker
    start_case hubiv5-test-u "$result" absent /usr/bin unset || return 1
    wait_for_result "$result" || return 1
    session="$(name_for session hubiv5-test-u)"
    marker="$(tmux -N -S "$SOCKET_PATH" show-environment -t "=$session" OPENAI_API_KEY)"
    [[ "$(cat "$result")" == absent=yes && "$marker" == -OPENAI_API_KEY ]]
}
check "unset allowlisted value blocks stale server inheritance" test_absent_value

test_launcher_path() {
    local result="$TEST_ROOT/path.result"
    start_case hubiv5-test-p "$result" path "$TEST_ROOT/bin:/usr/bin" unset || return 1
    wait_for_result "$result" && [[ "$(cat "$result")" == path=yes ]]
}
check "launcher PATH is explicitly available to the new agent" test_launcher_path

test_nonallowlisted_value() {
    local result="$TEST_ROOT/nonallowlisted.result"
    start_case hubiv5-test-n "$result" nonallowlisted /usr/bin unset || return 1
    wait_for_result "$result" && [[ "$(cat "$result")" == nonallowlisted=yes ]]
}
check "non-allowlisted launcher variable does not cross" test_nonallowlisted_value

test_later_attach_is_immutable() {
    local result="$TEST_ROOT/immutable.result" session before_path before_api after_path after_api
    local initial_path="$TEST_ROOT/bin:/usr/bin"
    start_case hubiv5-test-i "$result" immutable "$initial_path" immutable || return 1
    wait_for_result "$result" || return 1
    session="$(name_for session hubiv5-test-i)"
    before_path="$(tmux -N -S "$SOCKET_PATH" show-environment -t "=$session" PATH)"
    before_api="$(tmux -N -S "$SOCKET_PATH" show-environment -t "=$session" OPENAI_API_KEY)"
    env PATH=/usr/bin OPENAI_API_KEY=ENVIRONMENT_B \
        python3 "$ROOT/tests/tmux_client.py" readonly "$SOCKET_PATH" "$session" ignored >/dev/null
    after_path="$(tmux -N -S "$SOCKET_PATH" show-environment -t "=$session" PATH)"
    after_api="$(tmux -N -S "$SOCKET_PATH" show-environment -t "=$session" OPENAI_API_KEY)"
    [[ "$(cat "$result")" == initial=yes \
        && "$before_path" == "PATH=$initial_path" && "$after_path" == "$before_path" \
        && "$before_api" == OPENAI_API_KEY=ENVIRONMENT_A && "$after_api" == "$before_api" ]]
}
check "later client environment does not mutate an existing agent" test_later_attach_is_immutable

test_manager_path() {
    local result="$TEST_ROOT/manager.result"
    start_case hubiv5-test-m "$result" manager /usr/bin unset || return 1
    wait_for_result "$result" && [[ "$(cat "$result")" == manager=yes ]]
}
check "normal system PATH still starts an agent" test_manager_path

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
