#!/usr/bin/env bash

set -u

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
LAUNCHER="$ROOT/bin/forza-linux"

status=0
tests_run=0
tests_failed=0

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    return 1
}

assert_eq() {
    [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"
}

assert_log_contains() {
    rg -Fq -- "$1" "$FAKE_LOG" || fail "log does not contain [$1]"
}

assert_log_not_contains() {
    ! rg -Fq -- "$1" "$FAKE_LOG" || fail "log unexpectedly contains [$1]"
}

wait_for_file() {
    local path=$1
    local attempts=50
    while ((attempts > 0)); do
        [[ -e "$path" || -S "$path" ]] && return 0
        sleep 0.02
        ((attempts -= 1))
    done
    fail "timed out waiting for [$path]"
}

start_socket() {
    local socket_path=$1
    "$SOCKET_SERVER" "$socket_path" &
    SOCKET_PIDS+=("$!")
    wait_for_file "$socket_path"
}

set_up() {
    TEST_ROOT=$(mktemp -d)
    export XDG_RUNTIME_DIR="$TEST_ROOT/runtime"
    mkdir -p "$XDG_RUNTIME_DIR"
    FAKE_LOG="$TEST_ROOT/systemctl.log"
    : > "$FAKE_LOG"
    export FAKE_LOG
    export FAKE_SOCKET="$XDG_RUNTIME_DIR/xodus.sock"
    export FAKE_PID_FILE="$TEST_ROOT/xodus.pid"
    export FAKE_SERVICE_ACTIVE=${FAKE_SERVICE_ACTIVE:-0}
    export FAKE_CREATE_SOCKET=${FAKE_CREATE_SOCKET:-1}
    export FAKE_STOP_STATUS=${FAKE_STOP_STATUS:-0}
    SOCKET_PIDS=()

    SOCKET_SERVER="$TEST_ROOT/socket-server"
    cat > "$SOCKET_SERVER" <<'EOF'
#!/usr/bin/env bash
exec uv run python - "$1" <<'PY'
import os
import signal
import socket
import sys

path = sys.argv[1]
try:
    os.unlink(path)
except FileNotFoundError:
    pass
listener = socket.socket(socket.AF_UNIX)
listener.bind(path)
listener.listen(1)
try:
    signal.pause()
finally:
    listener.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
PY
EOF
    chmod +x "$SOCKET_SERVER"
    export SOCKET_SERVER

    FAKE_SYSTEMCTL="$TEST_ROOT/systemctl"
    cat > "$FAKE_SYSTEMCTL" <<'EOF'
#!/usr/bin/env bash
set -u

[[ ${1:-} == --user ]] && shift
command=${1:-}
shift || true

case "$command" in
    is-active)
        [[ ${FAKE_SERVICE_ACTIVE:-0} == 1 ]] && exit 0
        exit 3
        ;;
    start)
        unit=${!#}
        printf 'start %s\n' "$unit" >> "$FAKE_LOG"
        if [[ $unit == xodus-forza.service && ${FAKE_CREATE_SOCKET:-1} == 1 ]]; then
            "$SOCKET_SERVER" "$FAKE_SOCKET" &
            printf '%s\n' "$!" > "$FAKE_PID_FILE"
        fi
        ;;
    stop)
        unit=${!#}
        printf 'stop %s\n' "$unit" >> "$FAKE_LOG"
        if [[ $unit == xodus-forza.service && -f $FAKE_PID_FILE ]]; then
            kill "$(<"$FAKE_PID_FILE")" 2>/dev/null || true
            rm -f -- "$FAKE_PID_FILE" "$FAKE_SOCKET"
        fi
        exit "${FAKE_STOP_STATUS:-0}"
        ;;
    *)
        printf 'unexpected %s\n' "$command" >> "$FAKE_LOG"
        exit 64
        ;;
esac
EOF
    chmod +x "$FAKE_SYSTEMCTL"
    export FORZA_SYSTEMCTL="$FAKE_SYSTEMCTL"

    if [[ $FAKE_SERVICE_ACTIVE == 1 ]]; then
        start_socket "$FAKE_SOCKET"
    fi
}

tear_down() {
    local pid
    for pid in "${SOCKET_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    if [[ -f ${FAKE_PID_FILE:-} ]]; then
        kill "$(<"$FAKE_PID_FILE")" 2>/dev/null || true
    fi
    rm -rf -- "$TEST_ROOT"
}

run_launcher() {
    "$LAUNCHER" "$@"
    status=$?
}

test_rejects_missing_game_command() {
    run_launcher
    assert_eq "$status" 2 || return 1
    assert_log_not_contains 'start xodus-forza.service'
}

test_starts_and_stops_service_it_owns() {
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_preserves_preexisting_service() {
    export FAKE_SERVICE_ACTIVE=1
    start_socket "$FAKE_SOCKET"
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_not_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_times_out_without_a_real_socket() {
    export FAKE_CREATE_SOCKET=0
    local started=$SECONDS
    run_launcher /usr/bin/true
    local elapsed=$((SECONDS - started))
    assert_eq "$status" 1 || return 1
    ((elapsed <= 6)) || {
        fail "socket wait exceeded five-second bound: ${elapsed}s"
        return 1
    }
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_preserves_exact_game_argv() {
    local recorder="$TEST_ROOT/record-argv"
    local argv_log="$TEST_ROOT/argv.log"
    cat > "$recorder" <<'EOF'
#!/usr/bin/env bash
for argument in "$@"; do
    printf '[%s]\n' "$argument"
done > "$FORZA_ARGV_LOG"
EOF
    chmod +x "$recorder"
    export FORZA_ARGV_LOG="$argv_log"
    run_launcher "$recorder" 'space value' '' '*literal*'
    assert_eq "$status" 0 || return 1
    assert_eq "$(<"$argv_log")" $'[space value]\n[]\n[*literal*]'
}

test_returns_game_exit_status_after_cleanup() {
    run_launcher /usr/bin/bash -c 'exit 42'
    assert_eq "$status" 42 || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_stops_after_term_signal() {
    "$LAUNCHER" /usr/bin/bash -c 'sleep 0.2' &
    local launcher_pid=$!
    local attempts=50
    while ((attempts > 0)); do
        rg -Fq 'start xodus-forza.service' "$FAKE_LOG" && break
        sleep 0.02
        ((attempts -= 1))
    done
    ((attempts > 0)) || {
        fail 'launcher did not start Xodus'
        return 1
    }
    kill -TERM "$launcher_pid" || return 1
    wait "$launcher_pid"
    status=$?
    assert_eq "$status" 143 || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_completes_kwallet_pam_only_for_an_existing_socket() {
    export PAM_KWALLET5_LOGIN="$TEST_ROOT/missing-kwallet.sock"
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_not_contains 'start plasma-kwallet-pam.service' || return 1

    : > "$FAKE_LOG"
    export PAM_KWALLET5_LOGIN="$TEST_ROOT/kwallet.sock"
    start_socket "$PAM_KWALLET5_LOGIN"
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'start plasma-kwallet-pam.service'
}

test_stop_failure_does_not_mask_game_status() {
    export FAKE_STOP_STATUS=77
    run_launcher /usr/bin/bash -c 'exit 42'
    assert_eq "$status" 42 || return 1
    assert_log_contains 'stop xodus-forza.service'
}

run_test() {
    local name=$1
    ((tests_run += 1))
    FAKE_SERVICE_ACTIVE=0
    FAKE_CREATE_SOCKET=1
    FAKE_STOP_STATUS=0
    unset PAM_KWALLET5_LOGIN
    set_up
    if "$name"; then
        printf 'PASS: %s\n' "$name"
    else
        printf 'FAIL: %s\n' "$name" >&2
        ((tests_failed += 1))
    fi
    tear_down
}

for test_name in \
    test_rejects_missing_game_command \
    test_starts_and_stops_service_it_owns \
    test_preserves_preexisting_service \
    test_times_out_without_a_real_socket \
    test_preserves_exact_game_argv \
    test_returns_game_exit_status_after_cleanup \
    test_stops_after_term_signal \
    test_completes_kwallet_pam_only_for_an_existing_socket \
    test_stop_failure_does_not_mask_game_status; do
    run_test "$test_name"
done

printf '%d tests, %d failures\n' "$tests_run" "$tests_failed"
((tests_failed == 0))
