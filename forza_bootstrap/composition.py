# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Durable publication of a complete Proton payload before a Steam checkpoint.

This is the version 2 publication primitive, not an alternate legacy installer.
The parent coordinator must hold the shared launcher lease and supply its own
transaction directory and independently approved release digests. Publication
never adopts an existing tool, even when its content matches the release.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from . import proton
from .model import BootstrapError
from .safeio import (
    acquire_bootstrap_lock,
    atomic_write_private_json,
    open_owned_root,
    read_private_json,
    rename_noreplace,
)

_JOURNAL = "tool-publication.json"
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_TRANSACTION = re.compile(r"[a-f0-9]{24}\Z")
_KEYS = {
    "schema",
    "transaction_id",
    "manifest_sha256",
    "payload_sha256",
    "marker_sha256",
    "phase",
    "stage",
    "destination",
    "root_device",
    "root_inode",
    "tree_sha256",
}


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _record(value: dict, path: Path) -> proton.PublishedToolRecord:
    inspection = proton.inspect_composed_fm(
        path, value["payload_sha256"], value["marker_sha256"]
    )
    info = path.lstat()
    if (
        (info.st_dev, info.st_ino) != (value["root_device"], value["root_inode"])
        or inspection.tree_sha256 != value["tree_sha256"]
        or inspection.identity is None
    ):
        raise BootstrapError("composed publication ownership or contents changed")
    return proton.PublishedToolRecord(
        Path(value["destination"]),
        proton.ToolDisposition.CREATED,
        inspection.identity,
        info.st_dev,
        info.st_ino,
        inspection.tree_sha256,
    )


def _validate(value: object, expected: dict) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != _KEYS
        or type(value.get("schema")) is not int
        or value["schema"] != 2
        or value.get("phase")
        not in {"planned", "published", "rolling_back", "rolled_back"}
        or any(value.get(key) != item for key, item in expected.items())
        or any(
            type(value.get(key)) is not int or value[key] <= 0
            for key in ("root_device", "root_inode")
        )
        or not isinstance(value.get("tree_sha256"), str)
        or _SHA256.fullmatch(value["tree_sha256"]) is None
    ):
        raise BootstrapError("composed publication journal is invalid")
    return value


def _expected(
    transaction_id: str,
    manifest_sha256: str,
    stage: Path,
    destination: Path,
    payload_sha256: str,
    marker_sha256: str,
) -> dict:
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION.fullmatch(transaction_id) is None
    ):
        raise BootstrapError("composed parent transaction is invalid")
    for digest in (manifest_sha256, payload_sha256, marker_sha256):
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise BootstrapError("composed release identity is invalid")
    for path in (stage, destination):
        proton._path_parts(path, "composed publication")
    if (
        destination.name != "GE-Proton11-3-FM"
        or stage.parent.parent != destination.parent
        or stage.parent.name != f".forza-staging-{transaction_id}"
        or stage.name != "tool"
    ):
        raise BootstrapError(
            "composed stage must be private on the destination filesystem"
        )
    return {
        "transaction_id": transaction_id,
        "manifest_sha256": manifest_sha256,
        "payload_sha256": payload_sha256,
        "marker_sha256": marker_sha256,
        "stage": str(stage),
        "destination": str(destination),
    }


def publish_composed(
    transaction_dir: Path,
    transaction_id: str,
    manifest_sha256: str,
    stage: Path,
    destination: Path,
    payload_sha256: str,
    marker_sha256: str,
    *,
    boundary: Callable[[str], None] = lambda _: None,
) -> proton.PublishedToolRecord:
    """Persist ownership before rename, so a crash cannot lose provenance."""
    expected = _expected(
        transaction_id,
        manifest_sha256,
        stage,
        destination,
        payload_sha256,
        marker_sha256,
    )
    root_fd = open_owned_root(transaction_dir)
    lock_fd = acquire_bootstrap_lock(transaction_dir)
    try:
        try:
            value = _validate(read_private_json(root_fd, _JOURNAL), expected)
        except FileNotFoundError:
            if _exists(destination):
                raise BootstrapError(
                    "composed publication refuses an existing tool"
                ) from None
            inspection = proton.inspect_composed_fm(
                stage, payload_sha256, marker_sha256
            )
            stage_parent_fd = open_owned_root(stage.parent)
            parent_fd = proton._open_directory(destination.parent, "publication parent")
            try:
                info = stage.lstat()
                if info.st_dev != os.fstat(parent_fd).st_dev:
                    raise BootstrapError("composed publication must use one filesystem")
                if not proton._name_matches_directory(
                    parent_fd, stage.parent.name, stage_parent_fd
                ):
                    raise BootstrapError("composed staging directory changed")
            finally:
                os.close(parent_fd)
                os.close(stage_parent_fd)
            value = {
                "schema": 2,
                **expected,
                "phase": "planned",
                "root_device": info.st_dev,
                "root_inode": info.st_ino,
                "tree_sha256": inspection.tree_sha256,
            }
            _record(value, stage)
            atomic_write_private_json(root_fd, _JOURNAL, value)
        if value["phase"] in {"rolling_back", "rolled_back"}:
            raise BootstrapError(
                "composed publication is being or has been rolled back"
            )
        boundary("publication-intent-durable")
        if _exists(destination):
            if _exists(stage):
                raise BootstrapError(
                    "composed publication has conflicting stage and destination"
                )
            record = _record(value, destination)
        else:
            if value["phase"] != "planned":
                raise BootstrapError("published composed tool is missing")
            _record(value, stage)
            record = proton.publish_fm(stage, destination)
            if record.disposition is not proton.ToolDisposition.CREATED:
                raise BootstrapError("composed publication refuses adoption")
            # Rebind to the durable pre-rename inode, not merely the tool marker.
            _record(value, destination)
            boundary("publication-renamed")
        value["phase"] = "published"
        atomic_write_private_json(root_fd, _JOURNAL, value)
        return record
    finally:
        os.close(lock_fd)
        os.close(root_fd)


def inspect_publication(
    transaction_dir: Path,
    transaction_id: str,
    manifest_sha256: str,
    stage: Path,
    destination: Path,
    payload_sha256: str,
    marker_sha256: str,
) -> proton.PublishedToolRecord:
    """Validate ownership without writing, while the parent holds its lease."""
    expected = _expected(
        transaction_id,
        manifest_sha256,
        stage,
        destination,
        payload_sha256,
        marker_sha256,
    )
    root_fd = open_owned_root(transaction_dir)
    try:
        value = _validate(read_private_json(root_fd, _JOURNAL), expected)
    finally:
        os.close(root_fd)
    if _exists(destination):
        if _exists(stage) or value["phase"] == "rolled_back":
            raise BootstrapError("composed publication has conflicting paths or state")
        return _record(value, destination)
    if not _exists(stage) or value["phase"] == "published":
        raise BootstrapError("composed publication lost its recorded tool")
    return _record(value, stage)


def rollback_composed(
    transaction_dir: Path,
    transaction_id: str,
    manifest_sha256: str,
    stage: Path,
    destination: Path,
    payload_sha256: str,
    marker_sha256: str,
    *,
    boundary: Callable[[str], None] = lambda _: None,
) -> None:
    """Move the exact owned tool back to its private stage; retain recovery data."""
    expected = _expected(
        transaction_id,
        manifest_sha256,
        stage,
        destination,
        payload_sha256,
        marker_sha256,
    )
    root_fd = open_owned_root(transaction_dir)
    lock_fd = acquire_bootstrap_lock(transaction_dir)
    try:
        value = _validate(read_private_json(root_fd, _JOURNAL), expected)
        if value["phase"] == "rolled_back":
            return
        if _exists(destination):
            if _exists(stage):
                raise BootstrapError(
                    "composed publication has conflicting stage and destination"
                )
            _record(value, destination)
            value["phase"] = "rolling_back"
            atomic_write_private_json(root_fd, _JOURNAL, value)
            boundary("rollback-intent-durable")
            stage_parent_fd = open_owned_root(stage.parent)
            parent_fd = proton._open_directory(destination.parent, "publication parent")
            try:
                _record(value, destination)
                if not proton._name_matches_directory(
                    parent_fd, stage.parent.name, stage_parent_fd
                ):
                    raise BootstrapError("composed staging directory changed")
                rename_noreplace(
                    parent_fd, destination.name, stage_parent_fd, stage.name
                )
                os.fsync(parent_fd)
                os.fsync(stage_parent_fd)
                _record(value, stage)
            finally:
                os.close(parent_fd)
                os.close(stage_parent_fd)
            boundary("rollback-renamed")
        elif _exists(stage):
            if value["phase"] not in {"planned", "rolling_back"}:
                raise BootstrapError("published composed tool is missing")
            _record(
                value, stage
            )  # Leave the unpublished private stage as recovery evidence.
        else:
            # Rename may have happened, but neither name can prove what followed.
            raise BootstrapError("composed publication lost both recorded paths")
        value["phase"] = "rolled_back"
        atomic_write_private_json(root_fd, _JOURNAL, value)
    finally:
        os.close(lock_fd)
        os.close(root_fd)
