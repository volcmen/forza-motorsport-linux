# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import fcntl
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


PATCH_LOGICAL_PATHS = (
    "compat/controller",
    "prefix/controller",
    "compat/mountmgr",
    "prefix/mountmgr",
)

RUNTIME_LOGICAL_TARGETS = (
    ("user/.local/bin/forza-linux", 0o755),
    ("user/.local/bin/forza-doctor", 0o755),
    ("user/.local/libexec/forza-motorsport-linux/patch-known-build", 0o755),
    ("user/.local/share/forza-motorsport-linux/supported-builds.toml", 0o644),
    ("compat/xgameruntime-pe64", 0o755),
    ("compat/xgameruntime-unix64", 0o755),
    ("user/.local/libexec/xodus-forza/xodus-service", 0o755),
    ("user/.local/libexec/xodus-forza/xodus-cli", 0o755),
    ("user/.local/libexec/xodus-forza/xodus-overlay", 0o755),
    ("user/.config/systemd/user/xodus-forza.service", 0o644),
    ("user/.local/state/forza-motorsport-linux/install-manifest", 0o600),
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
            record(str(threading_path), None, private=True),
            "install",
            self.licensed_source,
            threading_path,
        )
        self.runtime_hash = sha256(b"runtime after")
        self.patch_hash = sha256(b"patch after")
        self.guard_hash = sha256(b"guard")
        steam_info = self.steam.stat()
        steam_root_id = f"{steam_info.st_dev}:{steam_info.st_ino}"
        before_records = [
            record("compat/tool-marker", b"guard"),
            record("prefix/threading", None, private=True),
        ]
        before_records.extend(
            record(logical_path, b"patch before")
            for logical_path in PATCH_LOGICAL_PATHS
        )
        before_records.extend(
            record(logical_path, None)
            for logical_path, _mode in RUNTIME_LOGICAL_TARGETS
        )
        self.before = Snapshot(
            1,
            self.state.transaction_id,
            self.manifest.sha256,
            steam_root_id,
            tuple(sorted(before_records, key=lambda item: item.logical_path)),
        )
        self.pre_finish = self.before
        after_records = [
            record("compat/tool-marker", b"guard"),
            record(
                "prefix/threading",
                self.licensed_source.read_bytes(),
                private=True,
            ),
        ]
        after_records.extend(
            record(logical_path, b"patch after")
            for logical_path in PATCH_LOGICAL_PATHS
        )
        after_records.extend(
            record(logical_path, b"runtime after", mode=mode)
            for logical_path, mode in RUNTIME_LOGICAL_TARGETS
        )
        self.after = Snapshot(
            1,
            self.state.transaction_id,
            self.manifest.sha256,
            steam_root_id,
            tuple(sorted(after_records, key=lambda item: item.logical_path)),
        )
        transaction = self.state_root / self.state.transaction_id
        from forza_bootstrap.snapshot import write_snapshot

        write_snapshot(transaction / "before.json", self.before)
        self.runtime_plan = RuntimePlan(
            sha256="9" * 64,
            evidence_sha256="a" * 64,
            targets=tuple(
                PlannedTarget(logical_path, self.runtime_hash, mode)
                for logical_path, mode in RUNTIME_LOGICAL_TARGETS
            ),
            disposition="install",
        )
        self.patch_plan = PatchInspection(
            states=("original",) * len(PATCH_LOGICAL_PATHS),
            targets=tuple(
                PlannedTarget(logical_path, self.patch_hash, 0o644)
                for logical_path in PATCH_LOGICAL_PATHS
            ),
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
            command_versions=lambda _context, _runtime: {
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
    assert document["patch_after_sha256"] == [fixture.patch_hash] * 4
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


def test_finish_refuses_a_held_launcher_lease_before_any_child_mutation(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    lock = fixture.runtime / "forza-linux.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    mutations: list[str] = []
    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.operations,
            install_threading=lambda *_args: mutations.append("threading"),
            install_runtime=lambda *_args: mutations.append("runtime"),
            apply_patches=lambda *_args: mutations.append("patch"),
        ),
    )
    try:
        with pytest.raises(BootstrapError, match="launcher lock is held"):
            fixture.run()
    finally:
        os.close(descriptor)

    assert mutations == []
    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None and state.phase is BootstrapPhase.AWAITING_STEAM_PREFIX


def test_finish_holds_the_launcher_lease_through_every_execution_boundary(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    expected = {
        "install-threading",
        "runtime-install",
        "runtime-status",
        "patch-apply",
        "doctor",
        "write-after-snapshot",
        "compare-snapshots",
        "steam-options",
    }
    checked: list[str] = []

    def prove_excluded(boundary: str) -> None:
        if boundary not in expected:
            return
        contender = os.open(
            fixture.runtime / "forza-linux.lock",
            os.O_RDWR | os.O_NOFOLLOW,
        )
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
        checked.append(boundary)

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=prove_excluded),
    )

    result = fixture.run()

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert set(checked) == expected
    contender = os.open(
        fixture.runtime / "forza-linux.lock",
        os.O_RDWR | os.O_NOFOLLOW,
    )
    try:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender)


def test_finish_revalidates_the_bound_launcher_lease_before_next_child(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    runtime_calls: list[str] = []

    def rebind_lock(boundary: str) -> None:
        if boundary != "install-threading":
            return
        lock = fixture.runtime / "forza-linux.lock"
        replacement = fixture.runtime / "replacement.lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        replacement.replace(lock)

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.operations,
            boundary=rebind_lock,
            install_runtime=lambda *_args: runtime_calls.append("runtime"),
        ),
    )

    with pytest.raises(BootstrapError, match="launcher lock changed"):
        fixture.run()

    assert runtime_calls == []
    state = load_unfinished_transaction(fixture.state_root)
    assert state is not None and state.phase is BootstrapPhase.INSTALLING_FINISH


@pytest.mark.parametrize("source", ("before", "fresh"))
def test_finish_rejects_wrong_steam_root_snapshot_before_child_mutation(
    tmp_path: Path, source: str
) -> None:
    fixture = FinishFixture(tmp_path)
    wrong = replace(
        fixture.before if source == "before" else fixture.pre_finish,
        steam_root_id="9:9",
    )
    if source == "before":
        from forza_bootstrap.snapshot import write_snapshot

        write_snapshot(
            fixture.state_root / fixture.state.transaction_id / "before.json",
            wrong,
        )
    else:
        fixture.pre_finish = wrong
    mutations: list[str] = []
    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.operations,
            install_threading=lambda *_args: mutations.append("threading"),
            install_runtime=lambda *_args: mutations.append("runtime"),
            apply_patches=lambda *_args: mutations.append("patch"),
        ),
    )

    with pytest.raises(BootstrapError, match="Steam-root identity"):
        fixture.run()

    assert mutations == []
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
        marker = next(
            item
            for item in value["records"]
            if item["logical_path"] == "compat/tool-marker"
        )
        marker["sha256"] = "f" * 64
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
        states=("original", "patched", "original", "original"),
        targets=fixture.patch_plan.targets,
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
            record("compat/tool-marker", b"changed")
            if item.logical_path == "compat/tool-marker"
            else item
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
    threading = next(
        item
        for item in fixture.after.records
        if item.logical_path == "prefix/threading"
    )
    fixture.before = fixture.after
    fixture.pre_finish = fixture.before
    fixture.after = fixture.before
    transaction = fixture.state_root / fixture.state.transaction_id
    from forza_bootstrap.snapshot import write_snapshot

    write_snapshot(transaction / "before.json", fixture.before)
    fixture.licensed_action = replace(
        fixture.licensed_action,
        before=replace(threading, logical_path=fixture.licensed_action.destination_logical),
        disposition="keep",
    )
    fixture.runtime_plan = replace(fixture.runtime_plan, disposition="keep")
    fixture.patch_plan = PatchInspection(
        states=("patched",) * len(PATCH_LOGICAL_PATHS),
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
    runtime_installer = tmp_path / "scripts/install-runtime-components"
    runtime_installer.parent.mkdir(parents=True)
    runtime_installer.write_bytes(b"#!/usr/bin/env python3\n")
    runtime_installer.chmod(0o755)
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
            "FORZA_RUNTIME_INTEGRATION_ROOT",
            "FORZA_RUNTIME_TESTING",
        }
        assert "LD_PRELOAD" not in environment
        executable = kwargs["executable"]
        assert isinstance(executable, str)
        assert executable.startswith("/proc/self/fd/")
        assert kwargs["pass_fds"]
    common = calls[0][0]
    assert common[0] == str(tmp_path / "scripts/install-runtime-components")
    assert "--artifact-bundle-root" in common
    assert str(fixture.steam) not in str(fixture.bundle)


def command_fixture(fixture: FinishFixture, payload: bytes) -> dict[str, Path]:
    paths = {
        "runtime-installer": fixture.context.repository_root
        / "scripts/install-runtime-components",
        "patcher": fixture.context.repository_root / "scripts/patch-known-build",
        "doctor": fixture.user / ".local/bin/forza-doctor",
        "steam-options": fixture.context.repository_root
        / "scripts/print-steam-options",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o755)
    return paths


def test_default_command_plan_binds_the_runtime_planned_installed_doctor(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    command_fixture(fixture, b"#!/bin/sh\nexit 0\n")
    planned_doctor = PlannedTarget(
        "user/.local/bin/forza-doctor", "7" * 64, 0o755
    )
    runtime = replace(fixture.runtime_plan, targets=(planned_doctor,))

    versions = default_finish_operations().command_versions(
        fixture.context, runtime
    )

    assert versions["doctor"] == planned_doctor.sha256


@pytest.mark.parametrize(
    ("command", "boundary"),
    (
        ("runtime-installer", "install-threading"),
        ("patcher", "runtime-status"),
        ("doctor", "patch-apply"),
        ("steam-options", "compare-snapshots"),
    ),
)
def test_finish_rejects_each_rebound_command_before_spawning_it(
    tmp_path: Path, command: str, boundary: str
) -> None:
    fixture = FinishFixture(tmp_path)
    original = b"#!/bin/sh\nexit 0\n"
    paths = command_fixture(fixture, original)
    versions = {name: sha256(original) for name in paths}
    calls: list[tuple[str, ...]] = []
    swapped = False

    def rebind(name: str) -> None:
        nonlocal swapped
        if name != boundary or swapped:
            return
        replacement = paths[command].with_name(paths[command].name + ".replacement")
        replacement.write_bytes(b"#!/bin/sh\nexit 42\n")
        replacement.chmod(0o755)
        replacement.replace(paths[command])
        swapped = True

    def runner(
        argv: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if len(argv) > 1 and argv[1] == "install":
            output = "TRANSACTION_ID=" + "b" * 24 + "\n"
        elif "--backup-root" in argv:
            root = Path(argv[argv.index("--backup-root") + 1])
            root.mkdir(parents=True, exist_ok=True)
            backup = root / "forza-patch-20260904T010203Z.json"
            backup.write_text("{}", encoding="ascii")
            backup.chmod(0o600)
            output = f"{backup}\n"
        elif Path(argv[0]).name == "forza-doctor":
            output = "doctor: ok\n"
        else:
            output = "EXACT=1 launcher %command%\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    defaults = default_finish_operations()
    operations = replace(
        fixture.operations,
        command_versions=lambda _context, *_args: versions,
        boundary=rebind,
        **{
            {
                "runtime-installer": "install_runtime",
                "patcher": "apply_patches",
                "doctor": "doctor",
                "steam-options": "steam_options",
            }[command]: {
                "runtime-installer": defaults.install_runtime,
                "patcher": defaults.apply_patches,
                "doctor": defaults.doctor,
                "steam-options": defaults.steam_options,
            }[command]
        },
    )
    fixture.context = replace(
        fixture.context,
        operations=operations,
        runner=runner,
    )

    with pytest.raises(BootstrapError, match="executable changed after Finish plan"):
        fixture.run()

    assert swapped is True
    assert calls == []


def test_command_spawn_executes_the_held_object_if_its_path_is_rebound(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    original = b"#!/bin/sh\nexit 0\n"
    path = command_fixture(fixture, original)["steam-options"]
    observed: list[bytes] = []

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(b"#!/bin/sh\nexit 42\n")
        replacement.chmod(0o755)
        replacement.replace(path)
        executable = kwargs["executable"]
        assert isinstance(executable, str)
        observed.append(Path(executable).read_bytes())
        pass_fds = kwargs["pass_fds"]
        assert isinstance(pass_fds, tuple) and pass_fds
        return subprocess.CompletedProcess(argv, 0, "EXACT=1 launcher %command%\n", "")

    fixture.context = replace(
        fixture.context,
        runner=runner,
        command_sha256={
            "runtime-installer": sha256(original),
            "patcher": sha256(original),
            "doctor": sha256(original),
            "steam-options": sha256(original),
        },
    )

    output = default_finish_operations().steam_options(fixture.context)

    assert output == "EXACT=1 launcher %command%\n"
    assert observed == [original]


def test_held_script_descriptor_executes_without_a_shell_string(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    payload = b"#!/bin/sh\nprintf '%s\\n' 'EXACT=1 launcher %command%'\n"
    path = command_fixture(fixture, payload)["steam-options"]
    fixture.context = replace(
        fixture.context,
        command_sha256={
            "runtime-installer": "1" * 64,
            "patcher": "2" * 64,
            "doctor": "3" * 64,
            "steam-options": sha256(payload),
        },
    )

    output = default_finish_operations().steam_options(fixture.context)

    assert path.exists()
    assert output == "EXACT=1 launcher %command%\n"
