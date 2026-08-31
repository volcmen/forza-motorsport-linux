import errno
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PATCHER_PATH = ROOT / "scripts" / "patch-known-build"
loader = importlib.machinery.SourceFileLoader("patch_known_build", str(PATCHER_PATH))
module_spec = importlib.util.spec_from_loader(loader.name, loader)
patcher = importlib.util.module_from_spec(module_spec)
sys.modules[loader.name] = patcher
loader.exec_module(patcher)

PatchSpec = patcher.PatchSpec
PatchState = patcher.PatchState
PatchMismatch = patcher.PatchMismatch
UnknownBuild = patcher.UnknownBuild
apply_forza = patcher.apply_forza
apply_target = patcher.apply_target
inspect_target = patcher.inspect_target
load_specs = patcher.load_specs
resolve_forza_targets = patcher.resolve_forza_targets
restore_forza = patcher.restore_forza
restore_target = patcher.restore_target
sha256 = patcher.sha256


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def synthetic_original(spec):
    size = max(edit.offset + len(edit.before) for edit in spec.edits)
    data = bytearray(size)
    for edit in spec.edits:
        data[edit.offset : edit.offset + len(edit.before)] = edit.before
    return bytes(data)


def synthetic_patched(spec):
    data = bytearray(synthetic_original(spec))
    for edit in spec.edits:
        data[edit.offset : edit.offset + len(edit.after)] = edit.after
    return bytes(data)


def synthetic_spec(spec):
    original = synthetic_original(spec)
    patched = bytearray(original)
    for edit in spec.edits:
        patched[edit.offset : edit.offset + len(edit.after)] = edit.after
    return replace(
        spec,
        original_sha256=sha256_bytes(original),
        patched_sha256=sha256_bytes(bytes(patched)),
    )


@pytest.fixture
def supported_builds():
    return load_specs(ROOT / "manifests" / "supported-builds.toml")


@pytest.fixture
def controller_spec(supported_builds):
    return synthetic_spec(supported_builds["windows.gaming.input.dll"])


@pytest.fixture
def synthetic_specs(supported_builds):
    return {name: synthetic_spec(spec) for name, spec in supported_builds.items()}


def make_patched_pair(tmp_path, controller_spec):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(synthetic_original(controller_spec))
    result = apply_target(target, controller_spec, tmp_path / "backups")
    return target, result.backup


def populate_forza_root(steam_root, specs):
    for target in resolve_forza_targets(steam_root):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(synthetic_original(specs[target.name]))


def replace_with_nonregular(target, replacement_type):
    replacement = target.with_name(f"{target.name}-{replacement_type}")
    if replacement_type == "symlink":
        link_text = "concurrent-symlink-destination"
        (target.parent / link_text).write_bytes(b"symlink sentinel")
        replacement.symlink_to(link_text)
        identity = replacement.lstat().st_ino
        os.replace(replacement, target)
        return identity, link_text

    replacement.mkdir()
    (replacement / "sentinel").write_bytes(b"directory sentinel")
    identity = replacement.stat().st_ino
    target.unlink()
    replacement.rename(target)
    return identity, b"directory sentinel"


def assert_nonregular_replacement(target, replacement_type, identity, detail):
    assert target.lstat().st_ino == identity
    if replacement_type == "symlink":
        assert target.is_symlink()
        assert os.readlink(target) == detail
    else:
        assert target.is_dir()
        assert (target / "sentinel").read_bytes() == detail


def assert_only_patched_recovery_remains(target, spec):
    recovery = target.with_name(f".{target.name}.forza-recovery-publication-conflict")
    assert recovery.read_bytes() == synthetic_patched(spec)
    assert list(target.parent.glob(f".{target.name}.forza-*")) == [recovery]


def test_manifest_records_the_reviewed_exact_build_hashes(supported_builds):
    assert supported_builds["windows.gaming.input.dll"].original_sha256 == (
        "57538166ba052dc763232880f18a4b48a07ab735a61b5b8fbf5a8f56a05f2ca5"
    )
    assert supported_builds["windows.gaming.input.dll"].patched_sha256 == (
        "f520d9b4d44d9cdc11e67396583a1ccc8c53eca60e0b3a69233f358556b59245"
    )
    assert supported_builds["mountmgr.sys"].original_sha256 == (
        "764f72bad9e639aead6535045f953815d9013ecb39b24b3b0027a70b846712b6"
    )
    assert supported_builds["mountmgr.sys"].patched_sha256 == (
        "61fd9e9bb22ec1e92a3156c55cbfe77e22df49186b2de8f42b26d71cae2349d2"
    )


def test_known_original_is_patched_backed_up_atomically_and_preserves_mode(
    tmp_path, controller_spec
):
    target = tmp_path / "windows.gaming.input.dll"
    original = synthetic_original(controller_spec)
    target.write_bytes(original)
    target.chmod(0o640)
    before_inode = target.stat().st_ino

    result = apply_target(target, controller_spec, tmp_path / "backups")

    assert result.before == controller_spec.original_sha256
    assert result.after == controller_spec.patched_sha256
    assert result.backup.read_bytes() == original
    assert controller_spec.original_sha256 in result.backup.name
    assert target.stat().st_ino != before_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert inspect_target(target, controller_spec) is PatchState.PATCHED


def test_success_retains_original_and_never_unlinks_a_private_name_swap(
    tmp_path, controller_spec, monkeypatch, capsys
):
    target = tmp_path / "windows.gaming.input.dll"
    original = synthetic_original(controller_spec)
    target.write_bytes(original)
    original_inode = target.stat().st_ino
    late_replacement = tmp_path / "late-private-name-replacement"
    late_replacement.symlink_to("late-user-link")
    late_identity = late_replacement.lstat().st_ino
    real_unlink = patcher.os.unlink

    def replace_before_private_unlink(path, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        if (
            isinstance(path, str)
            and path.startswith(f".{target.name}.forza-")
            and dir_fd is not None
            and Path(os.readlink(f"/proc/self/fd/{dir_fd}")) == target.parent
        ):
            os.replace(late_replacement, target.parent / path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patcher.os, "unlink", replace_before_private_unlink)
    apply_target(target, controller_spec, tmp_path / "backups")

    assert late_replacement.is_symlink()
    assert late_replacement.lstat().st_ino == late_identity
    assert os.readlink(late_replacement) == "late-user-link"
    recovery = target.with_name(f".{target.name}.forza-recovery-publication-success")
    assert recovery.read_bytes() == original
    assert recovery.stat().st_ino == original_inode
    assert str(recovery) in capsys.readouterr().err


def test_failed_exchange_reports_and_retains_a_private_name_replacement(
    tmp_path, controller_spec, monkeypatch, capsys
):
    target = tmp_path / "windows.gaming.input.dll"
    original = synthetic_original(controller_spec)
    target.write_bytes(original)
    late_replacement = tmp_path / "late-failed-exchange-replacement"
    late_replacement.symlink_to("late-failed-exchange-link")
    late_identity = late_replacement.lstat().st_ino
    real_renameat2 = patcher._renameat2
    failed = False

    def fail_exchange_after_name_swap(source_fd, source, destination_fd, destination, flags):
        nonlocal failed
        if flags == patcher.RENAME_EXCHANGE and not failed:
            failed = True
            os.replace(late_replacement, target.parent / source)
            raise OSError("simulated exchange failure")
        return real_renameat2(source_fd, source, destination_fd, destination, flags)

    monkeypatch.setattr(patcher, "_renameat2", fail_exchange_after_name_swap)
    with pytest.raises(OSError, match="simulated exchange failure"):
        apply_target(target, controller_spec, tmp_path / "backups")

    assert target.read_bytes() == original
    recovery = target.with_name(f".{target.name}.forza-recovery-unpublished-staging")
    assert recovery.is_symlink()
    assert recovery.lstat().st_ino == late_identity
    assert os.readlink(recovery) == "late-failed-exchange-link"
    assert str(recovery) in capsys.readouterr().err


def test_unknown_hash_is_refused_without_writing(tmp_path, controller_spec):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"unknown build")
    before = target.read_bytes()
    backup_root = tmp_path / "backups"

    with pytest.raises(UnknownBuild):
        apply_target(target, controller_spec, backup_root)

    assert target.read_bytes() == before
    assert not backup_root.exists()


def test_edit_precondition_failure_is_refused_without_backup(tmp_path, controller_spec):
    target = tmp_path / "windows.gaming.input.dll"
    original = synthetic_original(controller_spec)
    target.write_bytes(original)
    malformed = replace(
        controller_spec,
        edits=(
            replace(controller_spec.edits[0], before=b"\0\0"),
            controller_spec.edits[1],
        ),
    )

    with pytest.raises(PatchMismatch):
        apply_target(target, malformed, tmp_path / "backups")

    assert target.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_restore_requires_matching_patched_and_original_hashes(tmp_path, controller_spec):
    target, backup = make_patched_pair(tmp_path, controller_spec)

    result = restore_target(target, backup, controller_spec)

    assert result.before == controller_spec.patched_sha256
    assert result.after == controller_spec.original_sha256
    assert sha256(target) == controller_spec.original_sha256


def test_restore_refuses_mismatched_current_or_backup_without_writing(
    tmp_path, controller_spec
):
    target, backup = make_patched_pair(tmp_path, controller_spec)
    patched = target.read_bytes()
    target.write_bytes(b"unknown current")

    with pytest.raises(UnknownBuild):
        restore_target(target, backup, controller_spec)

    assert target.read_bytes() == b"unknown current"
    target.write_bytes(patched)
    backup.write_bytes(b"unknown backup")

    with pytest.raises(UnknownBuild):
        restore_target(target, backup, controller_spec)

    assert target.read_bytes() == patched


def test_forza_scope_resolves_only_known_64_bit_targets(tmp_path):
    targets = resolve_forza_targets(tmp_path)

    assert [target.relative_to(tmp_path).as_posix() for target in targets] == [
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/x86_64-windows/windows.gaming.input.dll",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/windows.gaming.input.dll",
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/x86_64-windows/mountmgr.sys",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/drivers/mountmgr.sys",
    ]


def test_apply_forza_preflights_every_target_before_any_write(tmp_path, synthetic_specs):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)
    targets[-1].write_bytes(b"unknown build")
    before = {target: target.read_bytes() for target in targets}

    with pytest.raises(UnknownBuild):
        apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")

    assert {target: target.read_bytes() for target in targets} == before
    assert not (tmp_path / "backups").exists()


def test_apply_forza_records_backups_and_restore_forza_preflights_all_pairs(
    tmp_path, synthetic_specs
):
    populate_forza_root(tmp_path, synthetic_specs)
    result = apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")
    targets = resolve_forza_targets(tmp_path)
    record = json.loads(result.backup_manifest.read_text())

    assert len(record["targets"]) == 4
    assert all(inspect_target(target, synthetic_specs[target.name]) is PatchState.PATCHED for target in targets)
    targets[0].write_bytes(synthetic_original(synthetic_specs[targets[0].name]))

    with pytest.raises(UnknownBuild):
        restore_forza(tmp_path, result.backup_manifest, synthetic_specs)

    assert inspect_target(targets[1], synthetic_specs[targets[1].name]) is PatchState.PATCHED


def test_apply_target_restores_verified_backup_after_post_replace_failure(
    tmp_path, controller_spec, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    original = synthetic_original(controller_spec)
    target.write_bytes(original)
    real_replace = patcher._atomic_replace_at
    replacements = 0

    def report_wrong_identity_once(*args, **kwargs):
        nonlocal replacements
        identity = real_replace(*args, **kwargs)
        replacements += 1
        return (0, 0) if replacements == 1 else identity

    monkeypatch.setattr(patcher, "_atomic_replace_at", report_wrong_identity_once)

    with pytest.raises(PatchMismatch, match="patched target verification failed"):
        apply_target(target, controller_spec, tmp_path / "backups")

    assert target.read_bytes() == original
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_apply_forza_rolls_back_when_backup_manifest_write_fails(
    tmp_path, synthetic_specs, monkeypatch
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)

    def fail_manifest(*_args):
        raise OSError("manifest write failure")

    monkeypatch.setattr(patcher, "_write_backup_manifest", fail_manifest)

    with pytest.raises(OSError, match="manifest write failure"):
        apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.ORIGINAL
        for target in targets
    )


def test_apply_forza_reports_original_and_aggregated_rollback_preflight_errors(
    tmp_path, synthetic_specs, monkeypatch
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)

    def corrupt_backup_then_fail(_steam_root, backup_root, _applied):
        next(backup_root.glob("*.dll")).write_bytes(b"corrupt backup")
        raise OSError("manifest write failure")

    monkeypatch.setattr(patcher, "_write_backup_manifest", corrupt_backup_then_fail)

    with pytest.raises(
        patcher.PatcherError,
        match="manifest write failure.*rollback preflight failed",
    ):
        apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.PATCHED
        for target in targets
    )


def test_backup_name_collision_never_overwrites_existing_backup(
    tmp_path, controller_spec, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(synthetic_original(controller_spec))
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    real_link = patcher.os.link
    collisions = []

    def create_concurrent_backup(source, destination, *args, **kwargs):
        if not collisions and Path(destination).parent == backup_root:
            Path(destination).write_bytes(b"concurrent backup")
            collisions.append(Path(destination))
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(patcher.os, "link", create_concurrent_backup)
    result = apply_target(target, controller_spec, backup_root)

    assert collisions[0].read_bytes() == b"concurrent backup"
    assert result.backup != collisions[0]


def test_apply_target_restores_concurrent_unknown_replacement(
    tmp_path, controller_spec
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(synthetic_original(controller_spec))
    replacement = tmp_path / "concurrent-replacement"
    replacement.write_bytes(b"concurrent unknown target")
    replacement_inode = replacement.stat().st_ino

    def replace_after_backup(_entry):
        os.replace(replacement, target)

    with pytest.raises(patcher.PatcherError, match="changed during publication"):
        apply_target(
            target,
            controller_spec,
            tmp_path / "backups",
            on_backup=replace_after_backup,
        )

    assert target.read_bytes() == b"concurrent unknown target"
    assert target.stat().st_ino == replacement_inode
    assert_only_patched_recovery_remains(target, controller_spec)
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert backups[0].read_bytes() == synthetic_original(controller_spec)


@pytest.mark.parametrize("replacement_type", ("symlink", "directory"))
def test_apply_target_restores_concurrent_nonregular_replacement(
    tmp_path, controller_spec, replacement_type
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(synthetic_original(controller_spec))
    replacement = None

    def replace_after_backup(_entry):
        nonlocal replacement
        replacement = replace_with_nonregular(target, replacement_type)

    with pytest.raises(patcher.PatcherError, match="changed during publication"):
        apply_target(
            target,
            controller_spec,
            tmp_path / "backups",
            on_backup=replace_after_backup,
        )

    assert replacement is not None
    assert_nonregular_replacement(target, replacement_type, *replacement)
    assert_only_patched_recovery_remains(target, controller_spec)


def test_directory_fsync_failure_after_exchange_restores_concurrent_symlink(
    tmp_path, controller_spec, monkeypatch, capsys
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(synthetic_original(controller_spec))
    replacement = None
    real_fsync = patcher.os.fsync
    failed = False

    def fail_first_target_directory_fsync(descriptor):
        nonlocal failed
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if (
            not failed
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
            and descriptor_path == target.parent
        ):
            failed = True
            raise OSError("simulated publication directory fsync failure")
        return real_fsync(descriptor)

    def replace_after_backup(_entry):
        nonlocal replacement
        replacement = replace_with_nonregular(target, "symlink")

    monkeypatch.setattr(patcher.os, "fsync", fail_first_target_directory_fsync)
    with pytest.raises(
        patcher.PatcherError, match="publication durability failed"
    ) as raised:
        apply_target(
            target,
            controller_spec,
            tmp_path / "backups",
            on_backup=replace_after_backup,
        )

    assert failed
    assert replacement is not None
    assert_nonregular_replacement(target, "symlink", *replacement)
    assert_only_patched_recovery_remains(target, controller_spec)
    recovery = target.with_name(f".{target.name}.forza-recovery-publication-conflict")
    assert str(target) in str(raised.value)
    assert str(recovery) in capsys.readouterr().err


def test_conflict_restore_reports_secondary_public_replacement(tmp_path, capsys):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"second concurrent public target")
    target_inode = target.stat().st_ino
    displaced = f".{target.name}.forza-displaced"
    (tmp_path / displaced).symlink_to("first-concurrent-link")
    displaced_inode = (tmp_path / displaced).lstat().st_ino
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)
    try:
        restored = patcher._restore_displaced_after_conflict(
            parent_fd,
            target.name,
            displaced,
            target,
        )
    finally:
        os.close(parent_fd)

    assert restored
    assert target.is_symlink()
    assert target.lstat().st_ino == displaced_inode
    assert os.readlink(target) == "first-concurrent-link"
    recovery = tmp_path / f".{target.name}.forza-recovery-publication-conflict"
    assert recovery.read_bytes() == b"second concurrent public target"
    assert recovery.stat().st_ino == target_inode
    assert str(recovery) in capsys.readouterr().err
    assert not (tmp_path / displaced).exists()


def test_second_exchange_io_failure_reports_deterministic_object_locations(
    tmp_path, controller_spec, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    original = synthetic_original(controller_spec)
    patched = synthetic_patched(controller_spec)
    target.write_bytes(original)
    expected_identity = (target.stat().st_dev, target.stat().st_ino)
    concurrent = tmp_path / "concurrent-symlink"
    concurrent.symlink_to("concurrent-link-text")
    concurrent_inode = concurrent.lstat().st_ino
    os.replace(concurrent, target)
    real_renameat2 = patcher._renameat2
    exchange_calls = 0

    def fail_restorative_exchange(source_fd, source, destination_fd, destination, flags):
        nonlocal exchange_calls
        if flags == patcher.RENAME_EXCHANGE:
            exchange_calls += 1
            if exchange_calls == 2:
                raise OSError(errno.EIO, "simulated restorative exchange failure")
        return real_renameat2(source_fd, source, destination_fd, destination, flags)

    monkeypatch.setattr(patcher, "_renameat2", fail_restorative_exchange)
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)
    try:
        with pytest.raises(patcher.PatcherError) as raised:
            patcher._atomic_replace_at(
                parent_fd,
                target.name,
                target,
                patched,
                0o644,
                expected_identity,
                sha256_bytes(original),
            )
    finally:
        os.close(parent_fd)

    recovery = target.with_name(f".{target.name}.forza-recovery-displaced-target")
    assert exchange_calls == 2
    assert target.read_bytes() == patched
    assert recovery.is_symlink()
    assert recovery.lstat().st_ino == concurrent_inode
    assert os.readlink(recovery) == "concurrent-link-text"
    assert sorted(target.parent.glob(f".{target.name}.forza-*")) == [recovery]
    assert str(target) in str(raised.value)
    assert str(recovery) in str(raised.value)


def test_recovery_fsync_failure_reports_exact_suffixed_path(
    tmp_path, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    source = f".{target.name}.forza-source"
    (tmp_path / source).symlink_to("concurrent-link-text")
    source_inode = (tmp_path / source).lstat().st_ino
    collision = target.with_name(f".{target.name}.forza-recovery-displaced-target")
    collision.write_bytes(b"existing recovery")
    expected = target.with_name(
        f".{target.name}.forza-recovery-displaced-target.1"
    )
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)

    def fail_recovery_fsync(descriptor):
        if descriptor == parent_fd:
            raise OSError("simulated recovery fsync failure")
        raise AssertionError(f"unexpected fsync descriptor: {descriptor}")

    monkeypatch.setattr(patcher.os, "fsync", fail_recovery_fsync)
    try:
        with pytest.raises(patcher.RollbackError) as raised:
            patcher._preserve_conflict_object(
                parent_fd,
                target.name,
                source,
                target,
                "displaced-target",
            )
    finally:
        os.close(parent_fd)

    assert expected.is_symlink()
    assert expected.lstat().st_ino == source_inode
    assert os.readlink(expected) == "concurrent-link-text"
    assert not (tmp_path / source).exists()
    assert str(expected) in str(raised.value)


def test_unsupported_recovery_rename_reports_exact_intact_private_object(
    tmp_path, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"published staging")
    source = f".{target.name}.forza-private"
    source_path = tmp_path / source
    source_path.symlink_to("concurrent-link-text")
    source_inode = source_path.lstat().st_ino

    def reject_recovery_rename(*_args):
        raise patcher.PatcherError("required renameat2 operation is unavailable")

    monkeypatch.setattr(patcher, "_renameat2", reject_recovery_rename)
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)
    try:
        with pytest.raises(patcher.RollbackError) as raised:
            patcher._preserve_conflict_object(
                parent_fd,
                target.name,
                source,
                target,
                "displaced-target",
            )
    finally:
        os.close(parent_fd)

    assert source_path.is_symlink()
    assert source_path.lstat().st_ino == source_inode
    assert os.readlink(source_path) == "concurrent-link-text"
    assert target.read_bytes() == b"published staging"
    assert not list(tmp_path.glob(f".{target.name}.forza-recovery-*"))
    assert str(source_path) in str(raised.value)


def test_conflict_restore_never_unlinks_a_name_swap_after_identity_check(
    tmp_path, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"known published patch")
    displaced = f".{target.name}.forza-displaced"
    (tmp_path / displaced).symlink_to("first-concurrent-link")
    late_replacement = tmp_path / "late-concurrent-link"
    late_replacement.symlink_to("second-concurrent-link")
    late_identity = late_replacement.lstat().st_ino
    real_unlink = patcher.os.unlink
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)

    def replace_before_unlink(path, *args, **kwargs):
        if path == displaced and kwargs.get("dir_fd") == parent_fd:
            os.replace(late_replacement, tmp_path / displaced)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patcher.os, "unlink", replace_before_unlink)
    try:
        assert patcher._restore_displaced_after_conflict(
            parent_fd,
            target.name,
            displaced,
            target,
        )
    finally:
        os.close(parent_fd)

    assert target.is_symlink()
    assert os.readlink(target) == "first-concurrent-link"
    assert late_replacement.is_symlink()
    assert late_replacement.lstat().st_ino == late_identity
    assert os.readlink(late_replacement) == "second-concurrent-link"


@pytest.mark.parametrize(("missing", "expected_restored"), (("public", True), ("displaced", False)))
def test_conflict_restore_handles_a_disappearing_name(
    tmp_path, monkeypatch, missing, expected_restored
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"known published patch")
    target_inode = target.stat().st_ino
    displaced = f".{target.name}.forza-displaced"
    (tmp_path / displaced).symlink_to("concurrent-link")
    displaced_inode = (tmp_path / displaced).lstat().st_ino
    vanished = tmp_path / f"vanished-{missing}"
    real_renameat2 = patcher._renameat2
    interrupted = False

    def disappear_before_exchange(source_fd, source, destination_fd, destination, flags):
        nonlocal interrupted
        if flags == patcher.RENAME_EXCHANGE and not interrupted:
            interrupted = True
            vanished_source = target if missing == "public" else tmp_path / displaced
            os.replace(vanished_source, vanished)
            raise FileNotFoundError
        return real_renameat2(source_fd, source, destination_fd, destination, flags)

    monkeypatch.setattr(patcher, "_renameat2", disappear_before_exchange)
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)
    try:
        restored = patcher._restore_displaced_after_conflict(
            parent_fd,
            target.name,
            displaced,
            target,
        )
    finally:
        os.close(parent_fd)

    assert restored is expected_restored
    if missing == "public":
        assert target.is_symlink()
        assert target.lstat().st_ino == displaced_inode
        assert vanished.read_bytes() == b"known published patch"
        assert vanished.stat().st_ino == target_inode
    else:
        assert target.read_bytes() == b"known published patch"
        assert target.stat().st_ino == target_inode
        assert vanished.is_symlink()
        assert vanished.lstat().st_ino == displaced_inode
        assert os.readlink(vanished) == "concurrent-link"
    assert not list(tmp_path.glob(f".{target.name}.forza-*"))


def test_failed_missing_name_fallback_preserves_displaced_object(
    tmp_path, monkeypatch
):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"known published patch")
    vanished_patch = tmp_path / "concurrently-moved-patch"
    displaced = f".{target.name}.forza-displaced"
    (tmp_path / displaced).symlink_to("concurrent-link")
    displaced_inode = (tmp_path / displaced).lstat().st_ino
    real_renameat2 = patcher._renameat2
    exchange_failed = False

    def fail_exchange_then_fallback(source_fd, source, destination_fd, destination, flags):
        nonlocal exchange_failed
        if flags == patcher.RENAME_EXCHANGE and not exchange_failed:
            exchange_failed = True
            os.replace(target, vanished_patch)
            raise FileNotFoundError
        if (
            flags == patcher.RENAME_NOREPLACE
            and source == displaced
            and destination == target.name
        ):
            raise OSError(errno.EIO, "simulated fallback restore failure")
        return real_renameat2(source_fd, source, destination_fd, destination, flags)

    monkeypatch.setattr(patcher, "_renameat2", fail_exchange_then_fallback)
    parent_fd = os.open(tmp_path, patcher.DIRECTORY)
    try:
        with pytest.raises(patcher.RollbackError) as raised:
            patcher._restore_displaced_after_conflict(
                parent_fd,
                target.name,
                displaced,
                target,
            )
    finally:
        os.close(parent_fd)

    recovery = target.with_name(f".{target.name}.forza-recovery-displaced-target")
    assert recovery.is_symlink()
    assert recovery.lstat().st_ino == displaced_inode
    assert os.readlink(recovery) == "concurrent-link"
    assert not list(tmp_path.glob(f".{target.name}.forza-displaced"))
    assert vanished_patch.read_bytes() == b"known published patch"
    assert str(target) in str(raised.value)
    assert str(recovery) in str(raised.value)


@pytest.mark.parametrize("replacement_type", ("symlink", "directory"))
def test_apply_forza_rollback_preserves_concurrent_nonregular_replacement(
    tmp_path, synthetic_specs, monkeypatch, replacement_type
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)
    raced_target = targets[1]
    backup_root = tmp_path / "backups"
    real_apply_target = patcher.apply_target
    replacement = None

    def apply_target_with_race(path, spec, backup_root, on_backup=None):
        if path != raced_target:
            return real_apply_target(path, spec, backup_root, on_backup=on_backup)

        def replace_after_backup(entry):
            nonlocal replacement
            if on_backup is not None:
                on_backup(entry)
            replacement = replace_with_nonregular(path, replacement_type)

        return real_apply_target(
            path,
            spec,
            backup_root,
            on_backup=replace_after_backup,
        )

    monkeypatch.setattr(patcher, "apply_target", apply_target_with_race)

    with pytest.raises(patcher.PatcherError, match="changed during publication"):
        apply_forza(tmp_path, synthetic_specs, backup_root)

    assert replacement is not None
    assert_nonregular_replacement(raced_target, replacement_type, *replacement)
    assert_only_patched_recovery_remains(
        raced_target, synthetic_specs[raced_target.name]
    )
    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.ORIGINAL
        for target in targets
        if target != raced_target
    )
    assert not list(backup_root.glob("*.json"))


@pytest.mark.parametrize("tool_name", ("GE-Proton11-3-FM", "OtherTool"))
def test_apply_forza_rejects_backup_root_inside_any_compatibility_tool(
    tmp_path, synthetic_specs, tool_name
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)
    backup_root = tmp_path / "compatibilitytools.d" / tool_name / "backups"

    with pytest.raises(patcher.PatcherError, match="outside compatibilitytools.d"):
        apply_forza(tmp_path, synthetic_specs, backup_root)

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.ORIGINAL
        for target in targets
    )
    assert not backup_root.exists()


def test_apply_target_rejects_backup_root_inside_unrelated_compatibility_tool(
    tmp_path, controller_spec
):
    target = tmp_path / "prefix" / "windows.gaming.input.dll"
    target.parent.mkdir()
    target.write_bytes(synthetic_original(controller_spec))
    backup_root = tmp_path / "compatibilitytools.d" / "OtherTool" / "backups"

    with pytest.raises(patcher.PatcherError, match="outside compatibilitytools.d"):
        apply_target(target, controller_spec, backup_root)

    assert target.read_bytes() == synthetic_original(controller_spec)
    assert not backup_root.exists()


def test_apply_forza_rejects_backup_root_symlinked_into_compatibility_tools(
    tmp_path, synthetic_specs
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)
    inside = tmp_path / "compatibilitytools.d" / "OtherTool"
    inside.mkdir()
    alias = tmp_path / "backup-alias"
    alias.symlink_to(inside, target_is_directory=True)

    with pytest.raises(patcher.PatcherError, match="outside compatibilitytools.d"):
        apply_forza(tmp_path, synthetic_specs, alias / "backups")

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.ORIGINAL
        for target in targets
    )
    assert not (inside / "backups").exists()


@pytest.mark.parametrize("mutation", ("incomplete", "duplicate"))
def test_restore_forza_rejects_incomplete_or_duplicate_backup_manifest(
    tmp_path, synthetic_specs, mutation
):
    populate_forza_root(tmp_path, synthetic_specs)
    result = apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")
    record = json.loads(result.backup_manifest.read_text())
    if mutation == "incomplete":
        record["targets"].pop()
    else:
        record["targets"].append(dict(record["targets"][0]))
    result.backup_manifest.write_text(json.dumps(record))

    with pytest.raises(patcher.PatcherError):
        restore_forza(tmp_path, result.backup_manifest, synthetic_specs)

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.PATCHED
        for target in resolve_forza_targets(tmp_path)
    )


def test_apply_forza_rejects_mixed_or_already_patched_target_states(
    tmp_path, synthetic_specs
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)
    apply_target(targets[0], synthetic_specs[targets[0].name], tmp_path / "manual-backup")

    with pytest.raises(patcher.PatcherError, match="mixed target states"):
        apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")

    assert inspect_target(targets[0], synthetic_specs[targets[0].name]) is PatchState.PATCHED
    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.ORIGINAL
        for target in targets[1:]
    )

    restore_target(
        targets[0],
        next((tmp_path / "manual-backup").iterdir()),
        synthetic_specs[targets[0].name],
    )
    result = apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")

    with pytest.raises(patcher.PatcherError, match="all targets are already patched"):
        apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")

    assert result.backup_manifest.exists()
    assert len(list((tmp_path / "backups").glob("*.json"))) == 1


def test_restore_forza_rejects_symlinked_backup_component(
    tmp_path, synthetic_specs
):
    populate_forza_root(tmp_path, synthetic_specs)
    result = apply_forza(tmp_path, synthetic_specs, tmp_path / "backups")
    record = json.loads(result.backup_manifest.read_text())
    backup_root = result.backup_manifest.parent
    original_backup = backup_root / record["targets"][0]["backup"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped").write_bytes(original_backup.read_bytes())
    (backup_root / "nested").symlink_to(outside, target_is_directory=True)
    record["targets"][0]["backup"] = "nested/escaped"
    result.backup_manifest.write_text(json.dumps(record))

    with pytest.raises(patcher.PatcherError, match="symlink"):
        restore_forza(tmp_path, result.backup_manifest, synthetic_specs)

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.PATCHED
        for target in resolve_forza_targets(tmp_path)
    )


def test_apply_forza_rolls_back_only_published_targets_after_pre_replace_failure(
    tmp_path, synthetic_specs, monkeypatch
):
    populate_forza_root(tmp_path, synthetic_specs)
    targets = resolve_forza_targets(tmp_path)
    backup_root = tmp_path / "backups"
    real_replace = patcher._atomic_replace_at

    def fail_second_target(parent_fd, leaf, path, *args):
        if path == targets[1]:
            raise OSError("target two replace failure")
        return real_replace(parent_fd, leaf, path, *args)

    monkeypatch.setattr(patcher, "_atomic_replace_at", fail_second_target)

    with pytest.raises(OSError, match="target two replace failure"):
        apply_forza(tmp_path, synthetic_specs, backup_root)

    assert all(
        inspect_target(target, synthetic_specs[target.name]) is PatchState.ORIGINAL
        for target in targets
    )
    assert not list(backup_root.glob("*.json"))
