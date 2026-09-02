# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TOOL = ROOT / "scripts/install-runtime-components"
SECURE_USER_FILES = ROOT / "scripts/secure-user-files"
PAYLOADS = (
    ".local/bin/forza-linux",
    ".local/bin/forza-doctor",
    ".local/libexec/forza-motorsport-linux/patch-known-build",
    ".local/share/forza-motorsport-linux/supported-builds.toml",
    ".local/libexec/xodus-forza/xodus-service",
    ".local/libexec/xodus-forza/xodus-cli",
    ".local/libexec/xodus-forza/xodus-overlay",
    ".config/systemd/user/xodus-forza.service",
)
XODUS_PAYLOADS = PAYLOADS[4:7]
USER_ROLE_PATHS = {
    "launcher-gate": PAYLOADS[0],
    "doctor": PAYLOADS[1],
    "known-build-patcher": PAYLOADS[2],
    "supported-builds": PAYLOADS[3],
    "xodus-service": PAYLOADS[4],
    "xodus-cli": PAYLOADS[5],
    "xodus-overlay": PAYLOADS[6],
    "systemd-unit": PAYLOADS[7],
}
MANIFEST = ".local/state/forza-motorsport-linux/install-manifest"
TRANSACTION_ROLES = (
    "launcher-gate",
    "doctor",
    "known-build-patcher",
    "supported-builds",
    "xodus-service",
    "xodus-cli",
    "xodus-overlay",
    "systemd-unit",
    "user-install-manifest",
    "xgameruntime-pe64",
    "xgameruntime-unix64",
)


def test_runtime_profile_manifest_selects_legacy_v0_1():
    data = tomllib.loads((ROOT / "manifests/runtime-profiles.toml").read_text())
    profile = data["profiles"][data["active"]]

    assert data["version"] == 1
    assert data["active"] == "legacy-v0.1"
    assert profile["xodus_revision"] == "23da0ab8323a1631ba2aacb069a06af22b8a20ac"
    assert len(profile["xgameruntime_revision"]) == 40
    assert profile["status"] == "candidate"


def test_locked_evidence_names_the_runtime_profile(tmp_path: Path):
    fixture = setup_fixture(tmp_path)

    run_tool(fixture, "lock-evidence")

    evidence = json.loads(fixture["evidence"].read_text())  # type: ignore[union-attr]
    assert evidence["version"] == 2
    assert evidence["runtime_profile"] == "fixture"
    assert set(evidence["source_revisions"]) == {
        "xgameruntime_git_sha",
        "xodus_git_sha",
        "integration_git_sha",
    }


@pytest.mark.parametrize(
    ("manifest", "message"),
    (
        ("version = 1\nactive = \"missing\"\n", "manifest is invalid"),
        (
            (
                "version = 1\n"
                'active = "legacy-v0.1"\n'
                "\n"
                '[profiles."legacy-v0.1"]\n'
                'status = "experimental"\n'
                'xgameruntime_revision = "0"\n'
                'xodus_revision = "0"\n'
            ),
            "profile is not installable",
        ),
        (
            (
                "version = 1\n"
                'active = "legacy-v0.1"\n'
                "\n"
                '[profiles."legacy-v0.1"]\n'
                'status = "candidate"\n'
                'xgameruntime_revision = "not-a-revision"\n'
                'xodus_revision = "also-not-a-revision"\n'
            ),
            "profile revisions are invalid",
        ),
    ),
)
def test_reviewed_runtime_profile_rejects_invalid_manifest(
    tmp_path: Path, manifest: str, message: str
):
    integration = tmp_path / "integration"
    profile = integration / "manifests/runtime-profiles.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text(manifest, encoding="ascii")

    with pytest.raises(RuntimeError, match=message):
        installer_module().reviewed_runtime_profile(integration)


def test_missing_runtime_profile_fails_before_destinations_change(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    before_user = tree_snapshot(fixture["user_root"])  # type: ignore[arg-type]
    before_compat = tree_snapshot(fixture["compat_root"])  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="manifest is invalid"):
        installer_module().reviewed_runtime_profile(fixture["integration_source"])

    assert_runtime_destinations_unchanged(fixture, before_user, before_compat)


def test_changed_profile_after_evidence_lock_fails_before_destinations_change(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    profile = commit_fixture_profile(fixture)
    run_tool(fixture, "lock-evidence")
    before_user = tree_snapshot(fixture["user_root"])  # type: ignore[arg-type]
    before_compat = tree_snapshot(fixture["compat_root"])  # type: ignore[arg-type]
    profile.write_text(profile.read_text().replace('status = "candidate"', 'status = "experimental"'))

    failed = run_tool(fixture, "plan", check=False)

    assert failed.returncode != 0
    assert "worktree is dirty" in failed.stderr.lower()
    assert_runtime_destinations_unchanged(fixture, before_user, before_compat)
    assert not any(
        path.is_dir() and len(path.name) == 24
        for path in fixture["evidence"].parent.iterdir()  # type: ignore[union-attr]
    )


def test_mismatched_fixture_profile_revision_fails_before_destinations_change(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    before_user = tree_snapshot(fixture["user_root"])  # type: ignore[arg-type]
    before_compat = tree_snapshot(fixture["compat_root"])  # type: ignore[arg-type]
    fixture["env"]["FORZA_RUNTIME_EXPECTED_XGAMERUNTIME_SHA"] = "0" * 40  # type: ignore[index]

    failed = run_tool(fixture, "lock-evidence", check=False)

    assert failed.returncode != 0
    assert "revision mismatch" in failed.stderr.lower()
    assert_runtime_destinations_unchanged(fixture, before_user, before_compat)


def test_v1_evidence_cannot_create_a_new_plan_or_install(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    run_tool(fixture, "lock-evidence")
    evidence: Path = fixture["evidence"]  # type: ignore[assignment]
    value = json.loads(evidence.read_text())
    value["version"] = 1
    value.pop("runtime_profile")
    value["source_revisions"] = {
        "winegdk_git_sha": value["source_revisions"]["xgameruntime_git_sha"],
        "xodus_git_sha": value["source_revisions"]["xodus_git_sha"],
        "integration_git_sha": value["source_revisions"]["integration_git_sha"],
    }
    evidence.write_text(json.dumps(value), encoding="ascii")
    evidence.chmod(0o600)
    before_user = tree_snapshot(fixture["user_root"])  # type: ignore[arg-type]
    before_compat = tree_snapshot(fixture["compat_root"])  # type: ignore[arg-type]

    planned = run_tool(fixture, "plan", check=False)
    installed = run_tool(fixture, "install", "--plan-sha256", "0" * 64, check=False)

    assert planned.returncode != 0
    assert installed.returncode != 0
    assert "version mismatch" in planned.stderr.lower()
    assert "version mismatch" in installed.stderr.lower()
    assert_runtime_destinations_unchanged(fixture, before_user, before_compat)


def test_v1_journal_status_and_interrupted_rollback_remain_supported(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    journal_path = next(
        path / "journal.json"
        for path in fixture["evidence"].parent.iterdir()  # type: ignore[union-attr]
        if path.is_dir() and len(path.name) == 24
    )
    journal = json.loads(journal_path.read_text())
    journal["version"] = 1
    journal.pop("runtime_profile")
    journal["source_revisions"] = {
        "winegdk_git_sha": journal["source_revisions"]["xgameruntime_git_sha"],
        "xodus_git_sha": journal["source_revisions"]["xodus_git_sha"],
        "integration_git_sha": journal["source_revisions"]["integration_git_sha"],
    }
    journal["state"] = "rolling_back"
    journal["recovery_operation"] = "rollback"
    journal_path.write_text(json.dumps(journal), encoding="ascii")
    journal_path.chmod(0o600)

    assert "state=rolling_back" in run_tool(fixture, "status").stdout
    run_tool(fixture, "rollback")

    restored = json.loads(journal_path.read_text())
    assert restored["version"] == 1
    assert "runtime_profile" not in restored
    assert restored["state"] == "rolled_back"
    assert_original_install_restored(fixture)


def test_accept_rejects_a_v1_journal(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    journal_path = next(
        path / "journal.json"
        for path in fixture["evidence"].parent.iterdir()  # type: ignore[union-attr]
        if path.is_dir() and len(path.name) == 24
    )
    journal = json.loads(journal_path.read_text())
    journal["version"] = 1
    journal.pop("runtime_profile")
    journal["source_revisions"] = {
        "winegdk_git_sha": journal["source_revisions"]["xgameruntime_git_sha"],
        "xodus_git_sha": journal["source_revisions"]["xodus_git_sha"],
        "integration_git_sha": journal["source_revisions"]["integration_git_sha"],
    }
    journal_path.write_text(json.dumps(journal), encoding="ascii")
    journal_path.chmod(0o600)

    failed = run_tool(fixture, "accept", check=False)

    assert failed.returncode != 0
    assert "exactly one matching" in failed.stderr.lower()
    assert json.loads(journal_path.read_text())["version"] == 1


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def installer_module():
    loader = importlib.machinery.SourceFileLoader("runtime_installer", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repo(path: Path, tracked_name: str) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.name", "Runtime Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    (path / ".gitignore").write_text("target/\n", encoding="ascii")
    (path / tracked_name).write_text("reviewed source\n", encoding="ascii")
    git(path, "add", ".gitignore", tracked_name)
    git(path, "commit", "-q", "-m", "fixture source")
    return git(path, "rev-parse", "HEAD")


def manifest_bytes(records: dict[str, tuple[int, str]]) -> bytes:
    lines = ["forza-motorsport-linux-install-v1"]
    lines.extend(
        f"{relative}\t{records[relative][0]:o}\t{records[relative][1]}"
        for relative in PAYLOADS
    )
    return ("\n".join(lines) + "\n").encode()


def tree_snapshot(root: Path) -> list[tuple[str, str, int]]:
    result: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            result.append(
                (relative, sha256(path.read_bytes()), stat.S_IMODE(info.st_mode))
            )
        else:
            result.append(
                (relative, stat.filemode(info.st_mode), stat.S_IMODE(info.st_mode))
            )
    return result


def setup_fixture(tmp_path: Path) -> dict[str, object]:
    user_root = tmp_path / "user"
    compat_root = tmp_path / "compat"
    wine_source = tmp_path / "wine-source"
    wine_build = tmp_path / "wine-build"
    xodus_source = tmp_path / "xodus-source"
    integration_source = tmp_path / "integration-source"
    xodus_build = xodus_source / "target/release"
    runtime_dir = tmp_path / "runtime"
    fake_systemctl = tmp_path / "systemctl"

    wine_sha = make_repo(wine_source, "wine-source.txt")
    xodus_sha = make_repo(xodus_source, "xodus-source.txt")
    make_repo(integration_source, "integration-source.txt")
    integration_files = {
        "bin/forza-linux": (b"new recovery-aware launcher\n", 0o755),
        "bin/forza-doctor": (b"new doctor\n", 0o755),
        "scripts/patch-known-build": (b"new patcher\n", 0o755),
        "manifests/supported-builds.toml": (b"[reviewed]\n", 0o644),
        "config/xodus-forza.service": (b"[Service]\nExecStart=/fixed\n", 0o644),
    }
    for relative, (data, mode) in integration_files.items():
        source = integration_source / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        source.chmod(mode)
    git(integration_source, "add", *integration_files)
    git(integration_source, "commit", "-q", "-m", "fixture integration")
    integration_sha = git(integration_source, "rev-parse", "HEAD")
    wine_build.mkdir()
    (wine_build / "Makefile").write_text(f"srcdir = {wine_source}\n", encoding="utf-8")
    xodus_build.mkdir(parents=True)

    artifacts = {
        "launcher-gate": (
            integration_source / "bin/forza-linux",
            integration_files["bin/forza-linux"][0],
        ),
        "doctor": (
            integration_source / "bin/forza-doctor",
            integration_files["bin/forza-doctor"][0],
        ),
        "known-build-patcher": (
            integration_source / "scripts/patch-known-build",
            integration_files["scripts/patch-known-build"][0],
        ),
        "supported-builds": (
            integration_source / "manifests/supported-builds.toml",
            integration_files["manifests/supported-builds.toml"][0],
        ),
        "systemd-unit": (
            integration_source / "config/xodus-forza.service",
            integration_files["config/xodus-forza.service"][0],
        ),
        "xgameruntime-pe64": (
            wine_build / "dlls/xgameruntime/x86_64-windows/xgameruntime.dll",
            b"new pe runtime\n",
        ),
        "xgameruntime-unix64": (
            wine_build / "dlls/xgameruntime/xgameruntime.so",
            b"new unix runtime\n",
        ),
        "xodus-service": (xodus_build / "xodus-service", b"new service\n"),
        "xodus-cli": (xodus_build / "xodus-cli", b"new cli\n"),
        "xodus-overlay": (xodus_build / "xodus-overlay", b"new overlay\n"),
    }
    for path, data in artifacts.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if path.name not in {"supported-builds.toml", "xodus-forza.service"}:
            path.chmod(0o755 if path.name != "xgameruntime.dll" else 0o644)

    old_wine = {
        "xgameruntime-pe64": b"old pe runtime\n",
        "xgameruntime-unix64": b"old unix runtime\n",
    }
    wine_destinations = {
        "xgameruntime-pe64": compat_root
        / "files/lib/wine/x86_64-windows/xgameruntime.dll",
        "xgameruntime-unix64": compat_root
        / "files/lib/wine/x86_64-unix/xgameruntime.so",
    }
    for role, path in wine_destinations.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(old_wine[role])
        path.chmod(0o644)

    old_records: dict[str, tuple[int, str]] = {}
    old_user: dict[str, bytes] = {}
    for index, relative in enumerate(PAYLOADS):
        data = f"old user payload {index}\n".encode()
        mode = (
            0o755
            if relative.startswith(".local/bin/") or "/libexec/" in relative
            else 0o644
        )
        destination = user_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(mode)
        old_records[relative] = (mode, sha256(data))
        old_user[relative] = data
    manifest_path = user_root / MANIFEST
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    old_manifest = manifest_bytes(old_records)
    manifest_path.write_bytes(old_manifest)
    manifest_path.chmod(0o600)

    runtime_dir.mkdir()
    fake_systemctl.write_text(
        '#!/bin/sh\n[ "${2:-}" = daemon-reload ] && exit 0\nexit 3\n',
        encoding="ascii",
    )
    fake_systemctl.chmod(0o755)
    evidence = (
        user_root
        / ".local/state/forza-motorsport-linux/runtime-transactions/artifact-evidence.json"
    )
    env = os.environ.copy()
    env.update(
        {
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "FORZA_SYSTEMCTL": str(fake_systemctl),
            "FORZA_RUNTIME_TESTING": "1",
            "FORZA_RUNTIME_EXPECTED_XGAMERUNTIME_SHA": wine_sha,
            "FORZA_RUNTIME_EXPECTED_XODUS_SHA": xodus_sha,
            "FORZA_RUNTIME_INTEGRATION_ROOT": str(integration_source),
        }
    )
    common = [
        "--compat-tool-root",
        str(compat_root),
        "--user-root",
        str(user_root),
        "--xgameruntime-build-dir",
        str(wine_build),
        "--xodus-build-dir",
        str(xodus_build),
        "--artifact-evidence-manifest",
        str(evidence),
    ]
    return {
        "user_root": user_root,
        "compat_root": compat_root,
        "wine_source": wine_source,
        "xodus_source": xodus_source,
        "integration_source": integration_source,
        "wine_build": wine_build,
        "xodus_build": xodus_build,
        "runtime_dir": runtime_dir,
        "fake_systemctl": fake_systemctl,
        "evidence": evidence,
        "env": env,
        "common": common,
        "artifacts": artifacts,
        "wine_destinations": wine_destinations,
        "old_wine": old_wine,
        "old_user": old_user,
        "old_manifest": old_manifest,
        "integration_sha": integration_sha,
    }


def run_tool(
    fixture: dict[str, object], operation: str, *extra: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(TOOL), operation, *fixture["common"], *extra],  # type: ignore[list-item]
        env=fixture["env"],  # type: ignore[arg-type]
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise AssertionError(f"tool failed: {result.stderr}\n{result.stdout}")
    return result


def lock_and_plan(fixture: dict[str, object], *extra: str) -> str:
    run_tool(fixture, "lock-evidence")
    output = run_tool(fixture, "plan", *extra).stdout
    return next(
        line.split("=", 1)[1]
        for line in output.splitlines()
        if line.startswith("PLAN_SHA256=")
    )


def assert_original_install_restored(fixture: dict[str, object]) -> None:
    for role, destination in fixture["wine_destinations"].items():  # type: ignore[union-attr]
        assert destination.read_bytes() == fixture["old_wine"][role]  # type: ignore[index]
        assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    for relative in PAYLOADS:
        destination = fixture["user_root"] / relative  # type: ignore[operator]
        assert destination.read_bytes() == fixture["old_user"][relative]  # type: ignore[index]
    assert (fixture["user_root"] / MANIFEST).read_bytes() == fixture["old_manifest"]  # type: ignore[operator]


def run_secure_user_files(
    fixture: dict[str, object], operation: str
) -> subprocess.CompletedProcess[str]:
    user_root: Path = fixture["user_root"]  # type: ignore[assignment]
    command = [str(SECURE_USER_FILES), operation, "--root", str(user_root)]
    if operation == "check":
        for relative in PAYLOADS:
            command.extend(["--source", f"{relative}={user_root / relative}"])
    return subprocess.run(command, check=False, capture_output=True, text=True)


def assert_runtime_destinations_unchanged(
    fixture: dict[str, object], before_user: list[tuple[str, str, int]], before_compat: list[tuple[str, str, int]]
) -> None:
    assert tree_snapshot(fixture["user_root"]) == before_user  # type: ignore[arg-type]
    assert tree_snapshot(fixture["compat_root"]) == before_compat  # type: ignore[arg-type]


def commit_fixture_profile(fixture: dict[str, object]) -> Path:
    integration: Path = fixture["integration_source"]  # type: ignore[assignment]
    profile = integration / "manifests/runtime-profiles.toml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "\n".join(
            (
                "version = 1",
                'active = "legacy-v0.1"',
                "",
                '[profiles."legacy-v0.1"]',
                'status = "candidate"',
                f'xgameruntime_revision = "{git(fixture["wine_source"], "rev-parse", "HEAD")}"',  # type: ignore[arg-type]
                f'xodus_revision = "{git(fixture["xodus_source"], "rev-parse", "HEAD")}"',  # type: ignore[arg-type]
                "",
            )
        ),
        encoding="ascii",
    )
    git(integration, "add", "manifests/runtime-profiles.toml")
    git(integration, "commit", "-q", "-m", "fixture runtime profile")
    return profile


def test_lock_evidence_binds_clean_revisions_and_ten_artifacts(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    run_tool(fixture, "lock-evidence")
    evidence_path: Path = fixture["evidence"]  # type: ignore[assignment]
    evidence = json.loads(evidence_path.read_text())

    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert evidence["source_revisions"] == {
        "xgameruntime_git_sha": fixture["env"]["FORZA_RUNTIME_EXPECTED_XGAMERUNTIME_SHA"],  # type: ignore[index]
        "xodus_git_sha": fixture["env"]["FORZA_RUNTIME_EXPECTED_XODUS_SHA"],  # type: ignore[index]
        "integration_git_sha": fixture["integration_sha"],
    }
    assert {artifact["role"] for artifact in evidence["artifacts"]} == {
        "launcher-gate",
        "doctor",
        "known-build-patcher",
        "supported-builds",
        "xgameruntime-pe64",
        "xgameruntime-unix64",
        "xodus-service",
        "xodus-cli",
        "xodus-overlay",
        "systemd-unit",
    }
    target_modes = {
        artifact["role"]: artifact["target_mode"] for artifact in evidence["artifacts"]
    }
    assert target_modes["supported-builds"] == 0o644
    assert target_modes["systemd-unit"] == 0o644
    assert all(
        mode == 0o755
        for role, mode in target_modes.items()
        if role not in {"supported-builds", "systemd-unit"}
    )
    revisions = set(evidence["source_revisions"].values())
    assert all(
        len(artifact["source_sha256"]) == 64
        and artifact["source_sha256"] not in revisions
        for artifact in evidence["artifacts"]
    )
    serialized = evidence_path.read_text().lower()
    assert "authorization" not in serialized
    assert "inviteaccept" not in serialized


def test_plan_is_read_only_and_install_is_bound_to_its_digest(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    run_tool(fixture, "lock-evidence")
    before = tree_snapshot(fixture["user_root"])  # type: ignore[arg-type]
    plan = run_tool(fixture, "plan").stdout
    after = tree_snapshot(fixture["user_root"])  # type: ignore[arg-type]
    digest_value = next(
        line.split("=", 1)[1]
        for line in plan.splitlines()
        if line.startswith("PLAN_SHA256=")
    )

    assert before == after
    assert len(digest_value) == 64
    failed = run_tool(fixture, "install", "--plan-sha256", "0" * 64, check=False)
    assert failed.returncode != 0
    assert "plan digest" in failed.stderr.lower()
    assert (
        fixture["wine_destinations"]["xgameruntime-pe64"].read_bytes()
        == fixture["old_wine"]["xgameruntime-pe64"]
    )  # type: ignore[index]


def test_legacy_unmanaged_user_files_require_explicit_adoption_and_roll_back(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    user_root: Path = fixture["user_root"]  # type: ignore[assignment]
    (user_root / MANIFEST).unlink()
    absent = {PAYLOADS[0], PAYLOADS[1], PAYLOADS[2], PAYLOADS[3]}
    for relative in absent:
        (user_root / relative).unlink()
    legacy = {
        relative: (user_root / relative).read_bytes()
        for relative in PAYLOADS
        if relative not in absent
    }
    run_tool(fixture, "lock-evidence")

    refused = run_tool(fixture, "plan", check=False)
    assert refused.returncode != 0
    assert "adopt-unmanaged" in refused.stderr.lower()
    plan = run_tool(fixture, "plan", "--adopt-unmanaged").stdout
    plan_digest = next(
        line.split("=", 1)[1]
        for line in plan.splitlines()
        if line.startswith("PLAN_SHA256=")
    )
    run_tool(
        fixture,
        "install",
        "--adopt-unmanaged",
        "--plan-sha256",
        plan_digest,
    )
    assert run_secure_user_files(fixture, "check").returncode == 0

    run_tool(fixture, "rollback")

    for relative in absent:
        assert not (user_root / relative).exists()
    for relative, data in legacy.items():
        assert (user_root / relative).read_bytes() == data
    assert not (user_root / MANIFEST).exists()


def test_adoption_does_not_bypass_a_broken_managed_install(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    user_root: Path = fixture["user_root"]  # type: ignore[assignment]
    (user_root / PAYLOADS[2]).unlink()
    run_tool(fixture, "lock-evidence")

    failed = run_tool(fixture, "plan", "--adopt-unmanaged", check=False)

    assert failed.returncode != 0
    assert "no such file" in failed.stderr.lower()


def test_install_and_rollback_preserve_hash_mode_and_old_manifest(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)

    for role, relative in USER_ROLE_PATHS.items():
        destination = fixture["user_root"] / relative  # type: ignore[operator]
        source = fixture["artifacts"][role][0]  # type: ignore[index]
        assert destination.read_bytes() == source.read_bytes()
        expected_mode = 0o644 if role in {"supported-builds", "systemd-unit"} else 0o755
        assert stat.S_IMODE(destination.stat().st_mode) == expected_mode
    for role, destination in fixture["wine_destinations"].items():  # type: ignore[union-attr]
        source = fixture["artifacts"][role][0]  # type: ignore[index]
        assert destination.read_bytes() == source.read_bytes()
        assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert "state=installed" in run_tool(fixture, "status").stdout
    transaction_root: Path = fixture["evidence"].parent  # type: ignore[union-attr]
    journal_path = next(
        path / "journal.json"
        for path in transaction_root.iterdir()
        if path.is_dir() and len(path.name) == 24
    )
    journal = json.loads(journal_path.read_text())
    assert journal["version"] == 2
    assert journal["runtime_profile"] == "fixture"
    assert set(journal["source_revisions"]) == {
        "xgameruntime_git_sha",
        "xodus_git_sha",
        "integration_git_sha",
    }
    assert [record["role"] for record in journal["artifacts"]] == list(
        TRANSACTION_ROLES
    )
    assert journal["user_manager_reload_required"] is False

    run_tool(fixture, "rollback")
    for role, destination in fixture["wine_destinations"].items():  # type: ignore[union-attr]
        assert destination.read_bytes() == fixture["old_wine"][role]  # type: ignore[index]
        assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    for relative in PAYLOADS:
        assert (fixture["user_root"] / relative).read_bytes() == fixture["old_user"][
            relative
        ]  # type: ignore[index,operator]
    assert (fixture["user_root"] / MANIFEST).read_bytes() == fixture["old_manifest"]  # type: ignore[operator]
    assert "state=rolled_back" in run_tool(fixture, "status").stdout


def test_failed_manager_reload_keeps_recovery_launcher_until_retry(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    fixture["fake_systemctl"].write_text(  # type: ignore[union-attr]
        '#!/bin/sh\n[ "${1:-} ${2:-}" = "--user daemon-reload" ] && exit 1\nexit 3\n',
        encoding="ascii",
    )

    failed = run_tool(fixture, "install", "--plan-sha256", plan_digest, check=False)

    assert failed.returncode != 0
    assert "cannot reload the user systemd manager" in failed.stderr.lower()
    recovery_status = run_tool(fixture, "status", check=False)
    assert recovery_status.returncode == 1
    assert "state=recovery_required" in recovery_status.stdout
    launcher = fixture["user_root"] / USER_ROLE_PATHS["launcher-gate"]  # type: ignore[operator]
    launcher_source = fixture["artifacts"]["launcher-gate"][0]  # type: ignore[index]
    assert launcher.read_bytes() == launcher_source.read_bytes()
    unit = fixture["user_root"] / USER_ROLE_PATHS["systemd-unit"]  # type: ignore[operator]
    assert unit.read_bytes() == fixture["old_user"][USER_ROLE_PATHS["systemd-unit"]]  # type: ignore[index]

    fixture["fake_systemctl"].write_text(  # type: ignore[union-attr]
        '#!/bin/sh\n[ "${2:-}" = daemon-reload ] && exit 0\nexit 3\n',
        encoding="ascii",
    )
    run_tool(fixture, "rollback")

    assert_original_install_restored(fixture)
    assert "state=rolled_back" in run_tool(fixture, "status").stdout


def test_accept_then_restore_runtime_keeps_manifest_owned_xodus(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    installed_xodus = {
        relative: (fixture["user_root"] / relative).read_bytes()  # type: ignore[operator]
        for relative in XODUS_PAYLOADS
    }
    installed_launcher = (fixture["user_root"] / PAYLOADS[0]).read_bytes()  # type: ignore[operator]
    accepted_manifest = (fixture["user_root"] / MANIFEST).read_bytes()  # type: ignore[operator]

    run_tool(fixture, "accept")
    run_tool(fixture, "restore-runtime")

    for role, destination in fixture["wine_destinations"].items():  # type: ignore[union-attr]
        assert destination.read_bytes() == fixture["old_wine"][role]  # type: ignore[index]
    for relative, data in installed_xodus.items():
        assert (fixture["user_root"] / relative).read_bytes() == data  # type: ignore[operator]
    assert (fixture["user_root"] / PAYLOADS[0]).read_bytes() == installed_launcher  # type: ignore[operator]
    assert (fixture["user_root"] / MANIFEST).read_bytes() == accepted_manifest  # type: ignore[operator]
    assert "state=runtime_restored" in run_tool(fixture, "status").stdout


def test_full_rollback_after_runtime_restore_restores_original_install(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    run_tool(fixture, "accept")
    run_tool(fixture, "restore-runtime")

    run_tool(fixture, "rollback")

    assert_original_install_restored(fixture)
    assert "state=rolled_back" in run_tool(fixture, "status").stdout


def test_source_symlink_and_unsafe_mode_are_rejected(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    source = fixture["artifacts"]["xodus-overlay"][0]  # type: ignore[index]
    original = source.with_suffix(".original")
    source.rename(original)
    source.symlink_to(original)
    failed = run_tool(fixture, "lock-evidence", check=False)
    assert failed.returncode != 0
    assert "symlink" in failed.stderr.lower() or "follow" in failed.stderr.lower()

    source.unlink()
    original.rename(source)
    source.chmod(0o775)
    failed = run_tool(fixture, "lock-evidence", check=False)
    assert failed.returncode != 0
    assert "unsafe mode" in failed.stderr.lower()


def test_dirty_integration_source_cannot_lock_the_launcher_gate(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    tracked = fixture["integration_source"] / "integration-source.txt"  # type: ignore[operator]
    tracked.write_text("unreviewed change\n")

    failed = run_tool(fixture, "lock-evidence", check=False)

    assert failed.returncode != 0
    assert "worktree is dirty" in failed.stderr.lower()
    assert not fixture["evidence"].exists()  # type: ignore[union-attr]


def test_mutation_refuses_the_launcher_lock_and_active_service(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    lock_path = fixture["runtime_dir"] / "forza-linux.lock"  # type: ignore[operator]
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failed = run_tool(fixture, "install", "--plan-sha256", plan_digest, check=False)
        assert failed.returncode != 0
        assert "launcher lock" in failed.stderr.lower()
    finally:
        os.close(lock_fd)

    fixture["fake_systemctl"].write_text("#!/bin/sh\nexit 0\n", encoding="ascii")  # type: ignore[union-attr]
    failed = run_tool(fixture, "install", "--plan-sha256", plan_digest, check=False)
    assert failed.returncode != 0
    assert "service is active" in failed.stderr.lower()


@pytest.mark.parametrize(
    "boundary",
    [
        *(f"stage-write:{role}" for role in TRANSACTION_ROLES),
        *(f"backup-rename:{role}" for role in TRANSACTION_ROLES),
        *(f"install-rename:{role}" for role in TRANSACTION_ROLES),
    ],
)
def test_every_install_mutation_boundary_recovers_after_process_exit(
    tmp_path: Path, boundary: str
):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    fixture["env"].update(  # type: ignore[union-attr]
        {
            "FORZA_RUNTIME_INTERRUPT_AT": boundary,
            "FORZA_RUNTIME_INTERRUPT_MODE": "crash",
        }
    )

    interrupted = run_tool(
        fixture, "install", "--plan-sha256", plan_digest, check=False
    )
    assert interrupted.returncode == 99
    fixture["env"].pop("FORZA_RUNTIME_INTERRUPT_AT")  # type: ignore[union-attr]
    fixture["env"].pop("FORZA_RUNTIME_INTERRUPT_MODE")  # type: ignore[union-attr]
    status = run_tool(fixture, "status").stdout
    assert "state=prepared" in status or "state=installing" in status

    run_tool(fixture, "rollback")
    assert_original_install_restored(fixture)
    assert "state=rolled_back" in run_tool(fixture, "status").stdout


@pytest.mark.parametrize(
    "boundary",
    [
        *(f"restore-current:{role}" for role in TRANSACTION_ROLES),
        *(f"restore-backup:{role}" for role in TRANSACTION_ROLES),
    ],
)
def test_rollback_is_resumable_at_every_restore_rename(tmp_path: Path, boundary: str):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    fixture["env"].update(  # type: ignore[union-attr]
        {
            "FORZA_RUNTIME_INTERRUPT_AT": boundary,
            "FORZA_RUNTIME_INTERRUPT_MODE": "crash",
        }
    )

    interrupted = run_tool(fixture, "rollback", check=False)

    assert interrupted.returncode == 99
    fixture["env"].pop("FORZA_RUNTIME_INTERRUPT_AT")  # type: ignore[union-attr]
    fixture["env"].pop("FORZA_RUNTIME_INTERRUPT_MODE")  # type: ignore[union-attr]
    assert "state=rolling_back" in run_tool(fixture, "status").stdout
    run_tool(fixture, "rollback")
    assert_original_install_restored(fixture)
    assert "state=rolled_back" in run_tool(fixture, "status").stdout


@pytest.mark.parametrize(
    "boundary",
    [
        *(
            f"restore-current:{role}"
            for role in ("xgameruntime-pe64", "xgameruntime-unix64")
        ),
        *(
            f"restore-backup:{role}"
            for role in ("xgameruntime-pe64", "xgameruntime-unix64")
        ),
    ],
)
def test_restore_runtime_resumes_without_rolling_back_accepted_xodus(
    tmp_path: Path, boundary: str
):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    run_tool(fixture, "accept")
    installed_xodus = {
        relative: (fixture["user_root"] / relative).read_bytes()  # type: ignore[operator]
        for relative in XODUS_PAYLOADS
    }
    fixture["env"].update(  # type: ignore[union-attr]
        {
            "FORZA_RUNTIME_INTERRUPT_AT": boundary,
            "FORZA_RUNTIME_INTERRUPT_MODE": "crash",
        }
    )

    interrupted = run_tool(fixture, "restore-runtime", check=False)

    assert interrupted.returncode == 99
    fixture["env"].pop("FORZA_RUNTIME_INTERRUPT_AT")  # type: ignore[union-attr]
    fixture["env"].pop("FORZA_RUNTIME_INTERRUPT_MODE")  # type: ignore[union-attr]
    refused = run_tool(fixture, "rollback", check=False)
    assert refused.returncode != 0
    run_tool(fixture, "restore-runtime")
    for role, destination in fixture["wine_destinations"].items():  # type: ignore[union-attr]
        assert destination.read_bytes() == fixture["old_wine"][role]  # type: ignore[index]
    for relative, data in installed_xodus.items():
        assert (fixture["user_root"] / relative).read_bytes() == data  # type: ignore[operator]
    assert "state=runtime_restored" in run_tool(fixture, "status").stdout


def test_rollback_restores_an_originally_absent_destination_to_absence(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    destination: Path = fixture["wine_destinations"]["xgameruntime-pe64"]  # type: ignore[index]
    destination.unlink()
    plan_digest = lock_and_plan(fixture)

    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    assert destination.is_file()
    run_tool(fixture, "rollback")

    assert not destination.exists()
    other: Path = fixture["wine_destinations"]["xgameruntime-unix64"]  # type: ignore[index]
    assert other.read_bytes() == fixture["old_wine"]["xgameruntime-unix64"]  # type: ignore[index]


def test_rollback_preserves_an_unexpected_destination_and_restores_the_original(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    destination = fixture["user_root"] / XODUS_PAYLOADS[1]  # type: ignore[operator]
    unexpected = b"user replacement during validation\n"
    destination.write_bytes(unexpected)
    destination.chmod(0o755)

    run_tool(fixture, "rollback")

    assert destination.read_bytes() == fixture["old_user"][XODUS_PAYLOADS[1]]  # type: ignore[index]
    candidates = destination.parent.glob(
        ".forza-runtime-*-xodus-cli.recovery-unexpected*"
    )
    assert any(candidate.read_bytes() == unexpected for candidate in candidates)


def test_accept_rejects_installed_artifact_drift(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    destination = fixture["user_root"] / XODUS_PAYLOADS[1]  # type: ignore[operator]
    destination.write_bytes(b"drifted after install\n")
    destination.chmod(0o755)

    failed = run_tool(fixture, "accept", check=False)

    assert failed.returncode != 0
    assert "artifact drift" in failed.stderr.lower()
    assert "state=installed" in run_tool(fixture, "status").stdout


def test_source_drift_after_evidence_lock_is_rejected_before_staging(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    run_tool(fixture, "lock-evidence")
    source: Path = fixture["artifacts"]["xodus-cli"][0]  # type: ignore[index]
    source.write_bytes(b"changed build output\n")
    source.chmod(0o755)

    failed = run_tool(fixture, "plan", check=False)

    assert failed.returncode != 0
    assert "changed after evidence lock" in failed.stderr.lower()
    transaction_root = fixture["evidence"].parent  # type: ignore[union-attr]
    assert not any(path.is_dir() for path in transaction_root.iterdir())


def test_malformed_evidence_is_rejected_without_a_traceback(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    run_tool(fixture, "lock-evidence")
    evidence: Path = fixture["evidence"]  # type: ignore[assignment]
    evidence.write_text('{"version":1,"artifacts":"not-a-list"}\n')
    evidence.chmod(0o600)

    failed = run_tool(fixture, "plan", check=False)

    assert failed.returncode != 0
    assert "evidence" in failed.stderr.lower()
    assert "traceback" not in failed.stderr.lower()


def test_all_destinations_are_revalidated_before_the_first_mutation(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    marker = tmp_path / "preflight-ready"
    release = tmp_path / "preflight-release"
    fixture["env"].update(  # type: ignore[union-attr]
        {
            "FORZA_RUNTIME_PAUSE_AT": "before-preflight",
            "FORZA_RUNTIME_PAUSE_MARKER": str(marker),
            "FORZA_RUNTIME_PAUSE_RELEASE": str(release),
        }
    )
    process = subprocess.Popen(
        [
            str(TOOL),
            "install",
            *fixture["common"],  # type: ignore[list-item]
            "--plan-sha256",
            plan_digest,
        ],
        env=fixture["env"],  # type: ignore[arg-type]
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    destination = fixture["user_root"] / XODUS_PAYLOADS[-1]  # type: ignore[operator]
    external_change = b"external change during staging\n"
    destination.write_bytes(external_change)
    destination.chmod(0o755)
    release.touch()

    _, stderr = process.communicate(timeout=3)

    assert process.returncode != 0
    assert "before first mutation" in stderr.lower()
    assert destination.read_bytes() == external_change
    for role, wine_destination in fixture["wine_destinations"].items():  # type: ignore[union-attr]
        assert wine_destination.read_bytes() == fixture["old_wine"][role]  # type: ignore[index]
    assert "state=rolled_back" in run_tool(fixture, "status").stdout


def test_accepted_upgrade_remains_owned_by_secure_check_and_uninstall(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    run_tool(fixture, "accept")

    checked = run_secure_user_files(fixture, "check")
    assert checked.returncode == 0, checked.stderr
    removed = run_secure_user_files(fixture, "uninstall")
    assert removed.returncode == 0, removed.stderr
    assert all(not (fixture["user_root"] / relative).exists() for relative in PAYLOADS)  # type: ignore[operator]
    assert not (fixture["user_root"] / MANIFEST).exists()  # type: ignore[operator]


def test_cli_rejects_relative_and_noncanonical_evidence_paths_without_traceback(
    tmp_path: Path,
):
    fixture = setup_fixture(tmp_path)
    command = [str(TOOL), "status", *fixture["common"]]  # type: ignore[list-item]
    command[command.index("--compat-tool-root") + 1] = "relative"
    failed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert failed.returncode == 2
    assert "must be an absolute path" in failed.stderr
    assert "traceback" not in failed.stderr.lower()

    command = [str(TOOL), "status", *fixture["common"]]  # type: ignore[list-item]
    command[command.index("--artifact-evidence-manifest") + 1] = str(
        fixture["user_root"] / "evidence.json"  # type: ignore[operator]
    )
    failed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert failed.returncode == 2
    assert "must be exactly" in failed.stderr
    assert "traceback" not in failed.stderr.lower()


def test_existing_transaction_is_bound_to_its_original_roots(tmp_path: Path):
    fixture = setup_fixture(tmp_path)
    plan_digest = lock_and_plan(fixture)
    run_tool(fixture, "install", "--plan-sha256", plan_digest)
    alternate = tmp_path / "alternate-compat"
    alternate.mkdir()
    common: list[str] = fixture["common"]  # type: ignore[assignment]
    common[common.index("--compat-tool-root") + 1] = str(alternate)

    failed = run_tool(fixture, "accept", check=False)

    assert failed.returncode != 0
    assert "root arguments" in failed.stderr.lower()
    assert "state=installed" in run_tool(fixture, "status").stdout
