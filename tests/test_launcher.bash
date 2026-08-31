#!/usr/bin/env bash

set -u

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
LAUNCHER="$ROOT/bin/forza-linux"

status=0
tests_run=0
tests_failed=0

fail() { printf 'FAIL: %s\n' "$*" >&2; return 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"; }
assert_log_contains() { rg -Fq -- "$1" "$FAKE_LOG" || fail "log does not contain [$1]"; }
assert_log_not_contains() { ! rg -Fq -- "$1" "$FAKE_LOG" || fail "log unexpectedly contains [$1]"; }

wait_for_file() {
    local path=$1 attempts=100
    while ((attempts > 0)); do
        [[ -e "$path" || -S "$path" ]] && return 0
        sleep 0.02
        ((attempts -= 1))
    done
    fail "timed out waiting for [$path]"
}

monotonic_ms() { awk '{ printf "%.0f\n", $1 * 1000 }' /proc/uptime; }
write_fake_state() { printf '%s %s %s\n' "$1" "$2" "$3" > "$FAKE_STATE"; }

start_socket() {
    "$SOCKET_SERVER" "$1" &
    SOCKET_PIDS+=("$!")
    wait_for_file "$1"
}

start_unlistening_socket() {
    uv run python - "$1" <<'PY' &
import os
import signal
import socket
import sys

path = sys.argv[1]
listener = socket.socket(socket.AF_UNIX)
listener.bind(path)
try:
    signal.pause()
finally:
    listener.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
PY
    SOCKET_PIDS+=("$!")
    wait_for_file "$1"
}

activate_preexisting_service() {
    write_fake_state active preexisting preexisting-owner
    start_socket "$FAKE_SOCKET"
}

set_up() {
    TEST_ROOT=$(mktemp -d)
    export XDG_RUNTIME_DIR="$TEST_ROOT/runtime"
    mkdir -p "$XDG_RUNTIME_DIR"
    FAKE_LOG="$TEST_ROOT/systemctl.log"
    FAKE_STATE="$TEST_ROOT/service.state"
    FAKE_COUNTER="$TEST_ROOT/invocation.counter"
    FAKE_OWNER_FILE="$TEST_ROOT/owner.property"
    : > "$FAKE_LOG"
    write_fake_state inactive none none
    printf '0\n' > "$FAKE_COUNTER"
    printf 'none\n' > "$FAKE_OWNER_FILE"
    export FAKE_LOG FAKE_STATE FAKE_COUNTER FAKE_OWNER_FILE
    export FAKE_SOCKET="$XDG_RUNTIME_DIR/xodus.sock"
    export FAKE_PID_FILE="$TEST_ROOT/xodus.pid"
    export FAKE_CREATE_SOCKET=${FAKE_CREATE_SOCKET:-1}
    export FAKE_STOP_STATUS=${FAKE_STOP_STATUS:-0}
    export FAKE_EXTERNAL_AFTER_SET_PROPERTY=${FAKE_EXTERNAL_AFTER_SET_PROPERTY:-0}
    export FAKE_EXTERNAL_ON_START=${FAKE_EXTERNAL_ON_START:-0}
    export FAKE_EMPTY_CAPTURE=${FAKE_EMPTY_CAPTURE:-0}
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

read_state() { read -r state invocation owner < "$FAKE_STATE"; }
write_state() { printf '%s %s %s\n' "$1" "$2" "$3" > "$FAKE_STATE"; }
next_invocation() {
    number=$(<"$FAKE_COUNTER")
    number=$((number + 1))
    printf '%s\n' "$number" > "$FAKE_COUNTER"
    printf 'test-invocation-%s\n' "$number"
}
stop_socket() {
    if [[ -f $FAKE_PID_FILE ]]; then
        kill "$(<"$FAKE_PID_FILE")" 2>/dev/null || true
        rm -f -- "$FAKE_PID_FILE" "$FAKE_SOCKET"
    fi
}
start_xodus() {
    read_state
    printf 'start xodus-forza.service\n' >> "$FAKE_LOG"
    [[ $state == active ]] && return 0
    if [[ -e $FAKE_SOCKET || -S $FAKE_SOCKET ]]; then
        printf 'start blocked by existing socket\n' >> "$FAKE_LOG"
        return 0
    fi
    invocation=$(next_invocation)
    owner=$(<"$FAKE_OWNER_FILE")
    if [[ ${FAKE_EXTERNAL_ON_START:-0} == 1 ]]; then
        owner=external-owner
    fi
    write_state active "$invocation" "$owner"
    if [[ ${FAKE_CREATE_SOCKET:-1} == 1 ]]; then
        "$SOCKET_SERVER" "$FAKE_SOCKET" &
        printf '%s\n' "$!" > "$FAKE_PID_FILE"
    fi
}

external_start() {
    read_state
    [[ $state == active ]] && return 0
    invocation=$(next_invocation)
    write_state active "$invocation" external-owner
    "$SOCKET_SERVER" "$FAKE_SOCKET" &
    printf '%s\n' "$!" > "$FAKE_PID_FILE"
}

[[ ${1:-} == --user ]] && shift
command=${1:-}
shift || true
unit=${!#}

case "$command" in
    is-active)
        read_state
        [[ $state == active ]] && exit 0
        exit 3
        ;;
    show)
        read_state
        if [[ $state == active && ${FAKE_EMPTY_CAPTURE:-0} != 1 ]]; then
            if [[ $* == *InvocationID* ]]; then
                printf '%s\n' "$invocation"
            elif [[ $* == *Environment* ]]; then
                printf 'FORZA_LAUNCH_OWNER=%s\n' "$owner"
            fi
        fi
        ;;
    set-property)
        value=
        for argument in "$@"; do
            [[ $argument == Environment=FORZA_LAUNCH_OWNER=* ]] && value=$argument
        done
        [[ $value == Environment=FORZA_LAUNCH_OWNER=* ]] || exit 64
        printf '%s\n' "${value#Environment=}" > "$FAKE_OWNER_FILE"
        printf 'set-property %s\n' "$value" >> "$FAKE_LOG"
        if [[ ${FAKE_EXTERNAL_AFTER_SET_PROPERTY:-0} == 1 ]]; then
            external_start
        fi
        ;;
    start)
        if [[ $unit == plasma-kwallet-pam.service ]]; then
            printf 'start %s\n' "$unit" >> "$FAKE_LOG"
        elif [[ $unit == xodus-forza.service ]]; then
            start_xodus
        else
            exit 64
        fi
        ;;
    restart)
        [[ $unit == xodus-forza.service ]] || exit 64
        printf 'restart %s\n' "$unit" >> "$FAKE_LOG"
        stop_socket
        write_state inactive none none
        start_xodus
        ;;
    stop)
        [[ $unit == xodus-forza.service ]] || exit 64
        printf 'stop %s\n' "$unit" >> "$FAKE_LOG"
        stop_socket
        write_state inactive none none
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
}

tear_down() {
    local pid
    for pid in "${SOCKET_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
    if [[ -f ${FAKE_PID_FILE:-} ]]; then kill "$(<"$FAKE_PID_FILE")" 2>/dev/null || true; fi
    rm -rf -- "$TEST_ROOT"
}

run_launcher() { "$LAUNCHER" "$@"; status=$?; }

make_waiting_game() {
    local game="$TEST_ROOT/waiting-game"
    cat > "$game" <<'EOF'
#!/usr/bin/env bash
printf ready > "$FORZA_GAME_READY"
while [[ ! -e $FORZA_GAME_RELEASE ]]; do sleep 0.02; done
EOF
    chmod +x "$game"
    printf '%s\n' "$game"
}

make_signal_game() {
    local game="$TEST_ROOT/signal game"
    cat > "$game" <<'EOF'
#!/usr/bin/env bash
printf ready > "$FORZA_GAME_READY"
printf '%s\n' "$$" > "$FORZA_GAME_PID"
trap 'printf "%s" "$FORZA_SIGNAL_NAME" > "$FORZA_GAME_SIGNALLED"; exit 0' TERM INT
if [[ ${FORZA_SELF_SIGNAL:-0} == 1 ]]; then
    sleep 0.1
    kill -"$FORZA_SIGNAL_NAME" "$PPID"
fi
while :; do sleep 1; done
EOF
    chmod +x "$game"
    printf '%s\n' "$game"
}

make_fd_game() {
    local game="$TEST_ROOT/fd-game"
    cat > "$game" <<'EOF'
#!/usr/bin/env bash
for descriptor in /proc/$$/fd/*; do
    [[ $(readlink "$descriptor") == "$FORZA_LOCK_PATH" ]] && {
        printf retained > "$FORZA_FD_RESULT"
        exit 1
    }
done
printf closed > "$FORZA_FD_RESULT"
EOF
    chmod +x "$game"
    printf '%s\n' "$game"
}

wait_for_process_exit() {
    local pid=$1 attempts=100
    while ((attempts > 0)); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.02
        ((attempts -= 1))
    done
    return 1
}

stop_test_game() {
    if [[ -f ${1:-} ]]; then
        kill -TERM "$(<"$1")" 2>/dev/null || true
    fi
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

test_accepts_active_listening_socket_without_ownership() {
    activate_preexisting_service
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_not_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_rejects_active_unlistening_socket() {
    write_fake_state active preexisting preexisting-owner
    start_unlistening_socket "$FAKE_SOCKET"
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_refuses_external_activation_after_reservation() {
    export FAKE_EXTERNAL_AFTER_SET_PROPERTY=1
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_refuses_external_activation_at_start_boundary() {
    export FAKE_EXTERNAL_ON_START=1
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_refuses_empty_ownership_capture_without_stopping() {
    export FAKE_EMPTY_CAPTURE=1
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_rejects_a_concurrent_launcher_without_stopping_the_owner() {
    local game ready release first_pid concurrent_status first_status no_stop=0
    game=$(make_waiting_game)
    ready="$TEST_ROOT/first-ready"
    release="$TEST_ROOT/first-release"
    FORZA_GAME_READY="$ready" FORZA_GAME_RELEASE="$release" "$LAUNCHER" "$game" &
    first_pid=$!
    wait_for_file "$ready" || return 1
    run_launcher /usr/bin/true
    concurrent_status=$status
    assert_log_not_contains 'stop xodus-forza.service' || no_stop=1
    : > "$release"
    wait "$first_pid"
    first_status=$?
    assert_eq "$concurrent_status" 1 || return 1
    ((no_stop == 0)) || return 1
    assert_eq "$first_status" 0 || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_does_not_stop_an_externally_restarted_invocation() {
    local game ready release launcher_pid
    game=$(make_waiting_game)
    ready="$TEST_ROOT/game-ready"
    release="$TEST_ROOT/game-release"
    FORZA_GAME_READY="$ready" FORZA_GAME_RELEASE="$release" "$LAUNCHER" "$game" &
    launcher_pid=$!
    wait_for_file "$ready" || return 1
    "$FAKE_SYSTEMCTL" --user restart xodus-forza.service
    : > "$release"
    wait "$launcher_pid"
    assert_eq "$?" 0 || return 1
    assert_log_contains 'restart xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_removes_a_stale_socket_before_starting() {
    start_socket "$FAKE_SOCKET"
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'start blocked by existing socket'
}

test_times_out_without_a_fresh_socket_within_five_seconds() {
    export FAKE_CREATE_SOCKET=0
    local started elapsed
    started=$(monotonic_ms)
    run_launcher /usr/bin/true
    elapsed=$(($(monotonic_ms) - started))
    assert_eq "$status" 1 || return 1
    ((elapsed <= 5100)) || {
        fail "socket wait exceeded five seconds: ${elapsed}ms"
        return 1
    }
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_game_child_does_not_inherit_the_coordinator_lease() {
    local game="$TEST_ROOT/fd-game" result="$TEST_ROOT/fd-result"
    game=$(make_fd_game)
    export FORZA_LOCK_PATH="$XDG_RUNTIME_DIR/forza-linux.lock" FORZA_FD_RESULT="$result"
    run_launcher "$game"
    assert_eq "$status" 0 || return 1
    assert_eq "$(<"$result")" closed
}

test_space_named_game_preserves_normal_status() {
    local game="$TEST_ROOT/game with spaces"
    cat > "$game" <<'EOF'
#!/usr/bin/env bash
exit 23
EOF
    chmod +x "$game"
    run_launcher "$game"
    assert_eq "$status" 23
}

test_preserves_exact_game_argv() {
    local recorder="$TEST_ROOT/record-argv" argv_log="$TEST_ROOT/argv.log"
    cat > "$recorder" <<'EOF'
#!/usr/bin/env bash
for argument in "$@"; do printf '[%s]\n' "$argument"; done > "$FORZA_ARGV_LOG"
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

run_signal_forwarding_test() {
    local signal_name=$1 expected_status=$2 game ready signalled game_pid launcher_pid
    game=$(make_signal_game)
    ready="$TEST_ROOT/${signal_name}-ready"
    signalled="$TEST_ROOT/${signal_name}-signalled"
    game_pid="$TEST_ROOT/${signal_name}-game-pid"
    FORZA_GAME_READY="$ready" FORZA_GAME_SIGNALLED="$signalled" FORZA_GAME_PID="$game_pid" FORZA_SIGNAL_NAME="$signal_name" "$LAUNCHER" "$game" &
    launcher_pid=$!
    wait_for_file "$ready" || {
        stop_test_game "$game_pid"
        return 1
    }
    kill -"$signal_name" "$launcher_pid" || {
        stop_test_game "$game_pid"
        return 1
    }
    if ! wait_for_process_exit "$launcher_pid"; then
        kill -TERM "$launcher_pid" 2>/dev/null || true
        stop_test_game "$game_pid"
        return 1
    fi
    wait "$launcher_pid"
    assert_eq "$?" "$expected_status" || return 1
    wait_for_file "$signalled" || {
        stop_test_game "$game_pid"
        return 1
    }
    assert_eq "$(<"$signalled")" "$signal_name" || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_forwards_term_and_reaps_the_game_child() {
    run_signal_forwarding_test TERM 143
}

test_forwards_int_and_reaps_the_game_child() {
    local game ready signalled game_pid
    game=$(make_signal_game)
    ready="$TEST_ROOT/INT-ready"
    signalled="$TEST_ROOT/INT-signalled"
    game_pid="$TEST_ROOT/INT-game-pid"
    export FORZA_GAME_READY="$ready" FORZA_GAME_SIGNALLED="$signalled" FORZA_GAME_PID="$game_pid"
    export FORZA_SIGNAL_NAME=INT FORZA_SELF_SIGNAL=0
    uv run python - "$LAUNCHER" "$game" <<'PY'
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

launcher, game = sys.argv[1:]
process = subprocess.Popen(
    [launcher, game],
    env=os.environ.copy(),
    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
)
ready = Path(os.environ["FORZA_GAME_READY"])
deadline = time.monotonic() + 2
while not ready.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if not ready.exists():
    process.terminate()
    process.wait()
    raise SystemExit("launcher did not start the signal child")
process.send_signal(signal.SIGINT)
if process.wait(timeout=2) != 130:
    raise SystemExit(f"expected launcher status 130, got {process.returncode}")
PY
    assert_eq "$?" 0 || return 1
    wait_for_file "$signalled" || return 1
    assert_eq "$(<"$signalled")" INT || return 1
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
    FAKE_CREATE_SOCKET=1
    FAKE_STOP_STATUS=0
    FAKE_EXTERNAL_AFTER_SET_PROPERTY=0
    FAKE_EXTERNAL_ON_START=0
    FAKE_EMPTY_CAPTURE=0
    unset PAM_KWALLET5_LOGIN FORZA_SELF_SIGNAL FORZA_LOCK_PATH FORZA_FD_RESULT
    set_up
    if "$name"; then printf 'PASS: %s\n' "$name"; else printf 'FAIL: %s\n' "$name" >&2; ((tests_failed += 1)); fi
    tear_down
}

for test_name in \
    test_rejects_missing_game_command \
    test_starts_and_stops_service_it_owns \
    test_accepts_active_listening_socket_without_ownership \
    test_rejects_active_unlistening_socket \
    test_refuses_external_activation_after_reservation \
    test_refuses_external_activation_at_start_boundary \
    test_refuses_empty_ownership_capture_without_stopping \
    test_rejects_a_concurrent_launcher_without_stopping_the_owner \
    test_does_not_stop_an_externally_restarted_invocation \
    test_removes_a_stale_socket_before_starting \
    test_times_out_without_a_fresh_socket_within_five_seconds \
    test_preserves_exact_game_argv \
    test_game_child_does_not_inherit_the_coordinator_lease \
    test_space_named_game_preserves_normal_status \
    test_returns_game_exit_status_after_cleanup \
    test_forwards_term_and_reaps_the_game_child \
    test_forwards_int_and_reaps_the_game_child \
    test_completes_kwallet_pam_only_for_an_existing_socket \
    test_stop_failure_does_not_mask_game_status; do
    run_test "$test_name"
done

printf '%d tests, %d failures\n' "$tests_run" "$tests_failed"
((tests_failed == 0))
