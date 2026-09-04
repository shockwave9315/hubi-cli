#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
RUNTIME="$TEST_ROOT/runtime"
SOCKET="hubi-v4-tests-$$"
PREFIX="hubiv4test$$"
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPOS"
mkdir -m 700 "$RUNTIME"

cleanup() {
    local scope
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
    # Every startup lock and trusted-state record lives under RUNTIME
    # (HUBI_RUNTIME_DIR), a private disposable root, never the caller's real
    # lock/state directory (P3-02) — so tearing down TEST_ROOT is sufficient.
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
        HUBI_RUNTIME_DIR="$RUNTIME" \
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
    timeout 2 env -u HUBI_ACTIVE -u TMUX HUBI_REPOS="$REPOS" HUBI_TMUX_SOCKET="$SOCKET" HUBI_RUNTIME_DIR="$RUNTIME" \
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
        ensure_agent_session codex "$RESOLVED_REPO_DIR" "$CODEX_BIN"
    ' >"$TEST_ROOT/start-1.log" 2>&1 & local pid1=$!
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_DIR" "$CODEX_BIN"
    ' >"$TEST_ROOT/start-2.log" 2>&1 & local pid2=$!
    wait "$pid1"; rc1=$?
    wait "$pid2"; rc2=$?
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    session="$(hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; agent_session_name codex "$RESOLVED_REPO_DIR"')"
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
        ensure_agent_session claude "$RESOLVED_REPO_DIR" "$CRASH" || true
        session=$(agent_session_name claude "$RESOLVED_REPO_DIR")
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
        ensure_agent_session claude "$RESOLVED_REPO_DIR" "$TREE_AGENT" "$PIDFILE"
    ' || return 1
    for _ in {1..30}; do [[ -s "$pidfile" ]] && break; sleep 0.1; done
    [[ -s "$pidfile" ]] || return 1
    child="$(<"$pidfile")"
    kill -0 "$child" 2>/dev/null || return 1
    # Environment variables intentionally expand in child Bash.
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; stop_agent_now claude "$RESOLVED_REPO_DIR"
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

# --- P1-01: two different canonical roots sharing the same relative repo key
# must never collide on session, scope, lock, or trusted-state identity. ---
test_repo_root_collision() {
    local root_a="$TEST_ROOT/roots/alpha" root_b="$TEST_ROOT/roots/beta" name="shared-name"
    mkdir -p "$root_a" "$root_b"
    git init -q "$root_a/$name"
    git init -q "$root_b/$name"
    local session_a session_b
    # shellcheck disable=SC2016
    session_a="$(env -u HUBI_ACTIVE -u TMUX HUBI_REPOS="$root_a" HUBI_RUNTIME_DIR="$RUNTIME" HUBI_FILE="$HUBI" \
        bash -c 'source "$HUBI_FILE"; resolve_repo "$1"; agent_session_name codex "$RESOLVED_REPO_DIR"' _ "$name")"
    # shellcheck disable=SC2016
    session_b="$(env -u HUBI_ACTIVE -u TMUX HUBI_REPOS="$root_b" HUBI_RUNTIME_DIR="$RUNTIME" HUBI_FILE="$HUBI" \
        bash -c 'source "$HUBI_FILE"; resolve_repo "$1"; agent_session_name codex "$RESOLVED_REPO_DIR"' _ "$name")"
    [[ -n "$session_a" && -n "$session_b" && "$session_a" != "$session_b" ]]
}
check "same relative repo name under different canonical roots never collides" test_repo_root_collision

# --- P2-07: a repo path containing ':' must not hash the same as a
# different (repo, instance) split of the same characters. ---
test_tuple_ambiguity() {
    local repo="$PREFIX-repo-colon" hash_a hash_b
    mkdir -p "$REPOS/$repo"
    git init -q "$REPOS/$repo"
    # shellcheck disable=SC2016
    hash_a="$(hubi_env HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; compute_identity_hash "$1" codex primary' _ "$REPOS/team:review")"
    # shellcheck disable=SC2016
    hash_b="$(hubi_env HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; compute_identity_hash "$1" codex review' _ "$REPOS/team")"
    [[ -n "$hash_a" && -n "$hash_b" && "$hash_a" != "$hash_b" ]]
}
check "structurally distinct identities never collide on hash" test_tuple_ambiguity

# --- P1-03: a systemctl that cannot answer must never be read as inactive,
# and stop must never claim success while the scope's state is unknown. ---
test_systemctl_error_fails_closed() {
    cat >"$TEST_ROOT/systemctl-broken" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == kill && "$2" == --help ]]; then
    echo "--kill-whom=WHOM --signal=SIGNAL"
    exit 0
fi
exit 17
EOF
    chmod +x "$TEST_ROOT/systemctl-broken"
    local output rc
    # shellcheck disable=SC2016
    output="$(hubi_env HUBI_SYSTEMCTL_BIN="$TEST_ROOT/systemctl-broken" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; scope_state some-scope.scope; echo "rc=$?"
    ' 2>&1)"; rc=$?
    [[ $rc -eq 0 && "$output" == *"rc=2"* ]]
}
check "an unanswerable systemctl query is reported as error, not inactive" test_systemctl_error_fails_closed

test_terminate_scope_fails_closed_on_error() {
    cat >"$TEST_ROOT/systemctl-broken2" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == kill && "$2" == --help ]]; then
    echo "--kill-whom=WHOM --signal=SIGNAL"
    exit 0
fi
exit 17
EOF
    chmod +x "$TEST_ROOT/systemctl-broken2"
    local output rc
    # shellcheck disable=SC2016
    output="$(hubi_env HUBI_SYSTEMCTL_BIN="$TEST_ROOT/systemctl-broken2" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; terminate_scope some-scope.scope; echo "rc=$?"
    ' 2>&1)"; rc=$?
    [[ $rc -eq 0 && "$output" == *"rc=2"* && "$output" == *"fail-closed"* ]]
}
check "terminate_scope never claims success when scope state is unknown" test_terminate_scope_fails_closed_on_error

# --- P2-03: a tmux lookalike (predictable name, even with a managed marker
# and matching-looking metadata) must never be destroyed by name alone. ---
test_tmux_lookalike_survives_start() {
    local repo="$PREFIX-repo-lookalike" session
    mkdir -p "$REPOS/$repo"
    git init -q "$REPOS/$repo"
    # shellcheck disable=SC2016
    session="$(hubi_env HUBI_FILE="$HUBI" bash -c 'source "$HUBI_FILE"; resolve_repo "$1"; agent_session_name codex "$RESOLVED_REPO_DIR"' _ "$repo")"
    tmux -L "$SOCKET" new-session -d -s "$session" -- sleep 30
    tmux -L "$SOCKET" set-option -t "$session" @hubi-managed v4
    tmux -L "$SOCKET" set-option -t "$session" @hubi-agent codex
    local output rc
    # shellcheck disable=SC2016
    output="$(hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_DIR" "$CODEX_BIN"
    ' 2>&1)"; rc=$?
    [[ $rc -ne 0 ]] && tmux -L "$SOCKET" has-session -t "$session" 2>/dev/null
}
check "a predictable-name lookalike with partial metadata is never destroyed" test_tmux_lookalike_survives_start

# --- P2-08/P3-03: the interactive stale/unknown-objects menu lists a
# managed identity whose repository has vanished. (list_trusted_records,
# trusted_record_diagnosis, and stop_trusted_record are exercised directly,
# including an actual stop, in tests/lifecycle_hardening.py; a plain
# printf-piped run here only reliably delivers its first keystroke before
# Hubi's own burst-paste protection drains the rest, same as the existing
# pagination tests above, so this only checks the menu renders correctly.)
test_stale_objects_menu() {
    local repo="$PREFIX-repo-04" output
    # shellcheck disable=SC2016
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        ensure_agent_session codex "$RESOLVED_REPO_DIR" "$CODEX_BIN"
    ' >/dev/null 2>&1 || return 1
    rm -rf "${REPOS:?}/$repo"
    output="$(printf 'o\n' | hubi_env "$HUBI" 2>&1)" || true
    [[ "$output" == *"OSIEROCONE"* && "$output" == *"STALE"* ]]
}
check "stale objects menu lists a managed identity with a vanished repo" test_stale_objects_menu

# Stop the agent created by the startup race without touching any other socket.
# Environment variables intentionally expand in child Bash.
# shellcheck disable=SC2016
hubi_env REPO_NAME="$PREFIX-repo-01" HUBI_FILE="$HUBI" bash -c \
    'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; stop_agent_now codex "$RESOLVED_REPO_DIR"' >/dev/null 2>&1 || true

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
