#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

set -u

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SETUP="$ROOT/setup"

status=0
tests_run=0
tests_failed=0
current_test_failed=0

fail() {
    current_test_failed=1
    printf 'FAIL: %s\n' "$*" >&2
    return 1
}

assert_eq() { [[ $1 == "$2" ]] || fail "expected [$2], got [$1]"; }
assert_contains() { [[ $1 == *"$2"* ]] || fail "expected [$1] to contain [$2]"; }

write_stub() {
    local path=$1
    shift
    printf '%s\n' '#!/usr/bin/env bash' "$@" >"$path"
    chmod 755 -- "$path"
}

set_up() {
    WORK_ROOT=$(mktemp -d)
    FIXTURE_ROOT="$WORK_ROOT/repository"
    ACTION_LOG="$WORK_ROOT/argv"
    mkdir -p -- "$FIXTURE_ROOT/scripts" "$FIXTURE_ROOT/bin"
    cp -- "$SETUP" "$FIXTURE_ROOT/setup"
    write_stub "$FIXTURE_ROOT/scripts/forza-bootstrap" \
        'printf "%s\0" "$@" >"$ACTION_LOG"'
    write_stub "$FIXTURE_ROOT/scripts/install-runtime-components" \
        'printf "legacy-runtime\0%s\0" "$@" >"$ACTION_LOG"'
    write_stub "$FIXTURE_ROOT/scripts/print-steam-options" 'exit 0'
    write_stub "$FIXTURE_ROOT/scripts/install-user" 'exit 0'
    write_stub "$FIXTURE_ROOT/bin/forza-doctor" 'exit 0'
    export ACTION_LOG
}

tear_down() {
    rm -rf -- "$WORK_ROOT"
}

read_argv() {
    mapfile -d '' -t FORWARDED_ARGV <"$ACTION_LOG"
}

test_setup_preserves_bootstrap_argv_exactly() {
    set_up
    local bundle="$WORK_ROOT/bundle with spaces.tar.zst"
    local threading="$WORK_ROOT/licensed runtime.dll"

    "$FIXTURE_ROOT/setup" bootstrap --bundle "$bundle" --threading-dll "$threading" || {
        fail 'setup bootstrap rejected valid multi-argument input'
        tear_down
        return 1
    }
    read_argv

    assert_eq "${#FORWARDED_ARGV[@]}" 5 || return 1
    assert_eq "${FORWARDED_ARGV[0]}" bootstrap || return 1
    assert_eq "${FORWARDED_ARGV[1]}" --bundle || return 1
    assert_eq "${FORWARDED_ARGV[2]}" "$bundle" || return 1
    assert_eq "${FORWARDED_ARGV[3]}" --threading-dll || return 1
    assert_eq "${FORWARDED_ARGV[4]}" "$threading" || return 1
    tear_down
}

test_snapshot_and_compare_forward_all_arguments_exactly() {
    set_up
    local before="$WORK_ROOT/before snapshot.json"
    local after="$WORK_ROOT/after snapshot.json"

    "$FIXTURE_ROOT/setup" snapshot --output "$before" || return 1
    read_argv
    assert_eq "${#FORWARDED_ARGV[@]}" 3 || return 1
    assert_eq "${FORWARDED_ARGV[0]}" snapshot || return 1
    assert_eq "${FORWARDED_ARGV[1]}" --output || return 1
    assert_eq "${FORWARDED_ARGV[2]}" "$before" || return 1

    "$FIXTURE_ROOT/setup" compare --before "$before" --after "$after" || return 1
    read_argv
    assert_eq "${#FORWARDED_ARGV[@]}" 5 || return 1
    assert_eq "${FORWARDED_ARGV[0]}" compare || return 1
    assert_eq "${FORWARDED_ARGV[1]}" --before || return 1
    assert_eq "${FORWARDED_ARGV[2]}" "$before" || return 1
    assert_eq "${FORWARDED_ARGV[3]}" --after || return 1
    assert_eq "${FORWARDED_ARGV[4]}" "$after" || return 1
    tear_down
}

test_bootstrap_rollback_is_distinct_from_legacy_runtime_rollback() {
    set_up

    "$FIXTURE_ROOT/setup" bootstrap --rollback || return 1
    read_argv
    assert_eq "${FORWARDED_ARGV[*]}" 'bootstrap --rollback' || return 1

    printf 'ROLLBACK\n' | HOME="$WORK_ROOT/home" \
        FORZA_INSTALL_ROOT="$WORK_ROOT/home" \
        FORZA_SETUP_COMPAT_TOOL_ROOT="$WORK_ROOT/compat" \
        FORZA_SETUP_XGAMERUNTIME_BUILD_DIR="$WORK_ROOT/xgr" \
        FORZA_SETUP_XODUS_BUILD_DIR="$WORK_ROOT/xodus" \
        "$FIXTURE_ROOT/setup" rollback >/dev/null || return 1
    read_argv
    assert_eq "${FORWARDED_ARGV[0]}" legacy-runtime || return 1
    assert_eq "${FORWARDED_ARGV[1]}" rollback || return 1
    tear_down
}

test_setup_help_names_new_boundary_without_claiming_steam_or_launch_control() {
    set_up
    local output

    output=$("$FIXTURE_ROOT/setup" --help) || return 1

    assert_contains "$output" './setup bootstrap' || return 1
    assert_contains "$output" './setup snapshot' || return 1
    assert_contains "$output" './setup compare' || return 1
    assert_contains "$output" './setup bootstrap --rollback' || return 1
    assert_contains "$output" './setup rollback' || return 1
    assert_contains "$output" 'prints redacted JSON; --output writes private evidence' || return 1
    assert_contains "$output" 'last transaction before/after pair by default' || return 1
    assert_contains "$output" 'bootstrap --check is read-only' || return 1
    assert_contains "$output" 'does not edit Steam or launch the game' || return 1
    [[ ! -e $ACTION_LOG ]] || fail 'help invoked a child command'
    tear_down
}

run_test() {
    ((tests_run += 1))
    current_test_failed=0
    "$1" || current_test_failed=1
    if ((current_test_failed == 0)); then
        printf 'PASS: %s\n' "$1"
    else
        ((tests_failed += 1))
        status=1
    fi
}

run_test test_setup_preserves_bootstrap_argv_exactly
run_test test_snapshot_and_compare_forward_all_arguments_exactly
run_test test_bootstrap_rollback_is_distinct_from_legacy_runtime_rollback
run_test test_setup_help_names_new_boundary_without_claiming_steam_or_launch_control

printf '%s tests run, %s failed\n' "$tests_run" "$tests_failed"
exit "$status"
