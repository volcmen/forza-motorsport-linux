# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import subprocess
from pathlib import Path

import pytest

from forza_bootstrap import proton
from forza_bootstrap.model import BootstrapError, canonical_json, sha256_bytes


def composed_fixture(root: Path, *, bytecode=False):
    root.mkdir()
    vdf = b'"compatibilitytools" { "compat_tools" { "GE-Proton11-3-FM" { "display_name" "GE-Proton11-3-FM" "install_path" "." } } }'
    (root / "compatibilitytool.vdf").write_bytes(vdf)
    for path in (
        *proton._MANAGED_PATCH_TARGETS.values(),
        *proton._MANAGED_RUNTIME_TARGETS.values(),
    ):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"composed binary: " + path.encode())
    (root / "proton").write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.dont_write_bytecode = True\n"
        "import os\nos.environ['PYTHONDONTWRITEBYTECODE'] = '1'\nimport helper\n"
    )
    (root / "proton").chmod(0o755)
    (root / "helper.py").write_text("value = 42\n")
    if bytecode:
        (root / "__pycache__").mkdir()
        (root / "__pycache__/helper.pyc").write_bytes(b"executable cache")
    fd = proton._open_directory(root, "fixture")
    try:
        payload = proton._tree_digest(fd)
        marker = canonical_json(
            {
                "schema": 2,
                "composition_profile": "fm-composed-v1",
                "name": "GE-Proton11-3-FM",
                "base_release": "GE-Proton11-3",
                "vdf_sha256": sha256_bytes(vdf),
                "managed_tree_sha256": payload,
                "managed_targets": proton._managed_target_hashes(fd),
            }
        )
    finally:
        os.close(fd)
    (root / ".forza-bootstrap.json").write_bytes(marker)
    return payload, sha256_bytes(marker)


def test_composed_tool_checkpoint_keeps_identity_and_rolls_back(tmp_path):
    source = tmp_path / "release"
    digests = composed_fixture(source)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    stage = staging / "tool"
    proton.prepare_composed_fm_tree(source, stage, *digests)
    assert (source / "helper.py").stat().st_ino != (stage / "helper.py").stat().st_ino
    destination = tmp_path / "GE-Proton11-3-FM"
    record = proton.publish_fm(stage, destination)
    subprocess.run([str(destination / "proton")], check=True)
    assert not (destination / "__pycache__").exists()
    assert (
        proton.inspect_composed_fm(destination, *digests).tree_sha256
        == record.published_tree_sha256
    )
    assert proton.rollback_published_fm(record) == proton.ToolDisposition.ABSENT
    assert source.is_dir()


def test_composed_tool_requires_independent_approved_identities(tmp_path):
    root = tmp_path / "release"
    payload, marker = composed_fixture(root)
    with pytest.raises(BootstrapError, match="approved release"):
        proton.inspect_composed_fm(root, "0" * 64, marker)
    with pytest.raises(BootstrapError, match="approved release"):
        proton.inspect_composed_fm(root, payload, "0" * 64)


def test_composed_tool_rejects_even_hash_approved_bytecode(tmp_path):
    root = tmp_path / "release"
    digests = composed_fixture(root, bytecode=True)
    with pytest.raises(BootstrapError, match="bytecode"):
        proton.inspect_composed_fm(root, *digests)


def test_unexpected_bytecode_prevents_rollback_and_is_preserved(tmp_path):
    source = tmp_path / "release"
    digests = composed_fixture(source)
    destination = tmp_path / "GE-Proton11-3-FM"
    record = proton.publish_fm(source, destination)
    unexpected = destination / "helper.pyc"
    unexpected.write_bytes(b"unexpected code")
    assert proton.rollback_published_fm(record) == proton.ToolDisposition.CONFLICT
    assert unexpected.read_bytes() == b"unexpected code"
    with pytest.raises(BootstrapError):
        proton.inspect_composed_fm(destination, *digests)


@pytest.mark.parametrize("mutation", ("profile", "missing-runtime", "unknown-schema"))
def test_composed_marker_rejects_unknown_contracts(tmp_path, mutation):
    root = tmp_path / "release"
    digests = composed_fixture(root)
    marker = root / ".forza-bootstrap.json"
    value = json.loads(marker.read_bytes())
    if mutation == "profile":
        value["composition_profile"] = "unreviewed"
    elif mutation == "missing-runtime":
        del value["managed_targets"][
            proton._MANAGED_RUNTIME_TARGETS["runtime/xgameruntime.so"]
        ]
    else:
        value["schema"] = 3
    marker.write_bytes(canonical_json(value))
    with pytest.raises(BootstrapError):
        proton.inspect_composed_fm(root, digests[0], sha256_bytes(marker.read_bytes()))
