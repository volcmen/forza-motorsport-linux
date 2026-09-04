import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
PUBLICATION_MANIFEST = ROOT / "docs/publication-manifest.toml"
PUBLICATION_STATES = {"verified", "experimental", "rejected", "unresolved"}
REQUIRED_CLAIMS = {
    "game-launch",
    "online-profile-content",
    "controller-navigation-driving",
    "controller-hotplug",
    "ap702-absent",
    "linux-to-windows-invite",
    "linux-join-windows-activity",
    "social-ui-keyboard-controller",
    "xodus-clean-shutdown",
    "repeat-launch-no-reboot",
    "incoming-invite-notifications",
}
FORBIDDEN_SUFFIXES = {".dll", ".exe", ".sys", ".so"}
FORBIDDEN_TEXT = ("authorization: " + "xbl3.0 x=", "proof_" + "private_key=")
FORBIDDEN_RELEASE_SUFFIXES = (".dll", ".so", ".tar.zst")
PUBLIC_COMPONENTS = {
    "xodus_legacy": (
        "https://github.com/volcmen/xodus",
        "forza-social-invite-join-v0.1",
        "7b236772297b3475ea4f3cb830feb5b224f3064a",
    ),
    "xgameruntime_legacy": (
        "https://github.com/volcmen/wine-forza-motorsport",
        "forza-xgameui-invite-join-v0.1",
        "a1548b1cf57371715d10b608bc81a77a188e40d4",
    ),
    "wine_controller": (
        "https://github.com/volcmen/wine-forza-motorsport",
        "wgi-physical-nonroamable-id",
        "ddd302d97c6008d79ea4f3e3ad56014cb548e514",
    ),
    "wine_storage": (
        "https://github.com/volcmen/wine-forza-motorsport",
        "storage-trim-property",
        "a7719bd8d0719e5ea6061387db020fa6a6b39d27",
    ),
    "winegdk_next": (
        "https://github.com/volcmen/WineGDK",
        "xodus-social-invite-bridge-experimental",
        "d96a768e25f632b04a457e4cb9f585e89ef5d095",
    ),
    "xodus_next": (
        "https://github.com/volcmen/xodus",
        "xodus-social-invite-bridge",
        "ee9db0f68122a9731b9b66cc24767693d1737f5c",
    ),
}


def load_publication_manifest():
    return tomllib.loads(PUBLICATION_MANIFEST.read_text(encoding="utf-8"))


def test_publication_manifest_pins_working_legacy_xodus():
    component = load_publication_manifest()["components"]["xodus_legacy"]
    assert component == {
        "state": "verified",
        "revision": "7b236772297b3475ea4f3cb830feb5b224f3064a",
        "evidence": "docs/evidence/v0.1-xodus.md",
        "repository": "https://github.com/volcmen/xodus",
        "branch": "forza-social-invite-join-v0.1",
        "source_url": (
            "https://github.com/volcmen/xodus/commit/"
            "7b236772297b3475ea4f3cb830feb5b224f3064a"
        ),
    }


def test_publication_manifest_links_exact_public_component_sources():
    data = load_publication_manifest()

    assert data["integration_repository"] == (
        "https://github.com/volcmen/forza-motorsport-linux"
    )
    for name, (repository, branch, revision) in PUBLIC_COMPONENTS.items():
        component = data["components"][name]
        assert component["repository"] == repository
        assert component["branch"] == branch
        assert component["revision"] == revision
        assert component["source_url"] == f"{repository}/commit/{revision}"


def test_publication_manifest_pins_reviewed_clean_builds():
    artifacts = {
        artifact["name"]: artifact["sha256"]
        for artifact in load_publication_manifest()["artifacts"]
    }

    assert artifacts["xodus-service-clean-build"] == (
        "07ffad8fea3dd66e8351b6b5b004e35b565e6f7f10ce9cbc537dcbd7b172c674"
    )
    assert artifacts["xodus-cli-clean-build"] == (
        "dfe2a638e7e6edeeca00d344f429395ec84a3d3c8a762a268c5ea753c6e28049"
    )
    assert artifacts["xodus-overlay-clean-build"] == (
        "3227377e92b84326c1b4686b5d0fcf129ee0c6dc92645b87c9c0f9fc06514de7"
    )
    assert artifacts["xgameruntime.dll-clean-build"] == (
        "c510d0e6c1db38af636f097fb000a90c578ca1a2bebd93d0ee939e95dd587d38"
    )
    assert artifacts["xgameruntime.so-clean-build"] == (
        "251f392cab1feacf2b23be051fa2ae53f056484bc8bf8e7a6c33de3717fe28b9"
    )


def _is_prohibited_binary(path):
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    with path.open("rb") as stream:
        return stream.read(2) == b"MZ"


def test_repository_contains_no_prohibited_binaries_or_live_secrets():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode().split("\0")
    paths = [ROOT / name for name in names if name]
    assert not [path for path in paths if _is_prohibited_binary(path)]
    findings = []
    for path in paths:
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        text = path.read_text(errors="ignore").lower()
        findings.extend((path, token) for token in FORBIDDEN_TEXT if token in text)
    assert findings == []


def test_release_artifacts_remain_untracked_and_runtime_local():
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT
    ).decode().split("\0")

    assert not [
        name for name in tracked if name.endswith(FORBIDDEN_RELEASE_SUFFIXES)
    ]
    assert "runtime/" in (ROOT / ".gitignore").read_text().splitlines()


def test_repository_policy_rejects_renamed_pe_binary(tmp_path):
    renamed = tmp_path / "runtime.dll.threading"
    renamed.write_bytes(b"MZ" + b"\0" * 16)
    assert _is_prohibited_binary(renamed)


def test_declared_licenses_exist():
    assert (ROOT / "LICENSES/GPL-3.0-or-later.txt").is_file()


def test_readme_names_supported_and_unsupported_boundaries():
    readme = (ROOT / "README.md").read_text()
    required = {
        "Tested in-game",
        "v0.2.0 setup has now been tested in-game on my system",
        "AppID 2440510",
        "legacy-v0.1",
        "7b236772297b3475ea4f3cb830feb5b224f3064a",
        "Verified live matrix",
        "Rejected live experiment",
        "KDE Wallet",
        "Do not install GNOME Keyring",
        "Microsoft binaries are not distributed",
        "Incoming invite notifications are not supported",
        "source-only",
        "Uninstall",
    }
    missing = {item for item in required if item not in readme}
    assert not missing


def test_readme_top_status_marks_outgoing_social_as_verified():
    readme = (ROOT / "README.md").read_text()
    status = readme.split("## Status and tested matrix", 1)[1].split(
        "## What this project does", 1
    )[0]

    assert (
        "| Social | Outgoing Microsoft/Xbox invite and join are **VERIFIED** "
        "through Xodus on the tested system. |"
    ) in status
    assert "RETEST REQUIRED" not in status


def test_readme_rejects_the_newer_pair_as_an_installable_runtime():
    readme = (ROOT / "README.md").read_text()
    newer_pair = (
        "d96a768e25f632b04a457e4cb9f585e89ef5d095",
        "ee9db0f68122a9731b9b66cc24767693d1737f5c",
    )
    pair_boundaries = [
        paragraph
        for paragraph in readme.split("\n\n")
        if all(revision in paragraph for revision in newer_pair)
    ]

    assert pair_boundaries
    assert all("REJECTED LIVE" in paragraph for paragraph in pair_boundaries)
    assert all(
        "reviewed installable pair" not in paragraph.lower()
        for paragraph in pair_boundaries
    )
    installable_boundaries = [
        paragraph
        for paragraph in readme.split("\n\n")
        if "installable profile is" in paragraph.lower()
        or "one installable runtime profile" in paragraph.lower()
    ]
    assert installable_boundaries
    assert all("legacy-v0.1" in paragraph for paragraph in installable_boundaries)
    assert all(
        not any(revision in paragraph for revision in newer_pair)
        for paragraph in installable_boundaries
    )


def test_verification_names_legacy_gate_ownership_and_current_counts():
    verification = (ROOT / "docs/verification.md").read_text()
    runtime_gate = verification.split(
        "## Recoverable runtime-component source gate", 1
    )[1]

    assert "97 passing cases" in runtime_gate
    assert "34 passing behavior cases" in runtime_gate
    assert (
        "exact clean integration, legacy XGameRuntime, and legacy Xodus revisions"
        in runtime_gate
    )
    assert "exact clean integration/WineGDK/Xodus" not in runtime_gate


def test_verify_workflow_installs_just_before_running_the_gate():
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    setup = "uses: extractions/setup-just@v3"
    python_tools = "uv sync --group dev"
    gate = "run: just verify"
    assert setup in workflow
    assert python_tools in workflow
    assert workflow.index(setup) < workflow.index(gate)
    assert workflow.index(python_tools) < workflow.index(gate)


def test_verify_workflow_installs_ripgrep_for_bash_assertions():
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    install_tools = "sudo apt-get install --yes shellcheck shfmt systemd ripgrep"
    gate = "run: just verify"

    assert install_tools in workflow
    assert workflow.index(install_tools) < workflow.index(gate)


def test_verify_workflow_provides_an_executable_unit_stub():
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    unit_stub = (
        'install -Dm755 /bin/true '
        '"$HOME/.local/libexec/xodus-forza/xodus-service"'
    )
    gate = "run: just verify"

    assert unit_stub in workflow
    assert workflow.index(unit_stub) < workflow.index(gate)


def test_verify_gate_runs_every_behavior_and_format_suite():
    gate = subprocess.run(
        ["just", "--dry-run", "verify"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout
    required = [
        "bash tests/test_launcher.bash",
        "bash tests/test_doctor.bash",
        "bash tests/test_steam_options.bash",
        "bash tests/test_installation.bash",
        "systemd-analyze --user verify config/xodus-forza.service",
        "shellcheck",
        "shfmt",
        "uv run ruff check",
        "uv run reuse lint",
        "git diff --check",
    ]
    assert all(command in gate for command in required)


def test_verify_gate_ignores_indirect_test_function_diagnostics_portably():
    gate = subprocess.run(
        ["just", "--dry-run", "verify"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout

    assert "shellcheck -e SC2317,SC2329" in gate


def test_xodus_unit_allows_creation_of_runtime_socket_and_tokens():
    lines = (ROOT / "config/xodus-forza.service").read_text().splitlines()
    writable = [line for line in lines if line.startswith("ReadWritePaths=")]

    assert writable == ["ReadWritePaths=%t"]


def test_readme_documents_source_provenance_and_required_runtime_placement():
    readme = (ROOT / "README.md").read_text()
    required = [
        "xodus-gaming/wine` `bleeding-edge",
        "xodus-gaming/xodus` `main",
        "Weather-OS/WineGDK` `b03ba49c4f326c36aa6930fbe6cf72841ef3738c",
        "not an upstream `xodus-gaming/xgameruntime` code PR",
        "https://github.com/xodus-gaming/wine.git",
        "git -C wine switch bleeding-edge",
        "https://github.com/xodus-gaming/xodus.git",
        "git -C xodus switch main",
        "https://github.com/Weather-OS/WineGDK.git",
        "git -C winegdk switch --detach b03ba49c4f326c36aa6930fbe6cf72841ef3738c",
        "forza-social-invite-join",
        "xodus-social-invite-bridge",
        "wgi-physical-nonroamable-id",
        "storage-trim-property",
        "source branches are public at the immutable revisions",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/xgameruntime.dll.threading",
        "AP702",
        "HDD/SSD launch rejection",
        "StorageDeviceTrimProperty",
        "already-missing paths are left absent",
    ]
    assert all(item in readme for item in required)
    assert "git -C xgameruntime switch oot-cpp" not in readme


def test_xgameruntime_plan_supersession_and_publication_boundary_are_consistent():
    plans = ROOT / "docs" / "superpowers" / "plans"
    old_plan = (plans / "2026-08-31-xgameruntime-invite-bridge.md").read_text()
    amendment = (plans / "2026-09-01-xgameruntime-winegdk-amendment.md").read_text()
    roadmap = (plans / "2026-08-31-forza-motorsport-linux-roadmap.md").read_text()
    publication = (plans / "2026-08-31-github-publication.md").read_text()
    spec = (
        ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-31-forza-motorsport-linux-publication-design.md"
    ).read_text()

    assert "Superseded — do not execute" in old_plan
    assert "Every task, command, checkbox, and expected result below is non-normative" in old_plan
    assert "REQUIRED SUB-SKILL" not in old_plan
    assert "must not be executed" in amendment
    assert "2026-09-01-xgameruntime-winegdk-amendment.md" in roadmap
    assert "first versions the reviewed Plan-3 XDUI contract" in roadmap
    assert "built artifact's SHA-256" in roadmap
    assert "AppID prefix and user-supplied Microsoft threading DLL remain untouched" in roadmap
    assert "Do not open an upstream `xodus-gaming/xgameruntime` code PR" in publication
    assert "do not submit it as an upstream `xodus-gaming/xgameruntime` code PR" in spec


def test_publication_manifest_classifies_every_required_claim():
    data = load_publication_manifest()
    claims = {claim["id"]: claim for claim in data["claims"]}
    assert set(claims) == REQUIRED_CLAIMS
    assert all(claim["state"] in PUBLICATION_STATES for claim in claims.values())
    assert claims["game-launch"]["state"] == "verified"
    assert claims["incoming-invite-notifications"]["state"] == "rejected"
    assert all(claim["evidence"] for claim in claims.values())


def test_live_validation_evidence_backs_every_required_claim():
    data = load_publication_manifest()
    required = [claim for claim in data["claims"] if claim["required"]]
    evidence_path = "docs/evidence/v0.1-live-validation.md"

    assert required
    assert all(claim["state"] == "verified" for claim in required)
    assert {claim["evidence"] for claim in required} == {evidence_path}
    assert data["components"]["xgameruntime_legacy"]["state"] == "verified"
    assert data["components"]["xgameruntime_legacy"]["evidence"] == evidence_path
    assert (ROOT / evidence_path).is_file()


def test_release_document_cannot_exist_with_required_unresolved_claims():
    data = load_publication_manifest()
    unresolved = [
        claim["id"]
        for claim in data["claims"]
        if claim["required"] and claim["state"] == "unresolved"
    ]
    release_document = ROOT / "docs/release-v0.1.0.md"
    assert not release_document.exists() or not unresolved


def test_release_document_describes_the_verified_source_only_boundary():
    data = load_publication_manifest()
    assert all(
        claim["state"] == "verified"
        for claim in data["claims"]
        if claim["required"]
    )

    release = (ROOT / "docs/release-v0.1.0.md").read_text()
    required = {
        "## Verified on the tested system",
        "## Source revisions",
        "## Known limitations",
        "## Installation boundary",
        "## Rollback",
        "source-only",
        "7b236772297b3475ea4f3cb830feb5b224f3064a",
        "a1548b1cf57371715d10b608bc81a77a188e40d4",
        "Automatic incoming Xbox invite notifications are not supported",
        "not ordinary upstream Proton support",
    }
    assert all(item in release for item in required)

    assert "https://github.com/volcmen/forza-motorsport-linux" in release
    for name in (
        "xodus_legacy",
        "xgameruntime_legacy",
        "wine_controller",
        "wine_storage",
    ):
        repository, _, revision = PUBLIC_COMPONENTS[name]
        assert f"{repository}/commit/{revision}" in release


def test_upstream_publication_drafts_are_complete_and_path_safe():
    draft_paths = [
        ROOT / "docs/upstream.md",
        ROOT / "docs/upstream/xodus-94-comment.md",
        ROOT / "docs/upstream/proton-7151-comment.md",
        ROOT / "docs/upstream/wine-controller-pr.md",
        ROOT / "docs/upstream/wine-storage-pr.md",
    ]
    assert all(path.is_file() for path in draft_paths)

    for path in draft_paths:
        text = path.read_text()
        assert "/home/" not in text
        assert "file://" not in text
        link_targets = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
        assert not [target for target in link_targets if target.startswith("/")]


def test_proton_draft_reports_only_manifest_verified_live_claims():
    data = load_publication_manifest()
    verified = {
        claim["id"]
        for claim in data["claims"]
        if claim["state"] == "verified"
    }
    claims = {
        "game-launch": "two successful launches without a reboot",
        "online-profile-content": "online profile and content download",
        "controller-navigation-driving": "controller navigation and driving",
        "controller-hotplug": "controller disconnect and reconnect",
        "ap702-absent": "AP702 warning was absent",
        "linux-to-windows-invite": "Invite from Linux to Windows",
        "linux-join-windows-activity": "Join from Linux to a compatible Windows activity",
        "social-ui-keyboard-controller": "keyboard and controller input in the social picker",
        "xodus-clean-shutdown": "clean Xodus shutdown after each game exit",
        "repeat-launch-no-reboot": "second launch without a system reboot",
    }
    proton = (ROOT / "docs/upstream/proton-7151-comment.md").read_text()

    assert set(claims) <= verified
    assert all(phrase in proton for phrase in claims.values())
    assert "Automatic incoming Xbox invite notifications are not supported" in proton
    assert "not ordinary upstream Proton support" in proton


def test_upstream_ledger_records_published_sources_and_review_urls():
    ledger = (ROOT / "docs/upstream.md").read_text()
    actions = [
        "Integration repository creation",
        "Xodus fork and legacy/current branches",
        "wine-forza-motorsport fork and reconstructed branch",
        "Wine fork and controller/storage branches",
        "WineGDK fork and experimental branch",
        "Xodus issue 94 comment",
        "Wine controller draft PR",
        "Wine storage draft RFC PR",
        "Proton issue 7151 comment",
        "v0.1.0 tag and source-only release",
    ]
    published = actions
    prepared = []
    assert all(f"| {action} | `published` |" in ledger for action in published)
    assert all(f"| {action} | `prepared` |" in ledger for action in prepared)
    assert "No issue comment, issue, pull request" not in ledger
    assert "https://github.com/volcmen/forza-motorsport-linux" in ledger
    for repository, branch, revision in PUBLIC_COMPONENTS.values():
        assert f"{repository}/tree/{branch}" in ledger
        assert f"{repository}/commit/{revision}" in ledger
    review_urls = {
        "https://github.com/xodus-gaming/xodus/issues/94#issuecomment-5526430802",
        "https://github.com/xodus-gaming/wine/pull/4",
        "https://github.com/xodus-gaming/wine/pull/5",
        "https://github.com/ValveSoftware/Proton/issues/7151#issuecomment-5526450265",
        "https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.1.0",
    }
    assert all(url in ledger for url in review_urls)

    controller = (ROOT / "docs/upstream/wine-controller-pr.md").read_text()
    storage = (ROOT / "docs/upstream/wine-storage-pr.md").read_text()
    assert controller.startswith(
        "# windows.gaming.input: identify physical XInput controllers\n"
    )
    assert "wgi-physical-nonroamable-id" in controller
    assert "b1dd32734a34472a28eb5be9922df06e07ac0834" in controller
    assert "ddd302d97c6008d79ea4f3e3ad56014cb548e514" in controller
    assert storage.startswith(
        "# mountmgr: define device-backed StorageDeviceTrimProperty semantics\n"
    )
    assert "storage-trim-property" in storage


def test_upstream_drafts_link_published_sources_without_placeholders():
    drafts = {
        "xodus-94-comment.md": ("xodus_legacy", "xodus_next"),
        "proton-7151-comment.md": (
            "xodus_legacy",
            "xgameruntime_legacy",
            "wine_controller",
            "wine_storage",
        ),
        "wine-controller-pr.md": ("wine_controller",),
        "wine-storage-pr.md": ("wine_storage",),
    }
    evidence_url = (
        "https://github.com/volcmen/forza-motorsport-linux/blob/"
        "d46640d20c304c9329a99ba823052289ed1f06b6/"
        "docs/evidence/v0.1-live-validation.md"
    )

    for filename, components in drafts.items():
        text = (ROOT / "docs/upstream" / filename).read_text()
        assert "will be inserted" not in text
        assert evidence_url in text
        for name in components:
            repository, _, revision = PUBLIC_COMPONENTS[name]
            assert f"{repository}/commit/{revision}" in text


def test_readme_and_verification_describe_sources_as_public():
    readme = (ROOT / "README.md").read_text()
    verification = (ROOT / "docs/verification.md").read_text()

    stale_phrases = (
        "not public yet",
        "remain local, planned, and unpublished",
        "The branch remains local: it has not been pushed",
    )
    assert not [phrase for phrase in stale_phrases if phrase in readme]
    assert not [phrase for phrase in stale_phrases if phrase in verification]
    assert "https://github.com/volcmen/xodus/commit/" in readme
    assert "https://github.com/volcmen/wine-forza-motorsport/commit/" in readme
    assert "https://github.com/volcmen/xodus/commit/" in verification
    assert "https://github.com/volcmen/wine-forza-motorsport/commit/" in verification
