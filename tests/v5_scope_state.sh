#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
SYSTEMCTL_FAULT="$ROOT/tests/systemctl_query_fault.sh"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
REPO_NAME="scope-state-$$"
SOCKET="$TEST_ROOT/runtime/tmux-$UID/hubi"
TMUX_SERVICE="hubiv5-test-scope-state-$$.service"
SERVICE_RUNNER=""
PASS_COUNT=0
FAIL_COUNT=0
declare -a TEST_SCOPES=()

mkdir -p "$REPOS/$REPO_NAME"
git init -q "$REPOS/$REPO_NAME"

# The path is resolved from the runtime repository root.
# shellcheck disable=SC1091
source "$ROOT/tests/lib/v5_test_server.sh"

cleanup_scope() {
    local scope="$1"
    /usr/bin/systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
}

cleanup() {
    local scope
    for scope in "${TEST_SCOPES[@]}"; do cleanup_scope "$scope"; done
    /usr/bin/tmux -N -S "$SOCKET" kill-server >/dev/null 2>&1 || true
    v5_test_server_stop "$TMUX_SERVICE" || true
    [[ -z "$SERVICE_RUNNER" ]] || wait "$SERVICE_RUNNER" 2>/dev/null || true
    if [[ -n "$TEST_ROOT" && "$TEST_ROOT" == /tmp/* && -d "$TEST_ROOT" ]]; then
        find "$TEST_ROOT" -depth -delete
    fi
}
trap cleanup EXIT

v5_test_server_start_path "$TMUX_SERVICE" "$SOCKET" || {
    printf 'Hubi scope-state test tmux service did not start.\n' >&2
    exit 1
}

pass() { printf 'ok - %s\n' "$1"; ((PASS_COUNT += 1)); }
fail() { printf 'not ok - %s\n' "$1" >&2; ((FAIL_COUNT += 1)); }
check() {
    local name="$1"
    shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}

hubi_env() {
    env -u HUBI_ACTIVE -u HUBI_AGENT_INSTANCE -u TMUX \
        HUBI_REPOS="$REPOS" \
        HUBI_TMUX_SOCKET_PATH="$SOCKET" \
        HUBI_TMUX_SERVICE="$TMUX_SERVICE" \
        HUBI_LOCK_ROOT="$TEST_ROOT/locks" \
        "$@"
}

fault_env() {
    local scope="$1" counter="$2" fail_at="$3" fail_kill="$4"
    shift 4
    mkdir -p "$counter"
    env HUBI_SYSTEMCTL_BIN="$SYSTEMCTL_FAULT" \
        HUBI_TEST_SYSTEMCTL_UNIT="$scope" \
        HUBI_TEST_SYSTEMCTL_COUNTER_DIR="$counter" \
        HUBI_TEST_SYSTEMCTL_FAIL_STATE_AT="$fail_at" \
        HUBI_TEST_SYSTEMCTL_FAIL_KILL="$fail_kill" "$@"
}

fault_hubi_env() {
    local scope="$1" counter="$2" fail_at="$3" fail_kill="$4"
    shift 4
    mkdir -p "$counter"
    hubi_env HUBI_SYSTEMCTL_BIN="$SYSTEMCTL_FAULT" \
        HUBI_TEST_SYSTEMCTL_UNIT="$scope" \
        HUBI_TEST_SYSTEMCTL_COUNTER_DIR="$counter" \
        HUBI_TEST_SYSTEMCTL_FAIL_STATE_AT="$fail_at" \
        HUBI_TEST_SYSTEMCTL_FAIL_KILL="$fail_kill" "$@"
}

scope_active() {
    [[ "$(/usr/bin/systemctl --user show "$1" --property=ActiveState --value 2>/dev/null)" == active ]]
}

wait_scope_active() {
    local scope="$1"
    for _ in {1..100}; do scope_active "$scope" && return 0; sleep 0.02; done
    return 1
}

start_disposable_scope() {
    local scope="$1"
    shift
    TEST_SCOPES+=("$scope")
    systemd-run --user --scope --collect --quiet --unit="$scope" -- "$@" >/dev/null 2>&1 &
    LAST_SCOPE_RUNNER=$!
    wait_scope_active "$scope"
}

terminal_session() {
    hubi_env INSTANCE="$1" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_session_name "$REPO" "$INSTANCE"'
}

terminal_scope() {
    hubi_env INSTANCE="$1" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_scope_name "$REPO" "$INSTANCE"'
}

agent_session() {
    hubi_env INSTANCE="$1" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO" "$INSTANCE"'
}

agent_scope() {
    hubi_env INSTANCE="$1" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex "$REPO" "$INSTANCE"'
}

create_terminal() {
    local instance="$1"
    hubi_env INSTANCE="$instance" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        attach_session() { :; }
        pause_for_ack() { :; }
        start_terminal "$REPO" "$INSTANCE"
    '
}

create_stubborn_terminal_descendant() {
    local instance="$1" pidfile="$2" session code
    session="$(terminal_session "$instance")"
    code="import os,signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); open(\"$pidfile\",\"w\").write(str(os.getpid())); exec(\"while True:\\n time.sleep(1)\")"
    /usr/bin/tmux -N -S "$SOCKET" send-keys -t "=$session:" \
        "setsid /usr/bin/python3 -c '$code' </dev/null >/dev/null 2>&1 &" Enter || return 1
    for _ in {1..100}; do [[ -s "$pidfile" ]] && return 0; sleep 0.02; done
    return 1
}

create_stubborn_agent() {
    local instance="$1" pidfile="$2" code
    code="import os,signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); open(\"$pidfile\",\"w\").write(str(os.getpid())); print(\"AGENT_READY\",flush=True); exec(\"while True:\\n time.sleep(1)\")"
    hubi_env INSTANCE="$instance" REPO="$REPO_NAME" AGENT_CODE="$code" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        resolve_repo "$REPO"
        HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session codex \
            "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" /usr/bin/python3 -c "$AGENT_CODE"
    ' || return 1
    for _ in {1..100}; do [[ -s "$pidfile" ]] && return 0; sleep 0.02; done
    return 1
}

discard_work() {
    local session="$1" scope="$2"
    cleanup_scope "$scope"
    /usr/bin/tmux -N -S "$SOCKET" kill-session -t "=$session" >/dev/null 2>&1 || true
}

test_generation_query_error_is_unknown() {
    local scope="hubiv5-test-query-generation-$$.scope" runner counter result rc invocation
    counter="$TEST_ROOT/counters/generation"
    start_disposable_scope "$scope" sleep 300 || return 1
    runner="$LAST_SCOPE_RUNNER"
    invocation="$(/usr/bin/systemctl --user show "$scope" --property=InvocationID --value)"
    set +e
    result="$(fault_env "$scope" "$counter" 1 no HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; scope_generation_token "$HUBI_TEST_SYSTEMCTL_UNIT"')"
    rc=$?
    set -e
    cleanup_scope "$scope"
    wait "$runner" 2>/dev/null || true
    [[ "$invocation" =~ ^[0-9a-fA-F]{32}$ && $rc -ne 0 && "$result" != inactive \
        && -e "$counter/state.1" ]]
}
check "an active scope state-query error is not an inactive generation" \
    test_generation_query_error_is_unknown

test_terminal_stop_query_error_fails_closed() {
    local target=stop-query sibling=stop-query-sibling target_session target_scope
    local sibling_session sibling_scope pidfile child counter output rc server
    create_terminal "$target" || return 1
    create_terminal "$sibling" || return 1
    target_session="$(terminal_session "$target")"
    target_scope="$(terminal_scope "$target")"
    sibling_session="$(terminal_session "$sibling")"
    sibling_scope="$(terminal_scope "$sibling")"
    TEST_SCOPES+=("$target_scope" "$sibling_scope")
    pidfile="$TEST_ROOT/stop-query.pid"
    create_stubborn_terminal_descendant "$target" "$pidfile" || return 1
    child="$(<"$pidfile")"
    server="$(/usr/bin/tmux -N -S "$SOCKET" display-message -p '#{pid}')"
    counter="$TEST_ROOT/counters/terminal-stop"
    set +e
    output="$(fault_hubi_env "$target_scope" "$counter" 3 no \
        INSTANCE="$target" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO" "$INSTANCE"' 2>&1)"
    rc=$?
    set -e
    (( rc != 0 )) && [[ "$output" == *"nie można potwierdzić stanu systemd"* \
        && -e "$counter/state.3" ]] \
        && /usr/bin/tmux -N -S "$SOCKET" has-session -t "=$target_session" 2>/dev/null \
        && scope_active "$target_scope" && kill -0 "$child" 2>/dev/null \
        && /usr/bin/tmux -N -S "$SOCKET" has-session -t "=$sibling_session" 2>/dev/null \
        && scope_active "$sibling_scope" \
        && [[ "$(/usr/bin/tmux -N -S "$SOCKET" display-message -p '#{pid}')" == "$server" ]]
    result=$?
    discard_work "$target_session" "$target_scope"
    discard_work "$sibling_session" "$sibling_scope"
    return "$result"
}
check "terminal stop preserves session scope descendant sibling and server on unknown state" \
    test_terminal_stop_query_error_fails_closed

test_wait_query_error_after_term_fails_closed() {
    local scope="hubiv5-test-query-wait-$$.scope" runner pidfile code pid invocation counter output rc
    pidfile="$TEST_ROOT/wait.pid"
    code="import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(\"$pidfile\",\"w\").write(str(os.getpid())); exec(\"while True:\\n time.sleep(1)\")"
    start_disposable_scope "$scope" /usr/bin/python3 -c "$code" || return 1
    runner="$LAST_SCOPE_RUNNER"
    for _ in {1..100}; do [[ -s "$pidfile" ]] && break; sleep 0.02; done
    [[ -s "$pidfile" ]] || return 1
    pid="$(<"$pidfile")"
    invocation="$(/usr/bin/systemctl --user show "$scope" --property=InvocationID --value)"
    counter="$TEST_ROOT/counters/wait"
    set +e
    output="$(fault_env "$scope" "$counter" 3 no EXPECTED="$invocation" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminate_scope "$HUBI_TEST_SYSTEMCTL_UNIT" "$EXPECTED"' 2>&1)"
    rc=$?
    set -e
    (( rc != 0 )) && [[ "$output" == *"podczas oczekiwania"* && -e "$counter/state.3" ]] \
        && scope_active "$scope" && kill -0 "$pid" 2>/dev/null
    result=$?
    cleanup_scope "$scope"
    wait "$runner" 2>/dev/null || true
    return "$result"
}
check "a post-TERM wait query error is not confirmed inactive" \
    test_wait_query_error_after_term_fails_closed

test_kill_and_state_query_failure_returns_failure() {
    local scope="hubiv5-test-query-kill-$$.scope" runner counter output rc
    counter="$TEST_ROOT/counters/kill"
    start_disposable_scope "$scope" sleep 300 || return 1
    runner="$LAST_SCOPE_RUNNER"
    set +e
    output="$(fault_env "$scope" "$counter" 1 yes HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; signal_scope "$HUBI_TEST_SYSTEMCTL_UNIT" TERM' 2>&1)"
    rc=$?
    set -e
    (( rc != 0 )) && [[ "$output" == *"nie można potwierdzić jego stanu"* \
        && -e "$counter/state.1" ]] && scope_active "$scope"
    result=$?
    cleanup_scope "$scope"
    wait "$runner" 2>/dev/null || true
    return "$result"
}
check "systemctl kill plus state-query failure returns failure" \
    test_kill_and_state_query_failure_returns_failure

test_existing_terminal_start_query_error_preserves_work() {
    local target=start-query sibling=start-query-sibling target_session target_scope
    local sibling_session sibling_scope pidfile child counter output rc pane server
    create_terminal "$target" || return 1
    create_terminal "$sibling" || return 1
    target_session="$(terminal_session "$target")"
    target_scope="$(terminal_scope "$target")"
    sibling_session="$(terminal_session "$sibling")"
    sibling_scope="$(terminal_scope "$sibling")"
    TEST_SCOPES+=("$target_scope" "$sibling_scope")
    pidfile="$TEST_ROOT/start-query.pid"
    create_stubborn_terminal_descendant "$target" "$pidfile" || return 1
    child="$(<"$pidfile")"
    pane="$(/usr/bin/tmux -N -S "$SOCKET" display-message -p -t "=$target_session:" '#{pane_pid}')"
    server="$(/usr/bin/tmux -N -S "$SOCKET" display-message -p '#{pid}')"
    counter="$TEST_ROOT/counters/terminal-start"
    set +e
    output="$(fault_hubi_env "$target_scope" "$counter" 1 no \
        INSTANCE="$target" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c '
            source "$HUBI_FILE"
            attach_session() { :; }
            pause_for_ack() { :; }
            start_terminal "$REPO" "$INSTANCE"
        ' 2>&1)"
    rc=$?
    set -e
    (( rc != 0 )) && [[ "$output" == *"odmowa restartu terminala"* \
        && -e "$counter/state.1" ]] \
        && /usr/bin/tmux -N -S "$SOCKET" has-session -t "=$target_session" 2>/dev/null \
        && [[ "$(/usr/bin/tmux -N -S "$SOCKET" display-message -p -t "=$target_session:" '#{pane_pid}')" == "$pane" ]] \
        && scope_active "$target_scope" && kill -0 "$child" 2>/dev/null \
        && /usr/bin/tmux -N -S "$SOCKET" has-session -t "=$sibling_session" 2>/dev/null \
        && scope_active "$sibling_scope" \
        && [[ "$(/usr/bin/tmux -N -S "$SOCKET" display-message -p '#{pid}')" == "$server" ]]
    result=$?
    discard_work "$target_session" "$target_scope"
    discard_work "$sibling_session" "$sibling_scope"
    return "$result"
}
check "existing terminal start preserves valid work on unknown state" \
    test_existing_terminal_start_query_error_preserves_work

test_agent_stop_wait_query_error_preserves_work() {
    local instance=agent-stop session scope pidfile pid counter output rc server
    pidfile="$TEST_ROOT/agent-stop.pid"
    create_stubborn_agent "$instance" "$pidfile" || return 1
    session="$(agent_session "$instance")"
    scope="$(agent_scope "$instance")"
    TEST_SCOPES+=("$scope")
    pid="$(<"$pidfile")"
    server="$(/usr/bin/tmux -N -S "$SOCKET" display-message -p '#{pid}')"
    counter="$TEST_ROOT/counters/agent-stop"
    set +e
    output="$(fault_hubi_env "$scope" "$counter" 3 no \
        INSTANCE="$instance" REPO="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_agent_now codex "$REPO" "$INSTANCE"' 2>&1)"
    rc=$?
    set -e
    (( rc != 0 )) && [[ "$output" == *"podczas oczekiwania"* && -e "$counter/state.3" ]] \
        && /usr/bin/tmux -N -S "$SOCKET" has-session -t "=$session" 2>/dev/null \
        && scope_active "$scope" && kill -0 "$pid" 2>/dev/null \
        && [[ "$(/usr/bin/tmux -N -S "$SOCKET" display-message -p '#{pid}')" == "$server" ]]
    result=$?
    discard_work "$session" "$scope"
    return "$result"
}
check "agent stop preserves session scope and process on unknown wait state" \
    test_agent_stop_wait_query_error_preserves_work

test_existing_agent_start_query_error_preserves_work() {
    local instance=agent-start session scope pidfile pid counter output rc pane
    pidfile="$TEST_ROOT/agent-start.pid"
    create_stubborn_agent "$instance" "$pidfile" || return 1
    session="$(agent_session "$instance")"
    scope="$(agent_scope "$instance")"
    TEST_SCOPES+=("$scope")
    pid="$(<"$pidfile")"
    pane="$(/usr/bin/tmux -N -S "$SOCKET" show-option -qv -t "=$session:" @hubi-pane)"
    counter="$TEST_ROOT/counters/agent-start"
    set +e
    output="$(fault_hubi_env "$scope" "$counter" 1 no INSTANCE="$instance" REPO="$REPO_NAME" \
        AGENT_CODE='raise SystemExit(99)' HUBI_FILE="$HUBI" bash -c '
            source "$HUBI_FILE"
            resolve_repo "$REPO"
            HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session codex \
                "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" /usr/bin/python3 -c "$AGENT_CODE"
        ' 2>&1)"
    rc=$?
    set -e
    (( rc != 0 )) && [[ "$output" == *"odmowa zmiany sesji"* && -e "$counter/state.1" ]] \
        && /usr/bin/tmux -N -S "$SOCKET" has-session -t "=$session" 2>/dev/null \
        && [[ "$(/usr/bin/tmux -N -S "$SOCKET" show-option -qv -t "=$session:" @hubi-pane)" == "$pane" ]] \
        && scope_active "$scope" && kill -0 "$pid" 2>/dev/null
    result=$?
    discard_work "$session" "$scope"
    return "$result"
}
check "existing agent start preserves valid work on unknown state" \
    test_existing_agent_start_query_error_preserves_work

test_real_inactive_scope_state() {
    local scope="hubiv5-test-query-absent-$$.scope" result rc
    set +e
    result="$(HUBI_FILE="$HUBI" UNIT="$scope" bash -c \
        'source "$HUBI_FILE"; scope_state "$UNIT"')"
    rc=$?
    set -e
    [[ $rc -eq 0 && "$result" == INACTIVE ]]
}
check "a real absent/inactive scope is confirmed inactive" test_real_inactive_scope_state

test_real_active_scope_state() {
    local scope="hubiv5-test-query-active-$$.scope" runner result rc
    start_disposable_scope "$scope" sleep 300 || return 1
    runner="$LAST_SCOPE_RUNNER"
    set +e
    result="$(HUBI_FILE="$HUBI" UNIT="$scope" bash -c \
        'source "$HUBI_FILE"; scope_state "$UNIT"')"
    rc=$?
    set -e
    cleanup_scope "$scope"
    wait "$runner" 2>/dev/null || true
    [[ $rc -eq 0 && "$result" == ACTIVE ]]
}
check "a real active scope is confirmed active" test_real_active_scope_state

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
