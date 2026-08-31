#!/usr/bin/env bash

set -u

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DOCTOR="$ROOT/bin/forza-doctor"

status=0
tests_run=0
tests_failed=0
current_test_failed=0

fail() { current_test_failed=1; printf 'FAIL: %s\n' "$*" >&2; return 1; }
assert_contains() { [[ "$1" == *"$2"* ]] || fail "expected [$1] to contain [$2]"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] || fail "expected [$1] not to contain [$2]"; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"; }
assert_log_not_contains() { ! rg -Fq -- "$1" "$FAKE_LOG" || fail "log unexpectedly contains [$1]"; }

snapshot_root() {
    (
        cd -- "$TEST_ROOT"
        tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner -cf - .
    ) | sha256sum
}

write_fake_commands() {
    FAKE_BIN="$WORK_ROOT/fake-bin"
    mkdir -p -- "$FAKE_BIN"

    cat > "$FAKE_BIN/busctl" <<'EOF'
#!/usr/bin/env bash
printf 'busctl %s\n' "$*" >> "$FAKE_LOG"
[[ ${FORZA_FAKE_SECRET_OWNER:-0} == 1 ]]
EOF
    cat > "$FAKE_BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$FAKE_LOG"
printf '%s\n' "${FORZA_FAKE_SERVICE_STATE:-disabled}"
EOF
    cat > "$FAKE_BIN/df" <<'EOF'
#!/usr/bin/env bash
printf 'df %s\n' "$*" >> "$FAKE_LOG"
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/test 999999999 0 %s 0%% /\n' "${FORZA_FAKE_DISK_KB:-209715200}"
EOF
    chmod +x -- "$FAKE_BIN/busctl" "$FAKE_BIN/systemctl" "$FAKE_BIN/df"
    export FORZA_BUSCTL="$FAKE_BIN/busctl" FORZA_SYSTEMCTL="$FAKE_BIN/systemctl" FORZA_DF="$FAKE_BIN/df"
}

write_fake_patcher() {
    local patcher="$TEST_ROOT/.local/libexec/forza-motorsport-linux/patch-known-build"
    mkdir -p -- "$(dirname -- "$patcher")"
    cat > "$patcher" <<'EOF'
#!/usr/bin/env bash
set -u
printf 'patch-known-build %s\n' "$*" >> "$FAKE_LOG"
for ((index = 1; index <= $#; index += 1)); do
    if [[ ${!index} == --target ]]; then
        target_index=$((index + 1))
        target=${!target_index}
        break
    fi
done
case "$target" in
    *windows.gaming.input.dll) state=${FORZA_FAKE_CONTROLLER_STATE:-patched} ;;
    *mountmgr.sys) state=${FORZA_FAKE_MOUNTMGR_STATE:-patched} ;;
    *) exit 64 ;;
esac
printf '%s\n' "$state"
[[ $state == original || $state == patched ]]
EOF
    chmod +x -- "$patcher"
    : > "$TEST_ROOT/.local/share/forza-motorsport-linux/supported-builds.toml"
}

make_ready_installation() {
    local steam="$TEST_ROOT/.local/share/Steam"
    mkdir -p -- \
        "$steam/steamapps/compatdata/2440510/pfx/drive_c/windows/system32/drivers" \
        "$steam/compatibilitytools.d/GE-Proton11-3-FM" \
        "$TEST_ROOT/.local/libexec/xodus-forza" \
        "$TEST_ROOT/.local/share/dbus-1/services" \
        "$TEST_ROOT/.local/share/forza-motorsport-linux"
    : > "$steam/steamapps/appmanifest_2440510.acf"
    : > "$steam/steamapps/compatdata/2440510/pfx/drive_c/windows/system32/xgameruntime.dll.threading"
    : > "$TEST_ROOT/.local/libexec/xodus-forza/xodus-service"
    chmod +x -- "$TEST_ROOT/.local/libexec/xodus-forza/xodus-service"
    mkdir -p -- "$TEST_ROOT/usr/bin"
    : > "$TEST_ROOT/usr/bin/ksecretd"
    chmod +x -- "$TEST_ROOT/usr/bin/ksecretd"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec="/usr/bin/ksecretd" --daemon' > \
        "$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    write_fake_patcher
}

set_up() {
    WORK_ROOT=$(mktemp -d)
    TEST_ROOT="$WORK_ROOT/test root; \$(touch should-not-run)"
    FAKE_LOG="$WORK_ROOT/commands.log"
    mkdir -p -- "$TEST_ROOT"
    : > "$FAKE_LOG"
    export FORZA_TEST_ROOT="$TEST_ROOT" FAKE_LOG
    export FORZA_FAKE_SECRET_OWNER=0 FORZA_FAKE_SERVICE_STATE=disabled
    export FORZA_FAKE_CONTROLLER_STATE=patched FORZA_FAKE_MOUNTMGR_STATE=patched
    export FORZA_FAKE_DISK_KB=209715200
    write_fake_commands
    make_ready_installation
}

tear_down() {
    rm -rf -- "$WORK_ROOT"
}

run_doctor() {
    output=$("$DOCTOR")
}

test_ready_installation_reports_each_check_and_does_not_mutate() {
    set_up
    local before after
    before=$(snapshot_root)
    run_doctor
    after=$(snapshot_root)
    assert_eq "$after" "$before"
    assert_eq "$(printf '%s\n' "$output" | wc -l)" 8
    assert_contains "$output" 'PASS Steam AppID manifest'
    assert_contains "$output" 'PASS GE-Proton11-3-FM directory'
    assert_contains "$output" 'PASS Xodus service binary'
    assert_contains "$output" 'PASS user-supplied xgameruntime.dll.threading'
    assert_contains "$output" 'PASS Secret Service'
    assert_contains "$output" 'PASS xodus-forza.service disabled state'
    assert_contains "$output" 'PASS controller and mountmgr known-build states'
    assert_contains "$output" 'PASS current free disk space'
    assert_log_not_contains 'start'
    assert_log_not_contains 'enable'
    assert_log_not_contains 'stop'
    assert_log_not_contains 'reload'
    assert_log_not_contains 'sudo'
    assert_log_not_contains 'install'
    tear_down
}

test_each_required_file_check_fails_when_its_input_is_missing() {
    local path label
    for path in \
        '.local/share/Steam/steamapps/appmanifest_2440510.acf' \
        '.local/share/Steam/compatibilitytools.d/GE-Proton11-3-FM' \
        '.local/libexec/xodus-forza/xodus-service' \
        '.local/share/Steam/steamapps/compatdata/2440510/pfx/drive_c/windows/system32/xgameruntime.dll.threading'; do
        set_up
        rm -rf -- "$TEST_ROOT/$path"
        run_doctor
        case "$path" in
            *appmanifest*) label='Steam AppID manifest' ;;
            *GE-Proton*) label='GE-Proton11-3-FM directory' ;;
            *xodus-service) label='Xodus service binary' ;;
            *) label='user-supplied xgameruntime.dll.threading' ;;
        esac
        assert_contains "$output" "FAIL $label"
        tear_down
    done
}

test_xodus_service_binary_must_be_executable() {
    set_up
    chmod -x -- "$TEST_ROOT/.local/libexec/xodus-forza/xodus-service"
    run_doctor
    assert_contains "$output" 'FAIL Xodus service binary'
    tear_down
}

test_xodus_service_binary_rejects_directories_and_symlinks() {
    local service
    set_up
    service="$TEST_ROOT/.local/libexec/xodus-forza/xodus-service"
    rm -- "$service"
    mkdir -- "$service"
    run_doctor
    assert_contains "$output" 'FAIL Xodus service binary'
    tear_down

    set_up
    service="$TEST_ROOT/.local/libexec/xodus-forza/xodus-service"
    rm -- "$service"
    : > "$WORK_ROOT/real-xodus-service"
    chmod +x -- "$WORK_ROOT/real-xodus-service"
    ln -s -- "$WORK_ROOT/real-xodus-service" "$service"
    run_doctor
    assert_contains "$output" 'FAIL Xodus service binary'
    tear_down
}

test_secret_service_accepts_owner_or_kde_provider_and_fails_without_either() {
    set_up
    export FORZA_FAKE_SECRET_OWNER=1
    run_doctor
    assert_contains "$output" 'PASS Secret Service'
    tear_down

    set_up
    rm -- "$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down
}

test_secret_service_rejects_non_definition_text_and_malformed_exec() {
    local definition
    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' '# Name=org.freedesktop.secrets' \
        "Exec=$TEST_ROOT/usr/bin/ksecretd" > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Name=org.freedesktop.secrets' 'Exec=/usr/bin/ksecretd' > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec="/usr/bin/ksecretd" --daemon' > "$definition"
    run_doctor
    assert_contains "$output" 'PASS Secret Service'
    tear_down
}

test_secret_service_rejects_wrong_sections_duplicates_and_untrusted_executables() {
    local definition
    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[Other]' 'Name=org.freedesktop.secrets' \
        "Exec=$TEST_ROOT/usr/bin/ksecretd" > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' '[D-BUS Service]' > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    mkdir -p -- "$TEST_ROOT/providers"
    : > "$TEST_ROOT/providers/ksecretd"
    chmod +x -- "$TEST_ROOT/providers/ksecretd"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        "Exec=$TEST_ROOT/providers/ksecretd" > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    rm -- "$TEST_ROOT/usr/bin/ksecretd"
    : > "$WORK_ROOT/real-ksecretd"
    chmod +x -- "$WORK_ROOT/real-ksecretd"
    ln -s -- "$WORK_ROOT/real-ksecretd" "$TEST_ROOT/usr/bin/ksecretd"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down
}

test_secret_service_first_xdg_definition_shadows_lower_priority_provider() {
    local high_definition low_definition
    set_up
    high_definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    low_definition="$TEST_ROOT/usr/share/dbus-1/services/org.freedesktop.secrets.service"
    mkdir -p -- "$(dirname -- "$low_definition")"
    mkdir -p -- "$TEST_ROOT/providers"
    : > "$TEST_ROOT/providers/ksecretd"
    chmod +x -- "$TEST_ROOT/providers/ksecretd"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        "Exec=$TEST_ROOT/providers/ksecretd" > "$high_definition"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' > "$low_definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down
}

test_secret_service_accepts_valid_crlf_definition() {
    local definition
    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '[D-BUS Service]\r\nName=org.freedesktop.secrets\r\nExec="/usr/bin/ksecretd" --daemon\r\n' > "$definition"
    run_doctor
    assert_contains "$output" 'PASS Secret Service'
    tear_down
}

test_secret_service_rejects_additional_structural_negatives() {
    local definition
    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' '[Other]' 'Name=org.freedesktop.secrets' > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' '[Other]' 'Exec=/usr/bin/ksecretd' > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' 'Exec=/usr/bin/ksecretd' > "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down

    set_up
    definition="$TEST_ROOT/.local/share/dbus-1/services/org.freedesktop.secrets.service"
    rm -- "$definition"
    printf '%s\n' '[D-BUS Service]' 'Name=org.freedesktop.secrets' \
        'Exec=/usr/bin/ksecretd' > "$WORK_ROOT/real-secrets.service"
    ln -s -- "$WORK_ROOT/real-secrets.service" "$definition"
    run_doctor
    assert_contains "$output" 'FAIL Secret Service'
    tear_down
}

test_service_and_known_build_states_distinguish_pass_warn_and_fail() {
    set_up
    export FORZA_FAKE_SERVICE_STATE=enabled
    run_doctor
    assert_contains "$output" 'FAIL xodus-forza.service disabled state'
    tear_down

    set_up
    export FORZA_FAKE_SERVICE_STATE=unknown
    export FORZA_FAKE_CONTROLLER_STATE=original
    export FORZA_FAKE_MOUNTMGR_STATE=original
    run_doctor
    assert_contains "$output" 'WARN xodus-forza.service disabled state'
    assert_contains "$output" 'WARN controller and mountmgr known-build states'
    tear_down

    set_up
    export FORZA_FAKE_CONTROLLER_STATE=unknown
    run_doctor
    assert_contains "$output" 'FAIL controller and mountmgr known-build states'
    tear_down
}

test_free_disk_space_has_pass_warn_and_fail_thresholds() {
    set_up
    export FORZA_FAKE_DISK_KB=209715200
    run_doctor
    assert_contains "$output" 'PASS current free disk space'
    tear_down

    set_up
    export FORZA_FAKE_DISK_KB=41943040
    run_doctor
    assert_contains "$output" 'WARN current free disk space'
    tear_down

    set_up
    export FORZA_FAKE_DISK_KB=1048576
    run_doctor
    assert_contains "$output" 'FAIL current free disk space'
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

run_test test_ready_installation_reports_each_check_and_does_not_mutate
run_test test_each_required_file_check_fails_when_its_input_is_missing
run_test test_xodus_service_binary_must_be_executable
run_test test_xodus_service_binary_rejects_directories_and_symlinks
run_test test_secret_service_accepts_owner_or_kde_provider_and_fails_without_either
run_test test_secret_service_rejects_non_definition_text_and_malformed_exec
run_test test_secret_service_rejects_wrong_sections_duplicates_and_untrusted_executables
run_test test_secret_service_first_xdg_definition_shadows_lower_priority_provider
run_test test_secret_service_accepts_valid_crlf_definition
run_test test_secret_service_rejects_additional_structural_negatives
run_test test_service_and_known_build_states_distinguish_pass_warn_and_fail
run_test test_free_disk_space_has_pass_warn_and_fail_thresholds

printf '%s tests run, %s failed\n' "$tests_run" "$tests_failed"
exit "$status"
