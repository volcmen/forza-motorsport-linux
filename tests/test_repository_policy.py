import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
FORBIDDEN_SUFFIXES = {".dll", ".exe", ".sys", ".so"}
FORBIDDEN_TEXT = ("authorization: " + "xbl3.0 x=", "proof_" + "private_key=")


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


def test_readme_documents_source_provenance_and_required_runtime_placement():
    readme = (ROOT / "README.md").read_text()
    required = [
        "xodus-gaming/wine` `bleeding-edge",
        "xodus-gaming/xodus` `main",
        "xgameruntime` `oot-cpp` / PR 19",
        "https://github.com/xodus-gaming/wine.git",
        "git -C wine switch bleeding-edge",
        "https://github.com/xodus-gaming/xodus.git",
        "git -C xodus switch main",
        "https://github.com/xodus-gaming/xgameruntime.git",
        "git -C xgameruntime switch oot-cpp",
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
