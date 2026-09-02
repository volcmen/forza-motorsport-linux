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
    required = [
        "Experimental",
        "AppID 2440510",
        "KDE Wallet",
        "Microsoft binaries are not distributed",
        "Incoming invite notifications are not supported",
        "Uninstall",
    ]
    assert all(item in readme for item in required)


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
