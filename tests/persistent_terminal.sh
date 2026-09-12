#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
SOCKET="hubi-persistent-terminal-$$"
SOCKET_PATH="/tmp/tmux-$UID/$SOCKET"
TMUX_SERVICE="hubiv5-test-terminal-$$.service"
REPO_ONE="terminal-one-$$"
REPO_TWO="terminal-two-$$"
REPO_REPLACED="terminal-replaced-$$"
REPO_UI="terminal-ui-$$"
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPOS/$REPO_ONE" "$REPOS/$REPO_TWO" "$REPOS/$REPO_REPLACED" "$REPOS/$REPO_UI"
git init -q "$REPOS/$REPO_ONE"
git init -q "$REPOS/$REPO_TWO"
git init -q "$REPOS/$REPO_REPLACED"
git init -q "$REPOS/$REPO_UI"

cleanup() {
    local scope
    while IFS= read -r scope; do
        [[ "$scope" == hubi-*.scope ]] || continue
        systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
    done < <(tmux -L "$SOCKET" list-sessions -F '#{@hubi-scope}' 2>/dev/null || true)
    tmux -L "$SOCKET" kill-server >/dev/null 2>&1 || true
    v5_test_server_stop "$TMUX_SERVICE" || true
    if [[ -S "/tmp/tmux-$UID/$SOCKET" ]]; then unlink -- "/tmp/tmux-$UID/$SOCKET"; fi
    if [[ -n "$TEST_ROOT" && "$TEST_ROOT" == /tmp/* && -d "$TEST_ROOT" ]]; then
        find "$TEST_ROOT" -depth -delete
    fi
}
trap cleanup EXIT

cat >"$TEST_ROOT/tmux-clean" <<'EOF'
#!/usr/bin/env bash
exec tmux -f /dev/null "$@"
EOF
chmod +x "$TEST_ROOT/tmux-clean"

# The path is resolved from the runtime repository root.
# shellcheck disable=SC1091
source "$ROOT/tests/lib/v5_test_server.sh"
v5_test_server_start "$TMUX_SERVICE" "$SOCKET" || {
    printf 'Hubi test tmux service did not start.\n' >&2
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
        HUBI_TMUX_SOCKET_PATH="$SOCKET_PATH" \
        HUBI_TMUX_SERVICE="$TMUX_SERVICE" \
        HUBI_TMUX_BIN="$TEST_ROOT/tmux-clean" \
        HUBI_LOCK_ROOT="$TEST_ROOT/locks" \
        "$@"
}

terminal_name() {
    hubi_env REPO_NAME="$1" INSTANCE="$2" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_session_name "$REPO_NAME" "$INSTANCE"'
}

terminal_scope() {
    hubi_env REPO_NAME="$1" INSTANCE="$2" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_scope_name "$REPO_NAME" "$INSTANCE"'
}

terminal_lock() {
    hubi_env REPO_NAME="$1" INSTANCE="$2" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_lifecycle_lock_file "$REPO_NAME" "$INSTANCE"'
}

wait_for_barrier() {
    local directory="$1" marker
    for _ in {1..500}; do
        marker="$(find "$directory" -maxdepth 1 -type f ! -name '*.release' -print -quit 2>/dev/null)"
        if [[ -n "$marker" ]]; then
            BARRIER_MARKER="$marker"
            return 0
        fi
        sleep 0.01
    done
    return 1
}

launch_barrier_start() {
    local repo="$1" instance="$2" barrier_dir="$3" log="$4"
    hubi_env REPO_NAME="$repo" INSTANCE="$instance" HUBI_FILE="$HUBI" \
        HUBI_FLOCK_BIN="$ROOT/tests/flock_barrier_wrapper.sh" \
        HUBI_TEST_FLOCK_BARRIER_DIR="$barrier_dir" HUBI_LOCK_TIMEOUT=8 bash -c '
            source "$HUBI_FILE"
            attach_session() { :; }
            pause_for_ack() { :; }
            start_terminal "$REPO_NAME" "$INSTANCE"
        ' >"$log" 2>&1 &
    LAUNCHED_PID=$!
}

launch_barrier_stop() {
    local repo="$1" instance="$2" barrier_dir="$3" log="$4"
    hubi_env REPO_NAME="$repo" INSTANCE="$instance" HUBI_FILE="$HUBI" \
        HUBI_FLOCK_BIN="$ROOT/tests/flock_barrier_wrapper.sh" \
        HUBI_TEST_FLOCK_BARRIER_DIR="$barrier_dir" HUBI_LOCK_TIMEOUT=8 bash -c '
            source "$HUBI_FILE"
            stop_terminal_now "$REPO_NAME" "$INSTANCE"
        ' >"$log" 2>&1 &
    LAUNCHED_PID=$!
}

lock_fd_absent() {
    local lock_file="$1" pid fd_target
    shift
    for pid in "$@"; do
        [[ "$pid" =~ ^[1-9][0-9]*$ && -d "/proc/$pid/fd" ]] || return 1
        while IFS= read -r fd_target; do
            [[ "$fd_target" != "$lock_file" && "$fd_target" != "$lock_file (deleted)" ]] || return 1
        done < <(find "/proc/$pid/fd" -maxdepth 1 -type l -printf '%l\n' 2>/dev/null)
    done
}

create_terminal() {
    hubi_env REPO_NAME="$1" INSTANCE="$2" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        attach_session() { :; }
        start_terminal "$REPO_NAME" "$INSTANCE"
    '
}

terminal_exists() {
    tmux -L "$SOCKET" has-session -t "=$(terminal_name "$1" "$2")" 2>/dev/null
}

test_naming() {
    local one_primary one_matrix two_primary
    one_primary="$(terminal_name "$REPO_ONE" primary)"
    one_matrix="$(terminal_name "$REPO_ONE" matrix)"
    two_primary="$(terminal_name "$REPO_TWO" primary)"
    [[ "$one_primary" == hubi-terminal-* \
        && "$one_primary" != "$one_matrix" \
        && "$one_primary" != "$two_primary" \
        && "$one_primary" == "$(terminal_name "$REPO_ONE" primary)" ]]
}
check "terminal names are deterministic and structurally unique" test_naming

test_validation() {
    local value
    for value in primary matrix pytest release-test debug2 A_b-2; do
        hubi_env VALUE="$value" HUBI_FILE="$HUBI" bash -c \
            'source "$HUBI_FILE"; validate_instance_name "$VALUE"' || return 1
    done
    for value in '' '.hidden' 'bad name' '../matrix' 'matrix/test' 'bad!' \
        'abcdefghijklmnopqrstuvwxyz1234567'; do
        if hubi_env VALUE="$value" HUBI_FILE="$HUBI" bash -c \
            'source "$HUBI_FILE"; validate_instance_name "$VALUE"'; then
            return 1
        fi
    done
}
check "terminal instance names use the shared bounded grammar" test_validation

test_multiple_terminals() {
    local primary scope
    primary="$(terminal_name "$REPO_ONE" primary)"
    scope="$(terminal_scope "$REPO_ONE" primary)"
    create_terminal "$REPO_ONE" primary >/dev/null 2>&1 \
        && create_terminal "$REPO_ONE" matrix >/dev/null 2>&1 \
        && create_terminal "$REPO_ONE" primary >/dev/null 2>&1 \
        && terminal_exists "$REPO_ONE" primary \
        && terminal_exists "$REPO_ONE" matrix \
        && [[ "$(tmux -L "$SOCKET" list-sessions -F '#S' | grep -Fxc "$primary")" -eq 1 ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-managed)" == v5 ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-kind)" == terminal ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-repo)" == "$REPO_ONE" ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-instance)" == primary ]] \
        && [[ -z "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-agent)" ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-scope)" == "$scope" ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-pane)" == %* ]] \
        && [[ "$(tmux -L "$SOCKET" display-message -p -t "=$primary:" '#{pane_current_path}')" \
            == "$REPOS/$REPO_ONE" ]] \
        && [[ "$(tmux -L "$SOCKET" show-window-options -v -t "=$primary:" window-size)" == largest ]] \
        && [[ "$(hubi_env SESSION="$primary" HUBI_FILE="$HUBI" bash -c \
            'source "$HUBI_FILE"; session_status "$SESSION"')" == '● RUNNING' ]] \
        && systemctl --user is-active --quiet "$scope"
}
check "multiple named terminals coexist in one repository" test_multiple_terminals

test_new_windows_inherit_largest() {
    local session new_window
    session="$(terminal_name "$REPO_ONE" primary)"
    [[ "$(tmux -L "$SOCKET" show-window-options -v -t "=$session:" window-size)" == largest ]] \
        || return 1
    new_window="$(tmux -L "$SOCKET" new-window -d -P -F '#{window_id}' -t "=$session:" -- bash)" \
        || return 1
    [[ "$(tmux -L "$SOCKET" show-window-options -v -t "$new_window" window-size)" == largest ]]
}
check "new persistent-terminal windows inherit largest sizing" test_new_windows_inherit_largest

test_terminal_delayed_cwd_readiness() {
    local instance=cwd-delayed session log="$TEST_ROOT/terminal-delayed-cwd.log"
    local marker="$TEST_ROOT/terminal-delayed-cwd.marker" result
    session="$(terminal_name "$REPO_ONE" "$instance")"
    hubi_env REPO_NAME="$REPO_ONE" INSTANCE="$instance" HUBI_FILE="$HUBI" \
        HUBI_TMUX_BIN="$ROOT/tests/tmux_cwd_wrapper.sh" HUBI_TEST_REAL_TMUX="$TEST_ROOT/tmux-clean" \
        HUBI_TEST_CWD_LOG="$log" HUBI_TEST_CWD_MARKER="$marker" HUBI_TEST_CWD_MODE=delayed \
        bash -c '
            source "$HUBI_FILE"
            attach_session() { :; }
            pause_for_ack() { :; }
            start_terminal "$REPO_NAME" "$INSTANCE"
        ' >/dev/null 2>&1 || return 1
    [[ "$(wc -l <"$log")" -ge 2 ]] \
        && tmux -L "$SOCKET" has-session -t "=$session" 2>/dev/null \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$session:" @hubi-kind)" == terminal ]]
    result=$?
    hubi_env REPO_NAME="$REPO_ONE" INSTANCE="$instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1 || true
    return "$result"
}
check "persistent terminal waits for delayed tmux cwd readiness" test_terminal_delayed_cwd_readiness

test_terminal_wrong_cwd_cleanup() {
    local instance=cwd-wrong session sentinel="terminal-cwd-sentinel-$$" output rc result
    local log="$TEST_ROOT/terminal-wrong-cwd.log" marker="$TEST_ROOT/terminal-wrong-cwd.marker"
    session="$(terminal_name "$REPO_ONE" "$instance")"
    tmux -L "$SOCKET" new-session -d -s "$sentinel" -- sleep 30 || return 1
    output="$(hubi_env REPO_NAME="$REPO_ONE" INSTANCE="$instance" HUBI_FILE="$HUBI" \
        HUBI_TMUX_BIN="$ROOT/tests/tmux_cwd_wrapper.sh" HUBI_TEST_REAL_TMUX="$TEST_ROOT/tmux-clean" \
        HUBI_TEST_CWD_LOG="$log" HUBI_TEST_CWD_MARKER="$marker" HUBI_TEST_CWD_MODE=wrong \
        bash -c '
            source "$HUBI_FILE"
            attach_session() { :; }
            pause_for_ack() { :; }
            start_terminal "$REPO_NAME" "$INSTANCE"
        ' 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"tmux nie zachował katalogu repozytorium"* \
        && "$(wc -l <"$log")" -eq 11 ]] \
        && ! tmux -L "$SOCKET" has-session -t "=$session" 2>/dev/null \
        && tmux -L "$SOCKET" has-session -t "=$sentinel" 2>/dev/null
    result=$?
    tmux -L "$SOCKET" kill-session -t "=$sentinel" >/dev/null 2>&1 || true
    return "$result"
}
check "persistent terminal rejects persistent wrong cwd and removes only its new session" \
    test_terminal_wrong_cwd_cleanup

test_repository_identity_revalidation() {
    local session output rc marker="$TEST_ROOT/repository-replaced.marker"
    session="$(terminal_name "$REPO_REPLACED" identity-check)"
    output="$(hubi_env REPO_NAME="$REPO_REPLACED" REPO_PATH="$REPOS/$REPO_REPLACED" \
        HUBI_FILE="$HUBI" HUBI_TMUX_BIN="$ROOT/tests/tmux_repo_replace_wrapper.sh" \
        HUBI_TEST_REAL_TMUX="$TEST_ROOT/tmux-clean" HUBI_TEST_REPO_PATH="$REPOS/$REPO_REPLACED" \
        HUBI_TEST_REPLACE_MARKER="$marker" bash -c '
            source "$HUBI_FILE"
            attach_session() { :; }
            pause_for_ack() { :; }
            start_terminal "$REPO_NAME" identity-check
        ' 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"tożsamość repozytorium zmieniła się"* ]] \
        && ! tmux -L "$SOCKET" has-session -t "=$session" 2>/dev/null
}
check "repository replacement aborts and removes the new terminal" test_repository_identity_revalidation

test_stale_hubi_active_is_not_inherited() {
    local session pane_pid inherited=0
    tmux -L "$SOCKET" set-environment -g HUBI_ACTIVE 1 || return 1
    create_terminal "$REPO_ONE" stale-env >/dev/null 2>&1 || return 1
    session="$(terminal_name "$REPO_ONE" stale-env)"
    pane_pid="$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_pid}')"
    [[ "$pane_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    if tr '\0' '\n' <"/proc/$pane_pid/environ" | grep -q '^HUBI_ACTIVE='; then inherited=1; fi
    hubi_env REPO_NAME="$REPO_ONE" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" stale-env' || return 1
    (( inherited == 0 ))
}
check "new terminal Bash does not inherit stale HUBI_ACTIVE" test_stale_hubi_active_is_not_inherited

test_repo_isolation() {
    create_terminal "$REPO_TWO" primary >/dev/null 2>&1 \
        && terminal_exists "$REPO_ONE" primary \
        && terminal_exists "$REPO_TWO" primary \
        && [[ "$(terminal_name "$REPO_ONE" primary)" != "$(terminal_name "$REPO_TWO" primary)" ]]
}
check "the same terminal instance is isolated across repositories" test_repo_isolation

test_attach_persistence() {
    local session
    session="$(terminal_name "$REPO_ONE" primary)"
    python3 "$ROOT/tests/tmux_client.py" readonly "$SOCKET" "$session" DISCONNECTED \
        >/dev/null 2>&1 || return 1
    tmux -L "$SOCKET" has-session -t "=$session" 2>/dev/null \
        && [[ "$(tmux -L "$SOCKET" list-clients -t "=$session" -F '#{client_name}' 2>/dev/null | wc -l)" -eq 0 ]]
}
check "client detach and launcher disappearance preserve the terminal" test_attach_persistence

test_scoped_bash_interactivity() {
    local instance=interactive session scope state_file jobs_file ctrl_file output
    state_file="$TEST_ROOT/interactive-state"
    jobs_file="$TEST_ROOT/interactive-jobs"
    ctrl_file="$TEST_ROOT/interactive-ctrl"
    create_terminal "$REPO_TWO" "$instance" >/dev/null 2>&1 || return 1
    session="$(terminal_name "$REPO_TWO" "$instance")"
    scope="$(terminal_scope "$REPO_TWO" "$instance")"
    tmux -L "$SOCKET" send-keys -t "=$session:" \
        "printf '%s|%s|%s\\n' \"\$PWD\" \"\$(test -t 0 && echo TTY || echo NO_TTY)\" \"\$(set -o | awk '\$1 == \"monitor\" {print \$2}')\" >'$state_file'" Enter \
        || return 1
    tmux -L "$SOCKET" send-keys -t "=$session:" \
        "sleep 30 & jobs -p >'$jobs_file'" Enter || return 1
    for _ in {1..50}; do [[ -s "$state_file" && -s "$jobs_file" ]] && break; sleep 0.05; done
    [[ -s "$state_file" && -s "$jobs_file" ]] || return 1
    output="$(<"$state_file")"
    [[ "$output" == "$REPOS/$REPO_TWO|TTY|on" ]] || return 1
    [[ "$(<"$jobs_file")" =~ ^[1-9][0-9]*$ ]] || return 1

    tmux -L "$SOCKET" send-keys -t "=$session:" 'sleep 30' Enter || return 1
    for _ in {1..30}; do
        [[ "$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_current_command}')" == sleep ]] && break
        sleep 0.05
    done
    tmux -L "$SOCKET" send-keys -t "=$session:" C-c || return 1
    tmux -L "$SOCKET" send-keys -t "=$session:" "printf ALIVE >'$ctrl_file'" Enter || return 1
    for _ in {1..30}; do [[ -s "$ctrl_file" ]] && break; sleep 0.05; done
    [[ "$(<"$ctrl_file")" == ALIVE && "$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_current_command}')" == bash \
        && "$(tmux -L "$SOCKET" show-option -qv -t "=$session:" @hubi-scope)" == "$scope" ]] || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"'
}
check "scoped Bash keeps cwd PTY job control jobs and Ctrl-C behavior" test_scoped_bash_interactivity

test_terminal_descendant_cleanup_and_scope_isolation() {
    local target=stubborn sibling=scope-sibling target_session target_scope sibling_scope
    local agent_scope agent_session pidfile="$TEST_ROOT/terminal-descendant.pid" child result=0
    create_terminal "$REPO_TWO" "$target" >/dev/null 2>&1 || return 1
    create_terminal "$REPO_TWO" "$sibling" >/dev/null 2>&1 || return 1
    target_session="$(terminal_name "$REPO_TWO" "$target")"
    target_scope="$(terminal_scope "$REPO_TWO" "$target")"
    sibling_scope="$(terminal_scope "$REPO_TWO" "$sibling")"
    hubi_env REPO_NAME="$REPO_TWO" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" /usr/bin/sleep 30
    ' >/dev/null 2>&1 || return 1
    agent_scope="$(hubi_env REPO_NAME="$REPO_TWO" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex "$REPO_NAME"')"
    agent_session="$(hubi_env REPO_NAME="$REPO_TWO" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO_NAME"')"

    tmux -L "$SOCKET" send-keys -t "=$target_session:" \
        "setsid bash -c 'trap \"\" INT TERM; echo \$\$ >\"$pidfile\"; while :; do sleep 1; done' &" Enter \
        || return 1
    for _ in {1..50}; do [[ -s "$pidfile" ]] && break; sleep 0.05; done
    [[ -s "$pidfile" ]] || return 1
    child="$(<"$pidfile")"
    kill -0 "$child" 2>/dev/null || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$target" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' || return 1
    ! kill -0 "$child" 2>/dev/null || result=1
    ! systemctl --user is-active --quiet "$target_scope" || result=1
    systemctl --user is-active --quiet "$sibling_scope" || result=1
    systemctl --user is-active --quiet "$agent_scope" || result=1
    tmux -L "$SOCKET" has-session -t "=$(terminal_name "$REPO_TWO" "$sibling")" 2>/dev/null || result=1
    tmux -L "$SOCKET" has-session -t "=$agent_session" 2>/dev/null || result=1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$sibling" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1 || result=1
    hubi_env REPO_NAME="$REPO_TWO" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_agent_now codex "$REPO_NAME"' >/dev/null 2>&1 || result=1
    return "$result"
}
check "terminal stop kills stubborn descendants without touching sibling scopes" \
    test_terminal_descendant_cleanup_and_scope_isolation

test_orphan_terminal_scope_reconciliation() {
    local instance=orphan-scope session scope pidfile="$TEST_ROOT/orphan-terminal.pid" child listing
    create_terminal "$REPO_TWO" "$instance" >/dev/null 2>&1 || return 1
    session="$(terminal_name "$REPO_TWO" "$instance")"
    scope="$(terminal_scope "$REPO_TWO" "$instance")"
    tmux -L "$SOCKET" send-keys -t "=$session:" \
        "setsid bash -c 'trap \"\" HUP INT TERM; echo \$\$ >\"$pidfile\"; while :; do sleep 1; done' &" Enter \
        || return 1
    for _ in {1..50}; do [[ -s "$pidfile" ]] && break; sleep 0.05; done
    [[ -s "$pidfile" ]] || return 1
    child="$(<"$pidfile")"
    tmux -L "$SOCKET" kill-session -t "=$session" || return 1
    for _ in {1..30}; do systemctl --user is-active --quiet "$scope" && break; sleep 0.05; done
    systemctl --user is-active --quiet "$scope" || return 1
    listing="$(hubi_env REPO_NAME="$REPO_TWO" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_list "$REPO_NAME"')"
    [[ "$listing" == *"$instance"* ]] || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' || return 1
    ! kill -0 "$child" 2>/dev/null && ! systemctl --user is-active --quiet "$scope"
}
check "orphaned terminal scope remains discoverable and is reconciled exactly" \
    test_orphan_terminal_scope_reconciliation

test_orphan_terminal_user_flow() {
    local orphan=aaa-orphan sibling=zzz-sibling orphan_session orphan_scope sibling_scope
    local pidfile="$TEST_ROOT/ui-orphan-child.pid" output
    create_terminal "$REPO_UI" "$orphan" >/dev/null 2>&1 || return 1
    create_terminal "$REPO_UI" "$sibling" >/dev/null 2>&1 || return 1
    orphan_session="$(terminal_name "$REPO_UI" "$orphan")"
    orphan_scope="$(terminal_scope "$REPO_UI" "$orphan")"
    sibling_scope="$(terminal_scope "$REPO_UI" "$sibling")"
    tmux -L "$SOCKET" send-keys -t "=$orphan_session:" \
        "setsid bash -c 'trap \"\" HUP INT TERM; echo \$\$ >\"$pidfile\"; while :; do sleep 1; done' &" Enter \
        || return 1
    for _ in {1..50}; do [[ -s "$pidfile" ]] && break; sleep 0.05; done
    [[ -s "$pidfile" ]] || return 1
    tmux -L "$SOCKET" kill-session -t "=$orphan_session" || return 1
    systemctl --user is-active --quiet "$orphan_scope" || return 1

    if ! output="$(hubi_env REPO_NAME="$REPO_UI" HUBI_FILE="$HUBI" \
        python3 "$ROOT/tests/terminal_menu_driver.py" bash -c \
        'source "$HUBI_FILE"; terminals_menu "$REPO_NAME"' 2>&1)"; then
        printf '%s\n' "$output" >&2
        return 1
    fi
    [[ "$output" == *"ORPHANED"* ]] \
        && ! systemctl --user is-active --quiet "$orphan_scope" \
        && systemctl --user is-active --quiet "$sibling_scope" \
        && terminal_exists "$REPO_UI" "$sibling" || return 1
    hubi_env REPO_NAME="$REPO_UI" INSTANCE="$sibling" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1
}
check "terminal list and menu can confirm and clean an orphan without touching siblings" \
    test_orphan_terminal_user_flow

test_concurrent_terminal_start_start() {
    local instance=race-start barrier="$TEST_ROOT/barrier-start" session scope lock_file
    local first second first_marker second_marker first_pane second_pane server_pid cgroup pid rc1 rc2
    mkdir -p "$barrier"
    session="$(terminal_name "$REPO_TWO" "$instance")"
    scope="$(terminal_scope "$REPO_TWO" "$instance")"
    lock_file="$(terminal_lock "$REPO_TWO" "$instance")"
    launch_barrier_start "$REPO_TWO" "$instance" "$barrier" "$TEST_ROOT/start-start-1.log"
    first=$LAUNCHED_PID
    wait_for_barrier "$barrier" || return 1
    first_marker=$BARRIER_MARKER
    launch_barrier_start "$REPO_TWO" "$instance" "$barrier" "$TEST_ROOT/start-start-2.log"
    second=$LAUNCHED_PID
    sleep 0.15
    [[ "$(find "$barrier" -maxdepth 1 -type f ! -name '*.release' | wc -l)" -eq 1 ]] || return 1
    : >"$first_marker.release"
    wait "$first"; rc1=$?
    (( rc1 == 0 )) || return 1
    first_pane="$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_pid}')"
    rm -f -- "$first_marker" "$first_marker.release"
    wait_for_barrier "$barrier" || return 1
    second_marker=$BARRIER_MARKER
    : >"$second_marker.release"
    wait "$second"; rc2=$?
    second_pane="$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_pid}')"
    (( rc2 == 0 )) \
        && [[ "$first_pane" =~ ^[1-9][0-9]*$ && "$second_pane" == "$first_pane" ]] \
        && [[ "$(tmux -L "$SOCKET" list-sessions -F '#S' | grep -Fxc "$session")" -eq 1 ]] \
        && systemctl --user is-active --quiet "$scope" || return 1

    server_pid="$(tmux -N -L "$SOCKET" display-message -p '#{pid}')"
    cgroup="$(systemctl --user show "$scope" --property=ControlGroup --value)"
    lock_fd_absent "$lock_file" "$server_pid" || return 1
    while IFS= read -r pid; do lock_fd_absent "$lock_file" "$pid" || return 1; done \
        <"/sys/fs/cgroup$cgroup/cgroup.procs"
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1
}
check "concurrent terminal start/start preserves one winner and leaks no lifecycle lock fd" \
    test_concurrent_terminal_start_start

test_concurrent_terminal_start_stop() {
    local instance=race-stop sibling=race-stop-sibling barrier="$TEST_ROOT/barrier-stop"
    local start_pid stop_pid start_marker stop_marker session scope sibling_scope rc1 rc2
    mkdir -p "$barrier"
    create_terminal "$REPO_TWO" "$sibling" >/dev/null 2>&1 || return 1
    sibling_scope="$(terminal_scope "$REPO_TWO" "$sibling")"
    session="$(terminal_name "$REPO_TWO" "$instance")"
    scope="$(terminal_scope "$REPO_TWO" "$instance")"
    launch_barrier_start "$REPO_TWO" "$instance" "$barrier" "$TEST_ROOT/start-stop-start.log"
    start_pid=$LAUNCHED_PID
    wait_for_barrier "$barrier" || return 1
    start_marker=$BARRIER_MARKER
    launch_barrier_stop "$REPO_TWO" "$instance" "$barrier" "$TEST_ROOT/start-stop-stop.log"
    stop_pid=$LAUNCHED_PID
    sleep 0.15
    [[ "$(find "$barrier" -maxdepth 1 -type f ! -name '*.release' | wc -l)" -eq 1 ]] || return 1
    : >"$start_marker.release"
    wait "$start_pid"; rc1=$?
    (( rc1 == 0 )) || return 1
    rm -f -- "$start_marker" "$start_marker.release"
    wait_for_barrier "$barrier" || return 1
    stop_marker=$BARRIER_MARKER
    : >"$stop_marker.release"
    wait "$stop_pid"; rc2=$?
    (( rc2 == 0 )) \
        && ! tmux -L "$SOCKET" has-session -t "=$session" 2>/dev/null \
        && ! systemctl --user is-active --quiet "$scope" \
        && systemctl --user is-active --quiet "$sibling_scope" \
        && terminal_exists "$REPO_TWO" "$sibling" || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$sibling" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1
}
check "concurrent terminal start/stop is serialized without touching a sibling" \
    test_concurrent_terminal_start_stop

test_orphan_start_rechecks_under_lock() {
    local instance=race-orphan barrier="$TEST_ROOT/barrier-orphan" session scope old_child pidfile
    local first second first_marker second_marker new_pane final_pane rc1 rc2
    mkdir -p "$barrier"
    create_terminal "$REPO_TWO" "$instance" >/dev/null 2>&1 || return 1
    session="$(terminal_name "$REPO_TWO" "$instance")"
    scope="$(terminal_scope "$REPO_TWO" "$instance")"
    pidfile="$TEST_ROOT/race-orphan-child.pid"
    tmux -L "$SOCKET" send-keys -t "=$session:" \
        "setsid bash -c 'trap \"\" HUP INT TERM; echo \$\$ >\"$pidfile\"; while :; do sleep 1; done' &" Enter \
        || return 1
    for _ in {1..50}; do [[ -s "$pidfile" ]] && break; sleep 0.05; done
    [[ -s "$pidfile" ]] || return 1
    old_child="$(<"$pidfile")"
    tmux -L "$SOCKET" kill-session -t "=$session" || return 1
    systemctl --user is-active --quiet "$scope" || return 1

    launch_barrier_start "$REPO_TWO" "$instance" "$barrier" "$TEST_ROOT/orphan-start-1.log"
    first=$LAUNCHED_PID
    wait_for_barrier "$barrier" || return 1
    first_marker=$BARRIER_MARKER
    launch_barrier_start "$REPO_TWO" "$instance" "$barrier" "$TEST_ROOT/orphan-start-2.log"
    second=$LAUNCHED_PID
    sleep 0.15
    [[ "$(find "$barrier" -maxdepth 1 -type f ! -name '*.release' | wc -l)" -eq 1 ]] || return 1
    : >"$first_marker.release"
    wait "$first"; rc1=$?
    (( rc1 == 0 )) || return 1
    new_pane="$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_pid}')"
    [[ "$new_pane" =~ ^[1-9][0-9]*$ ]] && ! kill -0 "$old_child" 2>/dev/null || return 1
    rm -f -- "$first_marker" "$first_marker.release"
    wait_for_barrier "$barrier" || return 1
    second_marker=$BARRIER_MARKER
    : >"$second_marker.release"
    wait "$second"; rc2=$?
    final_pane="$(tmux -L "$SOCKET" display-message -p -t "=$session:" '#{pane_pid}')"
    (( rc2 == 0 )) && [[ "$final_pane" == "$new_pane" ]] \
        && terminal_exists "$REPO_TWO" "$instance" \
        && systemctl --user is-active --quiet "$scope" || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1
}
check "orphan/start rechecks under the lock and cannot ABA-kill the replacement" \
    test_orphan_start_rechecks_under_lock

test_terminal_sibling_locks_are_independent() {
    local held=sibling-lock-a free=sibling-lock-b barrier="$TEST_ROOT/barrier-siblings"
    local held_pid held_marker free_session free_scope
    mkdir -p "$barrier"
    launch_barrier_start "$REPO_TWO" "$held" "$barrier" "$TEST_ROOT/sibling-held.log"
    held_pid=$LAUNCHED_PID
    wait_for_barrier "$barrier" || return 1
    held_marker=$BARRIER_MARKER
    free_session="$(terminal_name "$REPO_TWO" "$free")"
    free_scope="$(terminal_scope "$REPO_TWO" "$free")"
    timeout 2 env -u HUBI_ACTIVE -u HUBI_AGENT_INSTANCE -u TMUX \
        HUBI_REPOS="$REPOS" HUBI_TMUX_SOCKET_PATH="$SOCKET_PATH" \
        HUBI_TMUX_SERVICE="$TMUX_SERVICE" HUBI_TMUX_BIN="$TEST_ROOT/tmux-clean" \
        HUBI_LOCK_ROOT="$TEST_ROOT/locks" REPO_NAME="$REPO_TWO" INSTANCE="$free" \
        HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        attach_session() { :; }
        pause_for_ack() { :; }
        start_terminal "$REPO_NAME" "$INSTANCE"
    ' >/dev/null 2>&1 || return 1
    terminal_exists "$REPO_TWO" "$free" && systemctl --user is-active --quiet "$free_scope" || return 1
    : >"$held_marker.release"
    wait "$held_pid" || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$held" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1 || return 1
    hubi_env REPO_NAME="$REPO_TWO" INSTANCE="$free" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" "$INSTANCE"' >/dev/null 2>&1 || return 1
    ! tmux -L "$SOCKET" has-session -t "=$free_session" 2>/dev/null
}
check "a terminal lifecycle lock does not serialize a sibling terminal" \
    test_terminal_sibling_locks_are_independent

test_exact_stop_isolation() {
    local matrix primary agent_codex agent_claude
    matrix="$(terminal_name "$REPO_ONE" matrix)"
    primary="$(terminal_name "$REPO_ONE" primary)"
    agent_codex="hubi-codex-test-$$"
    agent_claude="hubi-claude-test-$$"
    tmux -L "$SOCKET" new-session -d -s "$agent_codex" -- bash
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-managed v5
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-agent codex
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-repo "$REPO_ONE"
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-instance primary
    tmux -L "$SOCKET" new-session -d -s "$agent_claude" -- bash
    tmux -L "$SOCKET" set-option -t "=$agent_claude:" @hubi-managed v5
    tmux -L "$SOCKET" set-option -t "=$agent_claude:" @hubi-agent claude
    tmux -L "$SOCKET" set-option -t "=$agent_claude:" @hubi-repo "$REPO_ONE"
    tmux -L "$SOCKET" set-option -t "=$agent_claude:" @hubi-instance primary
    hubi_env REPO_NAME="$REPO_ONE" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_terminal_now "$REPO_NAME" matrix' || return 1
    ! tmux -L "$SOCKET" has-session -t "=$matrix" 2>/dev/null \
        && tmux -L "$SOCKET" has-session -t "=$primary" 2>/dev/null \
        && tmux -L "$SOCKET" has-session -t "=$agent_codex" 2>/dev/null \
        && tmux -L "$SOCKET" has-session -t "=$agent_claude" 2>/dev/null
}
check "exact terminal stop leaves siblings and agent sessions untouched" test_exact_stop_isolation

test_discovery() {
    local arbitrary="arbitrary-$$" foreign invalid listing
    foreign="$(terminal_name "$REPO_TWO" foreign)"
    invalid="$(terminal_name "$REPO_ONE" valid-name)"
    tmux -L "$SOCKET" new-session -d -s "$arbitrary" -- bash
    tmux -L "$SOCKET" new-session -d -s "$foreign" -- bash
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-managed v5
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-kind terminal
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-repo "$REPO_TWO"
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-instance foreign
    tmux -L "$SOCKET" new-session -d -s "$invalid" -- bash
    tmux -L "$SOCKET" set-option -t "=$invalid:" @hubi-managed v5
    tmux -L "$SOCKET" set-option -t "=$invalid:" @hubi-kind terminal
    tmux -L "$SOCKET" set-option -t "=$invalid:" @hubi-repo "$REPO_ONE"
    tmux -L "$SOCKET" set-option -t "=$invalid:" @hubi-instance 'bad name'
    listing="$(hubi_env REPO_NAME="$REPO_ONE" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_list "$REPO_NAME" | sort')"
    [[ "$listing" == primary ]]
}
check "discovery returns only valid managed terminals for one repository" test_discovery

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
