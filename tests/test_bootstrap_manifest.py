# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest

from forza_bootstrap.manifest import load_bootstrap_manifest
from forza_bootstrap.model import (
    BootstrapError,
    canonical_json,
    plan_digest,
    sha256_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bootstrap"


def fixture_manifest_dict() -> dict[str, object]:
    return tomllib.loads((FIXTURES / "valid-bootstrap.toml").read_text(encoding="utf-8"))


def write_toml_fixture(tmp_path: Path, value: dict[str, object]) -> Path:
    def scalar(item: object) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, str):
            return json.dumps(item)
        if isinstance(item, list):
            return "[" + ", ".join(scalar(entry) for entry in item) + "]"
        raise TypeError(f"unsupported fixture value: {item!r}")

    lines: list[str] = []
    for key, item in value.items():
        is_table_array = isinstance(item, list) and item and isinstance(item[0], dict)
        if not isinstance(item, dict) and not is_table_array:
            lines.append(f"{key} = {scalar(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append("")
            lines.append(f"[{key}]")
            lines.extend(f"{field} = {scalar(entry)}" for field, entry in item.items())
        elif isinstance(item, list) and item and isinstance(item[0], dict):
            for entry in item:
                lines.append("")
                lines.append(f"[[{key}]]")
                lines.extend(f"{field} = {scalar(field_value)}" for field, field_value in entry.items())
    path = tmp_path / "bootstrap.toml"
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def test_valid_manifest_binds_exact_supported_stack() -> None:
    manifest = load_bootstrap_manifest(FIXTURES / "valid-bootstrap.toml")

    assert manifest.schema == 1
    assert manifest.app_id == "2440510"
    assert manifest.host == ("arch", "x86_64", "native-steam")
    assert manifest.protonup.version == "0.1.5"
    assert manifest.ge.release == "GE-Proton11-3"
    assert manifest.ge.size == 532_524_366
    assert manifest.ge.sha256 == "861c2edc8d40d051fb1e7a692deb953be52bd339c46d90f2b7dde50ddad91266"
    assert manifest.profile == "legacy-v0.1"
    assert manifest.sha256 == sha256_bytes((FIXTURES / "valid-bootstrap.toml").read_bytes())


def test_manifest_uses_repository_runtime_profile_not_an_adjacent_substitute(
    tmp_path: Path,
) -> None:
    value = fixture_manifest_dict()
    value["sources"][0]["revision"] = "0" * 40  # type: ignore[index]
    path = write_toml_fixture(tmp_path, value)
    (tmp_path / "runtime-profiles.toml").write_text(
        """version = 1
active = "legacy-v0.1"

[profiles."legacy-v0.1"]
status = "candidate"
xodus_revision = "0000000000000000000000000000000000000000"
xgameruntime_revision = "a1548b1cf57371715d10b608bc81a77a188e40d4"
""",
        encoding="ascii",
    )

    with pytest.raises(BootstrapError, match="runtime profile"):
        load_bootstrap_manifest(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema=True), "schema must be integer 1"),
        (lambda value: value.update(extra="value"), "unknown manifest key"),
        (lambda value: value["ge"].update(sha256="A" * 64), "lowercase SHA-256"),  # type: ignore[index]
        (lambda value: value["sources"][0].update(revision="short"), "40-character revision"),  # type: ignore[index]
        (lambda value: value["ge"].update(url="http://example.invalid/ge.tar.gz"), "HTTPS URL"),  # type: ignore[index]
        (lambda value: value["artifacts"][0].update(relative_path="../escape"), "safe relative path"),  # type: ignore[index]
        (lambda value: value.update(host=["arch", "x86_64", "flatpak-steam"]), "unsupported host"),
        (lambda value: value.update(profile="unknown"), "runtime profile"),
    ],
)
def test_manifest_rejects_ambiguous_or_unpinned_values(
    tmp_path: Path, mutation: object, message: str
) -> None:
    value = fixture_manifest_dict()
    mutation(value)  # type: ignore[operator]
    path = write_toml_fixture(tmp_path, value)

    with pytest.raises(BootstrapError, match=message):
        load_bootstrap_manifest(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", True, "schema must be integer 1"),
        ("size", True, "size must be an integer"),
        ("mode", True, "mode must be an integer"),
    ],
)
def test_manifest_rejects_booleans_in_integer_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    manifest = fixture_manifest_dict()
    target: dict[str, object] = manifest if field == "schema" else manifest["ge"] if field == "size" else manifest["artifacts"][0]  # type: ignore[assignment,index]
    target[field] = value

    with pytest.raises(BootstrapError, match=message):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


def test_manifest_rejects_duplicate_artifact_role_or_path(tmp_path: Path) -> None:
    manifest = fixture_manifest_dict()
    duplicate = copy.deepcopy(manifest["artifacts"][0])  # type: ignore[index]
    manifest["artifacts"].append(duplicate)  # type: ignore[index]

    with pytest.raises(BootstrapError, match="duplicate artifact"):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


def test_manifest_rejects_source_that_disagrees_with_active_profile(tmp_path: Path) -> None:
    manifest = fixture_manifest_dict()
    manifest["sources"][0]["revision"] = "0" * 40  # type: ignore[index]

    with pytest.raises(BootstrapError, match="runtime profile"):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


@pytest.mark.parametrize("alias", ("bin//xodus-service", "bin/./xodus-service", "bin/xodus-service/"))
def test_manifest_rejects_noncanonical_relative_paths(tmp_path: Path, alias: str) -> None:
    manifest = fixture_manifest_dict()
    manifest["artifacts"][0]["relative_path"] = alias  # type: ignore[index]

    with pytest.raises(BootstrapError, match="safe relative path"):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["protonup"].update(project_relative="vendor/protonup"),  # type: ignore[index]
        lambda value: value["ge"].update(name="GE-Proton11-4"),  # type: ignore[index]
        lambda value: value["ge"].update(url="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-4/GE-Proton11-3.tar.gz"),  # type: ignore[index]
        lambda value: value["ge"].update(filename="other.tar.gz"),  # type: ignore[index]
        lambda value: value["ge"].update(expected_root="other"),  # type: ignore[index]
        lambda value: value["ge"].update(size=1),  # type: ignore[index]
        lambda value: value["ge"].update(sha256="0" * 64),  # type: ignore[index]
        lambda value: value["sources"][0].update(repository="https://github.com/other/xodus"),  # type: ignore[index]
        lambda value: value["builder"].update(base_image="docker.io/library/archlinux:base-devel@sha256:" + "0" * 64),  # type: ignore[index]
        lambda value: value["builder"].update(arch_snapshot="https://archive.archlinux.org/repos/2026/09/02/$repo/os/$arch"),  # type: ignore[index]
    ],
)
def test_manifest_rejects_exact_pin_mutations(tmp_path: Path, mutation: object) -> None:
    manifest = fixture_manifest_dict()
    mutation(manifest)  # type: ignore[operator]

    with pytest.raises(BootstrapError, match="approved|runtime profile"):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


@pytest.mark.parametrize(
    "url",
    (
        "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-3/with space",
        "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-3/with\x01control",
        "https://[broken",
        "https://github.com:bad-port/GE-Proton11-3.tar.gz",
    ),
)
def test_manifest_rejects_malformed_urls_as_bootstrap_errors(tmp_path: Path, url: str) -> None:
    manifest = fixture_manifest_dict()
    manifest["ge"]["url"] = url  # type: ignore[index]

    with pytest.raises(BootstrapError, match="URL"):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


@pytest.mark.parametrize(
    ("target", "key", "expected"),
    [
        ("protonup", "unexpected", "unknown manifest key"),
        ("builder", "base_image", "missing manifest key"),
    ],
)
def test_manifest_rejects_nested_unknown_and_missing_keys(
    tmp_path: Path, target: str, key: str, expected: str
) -> None:
    manifest = fixture_manifest_dict()
    if expected.startswith("unknown"):
        manifest[target][key] = "value"  # type: ignore[index]
    else:
        del manifest[target][key]  # type: ignore[index]

    with pytest.raises(BootstrapError, match=expected):
        load_bootstrap_manifest(write_toml_fixture(tmp_path, manifest))


def test_canonical_plan_digest_is_stable_for_key_order() -> None:
    first = {"b": [2, 1], "a": {"z": "last", "x": "first"}}
    second = {"a": {"x": "first", "z": "last"}, "b": [2, 1]}

    assert canonical_json(first) == b'{"a":{"x":"first","z":"last"},"b":[2,1]}'
    assert plan_digest(first) == plan_digest(second)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_canonical_json_rejects_nonfinite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})
