# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

import forza_bootstrap.proton as proton_module
from forza_bootstrap.model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    BuilderSpec,
    DownloadSpec,
    ProtonUpSpec,
    SourceSpec,
)
from forza_bootstrap.proton import (
    ProtonRoots,
    ToolDisposition,
    acquire_ge,
    inspect_existing_fm,
    inspect_fm,
    prepare_fm_tree,
    protonup_argv,
    publish_fm,
    rollback_published_fm,
)

BASE_NAME = "GE-Proton11-3"
FM_NAME = "GE-Proton11-3-FM"
CONTROLLER = b"fixture controller"
MOUNTMGR = b"fixture mount manager"
RUNTIME_DLL = b"fixture xgameruntime dll"
RUNTIME_SO = b"fixture xgameruntime so"
CONTROLLER_PATH = "files/lib/wine/x86_64-windows/windows.gaming.input.dll"
MOUNTMGR_PATH = "files/lib/wine/x86_64-windows/mountmgr.sys"
RUNTIME_DLL_PATH = "files/lib/wine/x86_64-windows/xgameruntime.dll"
RUNTIME_SO_PATH = "files/lib/wine/x86_64-unix/xgameruntime.so"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_vdf(*, duplicate_display_name: bool = False) -> bytes:
    duplicate = (
        '\n            "display_name" "GE-Proton11-3"' if duplicate_display_name else ""
    )
    return (
        '"compatibilitytools"\n'
        "{\n"
        '    "compat_tools"\n'
        "    {\n"
        '        "GE-Proton11-3"\n'
        "        {\n"
        '            "install_path" "."\n'
        '            "display_name" "GE-Proton11-3"'
        f"{duplicate}\n"
        '            "from_oslist" "windows"\n'
        '            "to_oslist" "linux"\n'
        "        }\n"
        "    }\n"
        "}\n"
    ).encode("ascii")


def extracted_ge_fixture(
    root: Path,
    *,
    name: str = BASE_NAME,
    duplicate_display_name: bool = False,
) -> Path:
    source = root / name
    (source / Path(CONTROLLER_PATH).parent).mkdir(parents=True)
    (source / Path(RUNTIME_SO_PATH).parent).mkdir(parents=True)
    (source / "compatibilitytool.vdf").write_bytes(
        source_vdf(duplicate_display_name=duplicate_display_name)
    )
    (source / "proton").write_bytes(b"#!/bin/sh\n")
    (source / "proton").chmod(0o755)
    (source / CONTROLLER_PATH).write_bytes(CONTROLLER)
    (source / MOUNTMGR_PATH).write_bytes(MOUNTMGR)
    (source / "files/lib/wine/libfixture.so").write_bytes(b"library")
    (source / "files/lib/wine/libfixture-link.so").symlink_to("libfixture.so")
    return source


def tree_manifest(root: Path) -> dict[str, tuple[str, int, bytes | str | None]]:
    result: dict[str, tuple[str, int, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            result[relative] = ("regular", mode, path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = ("directory", mode, None)
        elif stat.S_ISLNK(info.st_mode):
            result[relative] = ("symlink", mode, os.readlink(path))
        else:
            result[relative] = ("special", mode, None)
    return result


def changed_paths(
    before: dict[str, tuple[str, int, bytes | str | None]],
    after: dict[str, tuple[str, int, bytes | str | None]],
) -> set[str]:
    return {
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }


def ge_archive(root: Path) -> bytes:
    source = extracted_ge_fixture(root / "archive-source")
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT
    ) as archive:
        archive.add(source, arcname=BASE_NAME, recursive=True)
    shutil.rmtree(root / "archive-source")
    return output.getvalue()


def fixture_manifest(tmp_path: Path, archive: bytes = b"archive") -> BootstrapManifest:
    return BootstrapManifest(
        schema=1,
        app_id="2440510",
        host=("arch", "x86_64", "native-steam"),
        profile="fixture",
        protonup=ProtonUpSpec(version="0.1.5", project_relative="tools/protonup"),
        ge=DownloadSpec(
            name=BASE_NAME,
            release=BASE_NAME,
            url="https://downloads.example.invalid/GE-Proton11-3.tar.gz",
            filename="GE-Proton11-3.tar.gz",
            size=len(archive),
            sha256=sha256(archive),
            expected_root=BASE_NAME,
            allowed_redirect_hosts=("downloads.example.invalid",),
        ),
        bundle=DownloadSpec(
            name="fixture-bundle",
            release="v1",
            url="https://downloads.example.invalid/bundle.tar.zst",
            filename="bundle.tar.zst",
            size=1,
            sha256="0" * 64,
            expected_root="fixture-bundle",
            allowed_redirect_hosts=("downloads.example.invalid",),
        ),
        sources=(SourceSpec("fixture", "https://example.invalid/repo", "1" * 40),),
        builder=BuilderSpec(
            image="example.invalid/image@sha256:" + "2" * 64,
            base_image="example.invalid/base@sha256:" + "3" * 64,
            arch_snapshot="https://archive.example.invalid/$repo/os/$arch",
            source_date_epoch=1,
        ),
        artifacts=(
            ArtifactSpec(
                "xgameruntime.dll",
                "runtime/xgameruntime.dll",
                len(RUNTIME_DLL),
                sha256(RUNTIME_DLL),
                0o755,
            ),
            ArtifactSpec(
                "xgameruntime.so",
                "runtime/xgameruntime.so",
                len(RUNTIME_SO),
                sha256(RUNTIME_SO),
                0o755,
            ),
        ),
        path=tmp_path / "bootstrap.toml",
        sha256="4" * 64,
    )


@pytest.fixture(autouse=True)
def synthetic_supported_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supported = tmp_path / "supported-builds.toml"
    supported.write_text(
        "\n".join(
            (
                "[[patches]]",
                'name = "controller"',
                'file = "windows.gaming.input.dll"',
                f'original_sha256 = "{sha256(CONTROLLER)}"',
                f'patched_sha256 = "{sha256(b"patched controller")}"',
                "edits = []",
                "",
                "[[patches]]",
                'name = "mountmgr"',
                'file = "mountmgr.sys"',
                f'original_sha256 = "{sha256(MOUNTMGR)}"',
                f'patched_sha256 = "{sha256(b"patched mount manager")}"',
                "edits = []",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(proton_module, "_SUPPORTED_BUILDS_PATH", supported)


def test_protonup_download_is_frozen_and_cannot_target_steam(tmp_path: Path) -> None:
    argv = protonup_argv(Path("/repo"), tmp_path / "cache")
    assert argv == (
        "uv",
        "run",
        "--frozen",
        "--project",
        "/repo/tools/protonup",
        "protonup",
        "--download",
        "-t",
        "GE-Proton11-3",
        "-o",
        str(tmp_path / "cache"),
        "-y",
    )
    assert "compatibilitytools.d" not in "\0".join(argv)


def test_acquire_reuses_verified_cache_without_invoking_protonup(
    tmp_path: Path,
) -> None:
    archive = ge_archive(tmp_path)
    manifest = fixture_manifest(tmp_path, archive)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    cached = cache / manifest.ge.filename
    cached.write_bytes(archive)
    cached.chmod(0o600)

    def forbidden_runner(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verified cache must not invoke ProtonUp")

    extracted = acquire_ge(
        manifest,
        ProtonRoots(Path("/repo"), cache, tmp_path / "extracted"),
        forbidden_runner,
    )

    assert extracted.name == BASE_NAME
    assert (extracted / "compatibilitytool.vdf").read_bytes() == source_vdf()


def test_acquire_invokes_once_with_no_shell_and_an_allowlisted_environment(
    tmp_path: Path,
) -> None:
    archive = ge_archive(tmp_path)
    manifest = fixture_manifest(tmp_path, archive)
    roots = ProtonRoots(Path("/repo"), tmp_path / "cache", tmp_path / "extracted")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(argv: tuple[str, ...], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        output = roots.cache_root / manifest.ge.filename
        output.write_bytes(archive)
        output.chmod(0o600)
        return object()

    extracted = acquire_ge(manifest, roots, runner)

    assert extracted.is_dir()
    assert calls == [
        (
            protonup_argv(Path("/repo"), roots.cache_root),
            {
                "check": True,
                "shell": False,
                "env": calls[0][1]["env"],
                "umask": 0o077,
            },
        )
    ]
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert set(environment) <= {
        "PATH",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "UV_CACHE_DIR",
    }
    assert "HOME" not in environment
    assert "STEAM_COMPAT_CLIENT_INSTALL_PATH" not in environment


def test_acquire_rejects_wrong_generated_archive_without_retry(tmp_path: Path) -> None:
    archive = ge_archive(tmp_path)
    manifest = fixture_manifest(tmp_path, archive)
    roots = ProtonRoots(Path("/repo"), tmp_path / "cache", tmp_path / "extracted")
    calls = 0

    def runner(_argv: tuple[str, ...], **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        output = roots.cache_root / manifest.ge.filename
        output.write_bytes(b"wrong")
        output.chmod(0o600)
        return object()

    with pytest.raises(BootstrapError, match="artifact|archive|digest|size"):
        acquire_ge(manifest, roots, runner)

    assert calls == 1
    assert not roots.extraction_root.exists()


def test_acquire_binds_pinned_archive_through_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = ge_archive(tmp_path)
    manifest = fixture_manifest(tmp_path, archive)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    cached = cache / manifest.ge.filename
    cached.write_bytes(archive)
    cached.chmod(0o600)
    malicious_root = tmp_path / "malicious-source"
    malicious_tree = extracted_ge_fixture(malicious_root)
    (malicious_tree / "proton").write_bytes(b"different but safe archive")
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT
    ) as replacement_tar:
        replacement_tar.add(malicious_tree, arcname=BASE_NAME, recursive=True)
    replacement = tmp_path / "replacement.tar.gz"
    replacement.write_bytes(output.getvalue())
    replacement.chmod(0o600)
    real_verify = proton_module.verify_cached_file
    replaced = False

    def replace_after_independent_verification(
        path: Path, expected_size: int, expected_sha256: str
    ) -> bool:
        nonlocal replaced
        result = real_verify(path, expected_size, expected_sha256)
        if result and not replaced:
            os.replace(replacement, path)
            replaced = True
        return result

    monkeypatch.setattr(
        proton_module, "verify_cached_file", replace_after_independent_verification
    )

    with pytest.raises(BootstrapError, match="archive|digest|size"):
        acquire_ge(
            manifest,
            ProtonRoots(Path("/repo"), cache, tmp_path / "extracted"),
            lambda *_args, **_kwargs: pytest.fail("cache was independently verified"),
        )

    assert replaced
    assert not (tmp_path / "extracted").exists()


def test_identity_rewrite_changes_only_staged_compatibilitytool_vdf(
    tmp_path: Path,
) -> None:
    source = extracted_ge_fixture(tmp_path)
    before = tree_manifest(source)

    identity = prepare_fm_tree(source, tmp_path / "stage")

    after = tree_manifest(tmp_path / "stage")
    assert identity.name == FM_NAME
    assert identity.base_release == BASE_NAME
    assert changed_paths(before, after) == {
        "compatibilitytool.vdf",
        ".forza-bootstrap.json",
    }
    assert b'"GE-Proton11-3-FM"' in after["compatibilitytool.vdf"][2]


def test_prepare_keeps_canonical_ge_byte_identical_and_hashes_link_text(
    tmp_path: Path,
) -> None:
    source = extracted_ge_fixture(tmp_path)
    before = tree_manifest(source)
    first = prepare_fm_tree(source, tmp_path / "stage-a")
    assert tree_manifest(source) == before

    link = source / "files/lib/wine/libfixture-link.so"
    link.unlink()
    link.symlink_to("different-link-text")
    second = prepare_fm_tree(source, tmp_path / "stage-b")

    assert tree_manifest(source) != before
    assert (
        tree_manifest(tmp_path / "stage-a")["files/lib/wine/libfixture.so"]
        == before["files/lib/wine/libfixture.so"]
    )
    assert first.managed_tree_sha256 != second.managed_tree_sha256


def test_prepare_refuses_duplicate_vdf_keys_before_creating_stage(
    tmp_path: Path,
) -> None:
    source = extracted_ge_fixture(tmp_path, duplicate_display_name=True)

    with pytest.raises(BootstrapError, match="duplicate VDF key"):
        prepare_fm_tree(source, tmp_path / "stage")

    assert not (tmp_path / "stage").exists()


def test_exact_managed_state_is_adopted_without_marker(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    destination = tmp_path / FM_NAME
    prepare_fm_tree(source, destination)
    marker = destination / ".forza-bootstrap.json"
    marker.unlink()
    before = tree_manifest(destination)

    disposition = inspect_existing_fm(destination, fixture_manifest(tmp_path))

    assert disposition is ToolDisposition.ADOPTED
    assert tree_manifest(destination) == before
    assert not marker.exists()


def test_exact_adopted_tool_inspection_binds_the_complete_tree(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    destination = tmp_path / FM_NAME
    prepare_fm_tree(source, destination)
    marker = destination / ".forza-bootstrap.json"
    marker.unlink()

    inspection = inspect_fm(destination, fixture_manifest(tmp_path))

    assert inspection.disposition is ToolDisposition.ADOPTED
    assert inspection.identity is not None
    assert inspection.identity.name == FM_NAME
    assert inspection.identity.marker_sha256 is None
    assert inspection.tree_sha256 is not None
    assert len(inspection.tree_sha256) == 64
    assert inspection.tree_sha256 == inspection.tree_sha256.lower()

    (destination / "proton").write_bytes(b"changed complete tree\n")
    changed = inspect_fm(destination, fixture_manifest(tmp_path))
    assert changed.tree_sha256 != inspection.tree_sha256


def test_adopted_tool_inspection_rejects_identity_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = extracted_ge_fixture(tmp_path)
    destination = tmp_path / FM_NAME
    prepare_fm_tree(source, destination)
    (destination / ".forza-bootstrap.json").unlink()
    real_tree_digest = proton_module._tree_digest
    calls = 0

    def mutate_vdf(root_fd: int, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            (destination / "compatibilitytool.vdf").write_bytes(
                source_vdf().replace(b"GE-Proton11-3-FM", b"GE-Proton11-3-XM")
            )
        return real_tree_digest(root_fd, **kwargs)

    monkeypatch.setattr(proton_module, "_tree_digest", mutate_vdf)

    with pytest.raises(BootstrapError, match="existing compatibility tool conflicts"):
        inspect_fm(destination, fixture_manifest(tmp_path))


def test_supported_installed_runtime_and_patched_targets_are_adopted(
    tmp_path: Path,
) -> None:
    source = extracted_ge_fixture(tmp_path)
    destination = tmp_path / FM_NAME
    prepare_fm_tree(source, destination)
    (destination / ".forza-bootstrap.json").unlink()
    (destination / CONTROLLER_PATH).write_bytes(b"patched controller")
    (destination / MOUNTMGR_PATH).write_bytes(b"patched mount manager")
    (destination / RUNTIME_DLL_PATH).write_bytes(RUNTIME_DLL)
    (destination / RUNTIME_DLL_PATH).chmod(0o755)
    (destination / RUNTIME_SO_PATH).write_bytes(RUNTIME_SO)
    (destination / RUNTIME_SO_PATH).chmod(0o755)

    assert (
        inspect_existing_fm(destination, fixture_manifest(tmp_path))
        is ToolDisposition.ADOPTED
    )


def test_unknown_existing_tool_is_preserved(tmp_path: Path) -> None:
    destination = tmp_path / FM_NAME
    destination.mkdir()
    (destination / "unknown").write_bytes(b"user")
    before = tree_manifest(destination)

    with pytest.raises(BootstrapError, match="existing compatibility tool conflicts"):
        inspect_existing_fm(destination, fixture_manifest(tmp_path))

    assert tree_manifest(destination) == before


def test_mixed_runtime_state_is_a_preserved_conflict(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    destination = tmp_path / FM_NAME
    prepare_fm_tree(source, destination)
    (destination / ".forza-bootstrap.json").unlink()
    (destination / RUNTIME_DLL_PATH).write_bytes(RUNTIME_DLL)
    before = tree_manifest(destination)

    with pytest.raises(BootstrapError, match="existing compatibility tool conflicts"):
        inspect_existing_fm(destination, fixture_manifest(tmp_path))

    assert tree_manifest(destination) == before


def test_publish_exact_destination_is_a_noop(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    shutil.copytree(stage, destination, symlinks=True)
    before = tree_manifest(destination)
    inode = destination.stat().st_ino

    record = publish_fm(stage, destination)

    assert record.disposition is ToolDisposition.ADOPTED
    assert destination.stat().st_ino == inode
    assert tree_manifest(destination) == before
    assert stage.exists()


def test_publish_uses_rename_noreplace_and_preserves_conflict(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    destination.mkdir()
    (destination / "user").write_bytes(b"keep")
    before = tree_manifest(destination)

    with pytest.raises(BootstrapError, match="publication destination conflicts"):
        publish_fm(stage, destination)

    assert tree_manifest(destination) == before
    assert stage.exists()


def test_publish_failure_removes_only_unchanged_created_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    real_fsync = proton_module.os.fsync
    failed = False

    def fail_first_parent_fsync_after_rename(fd: int) -> None:
        nonlocal failed
        if not failed and destination.exists() and not stage.exists():
            failed = True
            raise OSError("simulated post-rename fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(proton_module.os, "fsync", fail_first_parent_fsync_after_rename)

    with pytest.raises(BootstrapError, match="publication failed"):
        publish_fm(stage, destination)

    assert failed
    assert not destination.exists()


def test_publish_stage_name_swap_quarantines_foreign_tree_outside_live_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    displaced = tmp_path / "held-original-stage"
    foreign_payload = b"foreign tree published by stage-name swap"
    collision = tmp_path / f".{FM_NAME}.collision.publication-recovery"
    collision.mkdir(mode=0o700)
    (collision / "sentinel").write_bytes(b"pre-existing recovery")
    real_rename_noreplace = proton_module.rename_noreplace
    swapped = False
    replacement_added = False
    recovery_tokens = iter(("collision", "foreign", "replacement"))

    monkeypatch.setattr(
        proton_module.secrets, "token_hex", lambda _length: next(recovery_tokens)
    )

    def swap_stage_name_before_publish(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal replacement_added, swapped
        if (
            not swapped
            and source_name == stage.name
            and destination_name == destination.name
        ):
            stage.rename(displaced)
            shutil.copytree(displaced, stage, symlinks=True)
            (stage / "proton").write_bytes(foreign_payload)
            swapped = True
        real_rename_noreplace(source_fd, source_name, destination_fd, destination_name)
        if (
            source_name == destination.name
            and destination_name.endswith(".publication-recovery")
            and not replacement_added
        ):
            destination.mkdir(mode=0o700)
            (destination / "late").write_bytes(b"concurrent destination replacement")
            replacement_added = True

    monkeypatch.setattr(
        proton_module, "rename_noreplace", swap_stage_name_before_publish
    )

    with pytest.raises(BootstrapError, match="publication verification failed"):
        publish_fm(stage, destination)

    foreign_recovery = tmp_path / f".{FM_NAME}.foreign.publication-recovery"
    replacement_recovery = tmp_path / f".{FM_NAME}.replacement.publication-recovery"
    assert swapped
    assert replacement_added
    assert not destination.exists()
    assert (collision / "sentinel").read_bytes() == b"pre-existing recovery"
    assert (foreign_recovery / "proton").read_bytes() == foreign_payload
    assert (replacement_recovery / "late").read_bytes() == (
        b"concurrent destination replacement"
    )
    assert displaced.exists()


def test_failed_stage_cleanup_never_removes_a_concurrent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    real_tree_digest = proton_module._tree_digest
    replaced = False

    def replace_stage_then_fail(root_fd: int, **kwargs: object) -> str:
        nonlocal replaced
        if not replaced and stage.exists():
            replacement = tmp_path / "replacement"
            replacement.mkdir()
            (replacement / "user").write_bytes(b"preserve")
            displaced = tmp_path / "displaced-stage"
            stage.rename(displaced)
            replacement.rename(stage)
            replaced = True
            raise BootstrapError("simulated stage verification failure")
        return real_tree_digest(root_fd, **kwargs)

    monkeypatch.setattr(proton_module, "_tree_digest", replace_stage_then_fail)

    with pytest.raises(BootstrapError, match="simulated stage verification failure"):
        prepare_fm_tree(source, stage)

    assert replaced
    assert (stage / "user").read_bytes() == b"preserve"


def test_publish_and_rollback_remove_only_unchanged_created_tool(
    tmp_path: Path,
) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    record = publish_fm(stage, destination)

    assert record.disposition is ToolDisposition.CREATED
    assert rollback_published_fm(record) is ToolDisposition.ABSENT
    assert not destination.exists()


def test_rollback_preserves_and_adjudicates_post_publication_change(
    tmp_path: Path,
) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    record = publish_fm(stage, destination)
    changed = destination / "proton"
    changed.write_bytes(b"user changed this after publication")
    before = tree_manifest(destination)

    disposition = rollback_published_fm(record)

    assert disposition is ToolDisposition.CONFLICT
    assert tree_manifest(destination) == before


def test_rollback_does_nothing_for_adopted_destination(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    stage = tmp_path / "stage"
    prepare_fm_tree(source, stage)
    destination = tmp_path / FM_NAME
    shutil.copytree(stage, destination, symlinks=True)
    record = publish_fm(stage, destination)
    before = tree_manifest(destination)

    assert rollback_published_fm(record) is ToolDisposition.ADOPTED
    assert tree_manifest(destination) == before


def test_inspection_rejects_a_tampered_marker_without_writing(tmp_path: Path) -> None:
    source = extracted_ge_fixture(tmp_path)
    destination = tmp_path / FM_NAME
    prepare_fm_tree(source, destination)
    marker = destination / ".forza-bootstrap.json"
    value = json.loads(marker.read_bytes())
    value["managed_targets"] = {"unexpected": "0" * 64}
    marker.write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    )
    before = tree_manifest(destination)

    with pytest.raises(BootstrapError, match="existing compatibility tool conflicts"):
        inspect_existing_fm(destination, fixture_manifest(tmp_path))

    assert tree_manifest(destination) == before


@pytest.mark.parametrize("bad_path", [None, "relative", 7])
def test_public_path_errors_are_normalized(tmp_path: Path, bad_path: object) -> None:
    source = extracted_ge_fixture(tmp_path)
    with pytest.raises(BootstrapError):
        prepare_fm_tree(source, bad_path)  # type: ignore[arg-type]


def test_manifest_release_mismatch_is_rejected_before_runner(tmp_path: Path) -> None:
    manifest = fixture_manifest(tmp_path)
    manifest = replace(manifest, ge=replace(manifest.ge, release="GE-Proton11-4"))

    with pytest.raises(BootstrapError, match="GE-Proton manifest"):
        acquire_ge(
            manifest,
            ProtonRoots(Path("/repo"), tmp_path / "cache", tmp_path / "extracted"),
            lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
        )


@pytest.mark.parametrize("field", ["ge", "protonup"])
def test_malformed_direct_acquisition_manifest_is_normalized(
    tmp_path: Path, field: str
) -> None:
    manifest = replace(fixture_manifest(tmp_path), **{field: None})

    with pytest.raises(BootstrapError, match="GE-Proton manifest"):
        acquire_ge(
            manifest,
            ProtonRoots(Path("/repo"), tmp_path / "cache", tmp_path / "extracted"),
            lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
        )


def test_malformed_direct_inspection_manifest_is_normalized(tmp_path: Path) -> None:
    manifest = replace(fixture_manifest(tmp_path), ge=None)

    with pytest.raises(BootstrapError, match="GE-Proton manifest"):
        inspect_existing_fm(tmp_path / FM_NAME, manifest)
