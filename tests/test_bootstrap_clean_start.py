# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import forza_bootstrap.cli as cli_module
from forza_bootstrap.artifacts import VerifiedBundle
from forza_bootstrap.container_build import (
    ARCH_SNAPSHOT,
    BASE_IMAGE,
    BUNDLE_ARCHIVE,
    SOURCE_DATE_EPOCH,
    build_fixture_bundle,
)
from forza_bootstrap.coordinator import (
    FinishContext,
    HostProbe,
    PatchInspection,
    PlannedTarget,
    PrefixInspection,
    PrepareContext,
    PrepareInspection,
    PrepareOperations,
    RecoveryContext,
    ResolvedHost,
    default_finish_operations,
    default_prepare_operations,
    default_recovery_operations,
    finish,
    finish_context_from_prepare,
    prepare,
    rollback,
)
from forza_bootstrap.model import (
    ArtifactSpec,
    BootstrapManifest,
    BuilderSpec,
    DownloadSpec,
    ProtonUpSpec,
    SourceSpec,
)
from forza_bootstrap.proton import (
    PublishedToolRecord,
    ToolDisposition,
    ToolIdentity,
    ToolInspection,
)
from forza_bootstrap.snapshot import Snapshot, read_snapshot
from forza_bootstrap.state import BootstrapPhase

ROOT = Path(__file__).parents[1]

XODUS_REVISION = "1" * 40
XGAMERUNTIME_REVISION = "2" * 40
PATCH_PAYLOADS = {
    "controller": (
        b"synthetic controller original\n",
        b"synthetic controller patched\n",
    ),
    "mountmgr": (b"synthetic mountmgr original\n", b"synthetic mountmgr patched\n"),
}
BUNDLE_PAYLOADS = {
    "bin/xodus-service": ("xodus-service", b"fixture xodus service\n", 0o755),
    "bin/xodus-cli": ("xodus-cli", b"fixture xodus cli\n", 0o755),
    "bin/xodus-overlay": ("xodus-overlay", b"fixture xodus overlay\n", 0o755),
    "runtime/xgameruntime.dll": (
        "xgameruntime-pe64",
        b"fixture xgameruntime pe\n",
        0o644,
    ),
    "runtime/xgameruntime.so": (
        "xgameruntime-unix64",
        b"fixture xgameruntime unix\n",
        0o755,
    ),
}
PATCH_PATHS = {
    "compat/controller": Path(
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/"
        "x86_64-windows/windows.gaming.input.dll"
    ),
    "compat/mountmgr": Path(
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/"
        "x86_64-windows/mountmgr.sys"
    ),
    "prefix/controller": Path(
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/"
        "windows.gaming.input.dll"
    ),
    "prefix/mountmgr": Path(
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/drivers/mountmgr.sys"
    ),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_synthetic_pe(path: Path) -> Path:
    data = bytearray(0x80)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\0\0"
    data[0x44:0x46] = (0x8664).to_bytes(2, "little")
    data.extend(b"synthetic licensed fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o644)
    return path


def run_git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_integration_source(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    run_git(root, "config", "user.name", "Clean Start Fixture")
    run_git(root, "config", "user.email", "fixture@example.invalid")
    payloads = {
        "bin/forza-linux": (b"#!/bin/sh\nexit 0\n", 0o755),
        "bin/forza-doctor": (b"#!/bin/sh\nexit 0\n", 0o755),
        "scripts/patch-known-build": (b"#!/bin/sh\nexit 0\n", 0o755),
        "manifests/supported-builds.toml": (b"version = 1\n", 0o644),
        "config/xodus-forza.service": (b"[Service]\nExecStart=/fixture\n", 0o644),
        "manifests/runtime-profiles.toml": (
            (
                "version = 1\n"
                'active = "legacy-v0.1"\n\n'
                '[profiles."legacy-v0.1"]\n'
                'status = "candidate"\n'
                f'xgameruntime_revision = "{XGAMERUNTIME_REVISION}"\n'
                f'xodus_revision = "{XODUS_REVISION}"\n'
            ).encode("ascii"),
            0o644,
        ),
    }
    for relative, (data, mode) in payloads.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(mode)
    run_git(root, "add", ".")
    run_git(root, "commit", "-q", "-m", "clean-start fixture")
    return root, run_git(root, "rev-parse", "HEAD")


def file_identity(path: Path) -> tuple[int, int, int, int, int, str]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        sha256(path.read_bytes()),
    )


class FixtureSourceRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...], **_kwargs: object):
        self.calls.append(argv)
        assert Path(argv[0]).name == "build-bootstrap-bundle"
        assert "--clean" in argv
        output = Path(argv[argv.index("--output") + 1])
        build_fixture_bundle(output)
        return subprocess.CompletedProcess(argv, 0, "fixture Docker build\n", "")


class CleanStartFixture:
    def __init__(self, root: Path, mode: str) -> None:
        self.root = root
        self.mode = mode
        self.user = root / "home"
        self.steam = self.user / ".local/share/Steam"
        self.compatibility = self.steam / "compatibilitytools.d"
        self.runtime = root / "runtime"
        self.cache = self.user / ".cache/forza-motorsport-linux/bootstrap"
        self.state_root = self.user / ".local/state/forza-motorsport-linux/bootstrap"
        self.app_manifest = self.steam / "steamapps/appmanifest_2440510.acf"
        for directory in (
            self.user,
            self.steam / "steamapps",
            self.compatibility,
            self.runtime,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.app_manifest.write_text('"appid" "2440510"\n', encoding="ascii")
        self.sentinel = self.steam / "config/unrelated-sentinel.vdf"
        self.sentinel.parent.mkdir()
        self.sentinel.write_bytes(b"unrelated Steam configuration\n")
        self.sentinel.chmod(0o600)
        os.utime(self.sentinel, ns=(1_700_000_000_000_000_000,) * 2)
        self.sentinel_before = file_identity(self.sentinel)
        self.integration, integration_sha = make_integration_source(
            root / "integration"
        )
        self.fake_systemctl = root / "systemctl"
        self.fake_systemctl.write_text(
            '#!/bin/sh\n[ "${2:-}" = daemon-reload ] && exit 0\nexit 3\n',
            encoding="ascii",
        )
        self.fake_systemctl.chmod(0o755)
        source = root / "fixture-bundle"
        source.mkdir()
        self.local_bundle = build_fixture_bundle(source)
        self.manifest = self._manifest(integration_sha)
        self.host = ResolvedHost(
            "arch",
            "x86_64",
            self.user,
            self.steam,
            self.app_manifest,
            self.compatibility,
            self.state_root,
            self.cache,
            self.runtime,
        )
        self.source_runner = FixtureSourceRunner()
        self.verifier_calls: list[tuple[str, str]] = []
        self.stdout = io.StringIO()
        self.context = self._prepare_context()
        self.licensed = make_synthetic_pe(root / "licensed/input.dll")
        self.original_prefix: dict[str, bytes] = {}

    def _manifest(self, integration_sha: str) -> BootstrapManifest:
        del integration_sha
        artifacts = tuple(
            ArtifactSpec(name, relative, len(data), sha256(data), mode)
            for relative, (name, data, mode) in BUNDLE_PAYLOADS.items()
        )
        payload = self.local_bundle.read_bytes()
        return BootstrapManifest(
            schema=1,
            app_id="2440510",
            host=("arch", "x86_64", "native-steam"),
            profile="legacy-v0.1",
            protonup=ProtonUpSpec("0.1.5", "tools/protonup"),
            ge=DownloadSpec(
                "GE-Proton11-3",
                "GE-Proton11-3",
                "https://example.invalid/GE-Proton11-3.tar.gz",
                "GE-Proton11-3.tar.gz",
                1,
                "0" * 64,
                "GE-Proton11-3",
                ("example.invalid",),
            ),
            bundle=DownloadSpec(
                "fixture-bundle",
                "fixture",
                "https://example.invalid/forza-bootstrap-bundle-v1.tar.zst",
                BUNDLE_ARCHIVE,
                len(payload),
                sha256(payload),
                "forza-bootstrap-bundle-v1",
                ("example.invalid",),
            ),
            sources=(
                SourceSpec("xodus", "https://example.invalid/xodus", XODUS_REVISION),
                SourceSpec(
                    "xgameruntime",
                    "https://example.invalid/xgameruntime",
                    XGAMERUNTIME_REVISION,
                ),
            ),
            builder=BuilderSpec(
                "example.invalid/builder@sha256:" + "3" * 64,
                BASE_IMAGE,
                ARCH_SNAPSHOT,
                SOURCE_DATE_EPOCH,
            ),
            artifacts=artifacts,
            path=self.root / "fixture-bootstrap.toml",
            sha256="4" * 64,
        )

    def _inspection(self) -> PrepareInspection:
        steam_info = self.steam.stat()
        user_info = self.user.stat()
        runtime_info = self.runtime.stat()
        return PrepareInspection(
            root_ids={
                "user": f"{user_info.st_dev}:{user_info.st_ino}",
                "steam": f"{steam_info.st_dev}:{steam_info.st_ino}",
                "cache": f"{user_info.st_dev}:{user_info.st_ino}",
                "state": f"{user_info.st_dev}:{user_info.st_ino}",
                "runtime": f"{runtime_info.st_dev}:{runtime_info.st_ino}",
            },
            filesystem_ids={
                "cache-staging": str(steam_info.st_dev),
                "compatibility-destination": str(steam_info.st_dev),
            },
            available_bytes=10_000_000,
            required_bytes=1,
            tool_disposition=ToolDisposition.ABSENT,
            existing_tool_identity=None,
            existing_tool_tree_sha256=None,
            docker_available=True,
        )

    def _prepare_context(self) -> PrepareContext:
        defaults = default_prepare_operations()

        def acquire_ge(_context: PrepareContext) -> Path:
            ge = self.cache / "ge/GE-Proton11-3"
            ge.mkdir(parents=True)
            self.cache.chmod(0o700)
            return ge

        def acquire_bundle(context: PrepareContext) -> VerifiedBundle:
            verified = defaults.acquire_bundle(context)
            self.verifier_calls.append((self.mode, verified.manifest_sha256))
            return verified

        def publish(_context: PrepareContext, _ge: Path) -> PublishedToolRecord:
            destination = self.compatibility / "GE-Proton11-3-FM"
            marker = destination / ".forza-bootstrap.json"
            controller = destination / PATCH_PATHS["compat/controller"].relative_to(
                "compatibilitytools.d/GE-Proton11-3-FM"
            )
            mountmgr = destination / PATCH_PATHS["compat/mountmgr"].relative_to(
                "compatibilitytools.d/GE-Proton11-3-FM"
            )
            for path, payload in (
                (marker, b"synthetic bootstrap marker\n"),
                (controller, PATCH_PAYLOADS["controller"][0]),
                (mountmgr, PATCH_PAYLOADS["mountmgr"][0]),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                path.chmod(0o600 if path == marker else 0o644)
            info = destination.stat()
            identity = ToolIdentity(
                "GE-Proton11-3-FM",
                "GE-Proton11-3",
                "5" * 64,
                "6" * 64,
                sha256(marker.read_bytes()),
            )
            return PublishedToolRecord(
                destination,
                ToolDisposition.CREATED,
                identity,
                info.st_dev,
                info.st_ino,
                "7" * 64,
            )

        operations = PrepareOperations(
            preflight=lambda _context: self._inspection(),
            capture_before=defaults.capture_before,
            acquire_ge=acquire_ge,
            acquire_bundle=acquire_bundle,
            publish_fm=publish,
        )
        return PrepareContext(
            manifest=self.manifest,
            host=self.host,
            repository_root=ROOT,
            acquisition_mode=self.mode,
            bundle_path=self.local_bundle if self.mode == "local" else None,
            output=self.stdout,
            operations=operations,
            probe=HostProbe(runner=self.source_runner),
        )

    def prepare(self):
        return prepare(self.context, lambda digest: digest)

    def create_prefix(self) -> None:
        for logical, relative in PATCH_PATHS.items():
            if not logical.startswith("prefix/"):
                continue
            kind = "controller" if logical.endswith("controller") else "mountmgr"
            destination = self.steam / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = PATCH_PAYLOADS[kind][0]
            destination.write_bytes(payload)
            destination.chmod(0o644)
            self.original_prefix[logical] = payload

    def _finish_operations(self):
        defaults = default_finish_operations()

        def validate_prefix(_context: FinishContext) -> PrefixInspection:
            prefix = self.steam / "steamapps/compatdata/2440510/pfx"
            system32 = prefix / "drive_c/windows/system32"
            prefix_info = prefix.stat()
            system32_info = system32.stat()
            return PrefixInspection(
                f"{prefix_info.st_dev}:{prefix_info.st_ino}",
                f"{system32_info.st_dev}:{system32_info.st_ino}",
            )

        def inspect_patches(_context: FinishContext, _snapshot: Snapshot):
            targets = []
            for logical in PATCH_PATHS:
                kind = "controller" if logical.endswith("controller") else "mountmgr"
                targets.append(
                    PlannedTarget(logical, sha256(PATCH_PAYLOADS[kind][1]), 0o644)
                )
            return PatchInspection(
                ("original",) * len(targets), tuple(targets), "apply"
            )

        def apply_patches(
            _context: FinishContext,
            _inspection: PatchInspection,
            backup_root: Path,
        ) -> Path:
            for logical, relative in PATCH_PATHS.items():
                kind = "controller" if logical.endswith("controller") else "mountmgr"
                destination = self.steam / relative
                destination.write_bytes(PATCH_PAYLOADS[kind][1])
                destination.chmod(0o644)
            backup_root.mkdir(parents=True, exist_ok=True)
            record = backup_root / "forza-patch-20260904T010203Z.json"
            record.write_text("{}\n", encoding="ascii")
            record.chmod(0o600)
            return record

        return replace(
            defaults,
            validate_prefix=validate_prefix,
            inspect_patches=inspect_patches,
            apply_patches=apply_patches,
            doctor=lambda _context: "fixture doctor: ok\n",
            steam_options=lambda _context: "FIXTURE=1 %command%\n",
        )

    def finish(self, state):
        context = finish_context_from_prepare(self.context, state, self.licensed)
        context = replace(
            context,
            operations=self._finish_operations(),
            child_environment={
                "FORZA_RUNTIME_INTEGRATION_ROOT": str(self.integration),
                "FORZA_SYSTEMCTL": str(self.fake_systemctl),
            },
        )
        state = finish(context, lambda digest: digest)
        return context, state

    def path_for(self, logical: str) -> Path:
        if logical == "compat/tool-marker":
            return self.compatibility / "GE-Proton11-3-FM/.forza-bootstrap.json"
        if logical in PATCH_PATHS:
            return self.steam / PATCH_PATHS[logical]
        if logical == "compat/xgameruntime-pe64":
            return self.compatibility / (
                "GE-Proton11-3-FM/files/lib/wine/x86_64-windows/xgameruntime.dll"
            )
        if logical == "compat/xgameruntime-unix64":
            return self.compatibility / (
                "GE-Proton11-3-FM/files/lib/wine/x86_64-unix/xgameruntime.so"
            )
        if logical == "prefix/threading":
            return self.steam / (
                "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/"
                "xgameruntime.dll.threading"
            )
        assert logical.startswith("user/")
        return self.user / logical.removeprefix("user/")

    def managed_identity(self, snapshot: Snapshot):
        return {
            item.logical_path: file_identity(self.path_for(item.logical_path))
            for item in snapshot.records
            if item.state == "regular"
        }

    def rollback(self, context: FinishContext, state):
        recovery_context = RecoveryContext(
            state=state,
            state_root=self.state_root,
            output=self.stdout,
            prepare_context=self.context,
            finish_context=replace(context, state=state),
        )
        defaults = default_recovery_operations(recovery_context)

        def inspect_patches(_context: RecoveryContext, _state) -> str:
            states = []
            for logical, relative in PATCH_PATHS.items():
                kind = "controller" if logical.endswith("controller") else "mountmgr"
                states.append(
                    (self.steam / relative).read_bytes() == PATCH_PAYLOADS[kind][0]
                )
            return "rolled_back" if all(states) else "pending"

        def restore_patches(_context: RecoveryContext, _state) -> None:
            for logical, relative in PATCH_PATHS.items():
                kind = "controller" if logical.endswith("controller") else "mountmgr"
                destination = self.steam / relative
                destination.write_bytes(PATCH_PAYLOADS[kind][0])
                destination.chmod(0o644)

        destination = self.compatibility / "GE-Proton11-3-FM"

        operations = replace(
            defaults,
            inspect_patches=inspect_patches,
            restore_patches=restore_patches,
            inspect_tool=lambda _context, _state: (
                "pending" if destination.exists() else "rolled_back"
            ),
            rollback_tool=lambda _context, _state: shutil.rmtree(destination),
        )
        return rollback(
            replace(recovery_context, operations=operations), confirm="ROLLBACK"
        )


def run_clean_start(root: Path, mode: str) -> dict[str, object]:
    fixture = CleanStartFixture(root, mode)
    prepared = fixture.prepare()
    assert prepared.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    prepare_transaction = fixture.state_root / prepared.transaction_id
    prepare_evidence = {
        path.relative_to(prepare_transaction): file_identity(path)
        for path in prepare_transaction.rglob("*")
        if path.is_file()
    }
    checked_tool = ToolInspection(
        ToolDisposition.ADOPTED,
        ToolIdentity(
            "GE-Proton11-3-FM",
            "GE-Proton11-3",
            "5" * 64,
            "6" * 64,
            sha256(b"synthetic bootstrap marker\n"),
        ),
        "7" * 64,
    )
    with (
        patch.object(cli_module, "_prepare_context", return_value=fixture.context),
        patch("forza_bootstrap.coordinator.inspect_fm", return_value=checked_tool),
    ):
        check = io.StringIO()
        with redirect_stdout(check):
            assert cli_module.main(["bootstrap", "--check"]) == 0
    assert "PHASE=AWAITING_STEAM_PREFIX" in check.getvalue()
    assert "STATUS=waiting-for-steam-prefix" in check.getvalue()
    assert "CACHE=verified" in check.getvalue()
    assert "PREFIX=missing" in check.getvalue()
    assert {
        path.relative_to(prepare_transaction): file_identity(path)
        for path in prepare_transaction.rglob("*")
        if path.is_file()
    } == prepare_evidence
    with (
        patch.object(cli_module, "_prepare_context", return_value=fixture.context),
        patch(
            "forza_bootstrap.coordinator.inspect_fm",
            return_value=ToolInspection(ToolDisposition.ABSENT, None, None),
        ),
        redirect_stdout(io.StringIO()),
        redirect_stderr(io.StringIO()),
    ):
        assert cli_module.main(["bootstrap", "--check"]) == 1

    fixture.create_prefix()
    finish_context, ready = fixture.finish(prepared)
    assert ready.phase is BootstrapPhase.READY_TO_ATTEMPT
    transaction = fixture.state_root / ready.transaction_id
    after = read_snapshot(transaction / "after.json")
    comparison = transaction / "comparison.json"
    assert '"unexpected_changes":[]' in comparison.read_text(encoding="ascii")
    before_noop = fixture.managed_identity(after)

    with (
        patch.object(cli_module, "_prepare_context", return_value=fixture.context),
        patch("forza_bootstrap.coordinator.inspect_fm", return_value=checked_tool),
    ):
        ready_check = io.StringIO()
        with redirect_stdout(ready_check):
            assert cli_module.main(["bootstrap", "--check"]) == 0
        assert "PHASE=READY_TO_ATTEMPT" in ready_check.getvalue()
        assert "STATUS=ready-to-attempt" in ready_check.getvalue()
        assert "CACHE=verified" in ready_check.getvalue()
        assert "PREFIX=ready" in ready_check.getvalue()

        public_snapshot = io.StringIO()
        public_errors = io.StringIO()
        with redirect_stdout(public_snapshot), redirect_stderr(public_errors):
            assert cli_module.main(["snapshot"]) == 0
        assert public_errors.getvalue() == ""
        assert "[private local digest verified]" in public_snapshot.getvalue()
        assert str(fixture.licensed) not in public_snapshot.getvalue()
        assert sha256(fixture.licensed.read_bytes()) not in public_snapshot.getvalue()

        evidence = fixture.root / "evidence"
        evidence.mkdir(mode=0o700)
        explicit = evidence / "current.json"
        assert cli_module.main(["snapshot", "--output", str(explicit)]) == 0
        assert read_snapshot(explicit) == after

        comparison = io.StringIO()
        with redirect_stdout(comparison):
            assert cli_module.main(["compare"]) == 0
        assert "result: ok" in comparison.getvalue()

        assert cli_module.main(["bootstrap"]) == 0
    assert fixture.managed_identity(after) == before_noop

    rolled_back = fixture.rollback(finish_context, ready)
    assert rolled_back.phase is BootstrapPhase.ROLLED_BACK
    assert not (fixture.compatibility / "GE-Proton11-3-FM").exists()
    for logical, payload in fixture.original_prefix.items():
        restored = fixture.steam / PATCH_PATHS[logical]
        assert restored.read_bytes() == payload
        assert stat.S_IMODE(restored.stat().st_mode) == 0o644
    for item in after.records:
        if (
            item.logical_path.startswith("user/")
            or item.logical_path == "prefix/threading"
        ):
            assert not fixture.path_for(item.logical_path).exists()
    assert file_identity(fixture.sentinel) == fixture.sentinel_before
    with (
        patch.object(cli_module, "_prepare_context", return_value=fixture.context),
        patch(
            "forza_bootstrap.coordinator.inspect_fm",
            return_value=ToolInspection(ToolDisposition.ABSENT, None, None),
        ),
    ):
        terminal_check = io.StringIO()
        with redirect_stdout(terminal_check):
            assert cli_module.main(["bootstrap", "--check"]) == 0
    assert "PHASE=ROLLED_BACK" in terminal_check.getvalue()
    assert "STATUS=rolled-back" in terminal_check.getvalue()
    assert "NEXT=./setup bootstrap" in terminal_check.getvalue()
    return {
        "records": tuple(
            (item.logical_path, item.state, item.mode, item.size, item.sha256)
            for item in after.records
        ),
        "verifier_calls": tuple(fixture.verifier_calls),
        "source_calls": tuple(fixture.source_runner.calls),
    }


def test_local_and_fixture_source_clean_start_share_verifier_and_runtime_path(
    tmp_path: Path,
) -> None:
    local = run_clean_start(tmp_path / "local", "local")
    source = run_clean_start(tmp_path / "source", "source")

    assert local["records"] == source["records"]
    assert len(local["verifier_calls"]) == 1
    assert len(source["verifier_calls"]) == 1
    assert local["verifier_calls"][0][1] == source["verifier_calls"][0][1]
    assert local["source_calls"] == ()
    assert len(source["source_calls"]) == 1
