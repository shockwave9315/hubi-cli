#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
REPO_NAME="ownership-repo-$$"
SOCKET_ROOT="$TEST_ROOT/runtime"
GOOD_SERVICE="hubiv5-test-owner-good-$$.service"
STALE_SERVICE="hubiv5-test-owner-stale-$$.service"
WRONG_SERVICE="hubiv5-test-owner-wrong-$$.service"
RACE_SERVICE="hubiv5-test-owner-race-$$.service"
PASS_COUNT=0
FAIL_COUNT=0
UNANCHORED_PID=""

mkdir -p "$REPOS/$REPO_NAME" "$SOCKET_ROOT"
git init -q "$REPOS/$REPO_NAME"

# shellcheck source=tests/lib/v5_test_server.sh
source "$ROOT/tests/lib/v5_test_server.sh"

cleanup() {
    local service socket scope
    for socket in "$SOCKET_ROOT"/*/hubi; do
        [[ -S "$socket" ]] || continue
        while IFS= read -r scope; do
            [[ "$scope" == hubi-*.scope ]] || continue
            systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
        done < <(tmux -N -S "$socket" list-sessions -F '#{@hubi-scope}' 2>/dev/null || true)
        tmux -N -S "$socket" kill-server >/dev/null 2>&1 || true
    done
    for service in "$GOOD_SERVICE" "$STALE_SERVICE" "$WRONG_SERVICE" "$RACE_SERVICE"; do
        v5_test_server_stop "$service" || true
    done
    if [[ -n "$UNANCHORED_PID" ]]; then
        kill "$UNANCHORED_PID" >/dev/null 2>&1 || true
        wait "$UNANCHORED_PID" 2>/dev/null || true
    fi
    if [[ -n "$TEST_ROOT" && "$TEST_ROOT" == /tmp/* && -d "$TEST_ROOT" ]]; then
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

hubi_env() {
    local socket="$1" service="$2"
    shift 2
    env -u HUBI_ACTIVE -u HUBI_AGENT_INSTANCE -u HUBI_TMUX_SOCKET -u TMUX \
        HUBI_REPOS="$REPOS" \
        HUBI_TMUX_SOCKET_PATH="$socket" \
        HUBI_TMUX_SERVICE="$service" \
        HUBI_CODEX_BIN=/usr/bin/sleep \
        "$@"
}

session_name() {
    hubi_env "$1" "$2" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO_NAME"'
}

attempt_creation() {
    local socket="$1" service="$2"
    hubi_env "$socket" "$service" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" /usr/bin/sleep 30
    '
}

test_correct_owner_allows_creation() {
    local socket="$SOCKET_ROOT/good/hubi" session scope
    v5_test_server_start_path "$GOOD_SERVICE" "$socket" || return 1
    attempt_creation "$socket" "$GOOD_SERVICE" >/dev/null 2>&1 || return 1
    session="$(session_name "$socket" "$GOOD_SERVICE")"
    tmux -N -S "$socket" has-session -t "=$session" 2>/dev/null || return 1
    [[ "$(tmux -N -S "$socket" show-option -qv -t "=$session:" @hubi-managed)" == v5 ]] || return 1
    scope="$(tmux -N -S "$socket" show-option -qv -t "=$session:" @hubi-scope)"
    systemctl --user is-active --quiet "$scope"
}
check "active service and exact tmux cgroup owner allow creation" test_correct_owner_allows_creation

test_inactive_service_refuses_creation() {
    local socket="$SOCKET_ROOT/inactive/hubi" output rc session
    mkdir -p "$(dirname "$socket")"
    output="$(attempt_creation "$socket" "hubiv5-test-owner-inactive-$$.service" 2>&1)"; rc=$?
    session="$(session_name "$socket" "hubiv5-test-owner-inactive-$$.service")"
    [[ $rc -ne 0 && "$output" == *"odmowa utworzenia pracy"* \
        && ! -e "$socket" ]] \
        && ! tmux -N -S "$socket" has-session -t "=$session" 2>/dev/null
}
check "inactive service refuses creation without auto-spawn" test_inactive_service_refuses_creation

test_stale_socket_refuses_creation() {
    local socket="$SOCKET_ROOT/stale/hubi" output rc old_pid
    v5_test_server_start_path "$STALE_SERVICE" "$socket" || return 1
    old_pid="$(tmux -N -S "$socket" display-message -p '#{pid}')"
    systemctl --user kill --kill-whom=all --signal=KILL "$STALE_SERVICE" || return 1
    for _ in {1..50}; do systemctl --user is-active --quiet "$STALE_SERVICE" || break; sleep 0.02; done
    [[ -S "$socket" ]] || return 1
    output="$(attempt_creation "$socket" "$STALE_SERVICE" 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"odmowa utworzenia pracy"* \
        && ! -e "/proc/$old_pid" ]] \
        && ! tmux -N -S "$socket" display-message -p '#{pid}' >/dev/null 2>&1
}
check "a stale socket is refused and never becomes a new server" test_stale_socket_refuses_creation

test_wrong_unanchored_server_refuses_creation() {
    local socket="$SOCKET_ROOT/wrong/hubi" output rc session
    mkdir -p "$(dirname "$socket")"
    tmux -f /dev/null -S "$socket" -D >/dev/null 2>&1 &
    UNANCHORED_PID=$!
    for _ in {1..50}; do tmux -N -S "$socket" display-message -p '#{pid}' >/dev/null 2>&1 && break; sleep 0.02; done
    systemd-run --user --quiet --collect --service-type=simple --unit="$WRONG_SERVICE" -- /usr/bin/sleep 30 \
        || return 1
    output="$(attempt_creation "$socket" "$WRONG_SERVICE" 2>&1)"; rc=$?
    session="$(session_name "$socket" "$WRONG_SERVICE")"
    [[ $rc -ne 0 && "$output" == *"odmowa utworzenia pracy"* \
        && -e "/proc/$UNANCHORED_PID" ]] \
        && ! tmux -N -S "$socket" has-session -t "=$session" 2>/dev/null
}
check "an active wrong service cannot claim an unanchored server" test_wrong_unanchored_server_refuses_creation

test_exact_cgroup_match() {
    local proc="$TEST_ROOT/fake-proc" output rc socket="$SOCKET_ROOT/good/hubi"
    mkdir -p "$proc/4242"
    printf '0::/user.slice/hubi-tmux.service/not-the-owner.service\n' >"$proc/4242/cgroup"
    output="$(hubi_env "$socket" "$GOOD_SERVICE" HUBI_PROC_ROOT="$proc" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        hubi_server_pid() { printf 4242; }
        require_anchored_server
    ' 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"odmowa utworzenia pracy"* ]]
}
check "ownership requires the exact final cgroup service component" test_exact_cgroup_match

test_server_death_race_cannot_autospawn() {
    local socket="$SOCKET_ROOT/race/hubi" rc
    v5_test_server_start_path "$RACE_SERVICE" "$socket" || return 1
    hubi_env "$socket" "$RACE_SERVICE" SERVICE="$RACE_SERVICE" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        verify_hubi_server_ownership || exit 10
        systemctl --user kill --kill-whom=all --signal=KILL "$SERVICE" || exit 11
        for _ in {1..50}; do systemctl --user is-active --quiet "$SERVICE" || break; sleep 0.02; done
        tmux_cmd new-session -d -s forbidden -- sleep 30
    ' >/dev/null 2>&1
    rc=$?
    [[ $rc -ne 0 ]] || return 1
    sleep 0.05
    ! tmux -N -S "$socket" display-message -p '#{pid}' >/dev/null 2>&1
}
check "tmux -N closes the verification-to-creation server-death race" test_server_death_race_cannot_autospawn

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
