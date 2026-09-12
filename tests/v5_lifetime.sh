#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
REPO_NAME="lifetime-repo-$$"
SOCKET="$TEST_ROOT/runtime/tmux-$UID/hubi"
TMUX_SERVICE="hubiv5-test-lifetime-$$.service"
CLIENT_SCOPE="hubiv5-test-ssh-client-$$.scope"
CLIENT_RUNNER=""
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPOS/$REPO_NAME"
git init -q "$REPOS/$REPO_NAME"

# The path is resolved from the runtime repository root.
# shellcheck disable=SC1091
source "$ROOT/tests/lib/v5_test_server.sh"

cleanup() {
    local scope
    systemctl --user kill --kill-whom=all --signal=KILL "$CLIENT_SCOPE" >/dev/null 2>&1 || true
    if [[ -n "$CLIENT_RUNNER" ]]; then wait "$CLIENT_RUNNER" 2>/dev/null || true; fi
    while IFS= read -r scope; do
        [[ "$scope" == hubi-*.scope ]] || continue
        systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
    done < <(tmux -N -S "$SOCKET" list-sessions -F '#{@hubi-scope}' 2>/dev/null || true)
    tmux -N -S "$SOCKET" kill-server >/dev/null 2>&1 || true
    v5_test_server_stop "$TMUX_SERVICE" || true
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

cat >"$TEST_ROOT/agent" <<'EOF'
#!/usr/bin/env bash
printf 'AGENT_ALIVE\n'
trap 'exit 0' INT TERM
while :; do sleep 1; done
EOF
chmod +x "$TEST_ROOT/agent"

hubi_env() {
    env -u HUBI_ACTIVE -u HUBI_AGENT_INSTANCE -u HUBI_TMUX_SOCKET -u TMUX \
        HUBI_REPOS="$REPOS" \
        HUBI_TMUX_SOCKET_PATH="$SOCKET" \
        HUBI_TMUX_SERVICE="$TMUX_SERVICE" \
        HUBI_CODEX_BIN="$TEST_ROOT/agent" \
        "$@"
}

v5_test_server_start_path "$TMUX_SERVICE" "$SOCKET" || {
    printf 'Hubi test tmux service did not start.\n' >&2
    exit 1
}

test_scope_invocation_generation() (
    local scope="hubiv5-test-invocation-generation-$$.scope" runner="" first stable second
    # Invoked indirectly by the subshell EXIT trap.
    # shellcheck disable=SC2317
    cleanup_generation_scope() {
        systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
        [[ -z "$runner" ]] || wait "$runner" 2>/dev/null || true
    }
    trap cleanup_generation_scope EXIT

    systemd-run --user --scope --collect --quiet --unit="$scope" -- sleep 30 \
        >/dev/null 2>&1 & runner=$!
    for _ in {1..100}; do systemctl --user is-active --quiet "$scope" && break; sleep 0.02; done
    first="$(systemctl --user show "$scope" --property=InvocationID --value)"
    stable="$(systemctl --user show "$scope" --property=InvocationID --value)"
    [[ "$first" =~ ^[0-9a-fA-F]{32}$ && "$stable" == "$first" ]] || return 1
    systemctl --user kill --kill-whom=all --signal=KILL "$scope" || return 1
    wait "$runner" 2>/dev/null || true
    runner=""
    for _ in {1..100}; do systemctl --user show "$scope" >/dev/null 2>&1 || break; sleep 0.02; done

    systemd-run --user --scope --collect --quiet --unit="$scope" -- sleep 30 \
        >/dev/null 2>&1 & runner=$!
    for _ in {1..100}; do systemctl --user is-active --quiet "$scope" && break; sleep 0.02; done
    second="$(systemctl --user show "$scope" --property=InvocationID --value)"
    [[ "$second" =~ ^[0-9a-fA-F]{32}$ && "$second" != "$first" ]]
)
check "a reused transient scope name receives a new stable InvocationID generation" \
    test_scope_invocation_generation

create_managed_work() {
    hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$HUBI_CODEX_BIN"
        attach_session() { :; }
        start_terminal "$REPO_NAME" primary
    '
}

test_session_scope_independence() {
    local agent_session terminal_session agent_scope terminal_scope server_pid after_pid
    local terminal_marker="$TEST_ROOT/terminal-after-client-loss" client_cgroup commands pid
    create_managed_work >/dev/null 2>&1 || return 1
    agent_session="$(hubi_env HUBI_FILE="$HUBI" REPO_NAME="$REPO_NAME" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO_NAME"')"
    terminal_session="$(hubi_env HUBI_FILE="$HUBI" REPO_NAME="$REPO_NAME" bash -c \
        'source "$HUBI_FILE"; terminal_session_name "$REPO_NAME" primary')"
    agent_scope="$(tmux -N -S "$SOCKET" show-option -qv -t "=$agent_session:" @hubi-scope)"
    terminal_scope="$(tmux -N -S "$SOCKET" show-option -qv -t "=$terminal_session:" @hubi-scope)"
    server_pid="$(tmux -N -S "$SOCKET" display-message -p '#{pid}')"

    python3 "$ROOT/tests/scoped_tmux_client.py" "$CLIENT_SCOPE" "$SOCKET" "$terminal_session" &
    CLIENT_RUNNER=$!
    for _ in {1..100}; do
        if systemctl --user is-active --quiet "$CLIENT_SCOPE" \
            && [[ "$(tmux -N -S "$SOCKET" list-clients -t "=$terminal_session" -F '#{client_name}' 2>/dev/null | wc -l)" -ge 1 ]]; then
            break
        fi
        sleep 0.03
    done
    systemctl --user is-active --quiet "$CLIENT_SCOPE" || return 1
    [[ "$(tmux -N -S "$SOCKET" list-clients -t "=$terminal_session" -F '#{client_name}' | wc -l)" -ge 1 ]] \
        || return 1
    client_cgroup="$(systemctl --user show "$CLIENT_SCOPE" --property=ControlGroup --value)"
    commands=""
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/comm" ]] || continue
        commands+="$(<"/proc/$pid/comm") "
    done <"/sys/fs/cgroup$client_cgroup/cgroup.procs"
    [[ "$commands" == *"bash "* && "$commands" == *"tmux: client "* ]] || return 1

    systemctl --user kill --kill-whom=all --signal=KILL "$CLIENT_SCOPE" || return 1
    wait "$CLIENT_RUNNER" 2>/dev/null || true
    CLIENT_RUNNER=""
    for _ in {1..50}; do
        [[ "$(tmux -N -S "$SOCKET" list-clients -t "=$terminal_session" -F '#{client_name}' 2>/dev/null | wc -l)" -eq 0 ]] && break
        sleep 0.03
    done

    after_pid="$(tmux -N -S "$SOCKET" display-message -p '#{pid}')"
    [[ "$after_pid" == "$server_pid" ]] || return 1
    systemctl --user is-active --quiet "$TMUX_SERVICE" || return 1
    systemctl --user is-active --quiet "$agent_scope" || return 1
    systemctl --user is-active --quiet "$terminal_scope" || return 1
    tmux -N -S "$SOCKET" has-session -t "=$agent_session" || return 1
    tmux -N -S "$SOCKET" has-session -t "=$terminal_session" || return 1
    [[ "$(tmux -N -S "$SOCKET" capture-pane -p -t "=$agent_session:" 2>/dev/null)" == *AGENT_ALIVE* ]] \
        || return 1
    tmux -N -S "$SOCKET" send-keys -t "=$terminal_session:" \
        "printf SURVIVED >'$terminal_marker'" Enter || return 1
    for _ in {1..30}; do [[ -s "$terminal_marker" ]] && break; sleep 0.05; done
    [[ "$(<"$terminal_marker")" == SURVIVED ]]
}
check "fake SSH scope death leaves server agent and terminal scopes alive" \
    test_session_scope_independence

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
