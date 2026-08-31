#!/usr/bin/env bash

set -u

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
GENERATOR="$ROOT/scripts/print-steam-options"

status=0
tests_run=0
tests_failed=0
current_test_failed=0

fail() {
    current_test_failed=1
    printf 'FAIL: %s\n' "$*" >&2
    return 1
}
assert_contains() { [[ "$1" == *"$2"* ]] || fail "expected [$1] to contain [$2]"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] || fail "expected [$1] not to contain [$2]"; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"; }

set_up() {
    WORK_ROOT=$(mktemp -d)
    SENTINEL="$WORK_ROOT/injected"
    TEST_ROOT="$WORK_ROOT/launcher path; \$(touch $SENTINEL)"
    mkdir -p -- "$TEST_ROOT/.local/bin"
    cat >"$TEST_ROOT/.local/bin/forza-linux" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x -- "$TEST_ROOT/.local/bin/forza-linux"
    unset FORZA_TEST_UID
    export FORZA_TEST_ROOT="$TEST_ROOT"
}

tear_down() {
    rm -rf -- "$WORK_ROOT"
}

test_options_use_runtime_uid_and_installed_launcher_path() {
    set_up
    local output uid
    uid=$(id -u)
    output=$("$GENERATOR")
    assert_eq "$(printf '%s\n' "$output" | wc -l)" 1
    assert_contains "$output" "PRESSURE_VESSEL_FILESYSTEMS_RW=/run/user/$uid/xodus.sock"
    assert_contains "$output" 'WINEDLLOVERRIDES=xgameruntime=b'
    assert_contains "$output" '%command%'
    assert_contains "$output" '.local/bin/forza-linux'
    assert_not_contains "$output" 'PROTON_DISABLE_HIDRAW'
    assert_not_contains "$output" 'SkipTargetHardwareProfiler'
    tear_down
}

test_test_uid_override_and_bash_quoting_keep_injected_launcher_path_inert() {
    set_up
    local output command
    export FORZA_TEST_UID=1234
    output=$("$GENERATOR")
    assert_contains "$output" 'PRESSURE_VESSEL_FILESYSTEMS_RW=/run/user/1234/xodus.sock'
    command=${output/%command%/true}
    eval "$command"
    [[ ! -e $SENTINEL ]] || fail 'launcher path injection was evaluated'
    tear_down
}

test_invalid_test_uid_or_id_output_is_rejected_without_an_option_line() {
    set_up
    local output_file="$WORK_ROOT/options" error_file="$WORK_ROOT/error"
    export FORZA_TEST_UID="1234; touch $SENTINEL"
    if "$GENERATOR" >"$output_file" 2>"$error_file"; then
        fail 'invalid FORZA_TEST_UID was accepted'
    fi
    [[ ! -s $output_file ]] || fail 'invalid FORZA_TEST_UID produced an option line'
    [[ ! -e $SENTINEL ]] || fail 'invalid FORZA_TEST_UID was evaluated'
    tear_down

    set_up
    output_file="$WORK_ROOT/options"
    error_file="$WORK_ROOT/error"
    local fake_id="$WORK_ROOT/id"
    printf '%s\n' '#!/usr/bin/env bash' "printf '%s\\n' '1234; touch $SENTINEL'" >"$fake_id"
    chmod +x -- "$fake_id"
    unset FORZA_TEST_UID
    PATH="$WORK_ROOT:$PATH" "$GENERATOR" >"$output_file" 2>"$error_file" &&
        fail 'invalid id -u output was accepted'
    [[ ! -s $output_file ]] || fail 'invalid id -u output produced an option line'
    [[ ! -e $SENTINEL ]] || fail 'invalid id -u output was evaluated'
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

run_test test_options_use_runtime_uid_and_installed_launcher_path
run_test test_test_uid_override_and_bash_quoting_keep_injected_launcher_path_inert
run_test test_invalid_test_uid_or_id_output_is_rejected_without_an_option_line

printf '%s tests run, %s failed\n' "$tests_run" "$tests_failed"
exit "$status"
