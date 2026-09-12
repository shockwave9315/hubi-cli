#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUBI="$ROOT/hubi"
TEST_ROOT="$(mktemp -d)"
BIN="$TEST_ROOT/bin"
PROC_ROOT="$TEST_ROOT/proc"
SOCKET="$TEST_ROOT/runtime/tmux-$UID/hubi"
LOG="$TEST_ROOT/calls.log"
PASS_COUNT=0
FAIL_COUNT=0

mkdir -p "$BIN" "$PROC_ROOT/4242" "$(dirname "$SOCKET")"

cleanup() {
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

cat >"$BIN/tmux" <<'EOF'
#!/usr/bin/env bash
printf 'tmux %s\n' "$*" >>"$HUBI_TEST_LOG"
if [[ "${1:-}" == -V ]]; then printf 'tmux 3.6b\n'; exit 0; fi
[[ " $* " == *" -N -S $HUBI_TMUX_SOCKET_PATH "* ]] || exit 90
case " $* " in
    *" display-message -p #{pid} "*) [[ "${HUBI_TEST_TMUX_STATE:-up}" == up ]] || exit 1; printf '4242\n' ;;
    *" show-options -gv exit-empty "*) [[ "${HUBI_TEST_TMUX_STATE:-up}" == up ]] || exit 1; printf '%s\n' "${HUBI_TEST_EXIT_EMPTY:-off}" ;;
    *) exit 91 ;;
esac
EOF

cat >"$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$HUBI_TEST_LOG"
case " $* " in
    *" --user show-environment "*) [[ "${HUBI_TEST_MANAGER:-up}" == up ]] ;;
    *" --user show "*)
        [[ "${HUBI_TEST_MANAGER:-up}" == up ]] || exit 1
        printf 'MainPID=%s\nControlGroup=%s\nActiveState=%s\n' \
            "${HUBI_TEST_MAIN_PID:-4242}" \
            "${HUBI_TEST_CONTROL_GROUP:-/user.slice/user-$UID.slice/user@$UID.service/app.slice/hubi-tmux.service}" \
            "${HUBI_TEST_SERVICE:-active}"
        ;;
    *" kill --help "*) printf '%s\n' '  --kill-whom=WHOM --signal=SIGNAL';;
    *" --user list-units --all --type=scope --no-legend --plain "*)
        printf '%s\n' 'hubi-codex-test.scope loaded active running' \
            'unrelated.scope loaded active running'
        ;;
    *) exit 92 ;;
esac
EOF

cat >"$BIN/loginctl" <<'EOF'
#!/usr/bin/env bash
printf 'loginctl %s\n' "$*" >>"$HUBI_TEST_LOG"
[[ "${HUBI_TEST_LOGINCTL:-ok}" == ok ]] || exit 1
case " $* " in
    *" --property=Linger --value "*) printf '%s\n' "${HUBI_TEST_LINGER:-yes}" ;;
    *" --property=KillProcesses --value "*)
        [[ "${HUBI_TEST_KILL_QUERY:-ok}" == ok ]] || exit 1
        printf '%s\n' "${HUBI_TEST_KILL_PROCESSES:-no}"
        ;;
    *) exit 93 ;;
esac
EOF
chmod +x "$BIN/tmux" "$BIN/systemctl" "$BIN/loginctl"

doctor_env() {
    env -u HUBI_ACTIVE -u TMUX \
        HUBI_TMUX_BIN="$BIN/tmux" \
        HUBI_SYSTEMCTL_BIN="$BIN/systemctl" \
        HUBI_LOGINCTL_BIN="$BIN/loginctl" \
        HUBI_TMUX_SOCKET_PATH="$SOCKET" \
        HUBI_PROC_ROOT="$PROC_ROOT" \
        HUBI_TEST_LOG="$LOG" \
        "$@"
}

set_cgroup() {
    printf '0::/user.slice/user-%s.slice/user@%s.service/app.slice/%s\n' \
        "$UID" "$UID" "$1" >"$PROC_ROOT/4242/cgroup"
}

test_pass_report() {
    local output
    : >"$LOG"
    set_cgroup hubi-tmux.service
    output="$(doctor_env "$HUBI" doctor 2>&1)" || return 1
    [[ "$output" == *"Hubi version:             5"* \
        && "$output" == *"Linger:                   yes"* \
        && "$output" == *"KillUserProcesses:        no (information only)"* \
        && "$output" == *"server ownership:         PASS"* \
        && "$output" == *"exit-empty:               off"* \
        && "$output" == *"hubi-codex-test.scope"* \
        && "$output" != *"unrelated.scope"* \
        && "$output" == *"Summary: PASS"* ]] \
        && grep -Fq "tmux -N -S $SOCKET display-message" "$LOG" \
        && grep -Fq "tmux -N -S $SOCKET show-options" "$LOG"
}
check "doctor reports a fully anchored ready server" test_pass_report

test_linger_no() {
    local output rc
    set_cgroup hubi-tmux.service
    output="$(doctor_env HUBI_TEST_LINGER=no "$HUBI" doctor 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"Linger:                   no"* \
        && "$output" == *"loginctl enable-linger"* && "$output" == *"Summary: FAIL"* ]]
}
check "doctor fails closed on Linger=no with remediation" test_linger_no

test_linger_query_error() {
    local output rc
    set_cgroup hubi-tmux.service
    output="$(doctor_env HUBI_TEST_LOGINCTL=error "$HUBI" doctor 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"Linger:                   unknown"* \
        && "$output" == *"creation must fail closed"* ]]
}
check "doctor handles a linger query error deterministically" test_linger_query_error

test_informational_warning() {
    local output
    set_cgroup hubi-tmux.service
    output="$(doctor_env HUBI_TEST_KILL_QUERY=error "$HUBI" doctor 2>&1)" || return 1
    [[ "$output" == *"KillUserProcesses:        unknown (information only)"* \
        && "$output" == *"Summary: WARN"* ]]
}
check "doctor warns but does not gate on unknown KillUserProcesses" test_informational_warning

test_broken_user_bus() {
    local output rc
    set_cgroup hubi-tmux.service
    output="$(doctor_env HUBI_TEST_MANAGER=down "$HUBI" doctor 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"systemd user manager:     unreachable"* \
        && "$output" == *"full-cgroup kill:         unavailable"* ]]
}
check "doctor rejects a broken user bus despite successful systemctl help" test_broken_user_bus

test_wrong_owner() {
    local output rc
    set_cgroup hubiv5-test-wrong.service
    output="$(doctor_env "$HUBI" doctor 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"server ownership:         FAIL"* \
        && "$output" == *"hubiv5-test-wrong.service"* ]]
}
check "doctor rejects a server with the wrong cgroup owner" test_wrong_owner

test_read_only_absent_server() {
    local output rc before after
    : >"$LOG"
    before="$(find "$TEST_ROOT" -mindepth 1 -printf '%P %y\n' | sort)"
    output="$(doctor_env HUBI_TEST_SERVICE=inactive HUBI_TEST_MAIN_PID=0 \
        HUBI_TEST_CONTROL_GROUP= HUBI_TEST_TMUX_STATE=down "$HUBI" doctor 2>&1)"; rc=$?
    after="$(find "$TEST_ROOT" -mindepth 1 -printf '%P %y\n' | sort)"
    [[ $rc -ne 0 && "$output" == *"hubi-tmux.service:      inactive"* \
        && "$output" == *"Summary: FAIL"* && "$before" == "$after" \
        && ! -e "$SOCKET" ]] \
        && ! grep -Eq 'tmux .* (new-session|start-server|set-|kill-)|systemctl .* (start|enable|kill)' "$LOG"
}
check "doctor is read-only and cannot spawn an absent server" test_read_only_absent_server

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
