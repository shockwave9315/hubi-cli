#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
SOCKET="hubi-v4-tests-$$"
PREFIX="hubiv4test$$"
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPOS"

cleanup() {
    local scope agent repo digest lock_dir
    if command -v systemctl >/dev/null; then
        while IFS= read -r scope; do
            [[ "$scope" == hubi-*.scope ]] || continue
            systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
        done < <(tmux -L "$SOCKET" list-sessions -F '#{@hubi-scope}' 2>/dev/null || true)
    fi
    tmux -L "$SOCKET" kill-server >/dev/null 2>&1 || true
    if [[ -d "/tmp/tmux-$UID" ]]; then
        find "/tmp/tmux-$UID" -maxdepth 1 -type s -name "$SOCKET" -delete
    fi
    lock_dir="${XDG_RUNTIME_DIR:-/tmp}/hubi-locks-$UID"
    if [[ -d "$lock_dir" ]]; then
        for agent in codex claude; do
            for repo in "$PREFIX-repo-01" "$PREFIX-repo-02" "$PREFIX-repo-03"; do
                digest="$(printf '%s' "$agent:$repo" | sha256sum | cut -c1-12)"
                find "$lock_dir" -maxdepth 1 -type f -name "$digest.lock" -delete
            done
        done
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
    env -u HUBI_ACTIVE -u TMUX \
        HUBI_REPOS="$REPOS" \
        HUBI_TMUX_SOCKET="$SOCKET" \
        HUBI_CODEX_BIN="$TEST_ROOT/fake-agent" \
        HUBI_CLAUDE_BIN="$TEST_ROOT/fake-agent" \
        "$@"
}

make_repo() { git init -q "$REPOS/$1"; }

for number in $(seq -w 1 12); do make_repo "$PREFIX-repo-$number"; done

cat >"$TEST_ROOT/fake-agent" <<'EOF'
#!/usr/bin/env bash
trap 'exit 0' INT
while :; do sleep 1; done
EOF
chmod +x "$TEST_ROOT/fake-agent"

test_eof() {
    timeout 2 env -u HUBI_ACTIVE -u TMUX HUBI_REPOS="$REPOS" HUBI_TMUX_SOCKET="$SOCKET" \
        "$HUBI" </dev/null >/dev/null 2>&1
}
check "EOF exits without a loop" test_eof

for signal_spec in INT:130 HUP:129 TERM:143; do
    signal_name="${signal_spec%%:*}"
    expected_rc="${signal_spec##*:}"
    check "$signal_name has explicit exit semantics" hubi_env \
        python3 "$ROOT/tests/signal_runner.py" "$HUBI" "$signal_name" "$expected_rc"
done

test_nested() {
    local output rc
    output="$(HUBI_ACTIVE=1 "$HUBI" 2>&1)"; rc=$?
    [[ $rc -eq 125 && "$output" == *"zagnieżdżone"* ]]
}
check "nested HUBI_ACTIVE invocation is rejected" test_nested

test_safe_sequences() {
    local sequence output
    for sequence in $'\e[A' $'\e[B' $'\e[C' $'\e[D' $'\e[5~' $'\e[6~'; do
        output="$(printf '%s\n' "$sequence" | hubi_env "$HUBI" 2>&1)"
        [[ "$output" == *"Nieznany wybór"* ]] || return 1
        [[ "$output" != *"Zakończyć codex"* ]] || return 1
    done
}
check "typed arrows and PageUp/PageDown cannot invoke actions" test_safe_sequences

test_unknown_hint() {
    local output
    output="$(printf 'z\nq\n' | hubi_env "$HUBI" 2>&1)" || [[ $? -eq 98 ]]
    [[ "$output" == *"Nieznany wybór"* ]]
}
check "unknown keys display a hint" test_unknown_hint

test_attach_failure() {
    local output
    # Expansion is intentionally deferred to child Bash.
    # shellcheck disable=SC2016
    output="$(printf '\n' | env -u TMUX HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        tmux_cmd() {
            case "$1" in
                has-session) return 0 ;;
                list-clients) return 0 ;;
                attach-session) echo UNIQUE_TMUX_ATTACH_ERROR >&2; return 42 ;;
            esac
        }
        attach_session test-session
    ' 2>&1)" || true
    [[ "$output" == *"UNIQUE_TMUX_ATTACH_ERROR"* && "$output" == *"BŁĄD: nie udało się podłączyć"* && "$output" == *"Enter, aby kontynuować"* ]]
}
check "attach failure diagnostics remain visible" test_attach_failure

test_concurrent_start() {
    local repo="$PREFIX-repo-01" session count rc1 rc2
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$CODEX_BIN"
    ' >"$TEST_ROOT/start-1.log" 2>&1 & local pid1=$!
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$CODEX_BIN"
    ' >"$TEST_ROOT/start-2.log" 2>&1 & local pid2=$!
    wait "$pid1"; rc1=$?
    wait "$pid2"; rc2=$?
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    session="$(hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; agent_session_name codex "$REPO_NAME"')"
    count="$(tmux -L "$SOCKET" list-sessions -F '#S' | grep -Fxc "$session")"
    [[ $rc1 -eq 0 && $rc2 -eq 0 && $count -eq 1 ]]
}
check "concurrent startup converges on one session" test_concurrent_start

test_start_failure_evidence() {
    local repo="$PREFIX-repo-02" output
    cat >"$TEST_ROOT/crash-agent" <<'EOF'
#!/usr/bin/env bash
echo UNIQUE_AGENT_CRASH
exit 23
EOF
    chmod +x "$TEST_ROOT/crash-agent"
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    output="$(hubi_env REPO_NAME="$repo" CRASH="$TEST_ROOT/crash-agent" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session claude "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$CRASH" || true
        session=$(agent_session_name claude "$RESOLVED_REPO_KEY")
        printf "STATE=%s\n" "$(session_status "$session")"
        capture_session_output "$session"
    ' 2>&1)"
    [[ "$output" == *"UNIQUE_AGENT_CRASH"* && "$output" == *"STATE=⚠ EXITED"* ]]
}
check "startup failure remains as EXITED with final output" test_start_failure_evidence

test_worktree() {
    local source_repo="$REPOS/$PREFIX-source" worktree="$REPOS/$PREFIX-worktree" listing
    git init -q "$source_repo"
    git -C "$source_repo" -c user.name=Hubi -c user.email=hubi@example.invalid commit -q --allow-empty -m initial
    git -C "$source_repo" worktree add -q -b hubi-test-branch "$worktree"
    # HUBI_FILE intentionally expands in child Bash.
    # shellcheck disable=SC2016
    listing="$(hubi_env HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; repo_list')"
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    [[ -f "$worktree/.git" && "$listing" == *"$PREFIX-worktree"* ]] && \
        hubi_env REPO_NAME="$PREFIX-worktree" HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"'
}
check "Git worktrees are detected and validated" test_worktree

test_traversal() {
    mkdir -p "$TEST_ROOT/outside"
    git init -q "$TEST_ROOT/outside"
    local output rc
    # HUBI_FILE intentionally expands in child Bash.
    # shellcheck disable=SC2016
    output="$(hubi_env HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; resolve_repo ../outside' 2>&1)"; rc=$?
    [[ $rc -eq 2 && "$output" == *"nie może zawierać"* ]]
}
check "../ repository traversal is rejected" test_traversal

test_repo_pagination() {
    local output
    output="$(printf 'n\nq\n' | hubi_env "$HUBI" 2>&1)" || [[ $? -eq 98 ]]
    [[ "$output" == *"strona 2/"* && "$output" == *"$PREFIX-repo-10"* ]]
}
check "more than nine repositories are paginated" test_repo_pagination

test_session_pagination() {
    local number output
    for number in $(seq -w 1 12); do tmux -L "$SOCKET" new-session -d -s "$PREFIX-session-$number" -- sleep 30; done
    output="$(printf 'n\nb\n' | hubi_env "$HUBI" sessions 2>&1)"
    [[ "$output" == *"strona 2/"* && "$output" == *"$PREFIX-session-10"* ]]
}
check "more than nine sessions are paginated" test_session_pagination

test_descendant_cleanup() {
    local repo="$PREFIX-repo-03" pidfile="$TEST_ROOT/descendant.pid" child
    cat >"$TEST_ROOT/tree-agent" <<'EOF'
#!/usr/bin/env bash
trap '' INT TERM
setsid bash -c 'trap "" INT TERM; echo $$ >"$1"; while :; do sleep 1; done' _ "$1" &
while :; do sleep 1; done
EOF
    chmod +x "$TEST_ROOT/tree-agent"
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" PIDFILE="$pidfile" TREE_AGENT="$TEST_ROOT/tree-agent" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session claude "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$TREE_AGENT" "$PIDFILE"
    ' || return 1
    for _ in {1..30}; do [[ -s "$pidfile" ]] && break; sleep 0.1; done
    [[ -s "$pidfile" ]] || return 1
    child="$(<"$pidfile")"
    kill -0 "$child" 2>/dev/null || return 1
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; stop_agent_now claude "$REPO_NAME"
    ' || return 1
    ! kill -0 "$child" 2>/dev/null
}
check "agent stop kills descendants that changed process group" test_descendant_cleanup

test_readonly_client() {
    local session="$PREFIX-readonly" outfile="$TEST_ROOT/injected.txt"
    cat >"$TEST_ROOT/reader-agent" <<'EOF'
#!/usr/bin/env bash
while IFS= read -r line; do printf '%s\n' "$line" >>"$1"; done
EOF
    chmod +x "$TEST_ROOT/reader-agent"
    tmux -L "$SOCKET" new-session -d -s "$session" -- "$TEST_ROOT/reader-agent" "$outfile"
    python3 "$ROOT/tests/tmux_client.py" readonly "$SOCKET" "$session" FORBIDDEN
    [[ ! -e "$outfile" ]] || return 1
    python3 "$ROOT/tests/tmux_client.py" write "$SOCKET" "$session" ALLOWED
    for _ in {1..20}; do [[ -s "$outfile" ]] && break; sleep 0.05; done
    [[ "$(<"$outfile")" == "ALLOWED" ]]
}
check "read-only tmux client cannot inject input" test_readonly_client

# Stop the agent created by the startup race without touching any other socket.
# Environment variables intentionally expand in child Bash.
# shellcheck disable=SC2016
hubi_env REPO_NAME="$PREFIX-repo-01" HUBI_FILE="$HUBI" bash -c \
    'source "$HUBI_FILE"; stop_agent_now codex "$REPO_NAME"' >/dev/null 2>&1 || true

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
