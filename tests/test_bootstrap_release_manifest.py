# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from forza_bootstrap.container_build import build_fixture_bundle
from forza_bootstrap.manifest import load_bootstrap_manifest
from forza_bootstrap.model import ArtifactSpec, BootstrapError
from forza_bootstrap.release import (
    ReleaseBundle,
    inspect_release_bundle,
    render_bootstrap_manifest,
)

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "manifests/bootstrap-v1.toml"
WORKFLOW = ROOT / ".github/workflows/build-bundle.yml"
EXPECTED_ARTIFACTS = {
    "xodus-service": ("bin/xodus-service", 0o755),
    "xodus-cli": ("bin/xodus-cli", 0o755),
    "xodus-overlay": ("bin/xodus-overlay", 0o755),
    "xgameruntime-pe64": ("runtime/xgameruntime.dll", 0o644),
    "xgameruntime-unix64": ("runtime/xgameruntime.so", 0o755),
}
BUILDER_IMAGE = (
    "ghcr.io/volcmen/forza-motorsport-builder@sha256:" + "a" * 64
)


def test_production_bootstrap_manifest_has_no_mutable_or_empty_release_fields() -> None:
    manifest = load_bootstrap_manifest(MANIFEST)

    assert manifest.bundle.release == "v0.2.0"
    assert manifest.bundle.url == (
        "https://github.com/volcmen/forza-motorsport-linux/releases/download/"
        "v0.2.0/forza-bootstrap-bundle-v1.tar.zst"
    )
    assert manifest.bundle.filename == "forza-bootstrap-bundle-v1.tar.zst"
    assert manifest.bundle.size > 0
    assert re.fullmatch(r"[0-9a-f]{64}", manifest.bundle.sha256)
    assert manifest.ge.size == 532_524_366
    assert manifest.ge.sha256 == (
        "861c2edc8d40d051fb1e7a692deb953be52bd339c46d90f2b7dde50ddad91266"
    )
    assert manifest.protonup.version == "0.1.5"
    assert manifest.builder.base_image == (
        "docker.io/library/archlinux:base-devel@sha256:"
        "694da1fce635e3a14d90751941b08f02c500e8724682f7a00768e7152251ec34"
    )
    assert re.fullmatch(
        r"ghcr\.io/volcmen/forza-motorsport-builder@sha256:[0-9a-f]{64}",
        manifest.builder.image,
    )
    assert {source.revision for source in manifest.sources} == {
        "7b236772297b3475ea4f3cb830feb5b224f3064a",
        "a1548b1cf57371715d10b608bc81a77a188e40d4",
    }
    assert {
        artifact.logical_name: (artifact.relative_path, artifact.mode)
        for artifact in manifest.artifacts
    } == EXPECTED_ARTIFACTS
    assert all(artifact.size > 0 for artifact in manifest.artifacts)
    assert all(re.fullmatch(r"[0-9a-f]{64}", artifact.sha256) for artifact in manifest.artifacts)
    assert all(artifact.private is False for artifact in manifest.artifacts)


def test_release_bundle_is_not_tracked_by_git() -> None:
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .split("\0")
    )
    assert not [name for name in tracked if name.endswith((".dll", ".so", ".tar.zst"))]


def test_bundle_reproduction_workflow_is_source_only_and_secret_free() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "dockerd-rootless-setuptool.sh install --force" in workflow
    assert "actions/upload-artifact@" in workflow
    assert "./tools/build-bootstrap-bundle --clean" in workflow
    assert workflow.count("./tools/build-bootstrap-bundle --clean") == 2
    assert "cmp " in workflow
    assert "./tools/verify-bootstrap-bundle" in workflow
    assert "docker push" not in workflow
    assert "ghcr.io" not in workflow
    assert "secrets." not in workflow
    assert "steam" not in workflow.lower()
    assert "xbox" not in workflow.lower()
    assert "forza-linux" not in workflow


def test_release_finalizer_has_a_source_only_cli() -> None:
    result = subprocess.run(
        [str(ROOT / "tools/finalize-bootstrap-manifest"), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--bundle" in result.stdout
    assert "--matching-bundle" in result.stdout
    assert "--builder-image" in result.stdout
    assert "--output" in result.stdout
    assert "--push" not in result.stdout


def test_manifest_renderer_produces_a_strictly_loadable_trust_root(tmp_path: Path) -> None:
    artifacts = tuple(
        ArtifactSpec(name, path, index + 1, f"{index + 1:064x}", mode, False)
        for index, (name, (path, mode)) in enumerate(EXPECTED_ARTIFACTS.items())
    )
    release = ReleaseBundle(1234, "f" * 64, "e" * 64, artifacts)
    output = tmp_path / "bootstrap-v1.toml"

    output.write_bytes(render_bootstrap_manifest(release, BUILDER_IMAGE))
    manifest = load_bootstrap_manifest(output)

    assert manifest.builder.image == BUILDER_IMAGE
    assert manifest.bundle.size == 1234
    assert manifest.bundle.sha256 == "f" * 64
    assert manifest.artifacts == artifacts


def test_release_inspector_rejects_nonproduction_fixture_provenance(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    archive = build_fixture_bundle(output)

    with pytest.raises(BootstrapError, match="bundle manifest is invalid"):
        inspect_release_bundle(archive.resolve(), archive.resolve(), BUILDER_IMAGE)
