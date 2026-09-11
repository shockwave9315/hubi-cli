#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/config/tmux-server.conf"
SERVICE="$ROOT/systemd/user/hubi-tmux.service"
TEST_ROOT="$(mktemp -d)"
TEST_HOME="$TEST_ROOT/home"
SOCKET_DIR="$TEST_ROOT/runtime/tmux-$UID"
SOCKET="$SOCKET_DIR/hubi"
LOG="$TEST_ROOT/server.log"
SERVER_PID=""
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$TEST_HOME" "$SOCKET_DIR"

cleanup() {
    if [[ -n "$SERVER_PID" ]]; then kill "$SERVER_PID" >/dev/null 2>&1 || true; wait "$SERVER_PID" 2>/dev/null || true; fi
    if [[ -S "$SOCKET" ]]; then tmux -N -S "$SOCKET" kill-server >/dev/null 2>&1 || true; fi
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

cat >"$TEST_HOME/.tmux.conf" <<'EOF'
set -g history-limit 200000
set -g exit-empty on
set -g @hubiv5-user-config-sourced yes
EOF

client() { tmux -N -S "$SOCKET" "$@"; }

start_server() {
    HOME="$TEST_HOME" tmux -f "$CONFIG" -S "$SOCKET" -D >>"$LOG" 2>&1 &
    SERVER_PID=$!
    for _ in {1..50}; do
        if [[ -S "$SOCKET" ]] && client display-message -p '#{pid}' >/dev/null 2>&1; then return 0; fi
        kill -0 "$SERVER_PID" 2>/dev/null || return 1
        sleep 0.02
    done
    return 1
}

test_template_shape() {
    grep -Fxq 'source-file -q ~/.tmux.conf' "$CONFIG" \
        && [[ "$(tail -n 1 "$CONFIG")" == 'set -g exit-empty off' ]] \
        && grep -Fxq 'Type=simple' "$SERVICE" \
        && grep -Fxq 'ExecStartPre=/usr/bin/install -d -m 700 %t/tmux-%U' "$SERVICE" \
        && grep -Fxq 'ExecStart=/usr/bin/tmux -f %h/.config/hubi/tmux-server.conf -S %t/tmux-%U/hubi -D' "$SERVICE" \
        && grep -Fxq 'ExecStopPost=-/bin/rm -f %t/tmux-%U/hubi' "$SERVICE" \
        && grep -Fxq 'Restart=on-failure' "$SERVICE"
}
check "server templates preserve the audited lifetime ordering and semantics" test_template_shape

test_missing_parent_is_real() {
    local missing="$TEST_ROOT/probe/missing/hubi" output rc
    output="$(HOME="$TEST_HOME" timeout 1 tmux -f "$CONFIG" -S "$missing" -D 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"No such file or directory"* && ! -e "$(dirname "$missing")" ]]
}
check "tmux requires the service to create a clean socket parent" test_missing_parent_is_real

if ! start_server; then
    printf 'not ok - isolated foreground tmux server did not start\n' >&2
    exit 1
fi

test_config_order() {
    [[ "$(client show-options -gv @hubiv5-user-config-sourced)" == yes \
        && "$(client show-options -gv history-limit)" == 200000 \
        && "$(client show-options -gv exit-empty)" == off ]]
}
check "Hubi sources the user config then forces exit-empty off" test_config_order

test_empty_lifetime() {
    local reported before after
    before="$SERVER_PID"
    client new-session -d -s first -- sleep 30 || return 1
    reported="$(client display-message -p '#{pid}')"
    [[ "$reported" == "$before" ]] || return 1
    client kill-session -t '=first' || return 1
    sleep 0.15
    kill -0 "$before" 2>/dev/null || return 1
    after="$(client display-message -p '#{pid}')"
    [[ "$after" == "$before" ]] || return 1
    client new-session -d -s later -- sleep 30 || return 1
    [[ "$(client display-message -p '#{pid}')" == "$before" ]]
}
check "an empty server keeps its PID and accepts a later session" test_empty_lifetime

test_client_lifecycle() {
    local output
    client new-session -d -s lifecycle -- bash || return 1
    python3 "$ROOT/tests/tmux_client.py" write "$SOCKET" lifecycle CLIENT_LINE >/dev/null 2>&1 || return 1
    client send-keys -t '=lifecycle:' 'printf "SERVER_LINE\\n"' Enter || return 1
    for _ in {1..30}; do
        output="$(client capture-pane -p -t '=lifecycle:' 2>/dev/null || true)"
        [[ "$output" == *CLIENT_LINE* && "$output" == *SERVER_LINE* ]] && break
        sleep 0.05
    done
    [[ "$output" == *CLIENT_LINE* && "$output" == *SERVER_LINE* \
        && "$(client list-clients -t '=lifecycle' -F '#{client_name}' | wc -l)" -eq 0 ]]
}
check "new-session attach detach send-keys and capture use the anchored server" test_client_lifecycle

test_no_start_absent() {
    local absent="$TEST_ROOT/absent/hubi" rc
    tmux -N -S "$absent" new-session -d -s forbidden -- sleep 30 >/dev/null 2>&1; rc=$?
    [[ $rc -ne 0 && ! -e "$absent" ]]
}
check "tmux -N refuses creation when the server is absent" test_no_start_absent

test_stale_socket_recovery() {
    local killed replacement reported
    client kill-server >/dev/null 2>&1 || return 1
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""

    # A dead server may leave its private Unix socket behind after SIGKILL.
    HOME="$TEST_HOME" tmux -f "$CONFIG" -S "$SOCKET" -D >>"$LOG" 2>&1 &
    killed=$!
    for _ in {1..50}; do [[ -S "$SOCKET" ]] && break; sleep 0.02; done
    [[ -S "$SOCKET" ]] || return 1
    kill -KILL "$killed" || return 1
    wait "$killed" 2>/dev/null || true
    [[ -S "$SOCKET" ]] || return 1

    HOME="$TEST_HOME" tmux -f "$CONFIG" -S "$SOCKET" -D >>"$LOG" 2>&1 &
    replacement=$!
    SERVER_PID="$replacement"
    for _ in {1..50}; do
        reported="$(client display-message -p '#{pid}' 2>/dev/null || true)"
        [[ "$reported" == "$replacement" ]] && return 0
        kill -0 "$replacement" 2>/dev/null || return 1
        sleep 0.02
    done
    return 1
}
check "tmux safely replaces a stale private socket on restart" test_stale_socket_recovery

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
