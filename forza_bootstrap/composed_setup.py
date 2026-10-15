# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Explicit preview setup for a precomposed tool; legacy transactions stay separate."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import artifacts, composition, licensed, proton
from .coordinator import (
    HostProbe,
    ResolvedHost,
    _acquire_launcher_lease,
    _licensed_before_record,
    _owned_directory,
    _probe_forza_processes,
    _probe_xodus_service,
    _read_local_bundle,
    _read_os_id,
    _read_owned_regular,
    _validate_held_launcher_lock,
)
from .manifest import load_composed_bundle_manifest
from .model import BootstrapError, DownloadSpec, canonical_json, sha256_bytes
from .platform_check import SteamLayout, discover_steam, duplicate_dll_groups
from .safeio import (
    acquire_bootstrap_lock,
    atomic_write_private_json,
    ensure_private_directory,
    open_owned_root,
    read_private_json,
    write_all,
)
from .state import BootstrapPhase, load_latest_transaction_read_only

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"[a-f0-9]{24}\Z")
_PHASES = {
    "PREPARING",
    "AWAITING_STEAM_PREFIX",
    "INSTALLING_FINISH",
    "READY_TO_ATTEMPT",
    "ROLLING_BACK",
    "ROLLED_BACK",
}
_TRANSITIONS = {
    "PREPARING": {"AWAITING_STEAM_PREFIX", "ROLLING_BACK"},
    "AWAITING_STEAM_PREFIX": {"INSTALLING_FINISH", "ROLLING_BACK"},
    "INSTALLING_FINISH": {"READY_TO_ATTEMPT", "ROLLING_BACK"},
    "READY_TO_ATTEMPT": {"ROLLING_BACK"},
    "ROLLING_BACK": {"ROLLING_BACK", "ROLLED_BACK"},
    "ROLLED_BACK": set(),
}
_STATE_KEYS = {
    "schema",
    "transaction_id",
    "manifest_sha256",
    "prepare_plan_sha256",
    "finish_plan_sha256",
    "phase",
}
_TARGETS = {
    "files/lib/wine/x86_64-windows/windows.gaming.input.dll": "drive_c/windows/system32/windows.gaming.input.dll",
    "files/lib/wine/x86_64-windows/mountmgr.sys": "drive_c/windows/system32/drivers/mountmgr.sys",
    "files/lib/wine/x86_64-windows/xgameruntime.dll": "drive_c/windows/system32/xgameruntime.dll",
}


@dataclass(frozen=True)
class Context:
    repository: Path
    host: ResolvedHost
    layout: SteamLayout
    release: dict
    manifest_sha256: str
    probe: HostProbe


def _hash(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def load_release(repository: Path) -> tuple[dict, str]:
    data = _read_owned_regular(
        repository / "manifests/composed-v2.json", "composed release", 65536
    )
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise BootstrapError("composed release is invalid") from None
    expected = {
        "schema",
        "app_id",
        "profile",
        "status",
        "tool_name",
        "base_release",
        "payload_sha256",
        "marker_sha256",
        "managed_targets",
        "ge_revision",
        "wine_revision",
        "source_date_epoch",
        "sdk_image",
        "patches",
        "flatpak",
        "archive",
        "user_bundle_manifest_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value["schema"]) is not int
        or value["schema"] != 2
        or value["app_id"] != "2440510"
        or value["profile"] != "fm-composed-v1"
        or value["status"] != "candidate"
        or value["tool_name"] != "GE-Proton11-3-FM"
        or value["base_release"] != "GE-Proton11-3"
        or not all(
            _hash(value[key])
            for key in ("payload_sha256", "marker_sha256", "user_bundle_manifest_sha256")
        )
        or not isinstance(value["managed_targets"], dict)
        or set(value["managed_targets"])
        != set(_TARGETS) | {"files/lib/wine/x86_64-unix/xgameruntime.so"}
        or not all(_hash(item) for item in value["managed_targets"].values())
        or any(
            not isinstance(value[key], str)
            or re.fullmatch(r"[a-f0-9]{40}", value[key]) is None
            for key in ("ge_revision", "wine_revision")
        )
        or type(value["source_date_epoch"]) is not int
        or value["source_date_epoch"] <= 0
        or not isinstance(value["sdk_image"], str)
        or re.fullmatch(
            r"registry\.gitlab\.steamos\.cloud/proton/steamrt4/sdk/x86_64@sha256:[a-f0-9]{64}",
            value["sdk_image"],
        )
        is None
        or not isinstance(value["patches"], dict)
        or set(value["patches"])
        != {"proton-no-bytecode.patch", "wine-forza-platform.patch"}
        or not all(_hash(item) for item in value["patches"].values())
    ):
        raise BootstrapError("composed release contract is invalid")
    archive = value["archive"]
    if (
        not isinstance(archive, dict)
        or set(archive) != {"filename", "size", "sha256", "expected_root"}
        or archive["filename"] != "GE-Proton11-3-FM-composed-preview.tar.gz"
        or archive["expected_root"] != value["tool_name"]
        or type(archive["size"]) is not int
        or not 0 < archive["size"] < 2**31
        or not _hash(archive["sha256"])
    ):
        raise BootstrapError("composed archive contract is invalid")
    flatpak = value["flatpak"]
    if (
        not isinstance(flatpak, dict)
        or set(flatpak)
        != {
            "app_id",
            "branch",
            "commit",
            "runtime",
            "sha256",
            "size",
            "tested_runtime_commit",
        }
        or flatpak["app_id"] != "io.github.volcmen.ForzaXodus"
        or flatpak["branch"] != "preview"
        or flatpak["runtime"] != "org.gnome.Platform/x86_64/50"
        or not all(
            _hash(flatpak[key]) for key in ("commit", "sha256", "tested_runtime_commit")
        )
        or type(flatpak["size"]) is not int
        or flatpak["size"] <= 0
    ):
        raise BootstrapError("composed GUI contract is invalid")
    return value, sha256_bytes(data)


def resolve_context(
    repository: Path, env: Mapping[str, str], probe: HostProbe | None = None
) -> Context:
    probe = probe or HostProbe()
    if (
        _read_os_id(probe.os_release) not in {"arch", "steamos"}
        or probe.machine() != "x86_64"
    ):
        raise BootstrapError("composed preview requires Arch or SteamOS on x86_64")
    if os.getuid() == 0:
        raise BootstrapError("run composed setup as your desktop user")
    home = Path(env.get("HOME", ""))
    runtime = Path(env.get("XDG_RUNTIME_DIR", ""))
    for path in (home, runtime):
        _owned_directory(path, "composed host root")
    layout = discover_steam(env)
    for path in (layout.steam_root, layout.library_root, layout.game_root):
        _owned_directory(path, "composed Steam root")
    release, digest = load_release(repository)
    host = ResolvedHost(
        _read_os_id(probe.os_release),
        platform.machine(),
        home,
        layout.steam_root,
        layout.library_root / "steamapps/appmanifest_2440510.acf",
        layout.steam_root / "compatibilitytools.d",
        home / ".local/state/forza-motorsport-linux/bootstrap-v2",
        home / ".cache/forza-motorsport-linux/bootstrap-v2",
        runtime,
    )
    return Context(repository, host, layout, release, digest, probe)


def _read(path: Path) -> dict:
    fd = open_owned_root(path.parent)
    try:
        result = read_private_json(fd, path.name)
    finally:
        os.close(fd)
    if not isinstance(result, dict):
        raise BootstrapError("composed transaction record is invalid")
    return result


def _write(path: Path, value: dict) -> None:
    fd = open_owned_root(path.parent)
    try:
        atomic_write_private_json(fd, path.name, value)
    finally:
        os.close(fd)


def _transaction(context: Context, state: dict) -> Path:
    return context.host.state_root / state["transaction_id"]


def load_active(context: Context) -> dict | None:
    try:
        active = _read(context.host.state_root / "active.json")
    except FileNotFoundError:
        return None
    if (
        set(active) != {"schema", "transaction_id"}
        or type(active["schema"]) is not int
        or active["schema"] != 2
        or not isinstance(active["transaction_id"], str)
        or _TOKEN.fullmatch(active["transaction_id"]) is None
    ):
        raise BootstrapError("composed active transaction is invalid")
    state = _read(context.host.state_root / active["transaction_id"] / "state.json")
    if (
        set(state) != _STATE_KEYS
        or type(state["schema"]) is not int
        or state["schema"] != 2
        or state["transaction_id"] != active["transaction_id"]
        or state["manifest_sha256"] != context.manifest_sha256
        or state["phase"] not in _PHASES
        or not _hash(state["prepare_plan_sha256"])
        or (
            state["finish_plan_sha256"] is not None
            and not _hash(state["finish_plan_sha256"])
        )
        or (
            state["phase"] in {"INSTALLING_FINISH", "READY_TO_ATTEMPT"}
            and state["finish_plan_sha256"] is None
        )
        or (
            state["phase"] in {"PREPARING", "AWAITING_STEAM_PREFIX"}
            and state["finish_plan_sha256"] is not None
        )
    ):
        raise BootstrapError("composed state differs from its recorded release")
    plan = _read(_transaction(context, state) / "prepare-plan.json")
    if sha256_bytes(canonical_json(plan)) != state["prepare_plan_sha256"]:
        raise BootstrapError("composed Prepare plan changed")
    return state


def _phase(context: Context, state: dict, phase: str, **updates) -> dict:
    if phase not in _TRANSITIONS[state["phase"]] or set(updates) - {
        "finish_plan_sha256"
    }:
        raise BootstrapError("composed transition is invalid")
    updated = {**state, **updates, "phase": phase}
    _write(_transaction(context, state) / "state.json", updated)
    return updated


def _binding(path: Path) -> dict:
    fd = proton._open_directory(path, "composed root")
    try:
        info = os.fstat(fd)
        return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    finally:
        os.close(fd)


def _bindings(context: Context) -> dict:
    return {
        name: _binding(path)
        for name, path in {
            "home": context.host.user_root,
            "steam": context.layout.steam_root,
            "library": context.layout.library_root,
            "game": context.layout.game_root,
        }.items()
    }


def _quiet(context: Context, lease: int) -> None:
    _validate_held_launcher_lock(context.host, lease)
    _probe_forza_processes(context.probe)
    _probe_xodus_service(context.probe)


@contextlib.contextmanager
def _lease(context: Context):
    lease = _acquire_launcher_lease(context.host)
    try:
        _quiet(context, lease)
        yield lease
    finally:
        os.close(lease)


def _helper(context: Context, state: dict | None = None) -> tuple[dict, bytes]:
    path = (
        context.repository / "scripts/install-runtime-components"
        if state is None
        else _transaction(context, state) / "runtime-helper"
    )
    data = _read_owned_regular(path, "runtime helper", 1024 * 1024)
    if state is not None:
        plan = _read(_transaction(context, state) / "prepare-plan.json")
        if sha256_bytes(data) != plan["helper_sha256"]:
            raise BootstrapError("recorded runtime helper changed")
    # Execute only the bytes just verified. Sealing closes a writable-file race
    # between checksum validation and Python reading the recovery helper.
    fd = _sealed_helper(data)
    try:
        loader = importlib.machinery.SourceFileLoader(
            "forza_composed_runtime", f"/proc/self/fd/{fd}"
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        namespace = vars(module)
    finally:
        os.close(fd)
    namespace["integration_root"] = lambda: context.repository
    return namespace, data


def _sealed_helper(data: bytes) -> int:
    # Some standalone Python builds omit os.memfd_create even on modern Linux.
    # Use libc's Linux API with MFD_CLOEXEC | MFD_ALLOW_SEALING in that case.
    create = getattr(os, "memfd_create", None)
    if create is not None:
        fd = create("forza-runtime-helper", 0x0001 | 0x0002)
    else:
        library = ctypes.CDLL(None, use_errno=True)
        create = getattr(library, "memfd_create", None)
        if create is None:
            raise BootstrapError("sealed recovery helpers require Linux memfd support")
        create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        create.restype = ctypes.c_int
        fd = create(b"forza-runtime-helper", 0x0001 | 0x0002)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "cannot create sealed recovery helper")
    try:
        write_all(fd, data)
        # Stable Linux UAPI values from linux/fcntl.h, also omitted by some
        # standalone builds: F_ADD_SEALS=1024+9; SEAL|SHRINK|GROW|WRITE=0x0f.
        fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), 0x0F)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _legacy_guard(context: Context, helper: dict) -> None:
    legacy = load_latest_transaction_read_only(
        context.host.user_root / ".local/state/forza-motorsport-linux/bootstrap"
    )
    if legacy is not None and legacy.phase is not BootstrapPhase.ROLLED_BACK:
        raise BootstrapError(
            "legacy bootstrap exists; use its matching checkout for recovery"
        )
    for relative in helper["USER_PAYLOADS"]:
        if os.path.lexists(context.host.user_root / relative):
            raise BootstrapError(
                "existing user integration must be preserved; automatic conversion is unavailable"
            )
    if os.path.lexists(context.host.compatibility_tools / context.release["tool_name"]):
        raise BootstrapError("existing compatibility tool must be preserved")
    if any(
        journal["state"] != "rolled_back"
        for _, journal in helper["transaction_journals"](context.host.user_root)
    ):
        raise BootstrapError("another runtime transaction requires its own recovery")


def prepare_plan(context: Context, archive: Path, bundle: Path) -> dict:
    helper, helper_data = _helper(context)
    _legacy_guard(context, helper)
    spec = context.release["archive"]
    _read_local_bundle(archive, spec["size"], spec["sha256"])
    if (
        shutil.disk_usage(context.host.user_root).free < 12 * 1024**3
        or shutil.disk_usage(context.host.steam_root).free < 8 * 1024**3
    ):
        raise BootstrapError(
            "Prepare requires 12 GiB free for private cache/recovery and 8 GiB on the Steam tool filesystem"
        )
    user_bundle_manifest = load_composed_bundle_manifest(
        context.repository / "manifests/composed-bundle-v1.toml"
    )
    if user_bundle_manifest.sha256 != context.release["user_bundle_manifest_sha256"]:
        raise BootstrapError("composed user bundle manifest differs from the release")
    artifacts.verify_bundle_root(bundle, user_bundle_manifest)
    if duplicate_dll_groups(context.layout.game_root):
        raise BootstrapError(
            "case-insensitive duplicate game DLLs require manual review"
        )
    return {
        "schema": 2,
        "manifest_sha256": context.manifest_sha256,
        "bindings": _bindings(context),
        "archive": str(archive),
        "bundle": str(bundle),
        "helper_sha256": sha256_bytes(helper_data),
        "user_bundle_manifest_sha256": user_bundle_manifest.sha256,
        "tool_destination": str(
            context.host.compatibility_tools / context.release["tool_name"]
        ),
    }


def _publication_args(context: Context, state: dict) -> tuple:
    token = state["transaction_id"]
    return (
        _transaction(context, state),
        token,
        context.manifest_sha256,
        context.host.compatibility_tools / f".forza-staging-{token}" / "tool",
        context.host.compatibility_tools / context.release["tool_name"],
        context.release["payload_sha256"],
        context.release["marker_sha256"],
    )


def _validate_prepare_binding(context: Context, state: dict) -> dict:
    plan = _read(_transaction(context, state) / "prepare-plan.json")
    if (
        sha256_bytes(canonical_json(plan)) != state["prepare_plan_sha256"]
        or plan.get("manifest_sha256") != context.manifest_sha256
        or plan.get("bindings") != _bindings(context)
        or plan.get("tool_destination") != str(_publication_args(context, state)[4])
    ):
        raise BootstrapError("composed host or Prepare plan changed")
    return plan


def _prepare_locked(context: Context, state: dict, lease: int) -> dict:
    plan = _validate_prepare_binding(context, state)
    transaction = _transaction(context, state)
    if not (transaction / "runtime-helper").exists():
        _, data = _helper(context)
        if sha256_bytes(data) != plan["helper_sha256"]:
            raise BootstrapError("runtime helper changed after Prepare confirmation")
        fd = os.open(
            transaction / "runtime-helper",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o700,
        )
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = open_owned_root(transaction)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    _helper(context, state)
    cache = context.host.cache_root / state["transaction_id"]
    ensure_private_directory(cache)
    bundle = cache / "user-bundle"
    manifest = load_composed_bundle_manifest(
        context.repository / "manifests/composed-bundle-v1.toml"
    )
    if manifest.sha256 != plan["user_bundle_manifest_sha256"]:
        raise BootstrapError("recorded user bundle manifest changed")
    if not bundle.exists():
        artifacts.verify_bundle_root(Path(plan["bundle"]), manifest)
        shutil.copytree(Path(plan["bundle"]), bundle, symlinks=True)
    artifacts.verify_bundle_root(bundle, manifest)
    _quiet(context, lease)
    args = _publication_args(context, state)
    stage = args[3]
    ensure_private_directory(
        context.host.compatibility_tools
    ) if not context.host.compatibility_tools.exists() else _owned_directory(
        context.host.compatibility_tools, "compatibility tool directory"
    )
    ensure_private_directory(stage.parent)
    if not (transaction / "tool-publication.json").exists():
        if not stage.exists():
            record = context.release["archive"]
            spec = DownloadSpec(
                context.release["tool_name"],
                "preview",
                "",
                record["filename"],
                record["size"],
                record["sha256"],
                record["expected_root"],
                (),
            )
            extracted = proton._extract_pinned_ge(
                Path(plan["archive"]), cache / "tool-extracted", spec
            )
            proton.prepare_composed_fm_tree(extracted, stage, args[5], args[6])
        else:
            proton.inspect_composed_fm(stage, args[5], args[6])
    _quiet(context, lease)
    composition.publish_composed(*args)
    return _phase(context, state, "AWAITING_STEAM_PREFIX")


def prepare(
    context: Context,
    archive: Path | None,
    bundle: Path | None,
    confirm: Callable[[str], str],
) -> dict:
    with _lease(context) as lease:
        ensure_private_directory(context.host.state_root)
        lock = acquire_bootstrap_lock(context.host.state_root)
        try:
            state = load_active(context)
            if state is not None and state["phase"] == "PREPARING":
                return _prepare_locked(context, state, lease)
            if state is not None and state["phase"] != "ROLLED_BACK":
                raise BootstrapError(
                    "composed transaction already exists; inspect status or finish it"
                )
            if archive is None or bundle is None:
                raise BootstrapError(
                    "Prepare requires the reviewed tool archive and extracted user bundle"
                )
            plan = prepare_plan(context, archive, bundle)
            digest = sha256_bytes(canonical_json(plan))
            if confirm(digest) != digest:
                raise BootstrapError(
                    "Prepare confirmation did not match; no installation was attempted"
                )
            if prepare_plan(context, archive, bundle) != plan:
                raise BootstrapError("Prepare inputs changed after confirmation")
            token = secrets.token_hex(12)
            transaction = context.host.state_root / token
            ensure_private_directory(transaction)
            state = {
                "schema": 2,
                "transaction_id": token,
                "manifest_sha256": context.manifest_sha256,
                "prepare_plan_sha256": digest,
                "finish_plan_sha256": None,
                "phase": "PREPARING",
            }
            _write(transaction / "prepare-plan.json", plan)
            _write(transaction / "state.json", state)
            _write(
                context.host.state_root / "active.json",
                {"schema": 2, "transaction_id": token},
            )
            return _prepare_locked(context, state, lease)
        finally:
            os.close(lock)


def _prefix(context: Context) -> Path:
    layout = discover_steam(
        {
            "HOME": str(context.host.user_root),
            "XDG_DATA_HOME": str(context.host.steam_root.parent),
        }
    )
    if (layout.steam_root, layout.library_root, layout.game_root) != (
        context.layout.steam_root,
        context.layout.library_root,
        context.layout.game_root,
    ):
        raise BootstrapError(
            "Steam moved the game or library; review the layout before continuing"
        )
    if len(layout.prefix_candidates) != 1:
        raise BootstrapError(
            "start the game once with the composed tool, then stop it; exactly one prefix is required"
        )
    prefix = layout.prefix_candidates[0]
    _owned_directory(prefix, "Steam-created prefix")
    for relative in ("user.reg", "system.reg", "userdef.reg"):
        if not _read_owned_regular(
            prefix / relative, "Steam prefix registry", 128 * 1024 * 1024
        ):
            raise BootstrapError("Steam prefix registry is empty")
    for source, relative in _TARGETS.items():
        data = _read_owned_regular(
            prefix / relative, "composed prefix module", 64 * 1024 * 1024
        )
        if sha256_bytes(data) != context.release["managed_targets"][source]:
            raise BootstrapError(
                "prefix does not contain the reviewed composed modules"
            )
    if duplicate_dll_groups(layout.game_root):
        raise BootstrapError(
            "case-insensitive duplicate game DLLs require manual review"
        )
    return prefix


def _runtime_arguments(context: Context, state: dict) -> argparse.Namespace:
    bundle = context.host.cache_root / state["transaction_id"] / "user-bundle"
    return argparse.Namespace(
        operation="plan",
        user_root=context.host.user_root,
        compat_tool_root=None,
        artifact_bundle_root=bundle,
        bundle_manifest=bundle / "bundle-manifest.json",
        artifact_evidence_manifest=context.host.user_root
        / ".local/state/forza-motorsport-linux/runtime-transactions/artifact-evidence.json",
        xgameruntime_build_dir=None,
        xodus_build_dir=None,
        input_kind="bundle",
        user_only=True,
        runtime_profile="composed-preview-v1",
        parent_transaction=state["transaction_id"],
        parent_manifest_sha256=context.manifest_sha256,
        adopt_unmanaged=False,
    )


def _user_plan(context: Context, state: dict) -> tuple[dict, dict]:
    helper, _ = _helper(context, state)
    args = _runtime_arguments(context, state)
    profile, revisions, bundle_hash, records = helper["input_context"](args)
    evidence = {
        "version": helper["EVIDENCE_VERSION"],
        "input_kind": "bundle",
        "runtime_profile": profile,
        "source_revisions": revisions,
        "bundle_manifest_sha256": bundle_hash,
        "artifacts": [
            {key: value for key, value in records[role].items() if key != "data"}
            for role in helper["ROLES"]
        ],
    }
    evidence_hash = sha256_bytes(helper["canonical"](evidence) + b"\n")
    plan, digest, _, _ = helper["build_plan_from_inputs"](
        args, evidence, evidence_hash, records
    )
    return {"document": plan, "sha256": digest}, evidence


def _gui_check(context: Context) -> None:
    if context.host.os_id != "steamos":
        return
    expected = context.release["flatpak"]
    result = context.probe.runner(
        [
            "flatpak",
            "info",
            "--user",
            "--show-commit",
            f"{expected['app_id']}//{expected['branch']}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0 or result.stdout.strip() != expected["commit"]:
        raise BootstrapError(
            "install the reviewed Forza Xodus preview Flatpak for this user before Finish"
        )
    runtime = context.probe.runner(
        ["flatpak", "info", "--user", "--show-commit", expected["runtime"]],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if (
        runtime.returncode != 0
        or runtime.stdout.strip() != expected["tested_runtime_commit"]
    ):
        raise BootstrapError(
            "GNOME runtime differs from the tested deployment; qualify the update before changing the preview manifest"
        )
    # Global and application overrides can silently broaden the reviewed bundle.
    # Never remove overrides or downgrade a shared runtime as an installer fix.
    for scope in ("--user", "--system"):
        for app in ([], [expected["app_id"]]):
            overrides = context.probe.runner(
                ["flatpak", "override", scope, "--show", *app],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            if overrides.returncode != 0 or overrides.stdout.strip():
                raise BootstrapError(
                    "Flatpak overrides differ from the reviewed permission policy; review them explicitly before Finish"
                )


def finish_plan(
    context: Context, state: dict, threading_dll: Path
) -> tuple[dict, dict]:
    _validate_prepare_binding(context, state)
    proton.inspect_composed_fm(
        _publication_args(context, state)[4],
        context.release["payload_sha256"],
        context.release["marker_sha256"],
    )
    prefix = _prefix(context)
    _gui_check(context)
    action = licensed.plan_threading_copy(
        threading_dll, prefix / "drive_c/windows/system32/xgameruntime.dll.threading"
    )
    runtime, evidence = _user_plan(context, state)
    return {
        "schema": 2,
        "transaction_id": state["transaction_id"],
        "manifest_sha256": context.manifest_sha256,
        "prefix": _binding(prefix),
        "system32": _binding(prefix / "drive_c/windows/system32"),
        "licensed": action.private_json(),
        "runtime": runtime,
    }, evidence


def _finish_record(context: Context, state: dict) -> dict:
    plan = _read(_transaction(context, state) / "finish-plan.json")
    if (
        set(plan)
        != {
            "schema",
            "transaction_id",
            "manifest_sha256",
            "prefix",
            "system32",
            "licensed",
            "runtime",
        }
        or type(plan["schema"]) is not int
        or plan["schema"] != 2
        or plan["transaction_id"] != state["transaction_id"]
        or plan["manifest_sha256"] != context.manifest_sha256
        or sha256_bytes(canonical_json(plan)) != state["finish_plan_sha256"]
    ):
        raise BootstrapError("composed Finish plan changed")
    prefix = _prefix(context)
    if plan["prefix"] != _binding(prefix) or plan["system32"] != _binding(
        prefix / "drive_c/windows/system32"
    ):
        raise BootstrapError("composed prefix ownership changed")
    runtime = plan["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"document", "sha256"}
        or not _hash(runtime["sha256"])
        or sha256_bytes(canonical_json(runtime["document"])) != runtime["sha256"]
        or runtime["document"].get("version") != 3
        or runtime["document"].get("scope") != "user"
        or runtime["document"].get("parent")
        != {
            "transaction_id": state["transaction_id"],
            "manifest_sha256": context.manifest_sha256,
        }
        or runtime["document"].get("roots") != {"user": str(context.host.user_root)}
    ):
        raise BootstrapError("composed user plan authority is invalid")
    return plan


def _child(context: Context, state: dict, plan: dict) -> dict | None:
    helper, _ = _helper(context, state)
    children = helper["transaction_journals"](context.host.user_root)
    own = []
    parent = {
        "transaction_id": state["transaction_id"],
        "manifest_sha256": context.manifest_sha256,
    }
    for _, journal in children:
        if journal.get("parent") == parent:
            own.append(journal)
        elif journal["state"] != "rolled_back":
            raise BootstrapError(
                "foreign runtime transaction prevents composed recovery"
            )
    if len(own) > 1:
        raise BootstrapError("multiple runtime children claim this parent")
    if not own:
        return None
    journal = own[0]
    if (
        journal.get("version") != 3
        or journal.get("scope") != "user"
        or journal.get("roots") != {"user": str(context.host.user_root)}
        or journal.get("plan_sha256") != plan["runtime"]["sha256"]
    ):
        raise BootstrapError("runtime child does not match its parent plan")
    expected = {
        item["role"]: (item["source_sha256"], item["target_mode"])
        for item in plan["runtime"]["document"]["artifacts"]
    }
    manifest = plan["runtime"]["document"]["user_manifest"]
    expected["user-install-manifest"] = (
        manifest["installed_sha256"],
        manifest["installed_mode"],
    )
    if {
        item["role"]: (item["installed_sha256"], item["installed_mode"])
        for item in journal["artifacts"]
    } != expected:
        raise BootstrapError("runtime child artifacts differ from its parent plan")
    return journal


def _run_runtime(
    context: Context, state: dict, operation: str, lease: int, digest: str | None = None
) -> str:
    _, data = _helper(context, state)
    args = _runtime_arguments(context, state)
    path = _transaction(context, state) / "runtime-helper"
    info = path.lstat()
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or not stat.S_ISREG(info.st_mode)
    ):
        raise BootstrapError("recorded runtime helper has unsafe metadata")
    fd = _sealed_helper(data)
    try:
        argv = [
            sys.executable,
            f"/proc/self/fd/{fd}",
            operation,
            "--user-root",
            str(args.user_root),
            "--artifact-bundle-root",
            str(args.artifact_bundle_root),
            "--bundle-manifest",
            str(args.bundle_manifest),
            "--artifact-evidence-manifest",
            str(args.artifact_evidence_manifest),
        ]
        if operation in {"install", "lock-evidence"}:
            argv += [
                "--user-only",
                "--runtime-profile",
                args.runtime_profile,
                "--parent-transaction",
                state["transaction_id"],
                "--parent-manifest-sha256",
                context.manifest_sha256,
            ]
        if operation == "install":
            argv += ["--plan-sha256", digest]
        environment = {
            key: os.environ[key]
            for key in ("PATH", "DBUS_SESSION_BUS_ADDRESS")
            if key in os.environ
        }
        environment.update(
            {
                "HOME": str(context.host.user_root),
                "XDG_RUNTIME_DIR": str(context.host.runtime_dir),
                "FORZA_RUNTIME_INTEGRATION_ROOT": str(context.repository),
                "FORZA_LAUNCHER_LEASE_FD": str(lease),
            }
        )
        _quiet(context, lease)
        result = subprocess.run(
            argv,
            pass_fds=(fd, lease),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if result.returncode:
            raise BootstrapError(
                f"runtime {operation} failed; recovery material retained: {result.stderr.strip()}"
            )
        return result.stdout
    finally:
        os.close(fd)


def _licensed_action(
    context: Context, plan: dict, source: Path
) -> licensed.LicensedAction:
    value = plan["licensed"]
    before = _licensed_before_record(value)
    target = (
        Path(plan["prefix"]["path"])
        / "drive_c/windows/system32/xgameruntime.dll.threading"
    )
    if value.get("destination_logical") != str(target):
        raise BootstrapError("licensed destination differs from the prefix")
    return licensed.LicensedAction(
        str(target),
        value["source_size"],
        value["source_sha256"],
        before,
        value["disposition"],
        source,
        target,
    )


def _validate_licensed_journal(
    context: Context, state: dict, plan: dict | None
) -> None:
    path = _transaction(context, state) / "licensed/licensed.json"
    if not os.path.lexists(path):
        return
    if plan is None:
        raise BootstrapError("licensed journal has no confirmed parent plan")
    journal = licensed._journal_from_value(_read(path))
    action = _licensed_action(
        context, plan, Path("/unused-source-for-read-only-validation")
    )
    if any(
        getattr(journal, key) != getattr(action, key)
        for key in (
            "destination_logical",
            "source_size",
            "source_sha256",
            "before",
            "disposition",
        )
    ):
        raise BootstrapError("licensed journal does not match its parent plan")


def _doctor_check(context: Context, state: dict) -> None:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "DBUS_SESSION_BUS_ADDRESS")
        if key in os.environ
    }
    environment.update(
        HOME=str(context.host.user_root),
        XDG_DATA_HOME=str(context.host.steam_root.parent),
        XDG_RUNTIME_DIR=str(context.host.runtime_dir),
    )
    result = context.probe.runner(
        [str(context.host.user_root / ".local/bin/forza-doctor")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    _write(
        _transaction(context, state) / "doctor.json",
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    if result.returncode:
        raise BootstrapError(
            "Doctor found an unresolved prerequisite; inspect the private doctor.json report, correct it, then repeat Finish or roll back"
        )


def finish(
    context: Context, threading_dll: Path, confirm: Callable[[str], str]
) -> dict:
    with _lease(context) as lease:
        lock = acquire_bootstrap_lock(context.host.state_root)
        try:
            state = load_active(context)
            if state is None or state["phase"] not in {
                "AWAITING_STEAM_PREFIX",
                "INSTALLING_FINISH",
            }:
                raise BootstrapError("Finish requires the composed Prepare checkpoint")
            _validate_prepare_binding(context, state)
            transaction = _transaction(context, state)
            if state["phase"] == "AWAITING_STEAM_PREFIX":
                plan, evidence = finish_plan(context, state, threading_dll)
                digest = sha256_bytes(canonical_json(plan))
                if confirm(digest) != digest:
                    raise BootstrapError(
                        "Finish confirmation did not match; no installation was attempted"
                    )
                if finish_plan(context, state, threading_dll) != (plan, evidence):
                    raise BootstrapError("Finish inputs changed after confirmation")
                _write(transaction / "finish-plan.json", plan)
                _write(transaction / "runtime-evidence.json", evidence)
                state = _phase(
                    context, state, "INSTALLING_FINISH", finish_plan_sha256=digest
                )
            plan = _finish_record(context, state)
            _gui_check(context)
            _validate_licensed_journal(context, state, plan)
            child = _child(context, state, plan)
            if child is not None and child["state"] != "installed":
                raise BootstrapError(
                    "interrupted runtime child requires composed rollback"
                )
            action = _licensed_action(context, plan, threading_dll)
            _quiet(context, lease)
            licensed.install_threading_copy(action, transaction / "licensed")
            if child is None:
                output = _run_runtime(context, state, "lock-evidence", lease)
                expected = plan["runtime"]["document"]["evidence_manifest_sha256"]
                if output.strip() != f"EVIDENCE_SHA256={expected}":
                    raise BootstrapError(
                        "runtime source inputs changed since Finish confirmation"
                    )
                _run_runtime(
                    context, state, "install", lease, plan["runtime"]["sha256"]
                )
                child = _child(context, state, plan)
            if child is None or child["state"] != "installed":
                raise BootstrapError("runtime child did not complete installation")
            helper, _ = _helper(context, state)
            helper["verify_transaction_installed"](
                _runtime_arguments(context, state), child
            )
            _quiet(context, lease)
            _finish_record(context, state)
            proton.inspect_composed_fm(
                _publication_args(context, state)[4],
                context.release["payload_sha256"],
                context.release["marker_sha256"],
            )
            _doctor_check(context, state)
            _quiet(context, lease)
            return _phase(context, state, "READY_TO_ATTEMPT")
        finally:
            os.close(lock)


def rollback(context: Context, confirm: Callable[[str], str]) -> dict:
    with _lease(context) as lease:
        lock = acquire_bootstrap_lock(context.host.state_root)
        try:
            state = load_active(context)
            if state is None:
                raise BootstrapError("no composed transaction exists")
            if state["phase"] == "ROLLED_BACK":
                return state
            _validate_prepare_binding(context, state)
            if confirm("ROLLBACK") != "ROLLBACK":
                raise BootstrapError("rollback was not confirmed")
            transaction = _transaction(context, state)
            # Validate every authority record before the first rollback mutation.
            plan = (
                _finish_record(context, state)
                if state["finish_plan_sha256"] is not None
                else None
            )
            child = _child(context, state, plan) if plan is not None else None
            _validate_licensed_journal(context, state, plan)
            if (transaction / "tool-publication.json").exists():
                composition.inspect_publication(*_publication_args(context, state))
            elif os.path.lexists(_publication_args(context, state)[4]):
                raise BootstrapError("unrecorded compatibility tool must be preserved")
            state = _phase(context, state, "ROLLING_BACK")
            if child is not None and child["state"] != "rolled_back":
                _run_runtime(context, state, "rollback", lease)
                if _child(context, state, plan)["state"] != "rolled_back":
                    raise BootstrapError("runtime rollback remains incomplete")
            if (transaction / "licensed/licensed.json").exists():
                _quiet(context, lease)
                licensed.rollback_threading_copy(transaction / "licensed")
            _quiet(context, lease)
            if (transaction / "tool-publication.json").exists():
                composition.rollback_composed(*_publication_args(context, state))
            elif os.path.lexists(_publication_args(context, state)[4]):
                raise BootstrapError("unrecorded compatibility tool must be preserved")
            # Steam-created prefix and private build/recovery material remain intact.
            return _phase(context, state, "ROLLED_BACK")
        finally:
            os.close(lock)


def status(context: Context) -> dict:
    state = load_active(context)
    if state is None:
        return {"phase": "NOT_STARTED", "support": "experimental-preview"}
    _validate_prepare_binding(context, state)
    if state["phase"] == "READY_TO_ATTEMPT":
        plan = _finish_record(context, state)
        _validate_licensed_journal(context, state, plan)
        child = _child(context, state, plan)
        if child is None or child["state"] != "installed":
            raise BootstrapError("composed runtime child is not installed")
        helper, _ = _helper(context, state)
        helper["verify_transaction_installed"](
            _runtime_arguments(context, state), child
        )
        proton.inspect_composed_fm(
            _publication_args(context, state)[4],
            context.release["payload_sha256"],
            context.release["marker_sha256"],
        )
        target = Path(plan["licensed"]["destination_logical"])
        if licensed.validate_pe64(target).sha256 != plan["licensed"]["source_sha256"]:
            raise BootstrapError("licensed runtime changed after Finish")
        _gui_check(context)
    return {"phase": state["phase"], "support": "experimental-preview"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="setup composed", description=__doc__)
    parser.add_argument(
        "operation", choices=("plan", "prepare", "finish", "status", "rollback")
    )
    parser.add_argument("--tool-archive", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--threading-dll", type=Path)
    parser.add_argument(
        "--confirm", help="exact plan SHA-256, or ROLLBACK for rollback"
    )
    args = parser.parse_args(argv)
    try:
        context = resolve_context(Path(__file__).resolve().parents[1], os.environ)
        for path in (args.tool_archive, args.bundle_root, args.threading_dll):
            if path is not None and (not path.is_absolute() or ".." in path.parts):
                raise BootstrapError("input paths must be absolute without traversal")

        def confirm(digest):
            if args.confirm is not None:
                return args.confirm
            return input(f"Type {digest} to continue: ")

        if args.operation == "plan":
            state = load_active(context)
            if state is not None and state["phase"] == "AWAITING_STEAM_PREFIX":
                if args.threading_dll is None:
                    raise BootstrapError("Finish planning requires --threading-dll")
                plan, _ = finish_plan(context, state, args.threading_dll)
                print(
                    "Finish: install the private threading runtime and user integration; verify the complete tool remains unchanged."
                )
            else:
                if args.tool_archive is None or args.bundle_root is None:
                    raise BootstrapError(
                        "Prepare planning requires --tool-archive and --bundle-root"
                    )
                plan = prepare_plan(context, args.tool_archive, args.bundle_root)
                print(
                    "Prepare: publish the reviewed complete tool; retain private recovery material. Steam settings and game launch remain manual."
                )
            print(f"PLAN_SHA256={sha256_bytes(canonical_json(plan))}")
            return 0
        if args.operation == "prepare":
            result = prepare(context, args.tool_archive, args.bundle_root, confirm)
        elif args.operation == "finish":
            if args.threading_dll is None:
                raise BootstrapError("Finish requires --threading-dll")
            result = finish(context, args.threading_dll, confirm)
        elif args.operation == "rollback":
            result = rollback(context, confirm)
        else:
            result = status(context)
        print(f"PHASE={result['phase']}")
        if result["phase"] == "AWAITING_STEAM_PREFIX":
            print(
                "Restart Steam. Select GE-Proton11-3-FM for Forza, start it once, then stop it. Run composed plan and finish with your private threading DLL."
            )
        elif result["phase"] == "READY_TO_ATTEMPT":
            subprocess.run(
                [str(context.repository / "scripts/print-steam-options")], check=True
            )
            print(
                "Experimental preview: ready to attempt. Account authentication, gameplay, and physical Steam Deck behavior still require validation."
            )
        return 0
    except (
        BootstrapError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "Stopped. Inspect composed status before continuing; keep all recovery material."
        )
        return 130
