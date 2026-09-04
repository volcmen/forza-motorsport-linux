# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import forza_bootstrap.coordinator as coordinator_module
from forza_bootstrap.artifacts import VerifiedBundle
from forza_bootstrap.coordinator import (
    HostProbe,
    PrepareContext,
    PrepareInspection,
    PrepareOperations,
    ResolvedHost,
    build_prepare_plan,
    default_prepare_operations,
    inspect_prepare,
    prepare,
    resolve_supported_host,
)
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
from forza_bootstrap.proton import (
    PublishedToolRecord,
    ToolDisposition,
    ToolIdentity,
    ToolInspection,
)
from forza_bootstrap.snapshot import FileRecord, Snapshot
from forza_bootstrap.state import BootstrapPhase, load_unfinished_transaction


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def manifest_fixture(tmp_path: Path) -> BootstrapManifest:
    artifacts = (
        ArtifactSpec("xodus-service", "bin/xodus-service", 7, "2" * 64, 0o755),
        ArtifactSpec(
            "xgameruntime-pe64", "runtime/xgameruntime.dll", 8, "3" * 64, 0o644
        ),
        ArtifactSpec(
            "xgameruntime-unix64", "runtime/xgameruntime.so", 9, "4" * 64, 0o755
        ),
    )
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
            "5" * 64,
            "GE-Proton11-3",
            ("example.invalid",),
        ),
        bundle=DownloadSpec(
            "forza-bootstrap-bundle",
            "v1",
            "https://example.invalid/bundle.tar.zst",
            "bundle.tar.zst",
            200,
            "6" * 64,
            "forza-bootstrap-bundle-v1",
            ("example.invalid",),
        ),
        sources=(
            SourceSpec("xodus", "https://example.invalid/xodus", "7" * 40),
            SourceSpec("xgameruntime", "https://example.invalid/runtime", "8" * 40),
        ),
        builder=BuilderSpec(
            "example.invalid/builder@sha256:" + "9" * 64,
            "example.invalid/base@sha256:" + "a" * 64,
            "https://archive.example.invalid/$repo/os/$arch",
            1,
        ),
        artifacts=artifacts,
        path=tmp_path / "bootstrap.toml",
        sha256="b" * 64,
    )


class PrepareFixture:
    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str = "download",
        disposition: ToolDisposition = ToolDisposition.ABSENT,
        docker_available: bool = True,
    ) -> None:
        self.root = tmp_path
        self.user = tmp_path / "home"
        self.steam = self.user / ".local/share/Steam"
        self.state = self.user / ".local/state/forza-motorsport-linux/bootstrap"
        self.cache = self.user / ".cache/forza-motorsport-linux/bootstrap"
        self.runtime = tmp_path / "runtime"
        self.app_manifest = self.steam / "steamapps/appmanifest_2440510.acf"
        self.compatibility = self.steam / "compatibilitytools.d"
        for directory in (
            self.user,
            self.steam / "steamapps",
            self.compatibility,
            self.runtime,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.app_manifest.write_text('"appid" "2440510"\n', encoding="ascii")
        self.manifest = manifest_fixture(tmp_path)
        self.host = ResolvedHost(
            "arch",
            "x86_64",
            self.user,
            self.steam,
            self.app_manifest,
            self.compatibility,
            self.state,
            self.cache,
            self.runtime,
        )
        self.actions: list[str] = []
        self.stdout = io.StringIO()
        self.ge_root = self.cache / "ge/GE-Proton11-3"
        self.bundle_root = self.cache / "bundle/forza-bootstrap-bundle-v1"
        self.ge_root.mkdir(parents=True)
        self.bundle_root.mkdir(parents=True)
        self.cache.chmod(0o700)
        self.identity = ToolIdentity(
            "GE-Proton11-3-FM", "GE-Proton11-3", "c" * 64, "d" * 64, "e" * 64
        )
        self.inspection = PrepareInspection(
            root_ids={
                "user": "1:10",
                "steam": "1:11",
                "cache": "1:12",
                "state": "1:13",
                "runtime": "1:14",
            },
            filesystem_ids={
                "cache-staging": "1",
                "compatibility-destination": "1",
            },
            available_bytes=10_000_000,
            required_bytes=1_000,
            tool_disposition=disposition,
            existing_tool_identity=self.identity
            if disposition is ToolDisposition.ADOPTED
            else None,
            existing_tool_tree_sha256="0" * 64
            if disposition is ToolDisposition.ADOPTED
            else None,
            docker_available=docker_available,
        )

        def preflight(_context: PrepareContext) -> PrepareInspection:
            return self.inspection

        def snapshot(_context: PrepareContext, transaction_id: str) -> Snapshot:
            return Snapshot(
                1,
                transaction_id,
                self.manifest.sha256,
                "1:11",
                (FileRecord("compat/tool-marker", "absent", None, None, None),),
            )

        def acquire_ge(_context: PrepareContext) -> Path:
            return self.ge_root

        def acquire_bundle(_context: PrepareContext) -> VerifiedBundle:
            return VerifiedBundle(self.bundle_root, "f" * 64, self.manifest.artifacts)

        def publish(_context: PrepareContext, _ge: Path) -> PublishedToolRecord:
            return PublishedToolRecord(
                self.compatibility / "GE-Proton11-3-FM",
                ToolDisposition.CREATED,
                self.identity,
                1,
                100,
                "d" * 64,
            )

        self.context = PrepareContext(
            manifest=self.manifest,
            host=self.host,
            repository_root=tmp_path,
            acquisition_mode=mode,
            bundle_path=(tmp_path / "local-bundle.tar.zst")
            if mode == "local"
            else None,
            output=self.stdout,
            operations=PrepareOperations(
                preflight=preflight,
                capture_before=snapshot,
                acquire_ge=acquire_ge,
                acquire_bundle=acquire_bundle,
                publish_fm=publish,
                event=self.actions.append,
                boundary=lambda boundary: None,
            ),
        )

    @property
    def prepare_digest(self) -> str:
        return plan_digest(build_prepare_plan(self.context, self.inspection))

    def run(self, confirmation: str | None = None):
        digest = self.prepare_digest if confirmation is None else confirmation
        return prepare(self.context, lambda _expected: digest)


def test_prepare_from_clean_root_is_plan_bound_and_stops_for_steam(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)

    result = fixture.run()

    assert result.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert fixture.actions == [
        "preflight",
        "print-prepare-plan",
        "confirm-prepare-plan",
        "write-before-snapshot",
        "acquire-ge",
        "acquire-bundle",
        "publish-fm-tool",
        "checkpoint-awaiting-prefix",
    ]
    output = fixture.stdout.getvalue()
    assert f"PLAN_SHA256={fixture.prepare_digest}" in output
    assert "restart Steam" in output
    assert "select GE-Proton11-3-FM" in output
    assert "launch options empty" in output
    assert "Launch Forza yourself" in output
    transaction = fixture.state / result.transaction_id
    assert stat.S_IMODE((transaction / "plan.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((transaction / "before.json").stat().st_mode) == 0o600
    refs = json.loads((transaction / "prepare.json").read_text(encoding="ascii"))
    assert refs["ge_root"] == {"root": "cache", "relative": "ge/GE-Proton11-3"}
    assert refs["bundle_root"] == {
        "root": "cache",
        "relative": "bundle/forza-bootstrap-bundle-v1",
    }


@pytest.mark.parametrize(
    ("os_id", "architecture", "message"),
    (("ubuntu", "x86_64", "Arch Linux"), ("arch", "aarch64", "x86_64")),
)
def test_resolve_unsupported_host_is_read_only(
    tmp_path: Path, os_id: str, architecture: str, message: str
) -> None:
    user = tmp_path / "home"
    user.mkdir()
    release = tmp_path / "os-release"
    release.write_text(f"ID={os_id}\n", encoding="ascii")
    before = tree_manifest(tmp_path)
    probe = HostProbe(os_release=release, machine=lambda: architecture)

    with pytest.raises(BootstrapError, match=message):
        resolve_supported_host(
            {"HOME": str(user), "XDG_RUNTIME_DIR": str(tmp_path / "run")}, probe
        )

    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing-steam", "native Steam"),
        ("missing-app", "AppID 2440510"),
        ("flatpak", "Flatpak Steam"),
        ("wrong-owner", "ownership"),
    ),
)
def test_host_resolution_rejects_unsupported_layout_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, message: str
) -> None:
    user = tmp_path / "home"
    steam = user / ".local/share/Steam"
    app = steam / "steamapps/appmanifest_2440510.acf"
    runtime = tmp_path / "run"
    user.mkdir(parents=True)
    runtime.mkdir(parents=True)
    release = tmp_path / "os-release"
    release.write_text("ID=arch\n", encoding="ascii")
    if mutation not in {"missing-steam", "flatpak"}:
        app.parent.mkdir(parents=True)
        if mutation != "missing-app":
            app.write_text('"appid" "2440510"\n', encoding="ascii")
    if mutation == "flatpak":
        flatpak = user / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
        (flatpak / "steamapps").mkdir(parents=True)
    before = tree_manifest(tmp_path)
    real_lstat = Path.lstat

    if mutation == "wrong-owner":

        def foreign_lstat(path: Path):
            info = real_lstat(path)
            if path == steam:
                values = list(info)
                return os.stat_result((*values[:4], os.getuid() + 1, *values[5:]))
            return info

        monkeypatch.setattr(Path, "lstat", foreign_lstat)

    with pytest.raises(BootstrapError, match=message):
        resolve_supported_host(
            {"HOME": str(user), "XDG_RUNTIME_DIR": str(runtime)},
            HostProbe(os_release=release, machine=lambda: "x86_64"),
        )

    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ("disk", "disk space"),
        ("forza", "Forza Motorsport process"),
        ("service", "xodus-forza.service is active"),
        ("child", "unfinished runtime child"),
        ("launcher", "launcher lock"),
    ),
)
def test_preflight_blockers_leave_state_cache_and_target_untouched(
    tmp_path: Path, change: str, message: str
) -> None:
    fixture = PrepareFixture(tmp_path)
    inspection = fixture.inspection
    if change == "disk":
        inspection = replace(inspection, available_bytes=999, required_bytes=1_000)
    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            preflight=lambda context: (
                (_ for _ in ()).throw(BootstrapError(message))
                if change != "disk"
                else inspection
            ),
        ),
    )
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match=message):
        fixture.run()

    assert tree_manifest(tmp_path) == before


def test_confirmation_requires_the_full_lowercase_digest_before_any_write(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)
    before = tree_manifest(tmp_path)

    for value in (
        fixture.prepare_digest[:-1],
        fixture.prepare_digest.upper(),
        "0" * 64,
        f" {fixture.prepare_digest}",
    ):
        with pytest.raises(BootstrapError, match="confirmation did not match"):
            fixture.run(value)
        assert tree_manifest(tmp_path) == before


def test_prepare_revalidates_read_only_facts_after_confirmation_before_writing(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)
    changed = replace(
        fixture.inspection, root_ids={**fixture.inspection.root_ids, "steam": "1:99"}
    )
    inspections = iter((fixture.inspection, changed))
    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            preflight=lambda _context: next(inspections),
        ),
    )
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="changed during confirmation"):
        fixture.run()

    assert tree_manifest(tmp_path) == before


def test_prepare_holds_the_launcher_lease_across_every_mutation(tmp_path: Path) -> None:
    fixture = PrepareFixture(tmp_path)
    original = fixture.context.operations
    assert original is not None
    original_capture = original.capture_before

    def capture(context: PrepareContext, transaction_id: str) -> Snapshot:
        lock = fixture.runtime / "forza-linux.lock"
        fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        return original_capture(context, transaction_id)

    fixture.context = replace(
        fixture.context,
        operations=replace(original, capture_before=capture),
    )

    fixture.run()

    fd = os.open(fixture.runtime / "forza-linux.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_default_prepare_revalidation_reuses_held_launcher_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    context.host.cache_root.chmod(0o700)
    destination = context.host.compatibility_tools / "GE-Proton11-3-FM"
    identity = ToolIdentity(
        "GE-Proton11-3-FM", "GE-Proton11-3", "c" * 64, "d" * 64, "e" * 64
    )
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda _path, _manifest: ToolInspection(ToolDisposition.ABSENT, None, None),
    )
    monkeypatch.setattr(
        coordinator_module,
        "_acquire_ge",
        lambda _context: context.host.cache_root / "ge/GE-Proton11-3",
    )
    monkeypatch.setattr(
        coordinator_module,
        "_acquire_bundle",
        lambda _context: VerifiedBundle(
            context.host.cache_root / "bundle/forza-bootstrap-bundle-v1",
            "f" * 64,
            context.manifest.artifacts,
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "_publish_fm",
        lambda _context, _ge: PublishedToolRecord(
            destination,
            ToolDisposition.CREATED,
            identity,
            1,
            100,
            "d" * 64,
        ),
    )
    inspection = inspect_prepare(context)
    digest = plan_digest(build_prepare_plan(context, inspection))

    result = prepare(context, lambda _expected: digest)

    assert result.phase is BootstrapPhase.AWAITING_STEAM_PREFIX


@pytest.mark.parametrize(
    ("mode", "bundle_path", "message"),
    (
        ("source", Path("/tmp/local.tar.zst"), "mutually exclusive"),
        ("local", None, "local bundle"),
        ("unknown", None, "acquisition mode"),
    ),
)
def test_bundle_and_source_selection_is_exact_and_read_only_on_error(
    tmp_path: Path, mode: str, bundle_path: Path | None, message: str
) -> None:
    fixture = PrepareFixture(tmp_path)
    fixture.context = replace(
        fixture.context, acquisition_mode=mode, bundle_path=bundle_path
    )
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match=message):
        fixture.run()

    assert tree_manifest(tmp_path) == before


def test_docker_absence_blocks_only_source_mode(tmp_path: Path) -> None:
    source = PrepareFixture(tmp_path / "source", mode="source", docker_available=False)
    with pytest.raises(BootstrapError, match="Docker"):
        source.run()
    assert not source.state.exists()

    download = PrepareFixture(tmp_path / "download", docker_available=False)
    assert download.run().phase is BootstrapPhase.AWAITING_STEAM_PREFIX


def test_exact_existing_tool_is_adopted_without_ge_or_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PrepareFixture(tmp_path, disposition=ToolDisposition.ADOPTED)
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda _path, _manifest: ToolInspection(
            ToolDisposition.ADOPTED,
            fixture.identity,
            fixture.inspection.existing_tool_tree_sha256,
        ),
    )
    forbidden = lambda *_args: (_ for _ in ()).throw(AssertionError("must adopt"))
    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            acquire_ge=forbidden,
            publish_fm=forbidden,
        ),
    )

    state = fixture.run()

    assert state.compatibility_tool_disposition == "adopted"
    assert "acquire-ge" not in fixture.actions
    assert "publish-fm-tool" not in fixture.actions
    plan = build_prepare_plan(fixture.context, fixture.inspection)
    assert plan["destination"]["after"]["tree_sha256"] == "0" * 64


@pytest.mark.parametrize("drift", ("removed", "identity", "tree"))
def test_adopted_tool_is_reopened_after_acquisition_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    fixture = PrepareFixture(tmp_path, disposition=ToolDisposition.ADOPTED)
    changed_identity = replace(fixture.identity, vdf_sha256="9" * 64)
    inspection = {
        "removed": ToolInspection(ToolDisposition.ABSENT, None, None),
        "identity": ToolInspection(
            ToolDisposition.ADOPTED,
            changed_identity,
            fixture.inspection.existing_tool_tree_sha256,
        ),
        "tree": ToolInspection(
            ToolDisposition.ADOPTED,
            fixture.identity,
            "9" * 64,
        ),
    }[drift]
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda _path, _manifest: inspection,
    )

    with pytest.raises(BootstrapError, match="adopted compatibility tool changed"):
        fixture.run()

    state = load_unfinished_transaction(fixture.state)
    assert state is not None
    assert state.phase is BootstrapPhase.PREPARING
    assert "checkpoint-awaiting-prefix" not in fixture.actions


def test_absent_destination_plan_rejects_concurrent_adoption(tmp_path: Path) -> None:
    fixture = PrepareFixture(tmp_path)

    def concurrent_adoption(_context: PrepareContext, _ge: Path) -> PublishedToolRecord:
        return PublishedToolRecord(
            fixture.compatibility / "GE-Proton11-3-FM",
            ToolDisposition.ADOPTED,
            fixture.identity,
            1,
            100,
            "d" * 64,
        )

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            publish_fm=concurrent_adoption,
        ),
    )

    with pytest.raises(BootstrapError, match="changed after confirmation"):
        fixture.run()

    state = load_unfinished_transaction(fixture.state)
    assert state is not None
    assert state.phase is BootstrapPhase.PREPARING


def test_prepare_rejects_unbound_before_snapshot_before_persisting_it(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)

    def wrong_snapshot(_context: PrepareContext, _transaction_id: str) -> Snapshot:
        return Snapshot(
            1,
            "f" * 24,
            fixture.manifest.sha256,
            "1:11",
            (FileRecord("compat/tool-marker", "absent", None, None, None),),
        )

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            capture_before=wrong_snapshot,
        ),
    )

    with pytest.raises(BootstrapError, match="before snapshot identity"):
        fixture.run()

    state = load_unfinished_transaction(fixture.state)
    assert state is not None
    assert not (fixture.state / state.transaction_id / "before.json").exists()


@pytest.mark.parametrize("escaped", ("ge", "bundle"))
def test_prepare_rejects_acquisition_roots_outside_private_cache_before_publication(
    tmp_path: Path, escaped: str
) -> None:
    fixture = PrepareFixture(tmp_path)
    outside = tmp_path / f"outside-{escaped}"
    outside.mkdir()
    operations = fixture.context.operations
    assert operations is not None
    if escaped == "ge":
        operations = replace(operations, acquire_ge=lambda _context: outside)
    else:
        operations = replace(
            operations,
            acquire_bundle=lambda _context: VerifiedBundle(
                outside, "f" * 64, fixture.manifest.artifacts
            ),
        )
    operations = replace(
        operations,
        publish_fm=lambda *_args: (_ for _ in ()).throw(
            AssertionError("invalid acquisition must not reach publication")
        ),
    )
    fixture.context = replace(fixture.context, operations=operations)

    with pytest.raises(BootstrapError, match="escaped the private cache"):
        fixture.run()


@pytest.mark.parametrize("escaped", ("ge", "bundle"))
def test_prepare_rejects_acquisition_root_through_cache_symlink_before_publication(
    tmp_path: Path, escaped: str
) -> None:
    fixture = PrepareFixture(tmp_path)
    outside = tmp_path / f"outside-{escaped}"
    acquired = outside / "acquired"
    acquired.mkdir(parents=True)
    linked = fixture.cache / f"linked-{escaped}"
    linked.symlink_to(outside, target_is_directory=True)
    operations = fixture.context.operations
    assert operations is not None
    if escaped == "ge":
        operations = replace(
            operations, acquire_ge=lambda _context: linked / "acquired"
        )
    else:
        operations = replace(
            operations,
            acquire_bundle=lambda _context: VerifiedBundle(
                linked / "acquired", "f" * 64, fixture.manifest.artifacts
            ),
        )
    operations = replace(
        operations,
        publish_fm=lambda *_args: (_ for _ in ()).throw(
            AssertionError("symlinked acquisition must not reach publication")
        ),
    )
    fixture.context = replace(fixture.context, operations=operations)

    with pytest.raises(BootstrapError, match="escaped the private cache"):
        fixture.run()


def test_prepare_rejects_bundle_with_different_verified_artifact_contract(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)
    wrong = replace(fixture.manifest.artifacts[0], sha256="f" * 64)
    operations = fixture.context.operations
    assert operations is not None
    fixture.context = replace(
        fixture.context,
        operations=replace(
            operations,
            acquire_bundle=lambda _context: VerifiedBundle(
                fixture.bundle_root,
                "f" * 64,
                (wrong, *fixture.manifest.artifacts[1:]),
            ),
            publish_fm=lambda *_args: (_ for _ in ()).throw(
                AssertionError("invalid bundle must not reach publication")
            ),
        ),
    )

    with pytest.raises(BootstrapError, match="artifact contract"):
        fixture.run()


@pytest.mark.parametrize(
    "boundary",
    (
        "preflight",
        "print-prepare-plan",
        "confirm-prepare-plan",
        "write-before-snapshot",
        "acquire-ge",
        "acquire-bundle",
        "publish-fm-tool",
        "checkpoint-awaiting-prefix",
    ),
)
def test_interruption_at_each_prepare_boundary_is_fail_closed_and_durable(
    tmp_path: Path, boundary: str
) -> None:
    fixture = PrepareFixture(tmp_path)

    def interrupt(current: str) -> None:
        if current == boundary:
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.context.operations, boundary=interrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        fixture.run()

    state = load_unfinished_transaction(fixture.state)
    if boundary in {"preflight", "print-prepare-plan", "confirm-prepare-plan"}:
        assert state is None
        assert not fixture.state.exists()
    else:
        assert state is not None
        assert state.phase in {
            BootstrapPhase.PREPARING,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
        }
    forbidden = ("steam", "xdg-open", "proton", "forza_steamworks_release_final.exe")
    assert not any(word in "\0".join(fixture.actions).lower() for word in forbidden)


def test_prepare_plan_binds_only_logical_roots_and_exact_inputs(tmp_path: Path) -> None:
    fixture = PrepareFixture(tmp_path)

    plan = build_prepare_plan(fixture.context, fixture.inspection)
    encoded = json.dumps(plan, sort_keys=True)

    assert plan["manifest_sha256"] == fixture.manifest.sha256
    assert plan["acquisition"]["ge"]["sha256"] == fixture.manifest.ge.sha256
    assert plan["acquisition"]["bundle"]["sha256"] == fixture.manifest.bundle.sha256
    assert plan["destination"]["root"] == "steam"
    assert plan["destination"]["relative"] == "compatibilitytools.d/GE-Proton11-3-FM"
    assert plan["destination"]["before"] == {"disposition": "absent"}
    assert plan["destination"]["after"]["name"] == "GE-Proton11-3-FM"
    assert plan["roots"]["cache"]["identity"] == "1:12"
    assert plan["roots"]["cache"]["path_sha256"] == sha256(
        os.fsencode(str(fixture.cache))
    )
    assert str(fixture.user) not in encoded
    assert str(fixture.steam) not in encoded


@pytest.mark.parametrize(
    ("mode", "relative", "has_local_input"),
    (
        ("download", "downloads/bundle.tar.zst", False),
        ("local", None, True),
        ("source", "source-output/bundle.tar.zst", False),
    ),
)
def test_prepare_plan_names_the_exact_bundle_acquisition_object(
    tmp_path: Path, mode: str, relative: str | None, has_local_input: bool
) -> None:
    fixture = PrepareFixture(tmp_path, mode=mode)
    plan = build_prepare_plan(fixture.context, fixture.inspection)
    bundle = plan["acquisition"]["bundle"]

    assert bundle["cache"]["relative"] == relative
    assert (bundle["local_input"] is not None) is has_local_input
    if has_local_input:
        assert bundle["local_input"]["path_sha256"] == sha256(
            os.fsencode(str(fixture.context.bundle_path))
        )


def host_resolution_layout(
    tmp_path: Path,
) -> tuple[dict[str, str], HostProbe, Path, Path]:
    user = tmp_path / "home"
    steam = user / ".local/share/Steam"
    runtime = tmp_path / "run"
    app = steam / "steamapps/appmanifest_2440510.acf"
    app.parent.mkdir(parents=True)
    app.write_text('"appid" "2440510"\n', encoding="ascii")
    runtime.mkdir()
    release = tmp_path / "os-release"
    release.write_text("ID=arch\n", encoding="ascii")
    return (
        {"HOME": str(user), "XDG_RUNTIME_DIR": str(runtime)},
        HostProbe(os_release=release, machine=lambda: "x86_64"),
        steam / "steamapps/libraryfolders.vdf",
        app,
    )


@pytest.mark.parametrize("kind", ("symlink", "fifo", "oversize"))
def test_library_manifest_special_inputs_are_bounded_and_never_path_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env, probe, library_manifest, _app = host_resolution_layout(tmp_path)
    if kind == "symlink":
        outside = tmp_path / "outside-libraryfolders.vdf"
        outside.write_text('"libraryfolders" {}\n', encoding="ascii")
        library_manifest.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(library_manifest)
    else:
        library_manifest.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    before = tree_manifest(tmp_path)
    original_read_bytes = Path.read_bytes

    def reject_path_read(path: Path) -> bytes:
        if path == library_manifest:
            raise AssertionError("libraryfolders.vdf was read by pathname")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_path_read)
    with pytest.raises(BootstrapError, match="Steam library manifest"):
        resolve_supported_host(env, probe)
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)

    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize("kind", ("symlink", "fifo", "oversize"))
def test_app_manifest_rejects_links_special_files_and_oversize_without_writes(
    tmp_path: Path, kind: str
) -> None:
    env, probe, _library_manifest, app = host_resolution_layout(tmp_path)
    if kind == "symlink":
        outside = tmp_path / "outside-appmanifest.acf"
        outside.write_text('"appid" "2440510"\n', encoding="ascii")
        app.unlink()
        app.symlink_to(outside)
    elif kind == "fifo":
        app.unlink()
        os.mkfifo(app)
    else:
        app.write_bytes(b"x" * (1024 * 1024 + 1))
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="AppID 2440510 manifest"):
        resolve_supported_host(env, probe)

    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize("target_name", ("library", "app"))
def test_steam_metadata_rejects_name_rebinding_during_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    env, probe, library_manifest, app = host_resolution_layout(tmp_path)
    library_manifest.write_text('"libraryfolders" {}\n', encoding="ascii")
    target = library_manifest if target_name == "library" else app
    label = (
        "Steam library manifest"
        if target_name == "library"
        else "AppID 2440510 manifest"
    )
    replacement = target.read_bytes()
    backup = target.with_name(target.name + ".race-backup")
    real_stat = os.stat
    swapped = False

    def swap_during_binding(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal swapped
        if path == target.name and dir_fd is not None and not swapped:
            target.rename(backup)
            target.write_bytes(replacement)
            try:
                result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            finally:
                target.unlink()
                backup.rename(target)
            swapped = True
            return result
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(coordinator_module.os, "stat", swap_during_binding)
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match=label):
        resolve_supported_host(env, probe)

    assert swapped is True
    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize("target_name", ("library", "app"))
def test_steam_metadata_rejects_in_place_generation_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    env, probe, library_manifest, app = host_resolution_layout(tmp_path)
    library_manifest.write_text('"libraryfolders" {}\n', encoding="ascii")
    target = library_manifest if target_name == "library" else app
    label = (
        "Steam library manifest"
        if target_name == "library"
        else "AppID 2440510 manifest"
    )
    original = target.read_bytes()
    real_read = os.read
    changed = False
    before = tree_manifest(tmp_path)

    def change_generation(fd: int, size: int) -> bytes:
        nonlocal changed
        data = real_read(fd, size)
        if data and not changed:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if descriptor_path == target:
                target.write_bytes(b"z" * len(original))
                target.write_bytes(original)
                changed = True
        return data

    monkeypatch.setattr(coordinator_module.os, "read", change_generation)

    with pytest.raises(BootstrapError, match=label):
        resolve_supported_host(env, probe)

    assert changed is True
    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize("target_name", ("library", "app"))
def test_steam_metadata_requires_current_owner_on_open_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    env, probe, library_manifest, app = host_resolution_layout(tmp_path)
    library_manifest.write_text('"libraryfolders" {}\n', encoding="ascii")
    target = library_manifest if target_name == "library" else app
    label = (
        "Steam library manifest"
        if target_name == "library"
        else "AppID 2440510 manifest"
    )
    real_fstat = os.fstat
    before = tree_manifest(tmp_path)

    def foreign_file(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        try:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            return info
        if descriptor_path != target:
            return info
        values = list(info)
        return os.stat_result((*values[:4], os.getuid() + 1, *values[5:]))

    monkeypatch.setattr(coordinator_module.os, "fstat", foreign_file)

    with pytest.raises(BootstrapError, match=label):
        resolve_supported_host(env, probe)

    assert tree_manifest(tmp_path) == before


def test_default_host_probe_uses_only_read_only_commands(tmp_path: Path) -> None:
    user = tmp_path / "home"
    steam = user / ".local/share/Steam"
    runtime = tmp_path / "run"
    app = steam / "steamapps/appmanifest_2440510.acf"
    app.parent.mkdir(parents=True)
    app.write_text('"appid" "2440510"\n', encoding="ascii")
    runtime.mkdir()
    release = tmp_path / "os-release"
    release.write_text("ID=arch\n", encoding="ascii")
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3, "", "")

    host = resolve_supported_host(
        {"HOME": str(user), "XDG_RUNTIME_DIR": str(runtime)},
        HostProbe(os_release=release, machine=lambda: "x86_64", runner=runner),
    )

    assert host.steam_root == steam
    assert calls == []


def test_host_resolution_rejects_mislabeled_app_manifest_without_writes(
    tmp_path: Path,
) -> None:
    user = tmp_path / "home"
    steam = user / ".local/share/Steam"
    runtime = tmp_path / "run"
    app = steam / "steamapps/appmanifest_2440510.acf"
    app.parent.mkdir(parents=True)
    app.write_text('"appid" "999999"\n', encoding="ascii")
    runtime.mkdir()
    release = tmp_path / "os-release"
    release.write_text("ID=arch\n", encoding="ascii")
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="AppID 2440510 manifest is invalid"):
        resolve_supported_host(
            {"HOME": str(user), "XDG_RUNTIME_DIR": str(runtime)},
            HostProbe(os_release=release, machine=lambda: "x86_64"),
        )

    assert tree_manifest(tmp_path) == before


def test_host_resolution_rejects_additional_app_library_without_writes(
    tmp_path: Path,
) -> None:
    user = tmp_path / "home"
    steam = user / ".local/share/Steam"
    runtime = tmp_path / "run"
    external = tmp_path / "external-library"
    (steam / "steamapps").mkdir(parents=True)
    (external / "steamapps").mkdir(parents=True)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"path" "{external}"\n', encoding="utf-8"
    )
    (external / "steamapps/appmanifest_2440510.acf").write_text(
        '"appid" "2440510"\n', encoding="ascii"
    )
    runtime.mkdir()
    release = tmp_path / "os-release"
    release.write_text("ID=arch\n", encoding="ascii")
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="additional Steam libraries"):
        resolve_supported_host(
            {"HOME": str(user), "XDG_RUNTIME_DIR": str(runtime)},
            HostProbe(os_release=release, machine=lambda: "x86_64"),
        )

    assert tree_manifest(tmp_path) == before


def production_preflight_fixture(
    tmp_path: Path,
    *,
    service_status: int = 3,
    free_bytes: int = 10_000_000_000,
) -> tuple[PrepareContext, list[tuple[str, ...]]]:
    fixture = PrepareFixture(tmp_path)
    proc = tmp_path / "proc"
    proc.mkdir()
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, service_status, "", "")

    usage = shutil._ntuple_diskusage(20_000_000_000, 1, free_bytes)
    probe = HostProbe(
        os_release=tmp_path / "unused-os-release",
        machine=lambda: "x86_64",
        runner=runner,
        which=lambda command: f"/usr/bin/{command}",
        disk_usage=lambda _path: usage,
        proc_root=proc,
    )
    return replace(fixture.context, operations=None, probe=probe), calls


@pytest.mark.parametrize(
    ("blocker", "message"),
    (
        ("disk", "disk space"),
        ("forza", "Forza Motorsport process"),
        ("service", "xodus-forza.service is active"),
        ("child", "unfinished runtime child"),
        ("launcher", "launcher lock"),
    ),
)
def test_production_preflight_detects_each_live_blocker_without_writes(
    tmp_path: Path, blocker: str, message: str
) -> None:
    context, _calls = production_preflight_fixture(
        tmp_path,
        service_status=0 if blocker == "service" else 3,
        free_bytes=1 if blocker == "disk" else 10_000_000_000,
    )
    held_lock: int | None = None
    if blocker == "forza":
        command = context.probe.proc_root / "123/cmdline"
        command.parent.mkdir()
        command.write_bytes(b"forza_steamworks_release_final.exe\x00")
    elif blocker == "child":
        journal = (
            context.host.user_root
            / ".local/state/forza-motorsport-linux/runtime-transactions"
            / ("1" * 24)
            / "journal.json"
        )
        journal.parent.mkdir(parents=True)
        journal.write_text('{"state":"installing"}', encoding="ascii")
    elif blocker == "launcher":
        lock = context.host.runtime_dir / "forza-linux.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        held_lock = os.open(lock, os.O_RDWR)
        fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = tree_manifest(tmp_path)
    try:
        with pytest.raises(BootstrapError, match=message):
            inspect_prepare(context)
    finally:
        if held_lock is not None:
            os.close(held_lock)

    assert tree_manifest(tmp_path) == before


def test_preflight_accepts_missing_xodus_unit_as_inactive(tmp_path: Path) -> None:
    context, calls = production_preflight_fixture(tmp_path, service_status=4)

    inspection = inspect_prepare(context)

    assert inspection.tool_disposition is ToolDisposition.ABSENT
    assert calls == [
        ("systemctl", "--user", "is-active", "--quiet", "xodus-forza.service")
    ]


@pytest.mark.parametrize("status", (1, 2, 5))
def test_preflight_fails_closed_on_systemctl_errors(
    tmp_path: Path, status: int
) -> None:
    context, _calls = production_preflight_fixture(tmp_path, service_status=status)
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="cannot prove.*inactive"):
        inspect_prepare(context)

    assert tree_manifest(tmp_path) == before


@pytest.mark.parametrize("kind", ("state", "cache"))
def test_preflight_rejects_nonprivate_managed_root_before_any_write(
    tmp_path: Path, kind: str
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    root = context.host.state_root if kind == "state" else context.host.cache_root
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match=rf"{kind} root has unsafe mode"):
        inspect_prepare(context)

    assert tree_manifest(tmp_path) == before
    assert not (context.host.runtime_dir / "forza-linux.lock").exists()


@pytest.mark.parametrize("kind", ("state", "cache"))
def test_preflight_rejects_managed_root_symlink_ancestor_before_any_write(
    tmp_path: Path, kind: str
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    outside = tmp_path / f"outside-{kind}"
    (outside / "bootstrap").mkdir(parents=True, mode=0o700)
    link = tmp_path / f"linked-{kind}"
    link.symlink_to(outside, target_is_directory=True)
    host = replace(
        context.host,
        **{f"{kind}_root": link / "bootstrap"},
    )
    context = replace(context, host=host)
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match=rf"{kind} root has unsafe path"):
        inspect_prepare(context)

    assert tree_manifest(tmp_path) == before
    assert not (context.host.runtime_dir / "forza-linux.lock").exists()


def test_preflight_requires_cache_stage_and_destination_on_same_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    staging = context.host.cache_root / "staging"
    staging.mkdir(mode=0o700)
    original = coordinator_module._inspect_directory_path

    def different_devices(path: Path, label: str, *, private_if_exists: bool) -> object:
        result = original(path, label, private_if_exists=private_if_exists)
        device = (
            1001
            if path == staging
            else 2002
            if path == context.host.compatibility_tools
            else result.info.st_dev
        )
        info = SimpleNamespace(
            st_dev=device,
            st_ino=result.info.st_ino,
            st_mode=result.info.st_mode,
            st_uid=result.info.st_uid,
        )
        return coordinator_module._DirectoryProbe(result.path, info, result.exists)

    monkeypatch.setattr(
        coordinator_module, "_inspect_directory_path", different_devices
    )
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="same filesystem"):
        inspect_prepare(context)

    assert tree_manifest(tmp_path) == before
    assert not context.host.state_root.exists()
    assert not (context.host.runtime_dir / "forza-linux.lock").exists()


@pytest.mark.parametrize("low", ("staging", "destination"))
def test_preflight_checks_capacity_on_actual_staging_and_destination_filesystems(
    tmp_path: Path, low: str
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    staging = context.host.cache_root / "staging"
    staging.mkdir(mode=0o700)
    checked: list[Path] = []

    def disk_usage(path: Path) -> shutil._ntuple_diskusage:
        checked.append(path)
        constrained = staging if low == "staging" else context.host.compatibility_tools
        free = 1 if path == constrained else 10_000_000_000
        return shutil._ntuple_diskusage(20_000_000_000, 1, free)

    context = replace(context, probe=replace(context.probe, disk_usage=disk_usage))
    before = tree_manifest(tmp_path)

    with pytest.raises(BootstrapError, match="disk space"):
        inspect_prepare(context)

    assert staging in checked
    assert context.host.compatibility_tools in checked
    assert tree_manifest(tmp_path) == before
    assert not context.host.state_root.exists()
    assert not (context.host.runtime_dir / "forza-linux.lock").exists()


def test_preflight_rejects_symlinked_runtime_journal_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    transaction = (
        context.host.user_root
        / ".local/state/forza-motorsport-linux/runtime-transactions"
        / ("1" * 24)
    )
    transaction.mkdir(parents=True)
    outside = tmp_path / "outside-journal.json"
    outside.write_text('{"state":"accepted"}', encoding="ascii")
    journal = transaction / "journal.json"
    journal.symlink_to(outside)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == journal:
            raise AssertionError("unsafe journal pathname was followed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    with pytest.raises(
        BootstrapError, match="unfinished runtime child transaction is unsafe"
    ):
        inspect_prepare(context)


def test_production_preflight_requires_docker_only_for_source_mode(
    tmp_path: Path,
) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    without_docker = replace(
        context.probe,
        which=lambda command: None if command == "docker" else f"/usr/bin/{command}",
    )

    inspection = inspect_prepare(replace(context, probe=without_docker))
    assert inspection.docker_available is False

    with pytest.raises(BootstrapError, match="Docker"):
        inspect_prepare(
            replace(context, probe=without_docker, acquisition_mode="source")
        )


def test_source_bundle_build_uses_the_injected_host_runner(tmp_path: Path) -> None:
    context, _calls = production_preflight_fixture(tmp_path)
    context.host.cache_root.chmod(0o700)
    (context.host.cache_root / "bundle").chmod(0o700)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    context = replace(
        context,
        acquisition_mode="source",
        probe=replace(context.probe, runner=runner),
    )

    with pytest.raises(BootstrapError):
        default_prepare_operations().acquire_bundle(context)

    assert calls == [
        (
            (
                str(tmp_path / "tools/build-bootstrap-bundle"),
                "--clean",
                "--output",
                str(context.host.cache_root / "source-output"),
                "--builder-image",
                context.manifest.builder.image,
                "--expected-sha256",
                context.manifest.bundle.sha256,
            ),
            {"cwd": tmp_path, "check": True, "shell": False},
        )
    ]


def test_default_before_snapshot_covers_the_finite_managed_scope(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)
    marker = fixture.compatibility / "GE-Proton11-3-FM/.forza-bootstrap.json"
    marker.parent.mkdir()
    marker.write_bytes(b"fixture marker")

    snapshot = default_prepare_operations().capture_before(fixture.context, "1" * 24)

    records = {record.logical_path: record for record in snapshot.records}
    paths = set(records)
    assert "compat/tool-marker" in paths
    assert "compat/controller" in paths
    assert "prefix/controller" in paths
    assert "prefix/threading" in paths
    assert "user/.local/bin/forza-linux" in paths
    assert "user/.config/systemd/user/xodus-forza.service" in paths
    assert len(paths) == 17
    assert records["compat/tool-marker"].state == "regular"
    assert records["compat/tool-marker"].sha256 == sha256(b"fixture marker")
