# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import forza_bootstrap.coordinator as coordinator_module
from forza_bootstrap.coordinator import (
    FinishContext,
    FinishOperations,
    PatchInspection,
    PlannedTarget,
    PrefixInspection,
    PrepareContext,
    ResolvedHost,
    RuntimePlan,
    build_finish_plan,
    default_finish_operations,
    finish,
    finish_context_from_prepare,
    validate_prefix,
)
from forza_bootstrap.licensed import LicensedAction
from forza_bootstrap.model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    BuilderSpec,
    DownloadSpec,
    ProtonUpSpec,
    SourceSpec,
    plan_digest,
)
from forza_bootstrap.proton import ToolDisposition
from forza_bootstrap.snapshot import FileRecord, Snapshot
from forza_bootstrap.state import (
    BootstrapPhase,
    create_transaction,
    load_unfinished_transaction,
    transition,
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_fixture(tmp_path: Path) -> BootstrapManifest:
    return BootstrapManifest(
        schema=1,
        app_id="2440510",
        host=("arch", "x86_64", "native-steam"),
        profile="fixture",
        protonup=ProtonUpSpec("0.1.5", "tools/protonup"),
        ge=DownloadSpec(
            "GE-Proton11-3",
            "GE-Proton11-3",
            "https://example.invalid/ge.tar.gz",
            "GE-Proton11-3.tar.gz",
            100,
            "1" * 64,
            "GE-Proton11-3",
            ("example.invalid",),
        ),
        bundle=DownloadSpec(
            "forza-bootstrap-bundle",
            "v1",
            "https://example.invalid/bundle.tar.zst",
            "bundle.tar.zst",
            200,
            "2" * 64,
            "forza-bootstrap-bundle-v1",
            ("example.invalid",),
        ),
        sources=(
            SourceSpec("xodus", "https://example.invalid/xodus", "3" * 40),
            SourceSpec("xgameruntime", "https://example.invalid/runtime", "4" * 40),
        ),
        builder=BuilderSpec(
            "example.invalid/builder@sha256:" + "5" * 64,
            "example.invalid/base@sha256:" + "6" * 64,
            "https://archive.example.invalid/$repo/os/$arch",
            1,
        ),
        artifacts=(
            ArtifactSpec("xodus-service", "bin/xodus-service", 1, "7" * 64, 0o755),
        ),
        path=tmp_path / "bootstrap.toml",
        sha256="8" * 64,
    )


def record(
    logical_path: str,
    payload: bytes | None,
    *,
    mode: int = 0o644,
    private: bool = False,
) -> FileRecord:
    if payload is None:
        return FileRecord(logical_path, "absent", None, None, None, private)
    return FileRecord(
        logical_path,
        "regular",
        mode,
        len(payload),
        sha256(payload),
        private,
    )


class FinishFixture:
    def __init__(self, tmp_path: Path, *, doctor_failure: bool = False) -> None:
        self.user = tmp_path / "home"
        self.steam = self.user / ".local/share/Steam"
        self.state_root = self.user / ".local/state/forza-motorsport-linux/bootstrap"
        self.runtime = tmp_path / "run"
        self.cache = self.user / ".cache/forza-motorsport-linux/bootstrap"
        self.bundle = self.cache / "bundle/forza-bootstrap-bundle-v1"
        self.system32 = (
            self.steam
            / "steamapps/compatdata/2440510/pfx/drive_c/windows/system32"
        )
        self.compatibility = self.steam / "compatibilitytools.d"
        self.app_manifest = self.steam / "steamapps/appmanifest_2440510.acf"
        for directory in (
            self.user,
            self.system32 / "drivers",
            self.compatibility / "GE-Proton11-3-FM",
            self.bundle,
            self.runtime,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.app_manifest.write_text('"appid" "2440510"\n', encoding="ascii")
        self.cache.chmod(0o700)
        self.manifest = manifest_fixture(tmp_path)
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
        state = create_transaction(self.state_root, self.manifest.sha256)
        state = transition(state, BootstrapPhase.NEW, BootstrapPhase.PREPARING)
        self.state = transition(
            state,
            BootstrapPhase.PREPARING,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
            compatibility_tool_disposition="created",
        )
        self.stdout = io.StringIO()
        self.actions: list[str] = []
        self.after_ready = False
        self.licensed_source = tmp_path / "licensed.dll"
        self.licensed_source.write_bytes(b"synthetic private PE")
        self.licensed_hash = sha256(self.licensed_source.read_bytes())
        threading_path = self.system32 / "xgameruntime.dll.threading"
        self.licensed_action = LicensedAction(
            str(threading_path),
            self.licensed_source.stat().st_size,
            self.licensed_hash,
            record("prefix/threading", None, private=True),
            "install",
            self.licensed_source,
            threading_path,
        )
        self.runtime_hash = sha256(b"runtime after")
        self.patch_hash = sha256(b"patch after")
        self.guard_hash = sha256(b"guard")
        self.before = Snapshot(
            1,
            self.state.transaction_id,
            self.manifest.sha256,
            "1:2",
            (
                record("guard", b"guard"),
                record("runtime", None),
                record("patch", b"patch before"),
                record("prefix/threading", None, private=True),
            ),
        )
        self.pre_finish = self.before
        self.after = Snapshot(
            1,
            self.state.transaction_id,
            self.manifest.sha256,
            "1:2",
            (
                record("guard", b"guard"),
                record("runtime", b"runtime after", mode=0o755),
                record("patch", b"patch after"),
                record(
                    "prefix/threading",
                    self.licensed_source.read_bytes(),
                    private=True,
                ),
            ),
        )
        transaction = self.state_root / self.state.transaction_id
        from forza_bootstrap.snapshot import write_snapshot

        write_snapshot(transaction / "before.json", self.before)
        self.runtime_plan = RuntimePlan(
            sha256="9" * 64,
            evidence_sha256="a" * 64,
            targets=(PlannedTarget("runtime", self.runtime_hash, 0o755),),
            disposition="install",
        )
        self.patch_plan = PatchInspection(
            states=("original",),
            targets=(PlannedTarget("patch", self.patch_hash, 0o644),),
            disposition="apply",
        )
        self.patch_backup = transaction / "recovery/patches/forza-patch-20260904T010203Z.json"

        def validate_prefix(_context: FinishContext) -> PrefixInspection:
            return PrefixInspection("1:20", "1:21")

        def install_threading(
            _context: FinishContext, _action: LicensedAction, recovery: Path
        ) -> None:
            recovery.mkdir(parents=True, exist_ok=True)
            (recovery / "licensed.json").write_text("{}", encoding="ascii")

        def install_runtime(
            _context: FinishContext, _plan: RuntimePlan
        ) -> str | None:
            return "b" * 24

        def apply_patches(
            _context: FinishContext, _plan: PatchInspection, _root: Path
        ) -> Path | None:
            self.patch_backup.parent.mkdir(parents=True, exist_ok=True)
            self.patch_backup.write_text("{}", encoding="ascii")
            self.patch_backup.chmod(0o600)
            return self.patch_backup

        def doctor(_context: FinishContext) -> str:
            if doctor_failure:
                raise BootstrapError("doctor found blockers")
            self.after_ready = True
            return "doctor: ok\n"

        self.operations = FinishOperations(
            validate_prefix=validate_prefix,
            plan_threading=lambda _context: self.licensed_action,
            capture_current=lambda _context: (
                self.after if self.after_ready else self.pre_finish
            ),
            lock_runtime_evidence=lambda _context: "a" * 64,
            runtime_plan=lambda _context, _snapshot, _evidence: self.runtime_plan,
            inspect_patches=lambda _context, _snapshot: self.patch_plan,
            command_versions=lambda _context: {
                "runtime-installer": "c" * 64,
                "patcher": "d" * 64,
                "doctor": "e" * 64,
                "steam-options": "f" * 64,
            },
            install_threading=install_threading,
            install_runtime=install_runtime,
            runtime_status=lambda _context, _plan, _transaction: None,
            apply_patches=apply_patches,
            doctor=doctor,
            steam_options=lambda _context: "EXACT=1 launcher %command%\n",
            event=self.actions.append,
        )
        self.context = FinishContext(
            manifest=self.manifest,
            host=self.host,
            repository_root=tmp_path,
            state=self.state,
            threading_dll=self.licensed_source,
            bundle_root=self.bundle,
            bundle_manifest_sha256="0" * 64,
            compatibility_tool_tree_sha256="1" * 64,
            output=self.stdout,
            operations=self.operations,
        )

    @property
    def finish_plan(self):
        return build_finish_plan(self.context)

    def run(self, confirmation: str | None = None):
        digest = self.finish_plan.sha256 if confirmation is None else confirmation
        self.actions.clear()
        return finish(self.context, lambda _expected: digest)


def test_finish_binds_and_executes_all_children_in_order(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)

    state = fixture.run()

    assert state.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert fixture.actions == [
        "validate-prefix",
        "validate-threading-input",
        "lock-runtime-evidence",
        "runtime-plan",
        "patch-inspect",
        "print-finish-plan",
        "confirm-finish-plan",
        "install-threading",
        "runtime-install",
        "runtime-status",
        "patch-apply",
        "doctor",
        "write-after-snapshot",
        "compare-snapshots",
        "steam-options",
    ]
    document = json.loads(
        (fixture.state_root / state.transaction_id / "plan.json").read_text()
    )
    assert document["runtime_plan_sha256"] == fixture.runtime_plan.sha256
    assert document["patch_after_sha256"] == [fixture.patch_hash]
    assert fixture.licensed_hash not in fixture.stdout.getvalue()
    assert str(fixture.licensed_source) not in fixture.stdout.getvalue()
    assert "READY TO ATTEMPT" in fixture.stdout.getvalue()
    assert state.child_runtime_transaction == "b" * 24
    assert state.patch_backup_manifest == str(fixture.patch_backup)
    transaction = fixture.state_root / state.transaction_id
    for name in (
        "plan.json",
        "pre-finish.json",
        "licensed-plan.json",
        "after.json",
        "comparison.json",
    ):
        assert stat.S_IMODE((transaction / name).stat().st_mode) == 0o600


def test_finish_doctor_failure_retains_recovery_and_never_claims_ready(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path, doctor_failure=True)

    with pytest.raises(BootstrapError, match="doctor found blockers"):
        fixture.run()

    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None
    assert state.phase is BootstrapPhase.INSTALLING_FINISH
    assert state.child_runtime_transaction == "b" * 24
    assert state.patch_backup_manifest == str(fixture.patch_backup)
    assert (fixture.state_root / state.transaction_id / "recovery").is_dir()
    assert "READY TO ATTEMPT" not in fixture.stdout.getvalue()


def test_finish_rejects_changed_runtime_child_plan_before_mutation(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    calls = 0

    def changing_plan(
        _context: FinishContext, _snapshot: Snapshot, _evidence: str
    ) -> RuntimePlan:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return fixture.runtime_plan
        return replace(fixture.runtime_plan, sha256="f" * 64)

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, runtime_plan=changing_plan),
    )

    with pytest.raises(BootstrapError, match="changed after confirmation"):
        fixture.run()

    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None
    assert state.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert not (fixture.state_root / state.transaction_id / "licensed-plan.json").exists()


@pytest.mark.parametrize("mutation", ("missing", "incomplete", "symlink"))
def test_validate_prefix_rejects_missing_incomplete_or_symlinked_layout(
    tmp_path: Path, mutation: str
) -> None:
    fixture = FinishFixture(tmp_path)
    empty_proc = tmp_path / "proc"
    empty_proc.mkdir()
    inactive = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 3, "", ""
    )
    fixture.context = replace(
        fixture.context,
        probe=replace(fixture.context.probe, runner=inactive, proc_root=empty_proc),
    )
    prefix = fixture.system32.parents[2]
    if mutation == "missing":
        prefix.rename(prefix.with_name("pfx-away"))
    elif mutation == "incomplete":
        (fixture.system32 / "drivers").rmdir()
        fixture.system32.rmdir()
    else:
        real = tmp_path / "outside-system32"
        real.mkdir()
        (fixture.system32 / "drivers").rmdir()
        fixture.system32.rmdir()
        fixture.system32.symlink_to(real, target_is_directory=True)

    with pytest.raises(BootstrapError, match="prefix"):
        validate_prefix(fixture.context)


def test_validate_prefix_rejects_foreign_owned_intermediate_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FinishFixture(tmp_path)
    empty_proc = tmp_path / "proc"
    empty_proc.mkdir()
    inactive = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 3, "", ""
    )
    fixture.context = replace(
        fixture.context,
        probe=replace(fixture.context.probe, runner=inactive, proc_root=empty_proc),
    )
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: SimpleNamespace(
            disposition=ToolDisposition.ADOPTED,
            tree_sha256=fixture.context.compatibility_tool_tree_sha256,
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "verify_bundle_root",
        lambda *_args: SimpleNamespace(
            manifest_sha256=fixture.context.bundle_manifest_sha256
        ),
    )
    real_fstat = coordinator_module.os.fstat

    def foreign_drive_c(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        linked = Path(f"/proc/self/fd/{descriptor}").readlink()
        if linked == fixture.system32.parents[1]:
            fields = list(result)
            fields[4] = coordinator_module.os.getuid() + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(coordinator_module.os, "fstat", foreign_drive_c)

    with pytest.raises(BootstrapError, match="prefix.*ownership"):
        validate_prefix(fixture.context)


def test_validate_prefix_rejects_active_game_or_service_read_only(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)
    proc = tmp_path / "proc/123"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"forza_steamworks_release_final.exe")
    inactive = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 3, "", ""
    )
    fixture.context = replace(
        fixture.context,
        probe=replace(fixture.context.probe, runner=inactive, proc_root=tmp_path / "proc"),
    )
    before = tuple(sorted(path.as_posix() for path in tmp_path.rglob("*")))

    with pytest.raises(BootstrapError, match="process is active"):
        validate_prefix(fixture.context)

    assert tuple(sorted(path.as_posix() for path in tmp_path.rglob("*"))) == before

    active = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, "", ""
    )
    (proc / "cmdline").unlink()
    fixture.context = replace(
        fixture.context,
        probe=replace(fixture.context.probe, runner=active, proc_root=tmp_path / "proc"),
    )
    with pytest.raises(BootstrapError, match="service is active"):
        validate_prefix(fixture.context)


def test_finish_requires_licensed_option_before_child_evidence(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)
    fixture.context = replace(fixture.context, threading_dll=None)

    with pytest.raises(BootstrapError, match="--threading-dll"):
        finish(fixture.context, lambda value: value)

    assert fixture.actions == []


def test_finish_rejects_wrong_top_digest_before_coordinator_state_write(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    transaction = fixture.state_root / fixture.state.transaction_id

    with pytest.raises(BootstrapError, match="confirmation did not match"):
        fixture.run("0" * 64)

    assert not (transaction / "pre-finish.json").exists()
    assert not (transaction / "licensed-plan.json").exists()
    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None and state.phase is BootstrapPhase.AWAITING_STEAM_PREFIX


def test_finish_reopens_before_snapshot_after_confirmation_before_mutation(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    expected = fixture.finish_plan.sha256
    transaction = fixture.state_root / fixture.state.transaction_id

    def replace_before(_digest: str) -> str:
        value = json.loads((transaction / "before.json").read_text())
        value["records"][0]["sha256"] = "f" * 64
        (transaction / "before.json").write_text(json.dumps(value), encoding="ascii")
        (transaction / "before.json").chmod(0o600)
        return expected

    with pytest.raises(BootstrapError, match="changed after confirmation"):
        finish(fixture.context, replace_before)

    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None and state.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert not (transaction / "licensed-plan.json").exists()


def test_finish_context_binds_prepare_record_to_original_plan(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)
    transaction = fixture.state_root / fixture.state.transaction_id
    prepare_plan = {
        "schema": 1,
        "phase": "prepare",
        "manifest_sha256": fixture.manifest.sha256,
    }
    prepare_record = {
        "schema": 1,
        "plan_sha256": plan_digest(prepare_plan),
        "ge_root": {"root": "cache", "relative": "ge/GE-Proton11-3"},
        "bundle_root": {
            "root": "cache",
            "relative": "bundle/forza-bootstrap-bundle-v1",
        },
        "bundle_manifest_sha256": "0" * 64,
        "compatibility_tool": {
            "disposition": "created",
            "identity": {
                "name": "GE-Proton11-3-FM",
                "base_release": "GE-Proton11-3",
                "vdf_sha256": "1" * 64,
                "managed_tree_sha256": "2" * 64,
                "marker_sha256": "3" * 64,
            },
            "tree_sha256": "1" * 64,
        },
    }
    (fixture.cache / "ge/GE-Proton11-3").mkdir(parents=True)
    for name, value in (("plan.json", prepare_plan), ("prepare.json", prepare_record)):
        (transaction / name).write_text(json.dumps(value), encoding="ascii")
        (transaction / name).chmod(0o600)
    prepare_context = PrepareContext(
        fixture.manifest,
        fixture.host,
        tmp_path,
        output=fixture.stdout,
    )

    result = finish_context_from_prepare(
        prepare_context, fixture.state, fixture.licensed_source
    )
    assert result.bundle_root == fixture.bundle

    prepare_plan["phase"] = "changed"
    (transaction / "plan.json").write_text(json.dumps(prepare_plan), encoding="ascii")
    (transaction / "plan.json").chmod(0o600)
    with pytest.raises(BootstrapError, match="Prepare checkpoint"):
        finish_context_from_prepare(
            prepare_context, fixture.state, fixture.licensed_source
        )


@pytest.mark.parametrize(
    ("operation", "message"),
    (
        ("runtime", "runtime child failed"),
        ("patch", "patch failed"),
    ),
)
def test_finish_child_failure_keeps_installing_state_and_recovery(
    tmp_path: Path, operation: str, message: str
) -> None:
    fixture = FinishFixture(tmp_path)

    def fail(*_args: object) -> object:
        raise BootstrapError(message)

    operations = fixture.operations
    if operation == "runtime":
        operations = replace(operations, install_runtime=fail)  # type: ignore[arg-type]
    else:
        operations = replace(operations, apply_patches=fail)  # type: ignore[arg-type]
    fixture.context = replace(fixture.context, operations=operations)

    with pytest.raises(BootstrapError, match=message):
        fixture.run()

    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None and state.phase is BootstrapPhase.INSTALLING_FINISH
    assert (fixture.state_root / state.transaction_id / "recovery").is_dir()
    assert "READY TO ATTEMPT" not in fixture.stdout.getvalue()


def test_finish_rejects_mixed_patch_state_before_confirmation(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)
    mixed = PatchInspection(
        states=("original", "patched"),
        targets=(
            PlannedTarget("patch", fixture.patch_hash, 0o644),
            PlannedTarget("guard", fixture.guard_hash, 0o644),
        ),
        disposition="apply",
    )
    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, inspect_patches=lambda *_args: mixed),
    )

    with pytest.raises(BootstrapError, match="unknown or mixed"):
        build_finish_plan(fixture.context)


def test_finish_unexpected_snapshot_or_options_failure_never_claims_ready(
    tmp_path: Path,
) -> None:
    unexpected = FinishFixture(tmp_path / "unexpected")
    unexpected.after = replace(
        unexpected.after,
        records=tuple(
            record("guard", b"changed") if item.logical_path == "guard" else item
            for item in unexpected.after.records
        ),
    )
    with pytest.raises(BootstrapError, match="unexpected changes"):
        unexpected.run()
    assert "READY TO ATTEMPT" not in unexpected.stdout.getvalue()

    options = FinishFixture(tmp_path / "options")
    options.context = replace(
        options.context,
        operations=replace(options.operations, steam_options=lambda _context: ""),
    )
    with pytest.raises(BootstrapError, match="launch-options"):
        options.run()
    assert "READY TO ATTEMPT" not in options.stdout.getvalue()


def test_finish_exact_existing_state_is_adopted_without_child_or_patch_mutation(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    threading = record(
        "prefix/threading", fixture.licensed_source.read_bytes(), private=True
    )
    runtime = record("runtime", b"runtime after", mode=0o755)
    patch = record("patch", b"patch after")
    fixture.before = replace(
        fixture.before,
        records=(record("guard", b"guard"), runtime, patch, threading),
    )
    fixture.pre_finish = fixture.before
    fixture.after = fixture.before
    transaction = fixture.state_root / fixture.state.transaction_id
    from forza_bootstrap.snapshot import write_snapshot

    write_snapshot(transaction / "before.json", fixture.before)
    fixture.licensed_action = replace(
        fixture.licensed_action, before=threading, disposition="keep"
    )
    fixture.runtime_plan = replace(fixture.runtime_plan, disposition="keep")
    fixture.patch_plan = PatchInspection(
        states=("patched",),
        targets=fixture.patch_plan.targets,
        disposition="keep",
    )
    mutations: list[str] = []
    operations = replace(
        fixture.operations,
        plan_threading=lambda _context: fixture.licensed_action,
        runtime_plan=lambda _context, _snapshot, _evidence: fixture.runtime_plan,
        inspect_patches=lambda _context, _snapshot: fixture.patch_plan,
        install_runtime=lambda *_args: mutations.append("runtime"),
        apply_patches=lambda *_args: mutations.append("patch"),
    )
    fixture.context = replace(fixture.context, operations=operations)

    planned = fixture.finish_plan
    result = fixture.run()

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert result.child_runtime_transaction is None
    assert result.patch_backup_manifest is None
    assert mutations == []
    assert all(rule.policy == "unchanged" for rule in planned.snapshot_rules)


def test_default_finish_children_use_argv_no_shell_and_minimal_environment(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(argv: tuple[str, ...], **kwargs: object):
        calls.append((argv, kwargs))
        operation = argv[1]
        if operation == "lock-evidence":
            output = "EVIDENCE_SHA256=" + "a" * 64 + "\n"
        else:
            output = (
                "PLAN launcher-gate: user/.local/bin/forza-linux before=absent "
                f"source={'b' * 64} mode=0755\n"
                "PLAN doctor: user/.local/bin/forza-doctor before=absent "
                f"source={'c' * 64} mode=0755\n"
                "PLAN known-build-patcher: user/.local/libexec/forza-motorsport-linux/patch-known-build before=absent "
                f"source={'d' * 64} mode=0755\n"
                "PLAN supported-builds: user/.local/share/forza-motorsport-linux/supported-builds.toml before=absent "
                f"source={'e' * 64} mode=0644\n"
                "PLAN xgameruntime-pe64: compat-tool/files/lib/wine/x86_64-windows/xgameruntime.dll before=absent "
                f"source={'f' * 64} mode=0755\n"
                "PLAN xgameruntime-unix64: compat-tool/files/lib/wine/x86_64-unix/xgameruntime.so before=absent "
                f"source={'1' * 64} mode=0755\n"
                "PLAN xodus-service: user/.local/libexec/xodus-forza/xodus-service before=absent "
                f"source={'2' * 64} mode=0755\n"
                "PLAN xodus-cli: user/.local/libexec/xodus-forza/xodus-cli before=absent "
                f"source={'3' * 64} mode=0755\n"
                "PLAN xodus-overlay: user/.local/libexec/xodus-forza/xodus-overlay before=absent "
                f"source={'4' * 64} mode=0755\n"
                "PLAN systemd-unit: user/.config/systemd/user/xodus-forza.service before=absent "
                f"source={'5' * 64} mode=0644\n"
                f"PLAN_SHA256={'6' * 64}\n"
            )
        return subprocess.CompletedProcess(argv, 0, output, "")

    fixture.context = replace(
        fixture.context,
        runner=runner,
        child_environment={"FORZA_RUNTIME_TESTING": "1"},
        operations=None,
    )
    operations = default_finish_operations()

    evidence = operations.lock_runtime_evidence(fixture.context)
    runtime_plan = operations.runtime_plan(
        fixture.context, fixture.pre_finish, evidence
    )

    assert evidence == "a" * 64
    assert runtime_plan.sha256 == "6" * 64
    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, tuple)
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert set(environment) <= {
            "HOME",
            "LANG",
            "PATH",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
            "XDG_STATE_HOME",
            "FORZA_RUNTIME_TESTING",
        }
        assert "LD_PRELOAD" not in environment
    common = calls[0][0]
    assert common[0] == str(tmp_path / "scripts/install-runtime-components")
    assert "--artifact-bundle-root" in common
    assert str(fixture.steam) not in str(fixture.bundle)
