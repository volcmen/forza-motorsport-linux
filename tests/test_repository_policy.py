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


def load_publication_manifest():
    return tomllib.loads(PUBLICATION_MANIFEST.read_text(encoding="utf-8"))


def test_publication_manifest_pins_working_legacy_xodus():
    component = load_publication_manifest()["components"]["xodus_legacy"]
    assert component == {
        "state": "verified",
        "revision": "76cab7667119efc04938b1128a107c22dec613ca",
        "evidence": "docs/evidence/v0.1-xodus.md",
    }


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


def test_repository_policy_rejects_renamed_pe_binary(tmp_path):
    renamed = tmp_path / "runtime.dll.threading"
    renamed.write_bytes(b"MZ" + b"\0" * 16)
    assert _is_prohibited_binary(renamed)


def test_declared_licenses_exist():
    assert (ROOT / "LICENSES/GPL-3.0-or-later.txt").is_file()


def test_readme_names_supported_and_unsupported_boundaries():
    readme = (ROOT / "README.md").read_text()
    required = {
        "Experimental",
        "AppID 2440510",
        "legacy-v0.1",
        "76cab7667119efc04938b1128a107c22dec613ca",
        "Verified current: game launch",
        "Retest required",
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


def test_readme_top_status_marks_outgoing_social_as_retest_required():
    readme = (ROOT / "README.md").read_text()
    status = readme.split("## Status and tested matrix", 1)[1].split(
        "## What this project does", 1
    )[0]

    assert (
        "| Social | Outgoing Microsoft/Xbox invite and join are **RETEST REQUIRED** "
        "through Xodus. |"
    ) in status
    assert "invite and join are experimental" not in status


def test_readme_rejects_the_newer_pair_as_an_installable_runtime():
    readme = (ROOT / "README.md").read_text()
    newer_pair = (
        "ef069c85c8b3ee049971a9e22a317309b30bb09f",
        "b76690da6ff59ace8981bf0428cd729fcd15673d",
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
        "complete build-from-source path cannot be claimed",
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


def test_release_document_cannot_exist_with_required_unresolved_claims():
    data = load_publication_manifest()
    unresolved = [
        claim["id"]
        for claim in data["claims"]
        if claim["required"] and claim["state"] == "unresolved"
    ]
    release_document = ROOT / "docs/release-v0.1.0.md"
    assert not release_document.exists() or not unresolved
