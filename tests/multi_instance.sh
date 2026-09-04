#!/usr/bin/env bash
# Variables inside single-quoted bash -c scripts intentionally expand in the child.
# shellcheck disable=SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
REPOS="$TEST_ROOT/repos"
REPO_NAME="multi-instance-$$"
SOCKET="hubi-multi-instance-$$"
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPOS/$REPO_NAME"
git init -q "$REPOS/$REPO_NAME"

cleanup() {
    local scope lock_dir agent instance identity digest
    if command -v systemctl >/dev/null; then
        while IFS= read -r scope; do
            [[ "$scope" == hubi-*.scope ]] || continue
            systemctl --user kill --kill-whom=all --signal=KILL "$scope" >/dev/null 2>&1 || true
        done < <(tmux -L "$SOCKET" list-sessions -F '#{@hubi-scope}' 2>/dev/null || true)
    fi
    tmux -L "$SOCKET" kill-server >/dev/null 2>&1 || true
    lock_dir="${XDG_RUNTIME_DIR:-/tmp}/hubi-locks-$UID"
    if [[ -d "$lock_dir" ]]; then
        for agent in codex claude; do
            for instance in primary review upstream capture-one capture-two \
                argv-c-new argv-c-resume argv-a-new argv-a-resume; do
                if [[ "$instance" == primary ]]; then
                    identity="$agent:$REPO_NAME"
                    digest="$(printf '%s' "$identity" | sha256sum | cut -c1-12)"
                    rm -f -- "$lock_dir/$digest.lock"
                else
                    digest="$(printf '%s\0%s' "$REPO_NAME" "$instance" | sha256sum | cut -c1-12)"
                    rm -f -- "$lock_dir/$agent-$digest.lock"
                fi
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
        HUBI_TMUX_BIN="$TEST_ROOT/tmux-clean" \
        HUBI_CODEX_BIN="$TEST_ROOT/live-agent" \
        HUBI_CLAUDE_BIN="$TEST_ROOT/live-agent" \
        "$@"
}

cat >"$TEST_ROOT/tmux-clean" <<EOF
#!/usr/bin/env bash
exec tmux -f /dev/null "\$@"
EOF
chmod +x "$TEST_ROOT/tmux-clean"

cat >"$TEST_ROOT/live-agent" <<'EOF'
#!/usr/bin/env bash
printf 'READY:%s\n' "${1:-none}"
trap 'exit 0' INT TERM
while :; do sleep 1; done
EOF
chmod +x "$TEST_ROOT/live-agent"

session_name() {
    hubi_env AGENT="$1" INSTANCE="$2" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name "$AGENT" "$REPO_NAME" "$INSTANCE"'
}

scope_name() {
    hubi_env AGENT="$1" INSTANCE="$2" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name "$AGENT" "$REPO_NAME" "$INSTANCE"'
}

start_instance() {
    local agent="$1" instance="$2" marker="${3:-$2}"
    hubi_env AGENT="$agent" INSTANCE="$instance" MARKER="$marker" REPO_NAME="$REPO_NAME" \
        HUBI_FILE="$HUBI" bash -c '
            source "$HUBI_FILE"
            resolve_repo "$REPO_NAME"
            HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session "$AGENT" \
                "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$HUBI_CODEX_BIN" "$MARKER"
        '
}

scope_active() { systemctl --user is-active --quiet "$(scope_name "$1" "$2")"; }
session_exists() { tmux -L "$SOCKET" has-session -t "$(session_name "$1" "$2")" 2>/dev/null; }

test_naming() {
    local old_session old_scope primary_session primary_scope
    local codex_review codex_upstream claude_review
    old_session="hubi-codex-${REPO_NAME:0:28}-$(printf '%s' "$REPO_NAME" | sha256sum | cut -c1-12)"
    old_scope="hubi-codex-$(printf '%s' "$REPO_NAME" | sha256sum | cut -c1-12).scope"
    primary_session="$(session_name codex primary)"
    primary_scope="$(scope_name codex primary)"
    codex_review="$(session_name codex review)"
    codex_upstream="$(session_name codex upstream)"
    claude_review="$(session_name claude review)"
    [[ "$primary_session" == "$old_session" && "$primary_scope" == "$old_scope" ]] || return 1
    [[ "$primary_session" == "$(hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO_NAME"')" ]] || return 1
    [[ "$primary_scope" == "$(hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex "$REPO_NAME"')" ]] || return 1
    [[ "$codex_review" != "$primary_session" && "$codex_review" != "$codex_upstream" \
        && "$codex_review" != "$claude_review" ]] || return 1
    [[ "$(scope_name codex review)" != "$primary_scope" \
        && "$(scope_name codex review)" != "$(scope_name codex upstream)" \
        && "$(scope_name codex review)" != "$(scope_name claude review)" ]]
}
check "primary names are compatible and secondary identities are unique" test_naming

test_structural_identity_collision() {
    local primary_session secondary_session primary_scope secondary_scope
    local primary_lock secondary_lock primary_hash secondary_hash
    local lock_dir="${XDG_RUNTIME_DIR:-/tmp}/hubi-locks-$UID"
    local -a lock_paths=()
    git init -q "$REPOS/foo"
    git init -q "$REPOS/foo:bar"
    cat >"$TEST_ROOT/flock-recorder" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$7" >>"$LOCK_LOG"
EOF
    chmod +x "$TEST_ROOT/flock-recorder"
    primary_session="$(hubi_env HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "foo:bar" primary')"
    secondary_session="$(hubi_env HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex foo bar')"
    primary_scope="$(hubi_env HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex "foo:bar" primary')"
    secondary_scope="$(hubi_env HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex foo bar')"
    secondary_hash="$(hubi_env HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; instance_name_hash foo bar')"
    primary_hash="$(printf '%s' 'codex:foo:bar' | sha256sum | cut -c1-12)"
    hubi_env HUBI_FLOCK_BIN="$TEST_ROOT/flock-recorder" LOCK_LOG="$TEST_ROOT/lock-paths" \
        HUBI_FILE="$HUBI" bash -c '
            source "$HUBI_FILE"; resolve_repo "foo:bar"
            ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$HUBI_CODEX_BIN"
        ' || return 1
    hubi_env HUBI_FLOCK_BIN="$TEST_ROOT/flock-recorder" LOCK_LOG="$TEST_ROOT/lock-paths" \
        HUBI_FILE="$HUBI" bash -c '
            source "$HUBI_FILE"; resolve_repo foo
            HUBI_AGENT_INSTANCE=bar ensure_agent_session codex \
                "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$HUBI_CODEX_BIN"
        ' || return 1
    mapfile -t lock_paths <"$TEST_ROOT/lock-paths"
    primary_lock="${lock_paths[0]:-}"
    secondary_lock="${lock_paths[1]:-}"
    [[ "$primary_session" != "$secondary_session" \
        && "$primary_scope" != "$secondary_scope" \
        && "$primary_hash" != "$secondary_hash" \
        && "$primary_lock" == "$lock_dir/$primary_hash.lock" \
        && "$secondary_lock" == "$lock_dir/codex-$secondary_hash.lock" \
        && "$primary_lock" != "$secondary_lock" ]]
}
check "primary and secondary delimiter identities cannot collide" test_structural_identity_collision

test_invalid_names() {
    local value
    for value in '' 'bad name' '../review' 'review/test' '.review' 'review!' 'abcdefghijklmnopqrstuvwxyz1234567'; do
        if hubi_env VALUE="$value" HUBI_FILE="$HUBI" bash -c \
            'source "$HUBI_FILE"; validate_instance_name "$VALUE"'; then
            return 1
        fi
    done
    hubi_env HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; validate_instance_name primary && validate_instance_name opus-review && validate_instance_name test2'
}
check "instance names use the bounded human-readable grammar" test_invalid_names

start_instance codex primary primary >/dev/null 2>&1
start_instance codex review review >/dev/null 2>&1
start_instance codex upstream upstream >/dev/null 2>&1
start_instance claude review claude-review >/dev/null 2>&1

test_existing_primary_metadata_compatibility() {
    local primary found
    primary="$(session_name codex primary)"
    tmux -L "$SOCKET" set-option -u -t "$primary" @hubi-instance
    found="$(hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"
        find_agent_session codex "$REPO_NAME" primary || exit 1
        printf "%s|%s" "$FOUND_SESSION" "$FOUND_SESSION_KIND"
    ')"
    [[ "$found" == "$primary|managed" ]] && scope_active codex primary
}
check "existing v4 primary metadata remains compatible" test_existing_primary_metadata_compatibility

test_same_agent_siblings() {
    session_exists codex primary && session_exists codex review && session_exists codex upstream \
        && scope_active codex primary && scope_active codex review && scope_active codex upstream
}
check "same-agent sibling instances run simultaneously" test_same_agent_siblings

test_cross_agent_isolation() {
    session_exists codex review && session_exists claude review \
        && scope_active codex review && scope_active claude review \
        && [[ "$(session_name codex review)" != "$(session_name claude review)" ]] \
        && [[ "$(scope_name codex review)" != "$(scope_name claude review)" ]]
}
check "same-named Codex and Claude instances do not collide" test_cross_agent_isolation

hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
    'source "$HUBI_FILE"; stop_agent_now codex "$REPO_NAME" review' >/dev/null 2>&1

test_stop_isolation() {
    ! session_exists codex review && ! scope_active codex review \
        && session_exists codex primary && scope_active codex primary \
        && session_exists codex upstream && scope_active codex upstream \
        && session_exists claude review && scope_active claude review
}
check "stopping one instance leaves every sibling running" test_stop_isolation

test_capture_targets_instance_pane() {
    local first second first_output second_output
    cat >"$TEST_ROOT/crash-agent" <<'EOF'
#!/usr/bin/env bash
printf 'CAPTURE:%s\n' "$1"
exit 17
EOF
    chmod +x "$TEST_ROOT/crash-agent"
    for first in capture-one capture-two; do
        hubi_env INSTANCE="$first" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" \
            CRASH_AGENT="$TEST_ROOT/crash-agent" bash -c '
                source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
                HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session codex \
                    "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$CRASH_AGENT" "$INSTANCE"
            ' >/dev/null 2>&1 || true
    done
    first="$(session_name codex capture-one)"; second="$(session_name codex capture-two)"
    first_output="$(hubi_env SESSION="$first" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; capture_session_output "$SESSION"')"
    second_output="$(hubi_env SESSION="$second" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; capture_session_output "$SESSION"')"
    [[ "$first_output" == *'CAPTURE:capture-one'* && "$first_output" != *'CAPTURE:capture-two'* \
        && "$second_output" == *'CAPTURE:capture-two'* && "$second_output" != *'CAPTURE:capture-one'* ]]
}
check "EXITED capture uses the selected instance's pinned pane" test_capture_targets_instance_pane

test_exact_target_prefix_collision() {
    local repo="prefix-collision-$$" collision_instance primary_session collision_session
    local primary_scope collision_scope
    mkdir -p "$REPOS/$repo"
    git init -q "$REPOS/$repo"

    # An instance name equal to the repo's own name-hash makes the secondary
    # session name begin with the *exact* primary session name followed by a
    # "-": a real tmux prefix-match hazard, not merely two similar strings.
    collision_instance="$(hubi_env REPO="$repo" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; name_hash "$REPO"')"
    primary_session="$(hubi_env REPO="$repo" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO" primary')"
    collision_session="$(hubi_env REPO="$repo" INSTANCE="$collision_instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_session_name codex "$REPO" "$INSTANCE"')"
    primary_scope="$(hubi_env REPO="$repo" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex "$REPO" primary')"
    collision_scope="$(hubi_env REPO="$repo" INSTANCE="$collision_instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_scope_name codex "$REPO" "$INSTANCE"')"
    [[ "$collision_session" == "$primary_session"-* ]] || return 1

    # Start ONLY the collision-prone secondary; primary is never created, so
    # tmux's own prefix-matching is the one and only way anything could later
    # resolve "primary_session" to this sibling.
    hubi_env AGENT=codex INSTANCE="$collision_instance" REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session "$AGENT" \
            "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$HUBI_CODEX_BIN" "$INSTANCE"
    ' >/dev/null 2>&1 || return 1
    tmux -L "$SOCKET" has-session -t "=$collision_session" 2>/dev/null || return 1
    systemctl --user is-active --quiet "$collision_scope" || return 1

    # Now start PRIMARY for the first time. Before the exact-target fix,
    # tmux's prefix matching made Hubi believe the sibling *was* a stale
    # "primary" bootstrap session and kill it outright, while its scope kept
    # running untouched (a silent cross-instance kill).
    hubi_env AGENT=codex INSTANCE=primary REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session "$AGENT" \
            "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$HUBI_CODEX_BIN" "$INSTANCE"
    ' >/dev/null 2>&1 || return 1

    tmux -L "$SOCKET" has-session -t "=$primary_session" 2>/dev/null || return 1
    tmux -L "$SOCKET" has-session -t "=$collision_session" 2>/dev/null || return 1
    systemctl --user is-active --quiet "$collision_scope" || return 1
    systemctl --user is-active --quiet "$primary_scope" || return 1

    # Explicitly stopping the exact primary instance must target only that
    # session/scope, never the prefix-colliding sibling.
    hubi_env REPO_NAME="$repo" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; stop_agent_now codex "$REPO_NAME" primary' >/dev/null 2>&1 || return 1

    ! tmux -L "$SOCKET" has-session -t "=$primary_session" 2>/dev/null || return 1
    ! systemctl --user is-active --quiet "$primary_scope" || return 1
    tmux -L "$SOCKET" has-session -t "=$collision_session" 2>/dev/null || return 1
    systemctl --user is-active --quiet "$collision_scope" || return 1

    systemctl --user kill --kill-whom=all --signal=KILL "$collision_scope" >/dev/null 2>&1 || true
    rm -f -- "${XDG_RUNTIME_DIR:-/tmp}/hubi-locks-$UID/$(hubi_env REPO="$repo" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; name_hash "codex:$REPO"').lock" \
        "${XDG_RUNTIME_DIR:-/tmp}/hubi-locks-$UID/codex-$(hubi_env REPO="$repo" INSTANCE="$collision_instance" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; instance_name_hash "$REPO" "$INSTANCE"').lock" 2>/dev/null || true
    return 0
}
check "exact tmux targets survive a prefix-collision shape without cross-killing the sibling" test_exact_target_prefix_collision

test_orphan_secondary_discovery() {
    local instance="orphan-check-$$" session scope status entries

    # A real agent process routinely outlives a hung-up controlling
    # terminal; simulate that here so killing the tmux session detaches the
    # scope's process tree instead of tearing it down along with the pane.
    cat >"$TEST_ROOT/orphan-agent" <<'EOF'
#!/usr/bin/env bash
trap '' HUP
trap 'exit 0' INT TERM
printf 'READY:%s\n' "${1:-none}"
while :; do sleep 1; done
EOF
    chmod +x "$TEST_ROOT/orphan-agent"

    hubi_env AGENT=codex INSTANCE="$instance" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" \
        ORPHAN_AGENT="$TEST_ROOT/orphan-agent" bash -c '
        source "$HUBI_FILE"; resolve_repo "$REPO_NAME"
        HUBI_AGENT_INSTANCE="$INSTANCE" ensure_agent_session codex \
            "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$ORPHAN_AGENT" "$INSTANCE"
    ' >/dev/null 2>&1 || return 1

    session="$(session_name codex "$instance")"
    scope="$(scope_name codex "$instance")"
    session_exists codex "$instance" || return 1
    scope_active codex "$instance" || return 1

    # Remove only the tmux session; the systemd scope (and the process tree
    # it holds) is left running, exactly like a crashed/killed multiplexer
    # that never touched the supervised agent.
    tmux -L "$SOCKET" kill-session -t "=$session" || return 1
    ! session_exists codex "$instance" || return 1
    scope_active codex "$instance" || return 1

    status="$(hubi_env AGENT=codex INSTANCE="$instance" REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; agent_status "$AGENT" "$REPO_NAME" "$INSTANCE"')"
    [[ "$status" == *ORPHANED* ]] || return 1

    # The Instances menu is sourced from instance_list(); the orphan must
    # still be enumerated even though no tmux session carries its metadata.
    entries="$(hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; instance_list "$REPO_NAME"')"
    [[ "$entries" == *"codex|$instance"* ]] || return 1

    # Explicit user stop (the same kill_agent() path instance_menu's [x]
    # calls) must remove the exact orphaned scope on confirmation.
    printf 'y\n' | hubi_env REPO_NAME="$REPO_NAME" HUBI_FILE="$HUBI" bash -c \
        'source "$HUBI_FILE"; kill_agent codex "$REPO_NAME" "'"$instance"'"' >/dev/null 2>&1
    ! scope_active codex "$instance" || return 1

    # Siblings started earlier in this suite must remain untouched.
    session_exists codex primary && scope_active codex primary \
        && session_exists codex upstream && scope_active codex upstream \
        && session_exists claude review && scope_active claude review
}
check "orphaned secondary (tmux gone, scope alive) is discoverable and explicitly stoppable without touching siblings" \
    test_orphan_secondary_discovery

test_new_resume_argv() {
    local spec agent instance mode expected file content executable
    cat >"$TEST_ROOT/argv-agent" <<EOF
#!/usr/bin/env bash
: >"$TEST_ROOT/\${0##*/}.argv"
printf '%s\n' "\$@" >>"$TEST_ROOT/\${0##*/}.argv"
trap 'exit 0' INT TERM
while :; do sleep 1; done
EOF
    chmod +x "$TEST_ROOT/argv-agent"
    for spec in \
        'codex|argv-c-new|new|' \
        'codex|argv-c-resume|resume|resume' \
        'claude|argv-a-new|new|--permission-mode,bypassPermissions' \
        'claude|argv-a-resume|resume|--permission-mode,bypassPermissions,--resume'; do
        IFS='|' read -r agent instance mode expected <<<"$spec"
        file="$TEST_ROOT/$instance.argv"
        executable="$TEST_ROOT/$instance"
        cp "$TEST_ROOT/argv-agent" "$executable"
        hubi_env AGENT="$agent" INSTANCE="$instance" MODE="$mode" REPO_NAME="$REPO_NAME" \
            HUBI_FILE="$HUBI" HUBI_CODEX_BIN="$executable" HUBI_CLAUDE_BIN="$executable" bash -c '
                source "$HUBI_FILE"
                attach_session() { :; }
                start_agent "$AGENT" "$REPO_NAME" "$INSTANCE" "$MODE"
            ' >/dev/null 2>&1 || return 1
        for _ in {1..30}; do [[ -e "$file" ]] && break; sleep 0.05; done
        [[ -e "$file" ]] || return 1
        content="$(paste -sd, "$file")"
        [[ "$content" == "$expected" ]] || return 1
    done
}
check "Codex and Claude new/resume argv stay exact" test_new_resume_argv

start_instance codex review review >/dev/null 2>&1

cat >"$TEST_ROOT/tmux-attach-log" <<EOF
#!/usr/bin/env bash
for ((i = 1; i <= \$#; i++)); do
    if [[ "\${!i}" == attach-session ]]; then
        for ((j = i + 1; j <= \$#; j++)); do
            if [[ "\${!j}" == -t ]]; then
                ((j += 1)); printf '%s' "\${!j}" >"$TEST_ROOT/attach-target"; exit 42
            fi
        done
    fi
done
exec tmux -f /dev/null "\$@"
EOF
chmod +x "$TEST_ROOT/tmux-attach-log"

test_attach_and_launcher_disappearance() {
    local expected
    expected="$(session_name codex review)"
    env -u HUBI_ACTIVE -u TMUX HUBI_REPOS="$REPOS" HUBI_TMUX_SOCKET="$SOCKET" \
        HUBI_TMUX_BIN="$TEST_ROOT/tmux-attach-log" HUBI_CODEX_BIN="$TEST_ROOT/live-agent" \
        "$HUBI" codex "$REPO_NAME" review </dev/null >/dev/null 2>&1 || true
    # Hubi now targets tmux with the exact-match "=" form so a sibling
    # session cannot prefix-match; see the exact-target regressions below.
    [[ -f "$TEST_ROOT/attach-target" && "$(<"$TEST_ROOT/attach-target")" == "=$expected" ]] || return 1
    python3 "$ROOT/tests/tmux_client.py" readonly "$SOCKET" "$expected" DISCONNECTED >/dev/null 2>&1 || return 1
    session_exists codex review && scope_active codex review \
        && session_exists codex primary && scope_active codex primary \
        && session_exists codex upstream && scope_active codex upstream \
        && session_exists claude review && scope_active claude review
}
check "attach targets one instance and client/launcher loss preserves managed work" test_attach_and_launcher_disappearance

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
