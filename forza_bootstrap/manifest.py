# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    BuilderSpec,
    DownloadSpec,
    ProtonUpSpec,
    SourceSpec,
    sha256_bytes,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_ROOT_KEYS = frozenset(
    {
        "schema",
        "app_id",
        "host",
        "profile",
        "protonup",
        "ge",
        "bundle",
        "sources",
        "builder",
        "artifacts",
    }
)
_DOWNLOAD_KEYS = frozenset(
    {
        "name",
        "release",
        "url",
        "filename",
        "size",
        "sha256",
        "expected_root",
        "allowed_redirect_hosts",
    }
)
_PROTONUP_KEYS = frozenset({"version", "project_relative"})
_SOURCE_KEYS = frozenset({"name", "repository", "revision"})
_BUILDER_KEYS = frozenset({"image", "base_image", "arch_snapshot", "source_date_epoch"})
_ARTIFACT_KEYS = frozenset(
    {"logical_name", "relative_path", "size", "sha256", "mode", "private"}
)
_SUPPORTED_HOST = ("arch", "x86_64", "native-steam")
_APP_ID = "2440510"
_PROTONUP_VERSION = "0.1.5"


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise BootstrapError(f"{name} must be a table")
    return value


def _keys(value: Mapping[str, object], expected: frozenset[str], name: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise BootstrapError(f"unknown manifest key in {name}: {min(unknown)}")
    if missing:
        raise BootstrapError(f"missing manifest key in {name}: {min(missing)}")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BootstrapError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BootstrapError(f"{name} must be an integer")
    return value


def _sha256(value: object, name: str) -> str:
    digest = _string(value, name)
    if not _SHA256.fullmatch(digest):
        raise BootstrapError(f"{name} must be a lowercase SHA-256")
    return digest


def _safe_relative(value: object, name: str) -> str:
    path = _string(value, name)
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
        raise BootstrapError(f"{name} must be a safe relative path")
    return path


def _https_url(value: object, name: str) -> str:
    url = _string(value, name)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BootstrapError(f"{name} must be an HTTPS URL")
    return url


def _redirect_hosts(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise BootstrapError(f"{name} must be a non-empty array")
    hosts = tuple(_string(host, name) for host in value)
    if len(set(hosts)) != len(hosts):
        raise BootstrapError(f"{name} contains duplicate hosts")
    for host in hosts:
        parsed = urlparse(f"https://{host}")
        if parsed.hostname != host or parsed.port is not None:
            raise BootstrapError(f"{name} contains an invalid host")
    return hosts


def _download(value: object, name: str) -> DownloadSpec:
    table = _mapping(value, name)
    _keys(table, _DOWNLOAD_KEYS, name)
    size = _integer(table["size"], "size")
    if size < 0:
        raise BootstrapError("size must be non-negative")
    url = _https_url(table["url"], "url")
    hosts = _redirect_hosts(table["allowed_redirect_hosts"], "allowed_redirect_hosts")
    if urlparse(url).hostname not in hosts:
        raise BootstrapError("download URL host must be an allowed redirect host")
    return DownloadSpec(
        name=_string(table["name"], "name"),
        release=_string(table["release"], "release"),
        url=url,
        filename=_safe_relative(table["filename"], "filename"),
        size=size,
        sha256=_sha256(table["sha256"], "sha256"),
        expected_root=_safe_relative(table["expected_root"], "expected_root"),
        allowed_redirect_hosts=hosts,
    )


def _protonup(value: object) -> ProtonUpSpec:
    table = _mapping(value, "protonup")
    _keys(table, _PROTONUP_KEYS, "protonup")
    version = _string(table["version"], "protonup.version")
    if version != _PROTONUP_VERSION:
        raise BootstrapError(f"unsupported ProtonUp version: {version}")
    return ProtonUpSpec(version=version, project_relative=_safe_relative(table["project_relative"], "project_relative"))


def _sources(value: object, expected_revisions: Mapping[str, str]) -> tuple[SourceSpec, ...]:
    if not isinstance(value, list) or not value:
        raise BootstrapError("sources must be a non-empty array")
    sources: list[SourceSpec] = []
    names: set[str] = set()
    for item in value:
        table = _mapping(item, "sources")
        _keys(table, _SOURCE_KEYS, "sources")
        name = _string(table["name"], "source name")
        revision = _string(table["revision"], "revision")
        if not _REVISION.fullmatch(revision):
            raise BootstrapError("revision must be a lowercase 40-character revision")
        if name in names:
            raise BootstrapError(f"duplicate source name: {name}")
        names.add(name)
        repository = _https_url(table["repository"], "repository")
        sources.append(SourceSpec(name=name, repository=repository, revision=revision))
    actual_revisions = {source.name: source.revision for source in sources}
    if actual_revisions != dict(expected_revisions):
        raise BootstrapError("sources disagree with the active runtime profile")
    return tuple(sources)


def _builder(value: object) -> BuilderSpec:
    table = _mapping(value, "builder")
    _keys(table, _BUILDER_KEYS, "builder")
    image = _string(table["image"], "builder.image")
    base_image = _string(table["base_image"], "builder.base_image")
    for name, image_value in (("builder.image", image), ("builder.base_image", base_image)):
        if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image_value):
            raise BootstrapError(f"{name} must be pinned by lowercase SHA-256 digest")
    epoch = _integer(table["source_date_epoch"], "source_date_epoch")
    if epoch < 0:
        raise BootstrapError("source_date_epoch must be non-negative")
    return BuilderSpec(
        image=image,
        base_image=base_image,
        arch_snapshot=_https_url(table["arch_snapshot"], "arch_snapshot"),
        source_date_epoch=epoch,
    )


def _artifacts(value: object) -> tuple[ArtifactSpec, ...]:
    if not isinstance(value, list) or not value:
        raise BootstrapError("artifacts must be a non-empty array")
    artifacts: list[ArtifactSpec] = []
    names: set[str] = set()
    paths: set[str] = set()
    for item in value:
        table = _mapping(item, "artifacts")
        _keys(table, _ARTIFACT_KEYS, "artifacts")
        logical_name = _string(table["logical_name"], "logical_name")
        relative_path = _safe_relative(table["relative_path"], "relative_path")
        if logical_name in names or relative_path in paths:
            raise BootstrapError("duplicate artifact role or path")
        names.add(logical_name)
        paths.add(relative_path)
        size = _integer(table["size"], "size")
        mode = _integer(table["mode"], "mode")
        private = table["private"]
        if not isinstance(private, bool):
            raise BootstrapError("private must be a boolean")
        if size < 0 or mode < 0 or mode > 0o777:
            raise BootstrapError("artifact size or mode is invalid")
        artifacts.append(
            ArtifactSpec(
                logical_name=logical_name,
                relative_path=relative_path,
                size=size,
                sha256=_sha256(table["sha256"], "sha256"),
                mode=mode,
                private=private,
            )
        )
    return tuple(artifacts)


def _runtime_profile(manifest_path: Path, profile: str) -> Mapping[str, str]:
    runtime_path = manifest_path.parent / "runtime-profiles.toml"
    if not runtime_path.is_file():
        runtime_path = Path(__file__).parents[1] / "manifests" / "runtime-profiles.toml"
    try:
        data = tomllib.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BootstrapError("runtime profile manifest is unavailable") from error
    if set(data) != {"version", "active", "profiles"}:
        raise BootstrapError("runtime profile manifest is invalid")
    version = data["version"]
    active = data["active"]
    profiles = data["profiles"]
    if isinstance(version, bool) or version != 1 or not isinstance(active, str) or not isinstance(profiles, dict):
        raise BootstrapError("runtime profile manifest is invalid")
    if profile != active:
        raise BootstrapError("manifest profile disagrees with the active runtime profile")
    candidate = profiles.get(profile)
    if not isinstance(candidate, dict) or candidate.get("status") != "candidate":
        raise BootstrapError("runtime profile is not installable")
    expected = {
        "xodus": candidate.get("xodus_revision"),
        "xgameruntime": candidate.get("xgameruntime_revision"),
    }
    if any(not isinstance(revision, str) or not _REVISION.fullmatch(revision) for revision in expected.values()):
        raise BootstrapError("runtime profile is invalid")
    return expected  # type: ignore[return-value]


def load_bootstrap_manifest(path: Path) -> BootstrapManifest:
    """Load one complete, exact bootstrap trust manifest."""
    try:
        raw = path.read_bytes()
        data: Any = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise BootstrapError(f"cannot load bootstrap manifest: {path}") from error
    root = _mapping(data, "manifest")
    _keys(root, _ROOT_KEYS, "manifest")
    schema = root["schema"]
    if isinstance(schema, bool) or schema != 1:
        raise BootstrapError("schema must be integer 1")
    app_id = _string(root["app_id"], "app_id")
    if app_id != _APP_ID:
        raise BootstrapError(f"unsupported AppID: {app_id}")
    host_value = root["host"]
    if not isinstance(host_value, list) or len(host_value) != 3 or not all(
        isinstance(item, str) for item in host_value
    ):
        raise BootstrapError("host must be a three-part string array")
    host = tuple(host_value)
    if host != _SUPPORTED_HOST:
        raise BootstrapError("unsupported host")
    profile = _string(root["profile"], "profile")
    expected_revisions = _runtime_profile(path, profile)
    ge = _download(root["ge"], "ge")
    if ge.release != "GE-Proton11-3":
        raise BootstrapError(f"unsupported GE-Proton release: {ge.release}")
    return BootstrapManifest(
        schema=schema,
        app_id=app_id,
        host=host,
        profile=profile,
        protonup=_protonup(root["protonup"]),
        ge=ge,
        bundle=_download(root["bundle"], "bundle"),
        sources=_sources(root["sources"], expected_revisions),
        builder=_builder(root["builder"]),
        artifacts=_artifacts(root["artifacts"]),
        path=path,
        sha256=sha256_bytes(raw),
    )
