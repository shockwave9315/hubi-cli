#!/usr/bin/env bash

v5_test_server_start() {
    local service="$1" socket_name="$2" config="${3:-/dev/null}" reported
    [[ "$service" == hubiv5-test-*.service ]] || return 2
    systemd-run --user --quiet --collect --service-type=simple --unit="$service" -- \
        /usr/bin/tmux -f "$config" -L "$socket_name" -D || return 1
    for _ in {1..100}; do
        reported="$(tmux -N -L "$socket_name" display-message -p '#{pid}' 2>/dev/null || true)"
        if [[ "$reported" =~ ^[1-9][0-9]*$ ]] \
            && systemctl --user is-active --quiet "$service"; then
            return 0
        fi
        sleep 0.02
    done
    return 1
}

v5_test_server_stop() {
    local service="$1"
    [[ "$service" == hubiv5-test-*.service ]] || return 2
    systemctl --user stop "$service" >/dev/null 2>&1 || true
    for _ in {1..50}; do
        systemctl --user is-active --quiet "$service" || return 0
        sleep 0.02
    done
    return 1
}

v5_test_server_start_path() {
    local service="$1" socket_path="$2" config="${3:-/dev/null}" reported
    [[ "$service" == hubiv5-test-*.service && "$socket_path" == /tmp/* ]] || return 2
    mkdir -p "$(dirname "$socket_path")" || return 1
    systemd-run --user --quiet --collect --service-type=simple --unit="$service" -- \
        /usr/bin/tmux -f "$config" -S "$socket_path" -D || return 1
    for _ in {1..100}; do
        reported="$(tmux -N -S "$socket_path" display-message -p '#{pid}' 2>/dev/null || true)"
        if [[ "$reported" =~ ^[1-9][0-9]*$ ]] \
            && systemctl --user is-active --quiet "$service"; then
            return 0
        fi
        sleep 0.02
    done
    return 1
}
