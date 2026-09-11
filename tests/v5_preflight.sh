#!/usr/bin/env bash
# Variables inside the single-quoted bash -c script intentionally expand in the child.
# shellcheck disable=SC2016
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
[[ " $* " == *" -N -S $HUBI_TMUX_SOCKET_PATH "* ]] || exit 90
case " $* " in
    *" display-message -p #{pid} "*) printf '4242\n' ;;
    *) exit 91 ;;
esac
EOF

cat >"$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$HUBI_TEST_LOG"
case " $* " in
    *" --user show-environment "*) [[ "${HUBI_TEST_MANAGER:-up}" == up ]] ;;
    *" --user is-active --quiet "*) [[ "${HUBI_TEST_SERVICE:-active}" == active ]] ;;
    *" kill --help "*) printf '%s\n' '  --kill-whom=WHOM --signal=SIGNAL' ;;
    *) exit 92 ;;
esac
EOF

cat >"$BIN/loginctl" <<'EOF'
#!/usr/bin/env bash
printf 'loginctl %s\n' "$*" >>"$HUBI_TEST_LOG"
[[ "${HUBI_TEST_LOGINCTL:-ok}" == ok ]] || exit 1
case " $* " in
    *" --property=Linger --value "*) printf '%s\n' "${HUBI_TEST_LINGER:-yes}" ;;
    *" --property=KillProcesses --value "*) printf '%s\n' "${HUBI_TEST_KILL_PROCESSES:-yes}" ;;
    *) exit 93 ;;
esac
EOF
chmod +x "$BIN/tmux" "$BIN/systemctl" "$BIN/loginctl"

preflight_env() {
    env -u HUBI_ACTIVE -u HUBI_TMUX_SOCKET -u TMUX \
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

run_preflight() {
    preflight_env HUBI_FILE="$HUBI" "$@" bash -c \
        'source "$HUBI_FILE"; runtime_creation_preflight'
}

test_linger_yes() {
    : >"$LOG"
    set_cgroup hubi-tmux.service
    run_preflight HUBI_TEST_LINGER=yes HUBI_TEST_KILL_PROCESSES=yes >/dev/null 2>&1 \
        && ! grep -Fq -- '--property=KillProcesses' "$LOG"
}
check "Linger=yes permits creation and KillUserProcesses is not a gate" test_linger_yes

test_linger_no() {
    local output rc
    : >"$LOG"
    set_cgroup hubi-tmux.service
    output="$(run_preflight HUBI_TEST_LINGER=no 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"Linger=no"* \
        && "$output" == *"loginctl enable-linger"* ]] \
        && ! grep -Fq 'display-message' "$LOG"
}
check "Linger=no fails closed with explicit remediation" test_linger_no

test_linger_error() {
    local output rc
    : >"$LOG"
    set_cgroup hubi-tmux.service
    output="$(run_preflight HUBI_TEST_LOGINCTL=error 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"nie można potwierdzić stanu Linger"* ]] \
        && ! grep -Fq 'display-message' "$LOG"
}
check "a linger query error fails closed deterministically" test_linger_error

test_broken_bus() {
    local output rc
    : >"$LOG"
    set_cgroup hubi-tmux.service
    output="$(run_preflight HUBI_TEST_MANAGER=down 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"systemd --user jest niedostępna"* \
        && "$output" == *"odmowa utworzenia pracy"* ]] \
        && grep -Fq 'systemctl --user show-environment' "$LOG" \
        && ! grep -Fq 'display-message' "$LOG"
}
check "a broken user bus fails despite systemctl kill help support" test_broken_bus

test_inactive_service() {
    local output rc
    : >"$LOG"
    set_cgroup hubi-tmux.service
    output="$(run_preflight HUBI_TEST_SERVICE=inactive 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"dedykowany serwer tmux"* ]] \
        && ! grep -Fq 'display-message' "$LOG"
}
check "an inactive tmux service fails the runtime preflight" test_inactive_service

test_wrong_owner() {
    local output rc
    : >"$LOG"
    set_cgroup hubiv5-test-other.service
    output="$(run_preflight 2>&1)"; rc=$?
    [[ $rc -ne 0 && "$output" == *"dedykowany serwer tmux"* ]] \
        && grep -Fq 'display-message' "$LOG"
}
check "an ownership mismatch fails the runtime preflight" test_wrong_owner

printf '\n%d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
(( FAIL_COUNT == 0 ))
