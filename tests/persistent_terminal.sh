#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
SOCKET="hubi-persistent-terminal-$$"
REPO_ONE="terminal-one-$$"
REPO_TWO="terminal-two-$$"
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPOS/$REPO_ONE" "$REPOS/$REPO_TWO"
git init -q "$REPOS/$REPO_ONE"
git init -q "$REPOS/$REPO_TWO"

cleanup() {
    tmux -L "$SOCKET" kill-server >/dev/null 2>&1 || true
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

pass() { printf 'ok - %s\n' "$1"; ((PASS_COUNT += 1)); }
fail() { printf 'not ok - %s\n' "$1" >&2; ((FAIL_COUNT += 1)); }
check() {
    local name="$1"
    shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}

hubi_env() {
    env -u HUBI_ACTIVE -u TMUX \
        HUBI_REPOS="$REPOS" \
        HUBI_TMUX_SOCKET="$SOCKET" \
        HUBI_TMUX_BIN="$TEST_ROOT/tmux-clean" \
        "$@"
}

terminal_name() {
    hubi_env REPO_NAME="$1" INSTANCE="$2" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; terminal_session_name "$REPO_NAME" "$INSTANCE"'
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
    local primary
    primary="$(terminal_name "$REPO_ONE" primary)"
    create_terminal "$REPO_ONE" primary >/dev/null 2>&1 \
        && create_terminal "$REPO_ONE" matrix >/dev/null 2>&1 \
        && create_terminal "$REPO_ONE" primary >/dev/null 2>&1 \
        && terminal_exists "$REPO_ONE" primary \
        && terminal_exists "$REPO_ONE" matrix \
        && [[ "$(tmux -L "$SOCKET" list-sessions -F '#S' | grep -Fxc "$primary")" -eq 1 ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-managed)" == v4 ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-kind)" == terminal ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-repo)" == "$REPO_ONE" ]] \
        && [[ "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-instance)" == primary ]] \
        && [[ -z "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-agent)" ]] \
        && [[ -z "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-scope)" ]] \
        && [[ -z "$(tmux -L "$SOCKET" show-option -qv -t "=$primary:" @hubi-pane)" ]] \
        && [[ "$(tmux -L "$SOCKET" display-message -p -t "=$primary:" '#{pane_current_path}')" \
            == "$REPOS/$REPO_ONE" ]] \
        && [[ "$(tmux -L "$SOCKET" show-window-options -v -t "=$primary:" window-size)" == largest ]] \
        && [[ "$(hubi_env SESSION="$primary" HUBI_FILE="$HUBI" bash -c \
            'source "$HUBI_FILE"; session_status "$SESSION"')" == '● RUNNING' ]]
}
check "multiple named terminals coexist in one repository" test_multiple_terminals

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

test_exact_stop_isolation() {
    local matrix primary agent_codex agent_claude
    matrix="$(terminal_name "$REPO_ONE" matrix)"
    primary="$(terminal_name "$REPO_ONE" primary)"
    agent_codex="hubi-codex-test-$$"
    agent_claude="hubi-claude-test-$$"
    tmux -L "$SOCKET" new-session -d -s "$agent_codex" -- bash
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-managed v4
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-agent codex
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-repo "$REPO_ONE"
    tmux -L "$SOCKET" set-option -t "=$agent_codex:" @hubi-instance primary
    tmux -L "$SOCKET" new-session -d -s "$agent_claude" -- bash
    tmux -L "$SOCKET" set-option -t "=$agent_claude:" @hubi-managed v4
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
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-managed v4
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-kind terminal
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-repo "$REPO_TWO"
    tmux -L "$SOCKET" set-option -t "=$foreign:" @hubi-instance foreign
    tmux -L "$SOCKET" new-session -d -s "$invalid" -- bash
    tmux -L "$SOCKET" set-option -t "=$invalid:" @hubi-managed v4
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
