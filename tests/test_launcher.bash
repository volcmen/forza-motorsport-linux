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
assert_eq() { [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"; }
assert_contains() { [[ "$1" == *"$2"* ]] || fail "expected [$1] to contain [$2]"; }
assert_log_contains() { rg -Fq -- "$1" "$FAKE_LOG" || fail "log does not contain [$1]"; }
assert_log_not_contains() { ! rg -Fq -- "$1" "$FAKE_LOG" || fail "log unexpectedly contains [$1]"; }
assert_path_absent() { [[ ! -e "$1" ]] || fail "path unexpectedly exists [$1]"; }

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
write_fake_state() { printf '%s %s %s\n' "$1" "$2" "$3" >"$FAKE_STATE"; }

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

start_abstract_suffix_socket() {
    uv run python - "$1" <<'PY' &
import signal
import socket
import sys

path = "\0forza-test" + sys.argv[1]
listener = socket.socket(socket.AF_UNIX)
listener.bind(path)
listener.listen(1)
try:
    signal.pause()
finally:
    listener.close()
PY
    SOCKET_PIDS+=("$!")
    sleep 0.05
}

start_different_filesystem_suffix_socket() {
    uv run python - "$1" <<'PY' &
import os
import signal
import socket
import sys

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
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
    SOCKET_PIDS+=("$!")
    sleep 0.05
}

activate_preexisting_service() {
    write_fake_state active preexisting preexisting-owner
    start_socket "$FAKE_SOCKET"
}

set_up() {
    TEST_ROOT=$(mktemp -d)
    export XDG_RUNTIME_DIR="$TEST_ROOT/runtime"
    export FORZA_RUNTIME_TESTING=1
    export FORZA_RUNTIME_TEST_USER_ROOT="$TEST_ROOT/user"
    mkdir -p "$XDG_RUNTIME_DIR" "$FORZA_RUNTIME_TEST_USER_ROOT"
    FAKE_LOG="$TEST_ROOT/systemctl.log"
    FAKE_STATE="$TEST_ROOT/service.state"
    FAKE_COUNTER="$TEST_ROOT/invocation.counter"
    : >"$FAKE_LOG"
    write_fake_state inactive none none
    printf '0\n' >"$FAKE_COUNTER"
    export FAKE_LOG FAKE_STATE FAKE_COUNTER
    export FAKE_SOCKET="$XDG_RUNTIME_DIR/xodus.sock"
    export FAKE_PENDING_TOKEN="$XDG_RUNTIME_DIR/forza-xodus-owner.pending"
    export FAKE_ACTIVE_TOKEN="$XDG_RUNTIME_DIR/forza-xodus-owner.active"
    export FAKE_PID_FILE="$TEST_ROOT/xodus.pid"
    export FAKE_CREATE_SOCKET=${FAKE_CREATE_SOCKET:-1}
    export FAKE_STOP_STATUS=${FAKE_STOP_STATUS:-0}
    export FAKE_EMPTY_CAPTURE=${FAKE_EMPTY_CAPTURE:-0}
    export FAKE_EXTERNAL_AFTER_PENDING=${FAKE_EXTERNAL_AFTER_PENDING:-0}
    export FAKE_EXTERNAL_DURING_START=${FAKE_EXTERNAL_DURING_START:-0}
    export FAKE_SOCKET_PERSISTS=${FAKE_SOCKET_PERSISTS:-0}
    export FAKE_EXTERNAL_AFTER_PENDING_MARKER="$TEST_ROOT/external-pending-seen"
    export FAKE_EXTERNAL_DURING_START_MARKER="$TEST_ROOT/external-start-seen"
    SOCKET_PIDS=()

    SOCKET_SERVER="$TEST_ROOT/socket-server"
    cat >"$SOCKET_SERVER" <<'EOF'
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
    if os.environ.get("FAKE_SOCKET_PERSISTS") != "1":
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
PY
EOF
    chmod +x "$SOCKET_SERVER"
    export SOCKET_SERVER

    FAKE_SYSTEMCTL="$TEST_ROOT/systemctl"
    cat >"$FAKE_SYSTEMCTL" <<'EOF'
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
        socket_pid=$(<"$FAKE_PID_FILE")
        kill "$socket_pid" 2>/dev/null || true
        for _ in {1..100}; do
            kill -0 "$socket_pid" 2>/dev/null || break
            sleep 0.01
        done
        rm -f -- "$FAKE_PID_FILE"
        [[ ${FAKE_SOCKET_PERSISTS:-0} == 1 ]] || rm -f -- "$FAKE_SOCKET"
    fi
}
start_xodus() {
    read_state
    printf 'start xodus-forza.service\n' >> "$FAKE_LOG"
    [[ $state == active ]] && return 0
    if [[ ${FAKE_EXTERNAL_DURING_START:-0} == 1 && ! -e $FAKE_EXTERNAL_DURING_START_MARKER ]]; then
        : > "$FAKE_EXTERNAL_DURING_START_MARKER"
        external_start
        return
    fi
    if [[ ! -f $FAKE_PENDING_TOKEN || -e $FAKE_ACTIVE_TOKEN ]]; then
        printf 'start rejected without pending token\n' >> "$FAKE_LOG"
        return 1
    fi
    mv -T -- "$FAKE_PENDING_TOKEN" "$FAKE_ACTIVE_TOKEN"
    invocation=$(next_invocation)
    write_state active "$invocation" token-consumed
    if [[ ${FAKE_CREATE_SOCKET:-1} == 1 ]]; then
        "$SOCKET_SERVER" "$FAKE_SOCKET" &
        printf '%s\n' "$!" > "$FAKE_PID_FILE"
    fi
}

external_start() {
    start_xodus
}

[[ ${1:-} == --user ]] && shift
command=${1:-}
shift || true
unit=${!#}

case "$command" in
    is-active)
        if [[ ${FAKE_EXTERNAL_AFTER_PENDING:-0} == 1 && ! -e $FAKE_EXTERNAL_AFTER_PENDING_MARKER && -f $FAKE_PENDING_TOKEN ]]; then
            : > "$FAKE_EXTERNAL_AFTER_PENDING_MARKER"
            external_start || true
        fi
        read_state
        [[ $state == active ]] && exit 0
        exit 3
        ;;
    show)
        read_state
        if [[ $state == active && ${FAKE_EMPTY_CAPTURE:-0} != 1 ]]; then
            [[ $* == *InvocationID* ]] && printf '%s\n' "$invocation"
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

write_runtime_transaction() {
    local state=$1
    local root="$FORZA_RUNTIME_TEST_USER_ROOT/.local/state/forza-motorsport-linux/runtime-transactions"
    local transaction=aaaaaaaaaaaaaaaaaaaaaaaa
    mkdir -p -- "$root/$transaction"
    chmod 700 -- "$root" "$root/$transaction"
    printf '{"version":1,"transaction_id":"%s","state":"%s"}\n' \
        "$transaction" "$state" >"$root/$transaction/journal.json"
    chmod 600 -- "$root/$transaction/journal.json"
}

write_v2_runtime_transaction() {
    local state=$1
    local root="$FORZA_RUNTIME_TEST_USER_ROOT/.local/state/forza-motorsport-linux/runtime-transactions"
    local transaction=aaaaaaaaaaaaaaaaaaaaaaaa
    mkdir -p -- "$root/$transaction"
    chmod 700 -- "$root" "$root/$transaction"
    printf '{"version":2,"transaction_id":"%s","state":"%s","runtime_profile":"fixture","source_revisions":{"xgameruntime_git_sha":"1111111111111111111111111111111111111111","xodus_git_sha":"2222222222222222222222222222222222222222","integration_git_sha":"3333333333333333333333333333333333333333"}}\n' \
        "$transaction" "$state" >"$root/$transaction/journal.json"
    chmod 600 -- "$root/$transaction/journal.json"
}

write_runtime_journal() {
    local journal=$1
    local root="$FORZA_RUNTIME_TEST_USER_ROOT/.local/state/forza-motorsport-linux/runtime-transactions"
    local transaction=aaaaaaaaaaaaaaaaaaaaaaaa
    mkdir -p -- "$root/$transaction"
    chmod 700 -- "$root" "$root/$transaction"
    printf '%s\n' "$journal" >"$root/$transaction/journal.json"
    chmod 600 -- "$root/$transaction/journal.json"
}

tear_down() {
    local pid
    for pid in "${SOCKET_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
    if [[ -f ${FAKE_PID_FILE:-} ]]; then kill "$(<"$FAKE_PID_FILE")" 2>/dev/null || true; fi
    rm -rf -- "$TEST_ROOT"
}

run_launcher() {
    "$LAUNCHER" "$@"
    status=$?
}
run_launcher_capturing_stderr() {
    output=$("$LAUNCHER" "$@" 2>&1)
    status=$?
}

make_waiting_game() {
    local game="$TEST_ROOT/waiting-game"
    cat >"$game" <<'EOF'
#!/usr/bin/env bash
printf ready > "$FORZA_GAME_READY"
while [[ ! -e $FORZA_GAME_RELEASE ]]; do sleep 0.02; done
EOF
    chmod +x "$game"
    printf '%s\n' "$game"
}

make_signal_game() {
    local game="$TEST_ROOT/signal game"
    cat >"$game" <<'EOF'
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
    cat >"$game" <<'EOF'
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

test_refuses_every_unfinished_runtime_transaction_state() {
    local transaction_state
    for transaction_state in prepared installing rolling_back recovery_required; do
        write_runtime_transaction "$transaction_state"
        run_launcher_capturing_stderr /usr/bin/true
        assert_eq "$status" 1 || return 1
        assert_contains "$output" "requires recovery ($transaction_state)" || return 1
        assert_log_not_contains 'start xodus-forza.service' || return 1
    done
}

test_allows_an_installed_runtime_transaction_for_manual_validation() {
    write_runtime_transaction installed
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'start xodus-forza.service'
}

test_allows_v2_terminal_runtime_transactions_for_manual_validation() {
    local transaction_state
    for transaction_state in installed accepted rolled_back runtime_restored; do
        write_v2_runtime_transaction "$transaction_state"
        run_launcher /usr/bin/true
        assert_eq "$status" 0 || return 1
        assert_log_contains 'start xodus-forza.service' || return 1
        tear_down
        set_up
    done
}

test_refuses_v2_unfinished_runtime_transactions_before_service_start() {
    local transaction_state
    for transaction_state in prepared installing rolling_back recovery_required; do
        write_v2_runtime_transaction "$transaction_state"
        run_launcher_capturing_stderr /usr/bin/true
        assert_eq "$status" 1 || return 1
        assert_contains "$output" "requires recovery ($transaction_state)" || return 1
        assert_log_not_contains 'start xodus-forza.service' || return 1
        tear_down
        set_up
    done
}

test_refuses_malformed_v2_runtime_transactions_before_service_start() {
    local transaction=aaaaaaaaaaaaaaaaaaaaaaaa
    local valid_revisions='{"xgameruntime_git_sha":"1111111111111111111111111111111111111111","xodus_git_sha":"2222222222222222222222222222222222222222","integration_git_sha":"3333333333333333333333333333333333333333"}'
    local malformed
    for malformed in \
        "{\"version\":2,\"transaction_id\":\"$transaction\",\"state\":\"installed\",\"source_revisions\":$valid_revisions}" \
        "{\"version\":2,\"transaction_id\":\"$transaction\",\"state\":\"installed\",\"runtime_profile\":\"\",\"source_revisions\":$valid_revisions}" \
        "{\"version\":2,\"transaction_id\":\"$transaction\",\"state\":\"installed\",\"runtime_profile\":\"fixture\",\"source_revisions\":{\"xgameruntime_git_sha\":\"invalid\",\"xodus_git_sha\":\"2222222222222222222222222222222222222222\",\"integration_git_sha\":\"3333333333333333333333333333333333333333\"}}" \
        "{\"version\":2,\"transaction_id\":\"$transaction\",\"state\":\"installed\",\"runtime_profile\":\"fixture\",\"source_revisions\":{\"xgameruntime_git_sha\":\"1111111111111111111111111111111111111111\",\"xodus_git_sha\":\"2222222222222222222222222222222222222222\",\"integration_git_sha\":\"3333333333333333333333333333333333333333\",\"extra\":\"4444444444444444444444444444444444444444\"}}" \
        "{\"version\":2.0,\"transaction_id\":\"$transaction\",\"state\":\"installed\",\"runtime_profile\":\"fixture\",\"source_revisions\":$valid_revisions}" \
        "{\"version\":true,\"transaction_id\":\"$transaction\",\"state\":\"installed\",\"runtime_profile\":\"fixture\",\"source_revisions\":$valid_revisions}"; do
        write_runtime_journal "$malformed"
        run_launcher_capturing_stderr /usr/bin/true
        assert_eq "$status" 1 || return 1
        assert_contains "$output" 'cannot validate runtime transaction state' || return 1
        assert_log_not_contains 'start xodus-forza.service' || return 1
        tear_down
        set_up
    done
}

test_refuses_a_malformed_runtime_transaction_before_service_start() {
    local root="$FORZA_RUNTIME_TEST_USER_ROOT/.local/state/forza-motorsport-linux/runtime-transactions"
    mkdir -p -- "$root/bbbbbbbbbbbbbbbbbbbbbbbb"
    chmod 700 -- "$root" "$root/bbbbbbbbbbbbbbbbbbbbbbbb"
    run_launcher_capturing_stderr /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_contains "$output" 'cannot validate runtime transaction state' || return 1
    assert_log_not_contains 'start xodus-forza.service'
}

test_starts_and_stops_service_it_owns() {
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_owned_service_cleanup_removes_an_orphaned_socket() {
    export FAKE_SOCKET_PERSISTS=1
    run_launcher /usr/bin/true
    local launcher_status=$status
    export FAKE_SOCKET_PERSISTS=0
    assert_eq "$launcher_status" 0 || return 1
    assert_log_contains 'stop xodus-forza.service' || return 1
    assert_path_absent "$FAKE_SOCKET"
}

test_cleanup_does_not_execute_a_test_operator() {
    run_launcher_capturing_stderr /usr/bin/true
    assert_eq "$status" 0 || return 1
    [[ $output != *'command not found'* ]] || fail "cleanup emitted [$output]"
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

test_rejects_abstract_suffix_socket() {
    write_fake_state active preexisting preexisting-owner
    start_abstract_suffix_socket "$FAKE_SOCKET"
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_rejects_different_filesystem_suffix_socket() {
    local other_socket="$TEST_ROOT/other$FAKE_SOCKET"
    write_fake_state active preexisting preexisting-owner
    start_different_filesystem_suffix_socket "$other_socket"
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_rejects_direct_start_before_reservation() {
    local direct_status
    "$FAKE_SYSTEMCTL" --user start xodus-forza.service
    direct_status=$?
    assert_eq "$direct_status" 1 || return 1
    assert_log_contains 'start rejected without pending token' || return 1
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_external_start_during_pending_leaves_the_service_unowned() {
    export FAKE_EXTERNAL_AFTER_PENDING=1
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_start_boundary_consumption_binds_the_reservation() {
    export FAKE_EXTERNAL_DURING_START=1
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_log_contains 'start xodus-forza.service' || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_refuses_empty_ownership_capture_without_stopping() {
    export FAKE_EMPTY_CAPTURE=1
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_log_not_contains 'stop xodus-forza.service'
}

test_consumes_a_mode_0600_token_and_removes_it_after_cleanup() {
    local game ready release launcher_pid
    game=$(make_waiting_game)
    ready="$TEST_ROOT/token-game-ready"
    release="$TEST_ROOT/token-game-release"
    FORZA_GAME_READY="$ready" FORZA_GAME_RELEASE="$release" "$LAUNCHER" "$game" &
    launcher_pid=$!
    wait_for_file "$ready" || return 1
    assert_eq "$(stat -c %a "$FAKE_ACTIVE_TOKEN")" 600 || return 1
    assert_path_absent "$FAKE_PENDING_TOKEN" || return 1
    assert_log_not_contains 'FORZA_LAUNCH_OWNER' || return 1
    : >"$release"
    wait "$launcher_pid"
    assert_eq "$?" 0 || return 1
    assert_path_absent "$FAKE_ACTIVE_TOKEN"
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
    : >"$release"
    wait "$first_pid"
    first_status=$?
    assert_eq "$concurrent_status" 1 || return 1
    ((no_stop == 0)) || return 1
    assert_eq "$first_status" 0 || return 1
    assert_log_contains 'stop xodus-forza.service'
}

test_lock_open_preserves_a_preexisting_regular_file() {
    local lock_path="$XDG_RUNTIME_DIR/forza-linux.lock"
    printf 'preexisting lock contents\n' >"$lock_path"
    run_launcher /usr/bin/true
    assert_eq "$status" 0 || return 1
    assert_eq "$(<"$lock_path")" 'preexisting lock contents'
}

test_lock_open_rejects_a_symlink_without_touching_its_target() {
    local lock_path="$XDG_RUNTIME_DIR/forza-linux.lock" outside="$TEST_ROOT/outside-lock"
    printf 'outside lock sentinel\n' >"$outside"
    ln -s -- "$outside" "$lock_path"
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    assert_eq "$(<"$outside")" 'outside lock sentinel' || return 1
    assert_log_not_contains 'start xodus-forza.service'
}

test_lock_open_rejects_a_directory() {
    local lock_path="$XDG_RUNTIME_DIR/forza-linux.lock"
    mkdir -- "$lock_path"
    run_launcher /usr/bin/true
    assert_eq "$status" 1 || return 1
    [[ -d $lock_path && ! -L $lock_path ]] || fail 'lock directory was changed'
    assert_log_not_contains 'start xodus-forza.service'
}

test_restart_after_token_consumption_fails_closed() {
    local game ready release launcher_pid restart_status
    game=$(make_waiting_game)
    ready="$TEST_ROOT/game-ready"
    release="$TEST_ROOT/game-release"
    FORZA_GAME_READY="$ready" FORZA_GAME_RELEASE="$release" "$LAUNCHER" "$game" &
    launcher_pid=$!
    wait_for_file "$ready" || return 1
    "$FAKE_SYSTEMCTL" --user restart xodus-forza.service
    restart_status=$?
    : >"$release"
    wait "$launcher_pid"
    assert_eq "$?" 0 || return 1
    assert_eq "$restart_status" 1 || return 1
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
    ((elapsed <= 5000)) || {
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
    cat >"$game" <<'EOF'
#!/usr/bin/env bash
exit 23
EOF
    chmod +x "$game"
    run_launcher "$game"
    assert_eq "$status" 23
}

test_preserves_exact_game_argv() {
    local recorder="$TEST_ROOT/record-argv" argv_log="$TEST_ROOT/argv.log"
    cat >"$recorder" <<'EOF'
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
    : >"$FAKE_LOG"
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
    FAKE_EMPTY_CAPTURE=0
    FAKE_EXTERNAL_AFTER_PENDING=0
    FAKE_EXTERNAL_DURING_START=0
    FAKE_SOCKET_PERSISTS=0
    unset PAM_KWALLET5_LOGIN FORZA_SELF_SIGNAL FORZA_LOCK_PATH FORZA_FD_RESULT
    set_up
    if "$name"; then printf 'PASS: %s\n' "$name"; else
        printf 'FAIL: %s\n' "$name" >&2
        ((tests_failed += 1))
    fi
    tear_down
}

for test_name in \
    test_rejects_missing_game_command \
    test_refuses_every_unfinished_runtime_transaction_state \
    test_allows_an_installed_runtime_transaction_for_manual_validation \
    test_allows_v2_terminal_runtime_transactions_for_manual_validation \
    test_refuses_v2_unfinished_runtime_transactions_before_service_start \
    test_refuses_malformed_v2_runtime_transactions_before_service_start \
    test_refuses_a_malformed_runtime_transaction_before_service_start \
    test_starts_and_stops_service_it_owns \
    test_owned_service_cleanup_removes_an_orphaned_socket \
    test_cleanup_does_not_execute_a_test_operator \
    test_accepts_active_listening_socket_without_ownership \
    test_rejects_active_unlistening_socket \
    test_rejects_abstract_suffix_socket \
    test_rejects_different_filesystem_suffix_socket \
    test_rejects_direct_start_before_reservation \
    test_external_start_during_pending_leaves_the_service_unowned \
    test_start_boundary_consumption_binds_the_reservation \
    test_refuses_empty_ownership_capture_without_stopping \
    test_consumes_a_mode_0600_token_and_removes_it_after_cleanup \
    test_rejects_a_concurrent_launcher_without_stopping_the_owner \
    test_lock_open_preserves_a_preexisting_regular_file \
    test_lock_open_rejects_a_symlink_without_touching_its_target \
    test_lock_open_rejects_a_directory \
    test_restart_after_token_consumption_fails_closed \
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
