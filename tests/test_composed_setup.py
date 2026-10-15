# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import subprocess
import tarfile
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from test_bootstrap_artifacts import bundle_metadata_entries
from test_bootstrap_finish import manifest_fixture
from test_bootstrap_licensed import make_synthetic_pe
from test_composed_proton import composed_fixture
from test_runtime_components import PAYLOADS, commit_fixture_profile, git, setup_fixture

from forza_bootstrap import artifacts, proton
from forza_bootstrap import composed_setup as setup
from forza_bootstrap.coordinator import HostProbe, ResolvedHost
from forza_bootstrap.model import (
    ArtifactSpec,
    BootstrapError,
    SourceSpec,
    sha256_bytes,
)
from forza_bootstrap.platform_check import SteamLayout

ROOT = Path(__file__).resolve().parents[1]


def fixture(tmp_path, monkeypatch):
    data = setup_fixture(tmp_path, input_kind="bundle")
    home = data["user_root"]
    for relative in PAYLOADS:
        (home / relative).unlink()
    (home / ".local/state/forza-motorsport-linux/install-manifest").unlink()
    repo = data["integration_source"]
    commit_fixture_profile(data)
    shutil.copy2(
        ROOT / "scripts/install-runtime-components",
        repo / "scripts/install-runtime-components",
    )
    (repo / "bin/forza-doctor").write_text("#!/bin/sh\nexit 0\n")
    git(repo, "add", "scripts/install-runtime-components", "bin/forza-doctor")
    git(repo, "commit", "-qm", "snapshot composed runtime helper")
    bundle = data["bundle_root"]
    document = json.loads((bundle / "bundle-manifest.json").read_bytes())
    document["profile"] = "legacy-v0.1"
    manifest = replace(
        manifest_fixture(tmp_path),
        profile="legacy-v0.1",
        artifacts=tuple(ArtifactSpec(**record) for record in document["artifacts"]),
        sources=tuple(
            SourceSpec(name, f"https://example.invalid/{name}", revision)
            for name, revision in document["sources"].items()
        ),
    )
    (bundle / "bundle-manifest.json").write_bytes(
        artifacts._expected_internal_manifest(manifest)
    )
    for entry in bundle_metadata_entries("bundle", tuple(document["sources"])):
        target = tmp_path / entry.name
        if entry.kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry.data)
            target.chmod(entry.mode)
    monkeypatch.setattr(setup, "load_bootstrap_manifest", lambda _: manifest)
    steam = home / ".local/share/Steam"
    game = steam / "steamapps/common/ForzaFixture"
    game.mkdir(parents=True)
    (steam / "steamapps/appmanifest_2440510.acf").write_text(
        '"AppState" { "appid" "2440510" "installdir" "ForzaFixture" }'
    )
    source = tmp_path / "candidate"
    digests = composed_fixture(source)
    archive = tmp_path / "tool.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        out.add(source, arcname="GE-Proton11-3-FM")
    archive.chmod(0o600)
    release = {
        "tool_name": "GE-Proton11-3-FM",
        "payload_sha256": digests[0],
        "marker_sha256": digests[1],
        "managed_targets": json.loads((source / ".forza-bootstrap.json").read_bytes())[
            "managed_targets"
        ],
        "archive": {
            "filename": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256_bytes(archive.read_bytes()),
            "expected_root": "GE-Proton11-3-FM",
        },
    }
    proc = tmp_path / "proc"
    proc.mkdir()
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=arch\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    for key in tuple(os.environ):
        if key.startswith("FORZA_RUNTIME_"):
            monkeypatch.delenv(key)
    host = ResolvedHost(
        "arch",
        "x86_64",
        home,
        steam,
        steam / "steamapps/appmanifest_2440510.acf",
        steam / "compatibilitytools.d",
        home / ".local/state/forza-motorsport-linux/bootstrap-v2",
        home / ".cache/forza-motorsport-linux/bootstrap-v2",
        data["runtime_dir"],
    )
    context = setup.Context(
        repo,
        host,
        SteamLayout(steam, steam, game, ()),
        release,
        "f" * 64,
        HostProbe(os_release=os_release, proc_root=proc),
    )
    return context, archive, bundle, source


def steam_checkpoint(context, tool):
    prefix = context.host.steam_root / "steamapps/compatdata/2440510/pfx"
    prefix.mkdir(parents=True)
    for name in ("user.reg", "system.reg", "userdef.reg"):
        (prefix / name).write_text("WINE REGISTRY Version 2\n")
    for source, relative in setup._TARGETS.items():
        target = prefix / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tool / source, target)
    return prefix


def private_dll(tmp_path):
    path = tmp_path / "private.dll"
    make_synthetic_pe(path)
    path.chmod(0o600)
    return path


def test_complete_composed_prepare_finish_status_and_rollback(tmp_path, monkeypatch):
    context, archive, bundle, source = fixture(tmp_path, monkeypatch)
    plan = setup.prepare_plan(context, archive, bundle)
    assert not context.host.state_root.exists()
    state = setup.prepare(context, archive, bundle, lambda digest: digest)
    assert state["phase"] == "AWAITING_STEAM_PREFIX"
    tool = context.host.compatibility_tools / "GE-Proton11-3-FM"
    before = proton.inspect_composed_fm(
        tool, context.release["payload_sha256"], context.release["marker_sha256"]
    )
    prefix = steam_checkpoint(context, tool)
    state = setup.finish(context, private_dll(tmp_path), lambda digest: digest)
    assert state["phase"] == "READY_TO_ATTEMPT"
    assert setup.status(context)["phase"] == "READY_TO_ATTEMPT"
    assert (
        proton.inspect_composed_fm(
            tool, context.release["payload_sha256"], context.release["marker_sha256"]
        )
        == before
    )
    assert (prefix / "drive_c/windows/system32/xgameruntime.dll.threading").is_file()
    state = setup.rollback(context, lambda _: "ROLLBACK")
    assert state["phase"] == "ROLLED_BACK"
    assert not tool.exists()
    assert prefix.is_dir()
    assert not (prefix / "drive_c/windows/system32/xgameruntime.dll.threading").exists()
    assert all(not (context.host.user_root / path).exists() for path in PAYLOADS)
    assert source.is_dir()
    assert plan["helper_sha256"] == sha256_bytes(
        (context.repository / "scripts/install-runtime-components").read_bytes()
    )


def test_prepare_refuses_existing_integration_without_writes(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    existing = context.host.user_root / PAYLOADS[0]
    existing.write_text("preserve my working launcher")
    with pytest.raises(BootstrapError, match="existing user integration"):
        setup.prepare_plan(context, archive, bundle)
    assert existing.read_text() == "preserve my working launcher"
    assert not context.host.state_root.exists()


def test_finish_rejects_a_wrong_prefix_before_installing_user_files(
    tmp_path, monkeypatch
):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    setup.prepare(context, archive, bundle, lambda digest: digest)
    tool = context.host.compatibility_tools / "GE-Proton11-3-FM"
    prefix = steam_checkpoint(context, tool)
    (prefix / "drive_c/windows/system32/windows.gaming.input.dll").write_bytes(
        b"stock or changed module"
    )
    with pytest.raises(BootstrapError, match="reviewed composed modules"):
        setup.finish(context, private_dll(tmp_path), lambda digest: digest)
    assert all(not (context.host.user_root / path).exists() for path in PAYLOADS)


def test_recorded_helper_is_used_after_checkout_helper_changes(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    state = setup.prepare(context, archive, bundle, lambda digest: digest)
    original = setup._helper(context, state)[1]
    (context.repository / "scripts/install-runtime-components").write_text(
        "raise RuntimeError('wrong helper')\n"
    )
    assert setup._helper(context, state)[1] == original
    assert setup.rollback(context, lambda _: "ROLLBACK")["phase"] == "ROLLED_BACK"


def test_sealed_helper_cannot_change_after_verification():
    fd = setup._sealed_helper(b"verified helper")
    try:
        with pytest.raises(OSError):
            os.write(fd, b"changed helper")
    finally:
        os.close(fd)


def test_recovery_rejects_modified_helper_snapshot(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    state = setup.prepare(context, archive, bundle, lambda digest: digest)
    path = setup._transaction(context, state) / "runtime-helper"
    path.write_text("raise RuntimeError('must never execute')\n")
    with pytest.raises(BootstrapError, match="helper changed"):
        setup._helper(context, state)


def ready_fixture(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    setup.prepare(context, archive, bundle, lambda digest: digest)
    tool = context.host.compatibility_tools / "GE-Proton11-3-FM"
    prefix = steam_checkpoint(context, tool)
    source = private_dll(tmp_path)
    state = setup.finish(context, source, lambda digest: digest)
    return context, state, tool, prefix, source


def test_wrong_prepare_confirmation_does_not_publish_or_install(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    with pytest.raises(BootstrapError, match="confirmation did not match"):
        setup.prepare(context, archive, bundle, lambda _: "incorrect")
    assert setup.load_active(context) is None
    assert not context.host.compatibility_tools.exists()
    assert all(not (context.host.user_root / path).exists() for path in PAYLOADS)


def test_prepare_recovers_after_publication_without_original_archive(
    tmp_path, monkeypatch
):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    original = setup._phase

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted after publication")

    monkeypatch.setattr(setup, "_phase", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        setup.prepare(context, archive, bundle, lambda digest: digest)
    archive.unlink()
    monkeypatch.setattr(setup, "_phase", original)
    assert (
        setup.prepare(context, None, None, lambda _: pytest.fail("already confirmed"))[
            "phase"
        ]
        == "AWAITING_STEAM_PREFIX"
    )


def test_finish_recovers_after_licensed_copy(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    setup.prepare(context, archive, bundle, lambda digest: digest)
    prefix = steam_checkpoint(
        context, context.host.compatibility_tools / "GE-Proton11-3-FM"
    )
    source = private_dll(tmp_path)
    original = setup._run_runtime

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted before child")

    monkeypatch.setattr(setup, "_run_runtime", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        setup.finish(context, source, lambda digest: digest)
    assert (prefix / "drive_c/windows/system32/xgameruntime.dll.threading").exists()
    monkeypatch.setattr(setup, "_run_runtime", original)
    assert (
        setup.finish(context, source, lambda _: pytest.fail("already confirmed"))[
            "phase"
        ]
        == "READY_TO_ATTEMPT"
    )


def test_doctor_failure_keeps_finish_incomplete_and_can_resume(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    setup.prepare(context, archive, bundle, lambda digest: digest)
    steam_checkpoint(context, context.host.compatibility_tools / "GE-Proton11-3-FM")
    source = private_dll(tmp_path)
    original = setup._doctor_check

    def reject(*args):
        raise BootstrapError("Doctor prerequisite")

    monkeypatch.setattr(setup, "_doctor_check", reject)
    with pytest.raises(BootstrapError, match="Doctor prerequisite"):
        setup.finish(context, source, lambda digest: digest)
    assert setup.load_active(context)["phase"] == "INSTALLING_FINISH"
    monkeypatch.setattr(setup, "_doctor_check", original)
    assert (
        setup.finish(context, source, lambda _: pytest.fail("already confirmed"))[
            "phase"
        ]
        == "READY_TO_ATTEMPT"
    )


def test_changed_tool_blocks_rollback_before_user_files_change(tmp_path, monkeypatch):
    context, state, tool, _, _ = ready_fixture(tmp_path, monkeypatch)
    (tool / "foreign-file").write_bytes(b"preserve")
    with pytest.raises(BootstrapError):
        setup.rollback(context, lambda _: "ROLLBACK")
    assert setup.load_active(context) == state
    assert all((context.host.user_root / path).exists() for path in PAYLOADS)
    assert (tool / "foreign-file").read_bytes() == b"preserve"


def test_licensed_journal_cannot_redirect_parent_rollback(tmp_path, monkeypatch):
    context, state, tool, _, _ = ready_fixture(tmp_path, monkeypatch)
    path = setup._transaction(context, state) / "licensed/licensed.json"
    document = setup._read(path)
    document["source_sha256"] = "a" * 64
    setup._write(path, document)
    with pytest.raises(BootstrapError, match="does not match its parent"):
        setup.rollback(context, lambda _: "ROLLBACK")
    assert setup.load_active(context) == state
    assert tool.is_dir()
    assert all((context.host.user_root / path).exists() for path in PAYLOADS)


def test_rollback_resumes_after_child_completed(tmp_path, monkeypatch):
    context, _state, tool, prefix, _ = ready_fixture(tmp_path, monkeypatch)
    original = setup.licensed.rollback_threading_copy

    def interrupt(*args):
        raise RuntimeError("interrupted after child rollback")

    monkeypatch.setattr(setup.licensed, "rollback_threading_copy", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        setup.rollback(context, lambda _: "ROLLBACK")
    assert setup.load_active(context)["phase"] == "ROLLING_BACK"
    assert all(not (context.host.user_root / path).exists() for path in PAYLOADS)
    monkeypatch.setattr(setup.licensed, "rollback_threading_copy", original)
    assert setup.rollback(context, lambda _: "ROLLBACK")["phase"] == "ROLLED_BACK"
    assert prefix.is_dir() and not tool.exists()


def test_release_matches_read_only_doctor_and_reviewed_patches():
    release, digest = setup.load_release(ROOT)
    known = tomllib.loads((ROOT / "manifests/supported-builds.toml").read_text())[
        "composed"
    ]
    assert digest == known["manifest_sha256"]
    for name, expected in release["patches"].items():
        assert sha256_bytes((ROOT / "patches" / name).read_bytes()) == expected
    assert (
        known["windows_gaming_input_sha256"]
        == release["managed_targets"][
            "files/lib/wine/x86_64-windows/windows.gaming.input.dll"
        ]
    )
    assert (
        known["mountmgr_sha256"]
        == release["managed_targets"]["files/lib/wine/x86_64-windows/mountmgr.sys"]
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema", True),
        ("wine_revision", "invalid"),
        ("source_date_epoch", False),
        ("sdk_image", "untrusted:latest"),
        ("patches", {}),
    ],
)
def test_release_rejects_invalid_provenance(tmp_path, key, value):
    (tmp_path / "manifests").mkdir()
    document = json.loads((ROOT / "manifests/composed-v2.json").read_bytes())
    document[key] = value
    (tmp_path / "manifests/composed-v2.json").write_text(json.dumps(document))
    with pytest.raises(BootstrapError, match="release contract"):
        setup.load_release(tmp_path)


def test_doctor_recognizes_composed_modules_and_rejects_changed_records(
    tmp_path, monkeypatch
):
    context, state, _, _, _ = ready_fixture(tmp_path, monkeypatch)
    manifest = context.host.user_root / PAYLOADS[3]
    values = context.release["managed_targets"]
    manifest.write_text(
        "[composed]\n"
        f'manifest_sha256 = "{context.manifest_sha256}"\n'
        f'windows_gaming_input_sha256 = "{values["files/lib/wine/x86_64-windows/windows.gaming.input.dll"]}"\n'
        f'mountmgr_sha256 = "{values["files/lib/wine/x86_64-windows/mountmgr.sys"]}"\n'
    )
    systemctl = tmp_path / "doctor-systemctl"
    systemctl.write_text("#!/bin/sh\nprintf 'disabled\\n'\n")
    systemctl.chmod(0o755)
    df = tmp_path / "doctor-df"
    df.write_text(
        "#!/bin/sh\nprintf 'Filesystem Blocks Used Available Capacity Mounted\\nfixture 999999999 0 209715200 0%% /\\n'\n"
    )
    df.chmod(0o755)
    env = {
        **os.environ,
        "FORZA_TEST_ROOT": str(context.host.user_root),
        "FORZA_BUSCTL": "/usr/bin/true",
        "FORZA_SYSTEMCTL": str(systemctl),
        "FORZA_DF": str(df),
    }

    def doctor():
        return subprocess.run(
            [str(ROOT / "bin/forza-doctor")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    result = doctor()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS composed installation records" in result.stdout
    assert "PASS controller and mountmgr known-build states" in result.stdout
    plan_path = setup._transaction(context, state) / "finish-plan.json"
    plan_path.chmod(0o644)
    result = doctor()
    assert result.returncode == 1
    assert "FAIL composed installation records" in result.stdout


def test_complete_flow_uses_additional_library_and_prefix(tmp_path, monkeypatch):
    context, archive, bundle, _ = fixture(tmp_path, monkeypatch)
    steam = context.host.steam_root
    library = tmp_path / "microSD fixture"
    (library / "steamapps/common").mkdir(parents=True)
    game = library / "steamapps/common/ForzaFixture"
    context.layout.game_root.rename(game)
    (steam / "steamapps/appmanifest_2440510.acf").rename(
        library / "steamapps/appmanifest_2440510.acf"
    )
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{steam}" }} "1" {{ "path" "{library}" }} }}'
    )
    context = replace(context, layout=SteamLayout(steam, library, game, ()))
    setup.prepare(context, archive, bundle, lambda digest: digest)
    tool = context.host.compatibility_tools / "GE-Proton11-3-FM"
    prefix = steam_checkpoint(context, tool)
    target = library / "steamapps/compatdata/2440510/pfx"
    target.parent.mkdir(parents=True)
    prefix.rename(target)
    assert (
        setup.finish(context, private_dll(tmp_path), lambda digest: digest)["phase"]
        == "READY_TO_ATTEMPT"
    )
    assert setup.status(context)["phase"] == "READY_TO_ATTEMPT"
    assert setup.rollback(context, lambda _: "ROLLBACK")["phase"] == "ROLLED_BACK"
    assert target.is_dir()


@pytest.mark.parametrize("drift", [None, "runtime", "override"])
def test_steamos_gui_requires_reviewed_runtime_and_permission_policy(
    tmp_path, monkeypatch, drift
):
    context, _, _, _ = fixture(tmp_path, monkeypatch)
    release, _ = setup.load_release(ROOT)

    def runner(argv, **kwargs):
        if "info" in argv:
            value = (
                release["flatpak"]["tested_runtime_commit"]
                if argv[-1] == release["flatpak"]["runtime"]
                else release["flatpak"]["commit"]
            )
            if drift == "runtime" and argv[-1] == release["flatpak"]["runtime"]:
                value = "a" * 64
        else:
            value = "[Context]\nfilesystems=home;\n" if drift == "override" else ""
        return subprocess.CompletedProcess(argv, 0, value, "")

    context = replace(
        context,
        release=release,
        host=replace(context.host, os_id="steamos"),
        probe=replace(context.probe, runner=runner),
    )
    if drift:
        with pytest.raises(BootstrapError, match="runtime differs|overrides differ"):
            setup._gui_check(context)
    else:
        setup._gui_check(context)
