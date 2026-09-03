#!/usr/bin/env bash

# Stub bodies are intentionally single-quoted so they expand only when executed.
# shellcheck disable=SC2016

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
assert_not_contains() { [[ $1 != *"$2"* ]] || fail "expected [$1] not to contain [$2]"; }
assert_ordered() {
    local text=$1 first=$2 second=$3 remainder
    [[ $text == *"$first"* ]] || return 1
    remainder=${text#*"$first"}
    [[ $remainder == *"$second"* ]] || fail "expected [$first] before [$second]"
}

snapshot_directory() {
    (
        cd -- "$1" || return
        tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner -cf - .
    ) | sha256sum
}

write_stub() {
    local path=$1
    shift
    printf '%s\n' '#!/usr/bin/env bash' "$@" >"$path"
    chmod 755 -- "$path"
}

set_up() {
    WORK_ROOT=$(mktemp -d)
    FIXTURE_ROOT="$WORK_ROOT/repository"
    USER_ROOT="$WORK_ROOT/user"
    XODUS_BUILD="$WORK_ROOT/xodus/target/release"
    XGAMERUNTIME_BUILD="$WORK_ROOT/xgameruntime-build"
    COMPAT_TOOL="$WORK_ROOT/Steam/compatibilitytools.d/GE-Proton11-3-FM"
    ACTION_LOG="$WORK_ROOT/actions.log"

    mkdir -p -- "$FIXTURE_ROOT/bin" "$FIXTURE_ROOT/scripts" "$USER_ROOT" \
        "$XODUS_BUILD" "$XGAMERUNTIME_BUILD" "$COMPAT_TOOL"
    : >"$ACTION_LOG"

    if [[ -x $SETUP ]]; then
        cp -- "$SETUP" "$FIXTURE_ROOT/setup"
    fi

    write_stub "$FIXTURE_ROOT/bin/forza-doctor" \
        'printf "doctor\n" >>"$ACTION_LOG"' \
        'printf "PASS fixture doctor: ready\n"'
    write_stub "$FIXTURE_ROOT/scripts/install-user" \
        'printf "install-user" >>"$ACTION_LOG"' \
        'printf " %q" "$@" >>"$ACTION_LOG"' \
        'printf "\n" >>"$ACTION_LOG"' \
        'printf "PASS fixture install check\n"' \
        'exit "${FIXTURE_INSTALL_STATUS:-0}"'
    write_stub "$FIXTURE_ROOT/scripts/install-runtime-components" \
        'printf "runtime" >>"$ACTION_LOG"' \
        'printf " %q" "$@" >>"$ACTION_LOG"' \
        'printf "\n" >>"$ACTION_LOG"' \
        'case ${1:-} in' \
        '    lock-evidence)' \
        '        printf "EVIDENCE_SHA256=%s\n" "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"' \
        '        exit "${FIXTURE_EVIDENCE_STATUS:-0}"' \
        '        ;;' \
        '    plan)' \
        '        if [[ ${FIXTURE_INVALID_PLAN_DIGEST:-0} == 1 ]]; then' \
        '            printf "PLAN fixture: reviewed inputs\nPLAN_SHA256=not-a-digest\n"' \
        '        else' \
        '            printf "PLAN fixture: reviewed inputs\nPLAN_SHA256=%s\n" "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"' \
        '        fi' \
        '        ;;' \
        '    uninstall-user)' \
        '        exit "${FIXTURE_INACTIVE_STATUS:-0}"' \
        '        ;;' \
        '    status) printf "state=installed\n" ;;' \
        'esac'
    write_stub "$FIXTURE_ROOT/scripts/print-steam-options" \
        'printf "steam-options\n" >>"$ACTION_LOG"' \
        'printf "fixture launch options\n"'
    write_stub "$FIXTURE_ROOT/scripts/uninstall-user" \
        'printf "uninstall" >>"$ACTION_LOG"' \
        'printf " %q" "$@" >>"$ACTION_LOG"' \
        'printf "\n" >>"$ACTION_LOG"'

    export ACTION_LOG
}

tear_down() {
    rm -rf -- "$WORK_ROOT"
}

run_setup() {
    HOME="$USER_ROOT" \
        FORZA_INSTALL_ROOT="$USER_ROOT" \
        FORZA_SETUP_XODUS_BUILD_DIR="$XODUS_BUILD" \
        FORZA_SETUP_XGAMERUNTIME_BUILD_DIR="$XGAMERUNTIME_BUILD" \
        FORZA_SETUP_COMPAT_TOOL_ROOT="$COMPAT_TOOL" \
        "$FIXTURE_ROOT/setup" "$@"
}

test_check_is_read_only_and_uses_existing_preflight_tools() {
    set_up
    local before after expected
    [[ -x $FIXTURE_ROOT/setup ]] || {
        fail 'setup executable is missing'
        tear_down
        return 1
    }

    before=$(snapshot_directory "$USER_ROOT")
    run_setup check >/dev/null || {
        fail 'setup check failed'
        tear_down
        return 1
    }
    after=$(snapshot_directory "$USER_ROOT")

    expected=$(printf 'doctor\ninstall-user --check --root %q --xodus-build-dir %q' \
        "$USER_ROOT" "$XODUS_BUILD")
    assert_eq "$after" "$before" || return 1
    assert_eq "$(<"$ACTION_LOG")" "$expected" || return 1
    tear_down
}

test_interactive_setup_aborts_before_runtime_preparation() {
    set_up
    local before after output expected
    before=$(snapshot_directory "$USER_ROOT")
    output=$(printf '\n' | run_setup) || {
        fail 'declining setup should exit successfully'
        tear_down
        return 1
    }
    after=$(snapshot_directory "$USER_ROOT")

    expected=$(printf 'install-user --check --root %q --xodus-build-dir %q' \
        "$USER_ROOT" "$XODUS_BUILD")
    assert_eq "$after" "$before" || return 1
    assert_eq "$(<"$ACTION_LOG")" "$expected" || return 1
    assert_contains "$output" 'No changes made.' || return 1
    tear_down
}

test_interactive_setup_stops_on_a_relative_source_path() {
    set_up
    local output
    XODUS_BUILD=relative
    if output=$(run_setup </dev/null 2>&1); then
        fail 'setup accepted a relative Xodus build path'
        tear_down
        return 1
    fi
    assert_contains "$output" 'must be an absolute path' || return 1
    assert_eq "$(<"$ACTION_LOG")" '' || return 1
    tear_down
}

test_interactive_setup_rejects_wrong_plan_digest_before_install() {
    set_up
    local output log
    if output=$(printf 'y\nnot-the-plan-digest\n' | run_setup 2>&1); then
        fail 'setup accepted a wrong plan digest'
        tear_down
        return 1
    fi
    log=$(<"$ACTION_LOG")

    assert_contains "$output" 'PLAN_SHA256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' || return 1
    assert_contains "$output" 'plan digest confirmation did not match' || return 1
    assert_contains "$log" 'runtime lock-evidence ' || return 1
    assert_contains "$log" 'runtime plan ' || return 1
    assert_not_contains "$log" 'runtime install ' || return 1
    tear_down
}

test_interactive_setup_delegates_digest_bound_install_and_reports_next_step() {
    set_up
    local digest output log
    digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    output=$(printf 'y\n%s\n' "$digest" | run_setup) || {
        fail 'confirmed guided setup failed'
        tear_down
        return 1
    }
    log=$(<"$ACTION_LOG")

    assert_contains "$log" 'runtime install ' || return 1
    assert_contains "$log" "--plan-sha256 $digest" || return 1
    assert_contains "$log" 'runtime status ' || return 1
    assert_ordered "$log" 'install-user ' 'runtime lock-evidence ' || return 1
    assert_ordered "$log" 'runtime lock-evidence ' 'runtime plan ' || return 1
    assert_ordered "$log" 'runtime plan ' 'runtime install ' || return 1
    assert_ordered "$log" 'runtime install ' 'runtime status ' || return 1
    assert_ordered "$log" 'runtime status ' 'doctor' || return 1
    assert_ordered "$log" 'doctor' 'steam-options' || return 1
    assert_not_contains "$log" 'patch-known-build' || return 1
    assert_contains "$output" 'fixture launch options' || return 1
    assert_contains "$output" 'READY TO ATTEMPT — not a gameplay guarantee.' || return 1
    assert_contains "$output" 'Launch Forza yourself from Steam.' || return 1
    tear_down
}

test_status_reports_runtime_transaction_before_doctor() {
    set_up
    local output log
    output=$(run_setup status) || {
        fail 'setup status failed'
        tear_down
        return 1
    }
    log=$(<"$ACTION_LOG")

    assert_contains "$output" 'state=installed' || return 1
    assert_contains "$output" 'PASS fixture doctor: ready' || return 1
    assert_ordered "$log" 'runtime status ' 'doctor' || return 1
    assert_not_contains "$log" 'install-user ' || return 1
    assert_not_contains "$log" 'runtime install ' || return 1
    tear_down
}

test_rollback_requires_explicit_confirmation_before_delegating() {
    set_up
    local output log
    output=$(printf '\n' | run_setup rollback) || {
        fail 'declining rollback should exit successfully'
        tear_down
        return 1
    }
    assert_contains "$output" 'No changes made.' || return 1
    assert_not_contains "$(<"$ACTION_LOG")" 'runtime rollback ' || return 1
    tear_down

    set_up
    printf 'ROLLBACK\n' | run_setup rollback >/dev/null || {
        fail 'confirmed rollback failed'
        tear_down
        return 1
    }
    log=$(<"$ACTION_LOG")
    assert_contains "$log" 'runtime rollback ' || return 1
    assert_not_contains "$log" 'runtime install ' || return 1
    assert_not_contains "$log" 'uninstall' || return 1
    tear_down
}

test_rollback_prompts_for_the_original_nondefault_compat_tool() {
    set_up
    local log
    HOME="$USER_ROOT" \
        FORZA_INSTALL_ROOT="$USER_ROOT" \
        FORZA_SETUP_XODUS_BUILD_DIR="$XODUS_BUILD" \
        FORZA_SETUP_XGAMERUNTIME_BUILD_DIR="$XGAMERUNTIME_BUILD" \
        "$FIXTURE_ROOT/setup" rollback <<EOF >/dev/null || {
$COMPAT_TOOL
ROLLBACK
EOF
        fail 'rollback did not accept the original nondefault compatibility tool'
        tear_down
        return 1
    }
    log=$(<"$ACTION_LOG")
    assert_contains "$log" 'runtime rollback ' || return 1
    assert_contains "$log" "--compat-tool-root $COMPAT_TOOL" || return 1
    tear_down
}

test_rollback_stops_on_a_relative_compat_tool_path() {
    set_up
    local log
    if HOME="$USER_ROOT" \
        FORZA_INSTALL_ROOT="$USER_ROOT" \
        FORZA_SETUP_XODUS_BUILD_DIR="$XODUS_BUILD" \
        FORZA_SETUP_XGAMERUNTIME_BUILD_DIR="$XGAMERUNTIME_BUILD" \
        "$FIXTURE_ROOT/setup" rollback <<EOF >/dev/null 2>&1; then
relative
ROLLBACK
EOF
        fail 'rollback accepted a relative compatibility-tool path'
        tear_down
        return 1
    fi
    log=$(<"$ACTION_LOG")
    assert_not_contains "$log" 'runtime rollback ' || return 1
    tear_down
}

test_steam_options_only_delegates_to_the_existing_generator() {
    set_up
    local output
    output=$(run_setup steam-options) || {
        fail 'setup steam-options failed'
        tear_down
        return 1
    }
    assert_eq "$output" 'fixture launch options' || return 1
    assert_eq "$(<"$ACTION_LOG")" 'steam-options' || return 1
    tear_down
}

test_uninstall_requires_confirmation_and_preserves_runtime_boundary() {
    set_up
    local output log
    output=$(printf '\n' | run_setup uninstall) || {
        fail 'declining uninstall should exit successfully'
        tear_down
        return 1
    }
    assert_contains "$output" 'No changes made.' || return 1
    assert_not_contains "$(<"$ACTION_LOG")" 'uninstall' || return 1
    tear_down

    set_up
    output=$(printf 'UNINSTALL\n' | run_setup uninstall) || {
        fail 'confirmed uninstall failed'
        tear_down
        return 1
    }
    log=$(<"$ACTION_LOG")
    assert_contains "$output" 'does not restore compatibility-tool runtime components' || return 1
    assert_contains "$log" 'runtime uninstall-user ' || return 1
    assert_not_contains "$log" "uninstall --root $USER_ROOT" || return 1
    assert_not_contains "$log" 'runtime rollback ' || return 1
    assert_not_contains "$log" 'runtime restore-runtime ' || return 1
    tear_down
}

test_uninstall_stops_when_inactive_state_cannot_be_proven() {
    set_up
    local log
    export FIXTURE_INACTIVE_STATUS=9
    if printf 'UNINSTALL\n' | run_setup uninstall >/dev/null 2>&1; then
        fail 'uninstall continued when inactive state could not be proven'
        tear_down
        return 1
    fi
    log=$(<"$ACTION_LOG")
    assert_contains "$log" 'runtime uninstall-user ' || return 1
    assert_not_contains "$log" 'uninstall --root ' || return 1
    unset FIXTURE_INACTIVE_STATUS
    tear_down
}

test_help_is_successful_and_explains_mutating_and_read_only_modes() {
    set_up
    local output
    output=$(run_setup --help) || {
        fail 'setup --help failed'
        tear_down
        return 1
    }
    assert_contains "$output" './setup                 Guided installation' || return 1
    assert_contains "$output" './setup check           Read-only readiness check' || return 1
    assert_contains "$output" './setup rollback        Explicit transaction rollback' || return 1
    assert_contains "$output" 'does not download binaries, edit Steam, patch Wine, or launch the game' || return 1
    assert_eq "$(<"$ACTION_LOG")" '' || return 1
    tear_down
}

test_interactive_setup_rejects_malformed_planner_digest() {
    set_up
    local output log
    export FIXTURE_INVALID_PLAN_DIGEST=1
    if output=$(printf 'y\nnot-a-digest\n' | run_setup 2>&1); then
        fail 'setup accepted a malformed planner digest'
        tear_down
        return 1
    fi
    log=$(<"$ACTION_LOG")
    assert_contains "$output" 'runtime planner returned no single valid PLAN_SHA256' || return 1
    assert_not_contains "$log" 'runtime install ' || return 1
    unset FIXTURE_INVALID_PLAN_DIGEST
    tear_down
}

test_interactive_setup_stops_before_evidence_when_preflight_fails() {
    set_up
    local digest log
    digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    export FIXTURE_INSTALL_STATUS=7
    if printf 'y\n%s\n' "$digest" | run_setup >/dev/null 2>&1; then
        fail 'setup continued after a failed install preflight'
        tear_down
        return 1
    fi
    log=$(<"$ACTION_LOG")
    assert_contains "$log" 'install-user --check ' || return 1
    assert_not_contains "$log" 'runtime lock-evidence ' || return 1
    assert_not_contains "$log" 'runtime install ' || return 1
    unset FIXTURE_INSTALL_STATUS
    tear_down
}

test_interactive_setup_stops_when_evidence_lock_rejects_inputs() {
    set_up
    local digest log
    digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    export FIXTURE_EVIDENCE_STATUS=8
    if printf 'y\n%s\n' "$digest" | run_setup >/dev/null 2>&1; then
        fail 'setup continued after evidence lock rejected the inputs'
        tear_down
        return 1
    fi
    log=$(<"$ACTION_LOG")
    assert_contains "$log" 'runtime lock-evidence ' || return 1
    assert_not_contains "$log" 'runtime plan ' || return 1
    assert_not_contains "$log" 'runtime install ' || return 1
    unset FIXTURE_EVIDENCE_STATUS
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

run_test test_check_is_read_only_and_uses_existing_preflight_tools
run_test test_interactive_setup_aborts_before_runtime_preparation
run_test test_interactive_setup_stops_on_a_relative_source_path
run_test test_interactive_setup_rejects_wrong_plan_digest_before_install
run_test test_interactive_setup_delegates_digest_bound_install_and_reports_next_step
run_test test_status_reports_runtime_transaction_before_doctor
run_test test_rollback_requires_explicit_confirmation_before_delegating
run_test test_rollback_prompts_for_the_original_nondefault_compat_tool
run_test test_rollback_stops_on_a_relative_compat_tool_path
run_test test_steam_options_only_delegates_to_the_existing_generator
run_test test_uninstall_requires_confirmation_and_preserves_runtime_boundary
run_test test_uninstall_stops_when_inactive_state_cannot_be_proven
run_test test_help_is_successful_and_explains_mutating_and_read_only_modes
run_test test_interactive_setup_rejects_malformed_planner_digest
run_test test_interactive_setup_stops_before_evidence_when_preflight_fails
run_test test_interactive_setup_stops_when_evidence_lock_rejects_inputs

printf '%s tests run, %s failed\n' "$tests_run" "$tests_failed"
exit "$status"
