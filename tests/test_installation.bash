#!/usr/bin/env bash

set -u

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL="$ROOT/scripts/install-user"
UNINSTALL="$ROOT/scripts/uninstall-user"

status=0
tests_run=0
tests_failed=0
current_test_failed=0

readonly -a INSTALLED_RELATIVE_PATHS=(
    '.local/bin/forza-linux'
    '.local/bin/forza-doctor'
    '.local/libexec/forza-motorsport-linux/patch-known-build'
    '.local/share/forza-motorsport-linux/supported-builds.toml'
    '.local/libexec/xodus-forza/xodus-service'
    '.local/libexec/xodus-forza/xodus-cli'
    '.local/libexec/xodus-forza/xodus-overlay'
    '.config/systemd/user/xodus-forza.service'
)
readonly MANIFEST_RELATIVE_PATH='.local/state/forza-motorsport-linux/install-manifest'

fail() { current_test_failed=1; printf 'FAIL: %s\n' "$*" >&2; return 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"; }
assert_file_exists() { [[ -f $1 && ! -L $1 ]] || fail "regular file missing [$1]"; }
assert_path_absent() { [[ ! -e $1 && ! -L $1 ]] || fail "path unexpectedly exists [$1]"; }
assert_command_fails() {
    if "$@"; then
        fail "command unexpectedly succeeded [$*]"
    fi
}
assert_log_is_daemon_reload_only() {
    [[ $(wc -l < "$SYSTEMCTL_LOG") -gt 0 ]] || fail 'systemctl was not called'
    ! rg -vx 'systemctl --user daemon-reload' "$SYSTEMCTL_LOG" >/dev/null ||
        fail 'systemctl log contains a command other than daemon-reload'
}

snapshot_directory() {
    (
        cd -- "$1"
        tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner -cf - .
    ) | sha256sum
}

make_xodus_build() {
    local artifact
    mkdir -p -- "$XODUS_BUILD"
    for artifact in "$@"; do
        printf '#!/usr/bin/env bash\nexit 0\n' > "$XODUS_BUILD/$artifact"
        chmod 755 -- "$XODUS_BUILD/$artifact"
    done
}

set_up() {
    WORK_ROOT=$(mktemp -d)
    ESCAPE_SENTINEL="$WORK_ROOT/escaped"
    FAKE_ROOT="$WORK_ROOT/fake root; \$(touch $ESCAPE_SENTINEL)"
    XODUS_BUILD="$WORK_ROOT/xodus build"
    FAKE_BIN="$WORK_ROOT/fake-bin"
    SYSTEMCTL_LOG="$WORK_ROOT/systemctl.log"
    mkdir -p -- "$FAKE_ROOT" "$FAKE_BIN"
    : > "$SYSTEMCTL_LOG"
    cat > "$FAKE_BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$SYSTEMCTL_LOG"
[[ ${1:-} == --user && ${2:-} == daemon-reload && $# == 2 ]]
EOF
    chmod 755 -- "$FAKE_BIN/systemctl"
    export SYSTEMCTL_LOG
    make_xodus_build xodus-service xodus-cli xodus-overlay
    printf 'do not remove\n' > "$FAKE_ROOT/keep-me"
}

tear_down() {
    rm -rf -- "$WORK_ROOT"
}

run_install() {
    PATH="$FAKE_BIN:$PATH" FORZA_INSTALL_ROOT="$FAKE_ROOT" "$INSTALL" --root "$FAKE_ROOT" --xodus-build-dir "$XODUS_BUILD"
}

run_uninstall() {
    PATH="$FAKE_BIN:$PATH" FORZA_INSTALL_ROOT="$FAKE_ROOT" "$UNINSTALL" --root "$FAKE_ROOT"
}

manifest_path() { printf '%s\n' "$FAKE_ROOT/$MANIFEST_RELATIVE_PATH"; }

assert_installed_files_match_manifest() {
    local relative path mode digest manifest
    manifest=$(manifest_path)
    assert_file_exists "$manifest" || return 1
    assert_eq "$(head -n 1 -- "$manifest")" 'forza-motorsport-linux-install-v1' || return 1
    assert_eq "$(tail -n +2 -- "$manifest" | wc -l)" '8' || return 1
    for relative in "${INSTALLED_RELATIVE_PATHS[@]}"; do
        path="$FAKE_ROOT/$relative"
        assert_file_exists "$path" || return 1
        mode=$(stat -c '%a' -- "$path")
        digest=$(sha256sum -- "$path" | awk '{print $1}')
        rg -x -F -- "$relative"$'\t'"$mode"$'\t'"$digest" "$manifest" >/dev/null ||
            fail "manifest does not record mode and digest for [$relative]"
    done
}

assert_manifested_files_absent() {
    local relative
    for relative in "${INSTALLED_RELATIVE_PATHS[@]}"; do
        assert_path_absent "$FAKE_ROOT/$relative" || return 1
    done
    assert_path_absent "$(manifest_path)"
}

test_round_trip_touches_only_manifested_paths() {
    set_up
    run_install || return 1
    assert_installed_files_match_manifest || return 1
    run_uninstall || return 1
    assert_manifested_files_absent || return 1
    assert_file_exists "$FAKE_ROOT/keep-me" || return 1
    assert_path_absent "$ESCAPE_SENTINEL" || return 1
    assert_log_is_daemon_reload_only
    tear_down
}

test_identical_reinstall_keeps_bytes_and_manifest_stable() {
    set_up
    run_install || return 1
    local before after
    before=$(snapshot_directory "$FAKE_ROOT")
    run_install || return 1
    after=$(snapshot_directory "$FAKE_ROOT")
    assert_eq "$after" "$before" || return 1
    assert_installed_files_match_manifest || return 1
    assert_log_is_daemon_reload_only
    tear_down
}

test_fresh_identical_destination_is_rejected_without_a_manifest() {
    set_up
    local destination="$FAKE_ROOT/.local/bin/forza-linux" before
    mkdir -p -- "${destination%/*}"
    cp --preserve=mode -- "$ROOT/bin/forza-linux" "$destination"
    before=$(sha256sum -- "$destination")
    assert_command_fails run_install || return 1
    assert_eq "$(sha256sum -- "$destination")" "$before" || return 1
    assert_path_absent "$(manifest_path)"
    tear_down
}

test_fresh_install_requires_complete_xodus_build() {
    set_up
    rm -- "$XODUS_BUILD/xodus-overlay"
    assert_command_fails run_install || return 1
    assert_path_absent "$FAKE_ROOT/.local/libexec/xodus-forza" || return 1
    assert_path_absent "$(manifest_path)"
    tear_down
}

test_xodus_artifacts_reject_symlinks_and_non_executables() {
    local artifact
    for artifact in xodus-service xodus-cli xodus-overlay; do
        set_up
        chmod -x -- "$XODUS_BUILD/$artifact"
        assert_command_fails run_install || return 1
        assert_path_absent "$(manifest_path)" || return 1
        tear_down

        set_up
        mv -- "$XODUS_BUILD/$artifact" "$XODUS_BUILD/$artifact.real"
        ln -s -- "$XODUS_BUILD/$artifact.real" "$XODUS_BUILD/$artifact"
        assert_command_fails run_install || return 1
        assert_path_absent "$(manifest_path)" || return 1
        tear_down
    done
}

test_xodus_build_rejects_an_ancestor_symlink() {
    set_up
    local real_parent="$WORK_ROOT/real-build-parent" linked_parent="$WORK_ROOT/build-link"
    mkdir -p -- "$real_parent"
    mv -- "$XODUS_BUILD" "$real_parent/build"
    ln -s -- "$real_parent" "$linked_parent"
    XODUS_BUILD="$linked_parent/build"
    assert_command_fails run_install || return 1
    assert_path_absent "$(manifest_path)"
    tear_down
}

test_root_ancestor_symlink_is_rejected_without_outside_write() {
    set_up
    local real_parent="$WORK_ROOT/real-parent" linked_parent="$WORK_ROOT/linked-parent"
    mkdir -p -- "$real_parent/root"
    printf 'outside sentinel\n' > "$real_parent/sentinel"
    ln -s -- "$real_parent" "$linked_parent"
    FAKE_ROOT="$linked_parent/root"
    assert_command_fails run_install || return 1
    assert_eq "$(<"$real_parent/sentinel")" 'outside sentinel' || return 1
    assert_path_absent "$real_parent/root/.local/bin/forza-linux"
    tear_down
}

test_conflicting_existing_file_is_preserved() {
    set_up
    local conflict="$FAKE_ROOT/.local/bin/forza-linux" before
    mkdir -p -- "$(dirname -- "$conflict")"
    printf 'user file\n' > "$conflict"
    before=$(sha256sum -- "$conflict")
    assert_command_fails run_install || return 1
    assert_eq "$(sha256sum -- "$conflict")" "$before" || return 1
    assert_path_absent "$(manifest_path)" || return 1
    assert_path_absent "$FAKE_ROOT/.local/bin/forza-doctor"
    tear_down
}

test_mid_publish_failure_rolls_back_all_new_files() {
    set_up
    export FORZA_INSTALL_FAIL_AFTER_PUBLISH=3
    assert_command_fails run_install || return 1
    unset FORZA_INSTALL_FAIL_AFTER_PUBLISH
    assert_manifested_files_absent || return 1
    assert_file_exists "$FAKE_ROOT/keep-me" || return 1
    assert_path_absent "$ESCAPE_SENTINEL"
    tear_down
}

test_rollback_preserves_a_replacement_made_during_failure() {
    set_up
    local destination="$FAKE_ROOT/.local/bin/forza-linux" replacement="$WORK_ROOT/replacement" attacker_pid
    (
        for _ in {1..100}; do [[ -f $destination ]] && break; sleep 0.01; done
        [[ -f $destination ]] || exit 1
        printf 'replacement by user\n' > "$replacement"
        mv -f -- "$replacement" "$destination"
    ) &
    attacker_pid=$!
    export FORZA_INSTALL_PAUSE_AFTER_PUBLISH=1 FORZA_INSTALL_FAIL_AFTER_PUBLISH=3
    assert_command_fails run_install || return 1
    wait "$attacker_pid" || return 1
    unset FORZA_INSTALL_PAUSE_AFTER_PUBLISH FORZA_INSTALL_FAIL_AFTER_PUBLISH
    assert_eq "$(<"$destination")" 'replacement by user' || return 1
    assert_path_absent "$(manifest_path)"
    tear_down
}

test_parent_swap_during_publish_preserves_outside_sentinel() {
    set_up
    local outside="$WORK_ROOT/outside" attacker_pid
    mkdir -p -- "$outside"
    printf 'outside sentinel\n' > "$outside/sentinel"
    (
        for _ in {1..100}; do [[ -f $FAKE_ROOT/.local/bin/forza-linux ]] && break; sleep 0.01; done
        [[ -f $FAKE_ROOT/.local/bin/forza-linux ]] || exit 1
        mv -- "$FAKE_ROOT/.local" "$FAKE_ROOT/local-real"
        ln -s -- "$outside" "$FAKE_ROOT/.local"
    ) &
    attacker_pid=$!
    export FORZA_INSTALL_PAUSE_AFTER_PUBLISH=1 FORZA_INSTALL_FAIL_AFTER_PUBLISH=3
    assert_command_fails run_install || return 1
    wait "$attacker_pid" || return 1
    unset FORZA_INSTALL_PAUSE_AFTER_PUBLISH FORZA_INSTALL_FAIL_AFTER_PUBLISH
    assert_eq "$(<"$outside/sentinel")" 'outside sentinel' || return 1
    assert_path_absent "$outside/bin/forza-doctor"
    tear_down
}

test_concurrent_destination_at_publish_boundary_is_preserved() {
    set_up
    local destination="$FAKE_ROOT/.local/bin/forza-doctor" attacker_pid
    (
        for _ in {1..100}; do [[ -f $FAKE_ROOT/.local/bin/forza-linux ]] && break; sleep 0.01; done
        [[ -f $FAKE_ROOT/.local/bin/forza-linux ]] || exit 1
        printf 'concurrent user destination\n' > "$destination"
    ) &
    attacker_pid=$!
    export FORZA_INSTALL_PAUSE_BEFORE_PUBLISH=2
    assert_command_fails run_install || return 1
    wait "$attacker_pid" || return 1
    unset FORZA_INSTALL_PAUSE_BEFORE_PUBLISH
    assert_eq "$(<"$destination")" 'concurrent user destination' || return 1
    assert_path_absent "$(manifest_path)"
    tear_down
}

test_cleanup_symlink_swap_never_traverses_outside_root() {
    set_up
    local outside="$WORK_ROOT/outside" stage local_dir attacker_pid
    mkdir -p -- "$outside"
    printf 'outside sentinel\n' > "$outside/sentinel"
    (
        for _ in {1..100}; do
            for stage in "$FAKE_ROOT"/.forza-install-*; do
                local_dir="$stage/.local"
                [[ ! -e $FAKE_ROOT/.local/bin/forza-linux ]] || continue
                [[ -d $local_dir && ! -L $local_dir ]] || continue
                mv -- "$local_dir" "$stage/local-real"
                ln -s -- "$outside" "$local_dir"
                exit 0
            done
            sleep 0.01
        done
        exit 1
    ) &
    attacker_pid=$!
    export FORZA_INSTALL_FAIL_AFTER_PUBLISH=1 FORZA_INSTALL_PAUSE_AFTER_CLEANUP_LSTAT=1
    assert_command_fails run_install || return 1
    wait "$attacker_pid" || return 1
    unset FORZA_INSTALL_FAIL_AFTER_PUBLISH FORZA_INSTALL_PAUSE_AFTER_CLEANUP_LSTAT
    assert_eq "$(<"$outside/sentinel")" 'outside sentinel' || return 1
    assert_path_absent "$outside/bin/forza-linux"
    tear_down
}

test_failure_before_manifest_rolls_back_published_files() {
    set_up
    export FORZA_INSTALL_FAIL_BEFORE_MANIFEST=1
    assert_command_fails run_install || return 1
    unset FORZA_INSTALL_FAIL_BEFORE_MANIFEST
    assert_manifested_files_absent || return 1
    assert_file_exists "$FAKE_ROOT/keep-me"
    tear_down
}

write_bad_manifest() {
    local line=$1 manifest
    manifest=$(manifest_path)
    mkdir -p -- "$(dirname -- "$manifest")"
    printf '%s\n%s\n' 'forza-motorsport-linux-install-v1' "$line" > "$manifest"
}

test_uninstall_rejects_malicious_manifest_paths_before_delete() {
    local manifest target outside
    for target in \
        '/tmp/forza-escape\t755\t0000000000000000000000000000000000000000000000000000000000000000' \
        '../keep-me\t755\t0000000000000000000000000000000000000000000000000000000000000000'; do
        set_up
        run_install || return 1
        manifest=$(manifest_path)
        outside="$WORK_ROOT/outside"
        printf 'outside\n' > "$outside"
        write_bad_manifest "$(printf '%b' "$target")"
        assert_command_fails run_uninstall || return 1
        assert_file_exists "$FAKE_ROOT/.local/bin/forza-linux" || return 1
        assert_file_exists "$outside" || return 1
        tear_down
    done

    set_up
    run_install || return 1
    manifest=$(manifest_path)
    outside="$WORK_ROOT/outside-manifest"
    printf '%s\n' 'forza-motorsport-linux-install-v1' > "$outside"
    rm -- "$manifest"
    ln -s -- "$outside" "$manifest"
    assert_command_fails run_uninstall || return 1
    assert_file_exists "$FAKE_ROOT/.local/bin/forza-linux" || return 1
    assert_file_exists "$outside"
    tear_down
}

test_uninstall_rejects_duplicate_and_out_of_allowlist_manifest_entries() {
    local first_line
    set_up
    run_install || return 1
    first_line=$(sed -n '2p' "$(manifest_path)")
    printf '%s\n' "$first_line" >> "$(manifest_path)"
    assert_command_fails run_uninstall || return 1
    assert_file_exists "$FAKE_ROOT/.local/bin/forza-linux" || return 1
    tear_down

    set_up
    run_install || return 1
    printf '%s\n' $'.local/share/forza-motorsport-linux/other\t644\t0000000000000000000000000000000000000000000000000000000000000000' >> "$(manifest_path)"
    assert_command_fails run_uninstall || return 1
    assert_file_exists "$FAKE_ROOT/.local/bin/forza-linux" || return 1
    tear_down
}

test_uninstall_preserves_modified_installed_file_with_warning() {
    set_up
    run_install || return 1
    local modified="$FAKE_ROOT/.local/bin/forza-linux" output
    printf 'user modified launcher\n' > "$modified"
    output=$(run_uninstall 2>&1) || return 1
    assert_file_exists "$modified" || return 1
    [[ $output == *'warning: preserving modified file'* ]] || fail 'modified file warning missing'
    assert_path_absent "$(manifest_path)" || return 1
    assert_path_absent "$FAKE_ROOT/.local/bin/forza-doctor" || return 1
    assert_log_is_daemon_reload_only
    tear_down
}

test_uninstall_preserves_preexisting_empty_project_directory() {
    set_up
    mkdir -p -- "$FAKE_ROOT/.local/libexec/xodus-forza"
    run_install || return 1
    run_uninstall || return 1
    [[ -d $FAKE_ROOT/.local/libexec/xodus-forza && ! -L $FAKE_ROOT/.local/libexec/xodus-forza ]] ||
        fail 'pre-existing project directory was removed'
    tear_down
}

test_uninstall_is_idempotent_and_only_daemon_reloads() {
    set_up
    run_install || return 1
    run_uninstall || return 1
    local before after
    before=$(cat -- "$SYSTEMCTL_LOG")
    run_uninstall || return 1
    after=$(cat -- "$SYSTEMCTL_LOG")
    assert_eq "$after" "$before" || return 1
    assert_manifested_files_absent || return 1
    assert_log_is_daemon_reload_only
    tear_down
}

test_forza_install_root_seam_never_uses_home() {
    set_up
    local isolated_home="$WORK_ROOT/live-home" before after
    mkdir -p -- "$isolated_home"
    before=$(snapshot_directory "$isolated_home")
    PATH="$FAKE_BIN:$PATH" HOME="$isolated_home" FORZA_INSTALL_ROOT="$FAKE_ROOT" \
        "$INSTALL" --xodus-build-dir "$XODUS_BUILD" || return 1
    after=$(snapshot_directory "$isolated_home")
    assert_installed_files_match_manifest || return 1
    assert_eq "$after" "$before" || return 1
    assert_path_absent "$isolated_home/.local/bin/forza-linux" || return 1
    assert_path_absent "$ESCAPE_SENTINEL"
    tear_down
}

run_test() {
    ((tests_run += 1))
    current_test_failed=0
    unset FORZA_INSTALL_FAIL_AFTER_PUBLISH FORZA_INSTALL_FAIL_BEFORE_MANIFEST \
        FORZA_INSTALL_PAUSE_AFTER_PUBLISH FORZA_INSTALL_PAUSE_BEFORE_PUBLISH \
        FORZA_INSTALL_PAUSE_AFTER_CLEANUP_LSTAT
    "$1" || current_test_failed=1
    if ((current_test_failed == 0)); then
        printf 'PASS: %s\n' "$1"
    else
        ((tests_failed += 1))
        status=1
    fi
    [[ -n ${WORK_ROOT:-} ]] && rm -rf -- "$WORK_ROOT"
}

run_test test_round_trip_touches_only_manifested_paths
run_test test_identical_reinstall_keeps_bytes_and_manifest_stable
run_test test_fresh_identical_destination_is_rejected_without_a_manifest
run_test test_fresh_install_requires_complete_xodus_build
run_test test_xodus_artifacts_reject_symlinks_and_non_executables
run_test test_xodus_build_rejects_an_ancestor_symlink
run_test test_root_ancestor_symlink_is_rejected_without_outside_write
run_test test_conflicting_existing_file_is_preserved
run_test test_mid_publish_failure_rolls_back_all_new_files
run_test test_rollback_preserves_a_replacement_made_during_failure
run_test test_parent_swap_during_publish_preserves_outside_sentinel
run_test test_concurrent_destination_at_publish_boundary_is_preserved
run_test test_cleanup_symlink_swap_never_traverses_outside_root
run_test test_failure_before_manifest_rolls_back_published_files
run_test test_uninstall_rejects_malicious_manifest_paths_before_delete
run_test test_uninstall_rejects_duplicate_and_out_of_allowlist_manifest_entries
run_test test_uninstall_preserves_modified_installed_file_with_warning
run_test test_uninstall_preserves_preexisting_empty_project_directory
run_test test_uninstall_is_idempotent_and_only_daemon_reloads
run_test test_forza_install_root_seam_never_uses_home

printf '%s tests run, %s failed\n' "$tests_run" "$tests_failed"
exit "$status"
