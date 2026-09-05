import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
PUBLICATION_MANIFEST = ROOT / "manifests/publication-v0.1.toml"
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
        "evidence": "evidence/v0.1-xodus.md",
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
    # A documentation move can leave tracked deletions before the change is staged.
    paths = [ROOT / name for name in names if name and (ROOT / name).exists()]
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


def test_verify_workflow_installs_just_before_running_the_gate():
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    setup = "uses: extractions/setup-just@"
    python_tools = "uv sync --locked --group dev"
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
    evidence_path = "evidence/v0.1-live-validation.md"

    assert required
    assert all(claim["state"] == "verified" for claim in required)
    assert {claim["evidence"] for claim in required} == {evidence_path}
    assert data["components"]["xgameruntime_legacy"]["state"] == "verified"
    assert data["components"]["xgameruntime_legacy"]["evidence"] == evidence_path
    assert (ROOT / evidence_path).is_file()


def test_publication_evidence_paths_remain_local_and_resolve():
    data = load_publication_manifest()
    for record in [*data["components"].values(), *data["claims"]]:
        evidence = record.get("evidence")
        if evidence and evidence != "unsupported-by-design":
            path = ROOT / evidence
            assert path.is_relative_to(ROOT)
            assert path.is_file(), evidence


def test_readme_routes_readers_to_github_wiki():
    readme = (ROOT / "README.md").read_text()
    base = "https://github.com/volcmen/forza-motorsport-linux/wiki"
    assert f"[Open the wiki →]({base})" in readme
    for page in (
        "Install", "Play-and-verify", "Troubleshooting", "Recovery",
        "Advanced-setup", "How-it-works", "Evidence-and-history",
    ):
        assert f"({base}/{page})" in readme
    assert "](wiki/" not in readme
    assert "Incoming invite notifications are not supported" in readme
    assert "v0.2.0" in readme and "v0.1" in readme


def test_ci_actions_use_immutable_references_and_read_only_checkout():
    import re

    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = path.read_text()
        references = re.findall(r"uses:\s+([^\s]+)", workflow)
        assert references, path
        assert all(re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref) for ref in references)
        assert "permissions:\n  contents: read" in workflow
        assert "persist-credentials: false" in workflow
        assert "pull_request_target" not in workflow
        assert "timeout-minutes:" in workflow


def test_development_lock_excludes_vulnerable_pytest_versions():
    from packaging.version import Version

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == "pytest")
    assert Version(package["version"]) >= Version("9.0.3")
    assert "uv sync --locked --group dev" in (ROOT / ".github/workflows/verify.yml").read_text()


def test_security_reporting_and_license_are_discoverable():
    policy = (ROOT / ".github/SECURITY.md").read_text()
    assert "https://github.com/volcmen/forza-motorsport-linux/security/advisories/new" in policy
    assert (ROOT / "LICENSE").read_bytes() == (ROOT / "LICENSES/GPL-3.0-or-later.txt").read_bytes()
