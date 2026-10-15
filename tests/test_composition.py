# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import shutil

import pytest
from test_composed_proton import composed_fixture

from forza_bootstrap import composition
from forza_bootstrap.model import BootstrapError


class PowerLoss(BaseException):
    pass


def publication(tmp_path):
    transaction = tmp_path / "transaction"
    transaction.mkdir(mode=0o700)
    transaction_id = "b" * 24
    parent = tmp_path / f".forza-staging-{transaction_id}"
    parent.mkdir(mode=0o700)
    stage = parent / "tool"
    payload, marker = composed_fixture(stage)
    return (
        transaction,
        transaction_id,
        "c" * 64,
        stage,
        tmp_path / "GE-Proton11-3-FM",
        payload,
        marker,
    )


@pytest.mark.parametrize(
    "boundary", ("publication-intent-durable", "publication-renamed")
)
def test_resume_after_power_loss_keeps_pre_rename_ownership(tmp_path, boundary):
    args = publication(tmp_path)
    inode = args[3].stat().st_ino

    def interrupt(name):
        if name == boundary:
            raise PowerLoss()

    with pytest.raises(PowerLoss):
        composition.publish_composed(*args, boundary=interrupt)
    record = composition.publish_composed(*args)
    assert record.root_inode == inode
    assert args[4].stat().st_ino == inode
    composition.rollback_composed(*args)
    composition.rollback_composed(*args)
    assert not args[4].exists()
    with pytest.raises(BootstrapError, match="rolled back"):
        composition.publish_composed(*args)


def test_identical_foreign_tool_is_never_adopted_or_removed(tmp_path):
    args = publication(tmp_path)
    shutil.copytree(args[3], args[4])
    inode = args[4].stat().st_ino
    with pytest.raises(BootstrapError, match="existing tool"):
        composition.publish_composed(*args)
    assert args[4].stat().st_ino == inode
    assert not (args[0] / "tool-publication.json").exists()


def test_replacement_by_identical_tree_cannot_claim_transaction_ownership(tmp_path):
    args = publication(tmp_path)
    composition.publish_composed(*args)
    args[4].rename(tmp_path / "preserved-original")
    shutil.copytree(tmp_path / "preserved-original", args[4])
    with pytest.raises(BootstrapError, match="ownership"):
        composition.publish_composed(*args)
    with pytest.raises(BootstrapError, match="ownership"):
        composition.rollback_composed(*args)
    assert args[4].is_dir()
    assert (tmp_path / "preserved-original").is_dir()


def test_changed_payload_prevents_recovery_and_preserves_evidence(tmp_path):
    args = publication(tmp_path)
    composition.publish_composed(*args)
    changed = args[4] / "helper.py"
    changed.write_text("changed = True\n")
    before = (args[0] / "tool-publication.json").read_bytes()
    with pytest.raises(BootstrapError):
        composition.rollback_composed(*args)
    assert changed.read_text() == "changed = True\n"
    assert (args[0] / "tool-publication.json").read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    (
        ("schema", True),
        ("schema", 1),
        ("transaction_id", "d" * 24),
        ("manifest_sha256", "f" * 64),
        ("phase", "accepted"),
        ("root_inode", False),
        ("destination", "/unowned/tool"),
        ("extra_authority", "/unowned"),
    ),
)
def test_journal_tampering_is_rejected_without_writes(tmp_path, field, value):
    args = publication(tmp_path)
    composition.publish_composed(*args)
    journal = args[0] / "tool-publication.json"
    data = json.loads(journal.read_bytes())
    data[field] = value
    journal.write_text(json.dumps(data))
    before = journal.read_bytes()
    with pytest.raises(BootstrapError, match="journal"):
        composition.rollback_composed(*args)
    assert journal.read_bytes() == before
    assert args[4].is_dir()


def test_unpublished_rollback_preserves_stage(tmp_path):
    args = publication(tmp_path)

    def interrupt(_):
        raise PowerLoss()

    with pytest.raises(PowerLoss):
        composition.publish_composed(*args, boundary=interrupt)
    composition.rollback_composed(*args)
    assert args[3].is_dir()
    assert not args[4].exists()


@pytest.mark.parametrize("boundary", ("rollback-intent-durable", "rollback-renamed"))
def test_interrupted_rollback_retains_payload_and_resumes(tmp_path, boundary):
    args = publication(tmp_path)
    composition.publish_composed(*args)
    inode = args[4].stat().st_ino

    def interrupt(name):
        if name == boundary:
            raise PowerLoss()

    with pytest.raises(PowerLoss):
        composition.rollback_composed(*args, boundary=interrupt)
    with pytest.raises(BootstrapError, match="rolled back"):
        composition.publish_composed(*args)
    composition.rollback_composed(*args)
    assert not args[4].exists()
    assert args[3].stat().st_ino == inode
