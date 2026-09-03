# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BootstrapError(RuntimeError):
    """Raised when a bootstrap input cannot be safely trusted."""


def canonical_json(value: Any) -> bytes:
    """Return deterministic ASCII JSON suitable for hash-bound plans."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def plan_digest(plan: dict[str, Any]) -> str:
    """Return the digest the user must confirm for a canonical plan."""
    return sha256_bytes(canonical_json(plan))


@dataclass(frozen=True)
class ArtifactSpec:
    logical_name: str
    relative_path: str
    size: int
    sha256: str
    mode: int
    private: bool = False


@dataclass(frozen=True)
class DownloadSpec:
    name: str
    release: str
    url: str
    filename: str
    size: int
    sha256: str
    expected_root: str
    allowed_redirect_hosts: tuple[str, ...]


@dataclass(frozen=True)
class ProtonUpSpec:
    version: str
    project_relative: str


@dataclass(frozen=True)
class SourceSpec:
    name: str
    repository: str
    revision: str


@dataclass(frozen=True)
class BuilderSpec:
    image: str
    base_image: str
    arch_snapshot: str
    source_date_epoch: int


@dataclass(frozen=True)
class BootstrapManifest:
    schema: int
    app_id: str
    host: tuple[str, str, str]
    profile: str
    protonup: ProtonUpSpec
    ge: DownloadSpec
    bundle: DownloadSpec
    sources: tuple[SourceSpec, ...]
    builder: BuilderSpec
    artifacts: tuple[ArtifactSpec, ...]
    path: Path
    sha256: str
